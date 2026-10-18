import asyncio
import json

from sqlalchemy import select
from fastapi.testclient import TestClient

from backend.app.db import Candidate, Event, EventStage
from backend.app.decision import decide
from backend.app.gemini import GeminiWorker
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
    assert decide(['fall'], 3, {'status': 'scored', 'fall_score': .9}, .5).action == 'supported'
    assert decide(['fall'], 2, {'status': 'scored', 'fall_score': .42}, .4).action == 'supported'
    assert decide(['fall'], 2, {'status': 'scored', 'fall_score': .39}, .4).action == 'rejected'
    # Staggering uses its own, lower cutoff: staged stumbles scored 0.14-0.25, walking ~0.
    assert decide(['stumble'], 1, {'status': 'scored', 'staggering_score': .14}, .4).action == 'supported'
    assert decide(['stumble'], 1, {'status': 'scored', 'staggering_score': .05}, .4).action == 'rejected'


def test_restricted_standing_uses_rules_while_fall_uses_ml(guardian):
    guardian.settings.decision_mode = 'enforce'
    model_calls = []
    def score(candidate, samples):
        model_calls.append(candidate.rule_hits)
        return {'status': 'scored', 'model': 'test', 'fall_score': .9,
                'staggering_score': 0, 'latency_ms': 1}
    guardian.score_candidate = score
    frames = [frame.model_copy(update={'source': 'upload', 'camera_id': 'browser-camera'})
              for frame in scenario_frames('fall', 'margaret')]
    for frame in frames:
        guardian.ingest(frame)
    events = {event.rule_hits[0]: event for event in guardian.events()}
    assert set(events) == {'restricted_standing', 'fall'}
    assert events['restricted_standing'].verdict_status == 'rules_only'
    assert events['fall'].verdict_status == 'model_supported_no_gemini'
    assert model_calls and all(hits == ['fall'] for hits in model_calls)
    capture = guardian.capture_result('margaret', frames[0].session_id)
    assert capture['event']['rule_hits'] == ['fall']
    assert capture['candidate_decision'] == 'supported'
    assert {event['rule_hits'][0] for event in capture['events']} == {'fall', 'restricted_standing'}
    with guardian.db.sessions() as session:
        standing = session.scalar(select(Candidate).where(Candidate.event_id == events['restricted_standing'].id))
        assert standing.decision == 'rules_only' and standing.model_result is None
        assert not list(session.scalars(select(EventStage).where(
            EventStage.event_id == events['restricted_standing'].id, EventStage.stage == 'ml_result')))


def test_gemini_receives_context_only_after_ml_support(guardian):
    guardian.settings.decision_mode = 'enforce'
    guardian.settings.gemini_api_key = 'test'
    guardian.settings.gemini_billed_project_confirmed = True
    profile = next(p for p in guardian.profiles() if p.id == 'daniel')
    guardian.score_candidate = lambda candidate, samples: {
        'status': 'scored', 'model': 'test', 'fall_score': .9,
        'staggering_score': .01, 'latency_ms': 1,
    }
    for frame in scenario_frames('fall', 'daniel'):
        guardian.ingest(frame)
    # The ML-supported alert opens (and notifies) now; Gemini reviews it afterwards.
    opened = guardian.events()[0]
    assert opened.status == 'open' and opened.verdict_status == 'pending' and opened.notifications
    captured = {}
    async def review(package, frames):
        captured.update(package)
        return json.dumps({'verdict': 'fall', 'urgency': 2, 'confidence': .8,
                           'summary': 'A possible fall was observed.',
                           'evidence': [{'ref': 'ml.supported', 'detail': 'Model supported the rule'}],
                           'recommended_actions': ['Check on the person'], 'profile_factors': []})
    worker = GeminiWorker(guardian)
    worker.request = review
    asyncio.run(worker.tick())
    assert captured['model_evidence']['decision'] == 'supported'
    assert captured['profile']['conditions'] == profile.conditions
    assert guardian.events()[0].verdict_status == 'validated'


def test_gemini_reviews_fall_with_separate_restricted_standing_concern(guardian):
    guardian.settings.decision_mode = 'enforce'
    guardian.settings.gemini_api_key = 'test'
    guardian.settings.gemini_billed_project_confirmed = True
    profile = next(p for p in guardian.profiles() if p.id == 'margaret')
    guardian.save_profile(profile.model_copy(update={'gemini_consent': True}))
    guardian.score_candidate = lambda candidate, samples: {
        'status': 'scored', 'model': 'test', 'fall_score': .9,
        'staggering_score': 0, 'latency_ms': 1,
    }
    frames = [frame.model_copy(update={'source': 'upload', 'camera_id': 'browser-camera'})
              for frame in scenario_frames('fall', 'margaret')]
    for frame in frames:
        guardian.ingest(frame)
    pending = guardian.capture_result('margaret', frames[0].session_id)
    assert pending['status'] == 'reviewing'
    assert pending['event']['rule_hits'] == ['fall']
    assert {event['rule_hits'][0] for event in pending['events']} == {'fall', 'restricted_standing'}
    captured = {}
    async def review(package, evidence):
        captured.update(package)
        return json.dumps({'verdict': 'fall', 'urgency': 2, 'confidence': .8,
                           'summary': 'Possible fall. Check on the person.',
                           'evidence': [{'ref': 'ml.supported', 'detail': 'Model supported the fall rule'}],
                           'recommended_actions': ['Check on the person']})
    worker = GeminiWorker(guardian)
    worker.request = review
    asyncio.run(worker.tick())
    completed = guardian.capture_result('margaret', frames[0].session_id)
    assert completed['event']['verdict_status'] == 'validated'
    assert {event['rule_hits'][0] for event in completed['events']} == {'fall', 'restricted_standing'}
    assert any('restricted_standing' in event['rules'] for event in captured['recent_events'])


def test_legacy_rules_only_pending_event_cannot_reach_gemini(guardian):
    guardian.settings.gemini_api_key = 'test'
    for frame in scenario_frames('fall', 'daniel'):
        guardian.ingest(frame)
    event = guardian.events()[0]
    with guardian.db.sessions.begin() as session:
        session.get(Event, event.id).verdict_status = 'pending'
    worker = GeminiWorker(guardian)
    async def forbidden(*args):
        raise AssertionError('Rules-only evidence must not reach Gemini')
    worker.request = forbidden
    asyncio.run(worker.tick())
    assert guardian.event(event.id).verdict_status == 'rules_only'


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
