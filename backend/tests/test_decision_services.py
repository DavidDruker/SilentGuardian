from sqlalchemy import select
from fastapi.testclient import TestClient

from backend.app.db import Candidate, EventStage
from backend.app.decision import decide
from backend.app.replay import scenario_frames
from backend.app.rules_api import app as rules_app
from backend.app.detector import sample


def test_rule_service_requires_token_and_rejects_stale_profile(guardian):
    profile = next(p for p in guardian.profiles() if p.id == 'daniel')
    frame = next(scenario_frames('normal', 'daniel'))
    body = {'schema_version': 1, 'correlation_id': 'clip-1', 'profile_version': profile.version,
            'frame': frame.model_dump(), 'profile': profile.model_dump(), 'state': {}}
    with TestClient(rules_app) as client:
        assert client.post('/internal/evaluate', json=body).status_code == 401
        ok = client.post('/internal/evaluate', json=body,
                         headers={'x-internal-token': guardian.settings.internal_service_token})
        assert ok.status_code == 200
        assert ok.json()['correlation_id'] == 'clip-1'
        body['profile_version'] += 1
        assert client.post('/internal/evaluate', json=body,
                           headers={'x-internal-token': guardian.settings.internal_service_token}).status_code == 409


def test_enforced_policy_rejects_unsupported_fall_and_keeps_candidate(guardian):
    guardian.settings.decision_mode = 'enforce'
    guardian.score_candidate = lambda candidate, samples: {'status': 'scored', 'model': 'test',
        'fall_score': 0.01, 'staggering_score': 0.01, 'latency_ms': 1}
    frames = list(scenario_frames('fall', 'daniel'))
    for frame in frames:
        guardian.ingest(frame)
    guardian.ingest(frames[-1])  # Duplicate frame must not create a second candidate.
    assert guardian.events() == []
    with guardian.db.sessions() as session:
        candidates = list(session.scalars(select(Candidate)))
        assert candidates and all(c.decision == 'rejected' for c in candidates)
        assert len({c.idempotency_key for c in candidates}) == len(candidates)
        assert not list(session.scalars(select(EventStage).where(EventStage.stage == 'incident_opened')))


def test_enforced_policy_preserves_urgent_model_outage_fallback(guardian):
    guardian.settings.decision_mode = 'enforce'
    guardian.score_candidate = lambda candidate, samples: None
    for frame in scenario_frames('fall', 'daniel'):
        guardian.ingest(frame)
    event = guardian.events()[0]
    assert event.verdict_status == 'model_fallback'
    with guardian.db.sessions() as session:
        candidate = session.scalar(select(Candidate).where(Candidate.event_id == event.id))
        assert candidate.decision == 'fallback_open'
        assert session.scalar(select(EventStage).where(EventStage.stage == 'incident_opened'))


def test_policy_uses_uncertainty_for_unrelated_action_class():
    assert decide(['bed_exit'], 2, {'status': 'scored', 'fall_score': 0, 'staggering_score': 0}, .5).action == 'hold'
    assert decide(['prolonged_stillness'], 2, None, .5).action == 'fallback_open'


def test_ml_service_auth_quality_and_bounded_inference(guardian, monkeypatch):
    from backend.app import ml_api
    profile = next(p for p in guardian.profiles() if p.id == 'daniel')
    samples = [sample(frame, profile) for frame in list(scenario_frames('normal', 'daniel'))[:31]]
    class FakeModel:
        def __init__(self, *args):
            pass
        def predict(self, evidence):
            return {'status': 'scored', 'model': 'test', 'fall_score': .8,
                    'staggering_score': .02, 'latency_ms': 1,
                    'observed_at': evidence[-1]['t']}
    monkeypatch.setattr(ml_api, 'ActionModel', FakeModel)
    body = {'schema_version': 1, 'correlation_id': 'corr', 'candidate_id': 'candidate',
            'idempotency_key': 'same-key', 'profile_version': profile.version,
            'observed_at': samples[-1]['t'], 'rule_hits': ['fall'],
            'quality': 'good', 'samples': samples}
    with TestClient(ml_api.app) as client:
        assert client.post('/internal/predict', json=body).status_code == 401
        headers = {'x-internal-token': guardian.settings.internal_service_token}
        scored = client.post('/internal/predict', json=body, headers=headers)
        assert scored.status_code == 200 and scored.json()['candidate_id'] == 'candidate'
        body['quality'] = 'uncertain'
        assert client.post('/internal/predict', json=body, headers=headers).status_code == 422
        body['quality'] = 'good'
        assert ml_api.semaphore.acquire(blocking=False)
        try:
            assert client.post('/internal/predict', json=body, headers=headers).status_code == 503
        finally:
            ml_api.semaphore.release()
