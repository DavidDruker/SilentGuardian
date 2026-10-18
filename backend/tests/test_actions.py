import json
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import select

from backend.app.action_model import ActionModel, COCO_FROM_MEDIAPIPE, prepare_sequence
from backend.app.db import Candidate, Event, Window
from backend.app.detector import sample
from backend.app.gemini import event_package
from backend.app.replay import scenario_frames
from backend.app.seed import demo_profiles


def samples():
    profile = demo_profiles()[1]
    return [sample(frame, profile) for frame in list(scenario_frames('normal', profile.id, 1792217400))[:31]]


class FakeModel:
    parameter_count = 1

    def predict(self, poses):
        return dict(status='scored', mode='shadow', model='fake-test-only',
                    observed_at=poses[-1]['t'], fall_score=.99, staggering_score=.01,
                    latency_ms=1, top_actions=[dict(class_id=42, label='falling', score=.99)])


def ready(g):
    g.actions.model, g.actions.state = FakeModel(), 'ready'


def test_mapping_normalization_visibility_and_person_padding():
    poses = samples()
    for p in poses:
        for i, joint in enumerate(p['landmarks']):
            joint.update(x=i/40, y=i/50, z=10, visibility=.99)
    data = prepare_sequence(poses)
    assert data.shape == (1, 2, 100, 17, 3)
    np.testing.assert_allclose(data[0, 0, 0, :, 0], np.array(COCO_FROM_MEDIAPIPE)/20-1, atol=1e-6)
    np.testing.assert_allclose(data[0, 0, -1, :, 1], np.array(COCO_FROM_MEDIAPIPE)/25-1, atol=1e-6)
    assert np.all(data[0, 0, :, :, 2] == np.float32(.99))
    assert not data[0, 1].any()


@pytest.mark.parametrize('problem', ['gap', 'reverse', 'short', 'visibility', 'nan'])
def test_incomplete_or_bad_sequences_are_not_classified(problem):
    poses = samples()
    if problem == 'gap': poses = poses[:10]+poses[20:]
    if problem == 'reverse': poses.reverse()
    if problem == 'short': poses = poses[:10]
    if problem == 'visibility':
        for j in poses[15]['landmarks']: j['visibility'] = .1
    if problem == 'nan': poses[15]['landmarks'][11]['x'] = float('nan')
    with pytest.raises(ValueError): prepare_sequence(poses)


def test_shadow_prediction_persisted_without_creating_fall(guardian):
    ready(guardian)
    for f in scenario_frames('normal', 'daniel', guardian.clock(), duration=3):
        guardian.ingest(f)
        guardian.actions.tick()
    assert not guardian.events()  # Fake classifier is 99% falling on every normal window.
    assert guardian.actions.completed > 0
    with guardian.db.sessions() as s:
        windows = list(s.scalars(select(Window)))
    predictions = [json.loads(w.features['action_prediction_json']) for w in windows if 'action_prediction_json' in w.features]
    assert predictions and all(p['fall_score'] == .99 and p['rule_hits'] == [] for p in predictions)


def test_event_comparison_never_changes_urgency_or_gemini_input(guardian):
    ready(guardian)
    for f in scenario_frames('fall', 'daniel', guardian.clock(), duration=4):
        guardian.ingest(f)
        guardian.actions.tick()
    event = guardian.events()[0]
    assert event.urgency == 2 and event.action_prediction['fall_score'] == .99
    with guardian.db.sessions() as s:
        package, refs = event_package(s.get(Event, event.id), [])
    assert 'action_prediction_json' not in package['metrics']
    assert 'metric.action_prediction_json' not in refs
    guardian.acknowledge(event.id, 'test', '')
    guardian.actions.tick()
    assert guardian.event(event.id).status == 'acknowledged'


@pytest.mark.parametrize('configured', [True, False])
def test_supporting_shadow_score_sends_rule_alert_to_gemini(guardian, configured):
    guardian.settings.gemini_api_key = 'test-key' if configured else ''
    guardian.settings.gemini_billed_project_confirmed = configured
    ready(guardian)
    for f in scenario_frames('fall', 'daniel', guardian.clock(), duration=4):
        guardian.ingest(f)
        guardian.actions.tick()
    event = guardian.events()[0]
    # The alert stays open with its original urgency; only the review path changes.
    assert event.status == 'open' and event.urgency == 2
    assert event.verdict_status == ('pending' if configured else 'model_supported_no_gemini')
    with guardian.db.sessions() as s:
        candidate = s.get(Candidate, s.get(Event, event.id).features['candidate_id'])
    assert candidate.decision == 'supported' and candidate.model_result['status'] == 'scored'


def test_unsupportive_shadow_score_leaves_rule_alert_alone(guardian):
    guardian.settings.gemini_api_key, guardian.settings.gemini_billed_project_confirmed = 'test-key', True
    ready(guardian)
    low = FakeModel.predict
    guardian.actions.model.predict = lambda poses: {**low(None, poses), 'fall_score': .01}
    for f in scenario_frames('fall', 'daniel', guardian.clock(), duration=4):
        guardian.ingest(f)
        guardian.actions.tick()
    assert guardian.events()[0].verdict_status == 'rules_only'


def test_shadow_result_skips_restricted_standing_alert(guardian):
    ready(guardian)
    for frame in scenario_frames('fall', 'margaret', guardian.clock(), duration=4):
        guardian.ingest(frame)
        guardian.actions.tick()
    events = {event.rule_hits[0]: event for event in guardian.events()}
    assert set(events) == {'restricted_standing', 'fall'}
    assert events['fall'].action_prediction['fall_score'] == .99
    assert events['restricted_standing'].action_prediction is None


def test_late_result_is_discarded_after_consent_revocation(guardian):
    ready(guardian)
    for f in scenario_frames('normal', 'daniel', guardian.clock(), duration=2.5): guardian.ingest(f)
    predict = guardian.actions.model.predict
    def revoke(poses):
        profile = next(p for p in guardian.profiles() if p.id == 'daniel')
        guardian.save_profile(profile.model_copy(update={'consent': False}))
        return predict(poses)
    guardian.actions.model.predict = revoke
    guardian.actions.tick()
    assert guardian.actions.completed == 0 and not guardian.actions.streams
    with guardian.db.sessions() as s:
        assert all('action_prediction_json' not in w.features for w in s.scalars(select(Window)))


def test_event_retains_peak_with_its_original_window(guardian):
    ready(guardian)
    original = guardian.actions.model.predict
    start = guardian.clock()
    def peaked(poses):
        result = original(poses)
        result['fall_score'] = .99 if poses[-1]['t']-start < 3 else .01
        return result
    guardian.actions.model.predict = peaked
    for f in scenario_frames('fall', 'daniel', start, duration=4):
        guardian.ingest(f)
        guardian.actions.tick()
    prediction = guardian.events()[0].action_prediction
    assert prediction['fall_score'] == .99
    assert prediction['observed_at']-start < 3
    with guardian.db.sessions() as session:
        latest = session.scalar(select(Window).order_by(Window.ts.desc()).limit(1))
        assert json.loads(latest.features['action_prediction_json'])['fall_score'] == .01


def test_queue_bounded_and_tracking_gap_discards_pending(guardian):
    ready(guardian)
    profile = guardian.profiles()[0]
    poses = samples()
    for i in range(40):
        guardian.actions.observe(str(i), profile, poses, 'good', None, [], 'live')
    assert len(guardian.actions.pending) == len(guardian.actions.streams) == 32
    guardian.actions.observe('39', profile, [], 'uncertain', None, [], 'live')
    assert '39' not in guardian.actions.pending
    assert '39' not in guardian.actions.streams
    guardian.actions.close()
    assert not guardian.actions.pending and not guardian.actions.streams


def test_overload_preserves_event_transition_before_later_static_window(guardian):
    ready(guardian)
    profile = guardian.profiles()[1]
    early = samples()
    guardian.actions.observe('stream', profile, early, 'good', 'event-a', ['fall'], 'synthetic')
    later = [{**p, 't': p['t']+4} for p in early]
    guardian.actions.observe('stream', profile, later, 'good', 'event-a', [], 'synthetic')
    job = guardian.actions.pending['stream']
    assert job['event_transition'] and job['samples'][-1]['t'] == early[-1]['t']
    assert job['rule_hits'] == ['fall']


def test_final_prediction_retrievable_without_more_pose_frames(guardian):
    ready(guardian)
    frames = list(scenario_frames('normal', 'daniel', guardian.clock(), duration=3))
    for frame in frames: guardian.ingest(frame)
    guardian.actions.tick()
    key = f'{frame.person_id}:{frame.camera_id}:{frame.session_id}'
    assert guardian.actions.latest(key)['status'] == 'scored'
    guardian.actions.clear_person('daniel')
    assert guardian.actions.latest(key) is None


def test_unavailable_model_fails_open_for_existing_rules(guardian, tmp_path):
    guardian.settings.action_model_path = str(tmp_path/'absent.pth')
    guardian.actions.load()
    assert guardian.actions.state == 'unavailable'
    for f in scenario_frames('fall', 'daniel', guardian.clock(), duration=4): guardian.ingest(f)
    assert guardian.events()[0].urgency == 2


def test_checkpoint_tampering_rejected_before_deserialization(tmp_path):
    path = tmp_path/'invalid.pth'
    path.write_bytes(b'not the trusted weights')
    with pytest.raises(ValueError, match='checksum'): ActionModel(path)


def test_model_status_requires_login(client):
    assert client.get('/api/action-model').status_code == 401
    client.post('/api/session', json={'token': 'local-demo-only'})
    result = client.get('/api/action-model').json()
    assert result['alert_authority'] is False


def test_inflight_prediction_cannot_recreate_purged_data(client):
    client.post('/api/session', json={'token': 'local-demo-only'})
    g = client.app.state.guardian
    ready(g)
    for f in scenario_frames('fall', 'daniel', g.clock(), duration=3): g.ingest(f)
    predict = g.actions.model.predict
    def purge(poses):
        response = client.post('/api/privacy/daniel/purge', json={'confirm_alias': 'Daniel'})
        assert response.status_code == 200
        assert all(v == 0 for v in response.json()['remaining'].values())
        return predict(poses)
    g.actions.model.predict = purge
    g.actions.tick()
    assert g.actions.completed == 0 and not g.actions.streams
    assert all(v == 0 for v in client.get('/api/privacy/daniel').json()['counts'].values())


def test_manifest_rejects_reused_recordings_and_synthetic_data(tmp_path):
    from scripts.evaluate_actions import load_manifest
    frames = list(scenario_frames('normal', 'daniel', 1792217400, duration=3))
    recording = tmp_path/'clip.json'
    recording.write_text(json.dumps({'kind': 'skeleton-recording', 'frames': [f.model_dump() for f in frames]}))
    manifest = tmp_path/'manifest.json'
    item = dict(recording='clip.json', label='normal', split='tuning', person_alias='a', angle='front')
    manifest.write_text(json.dumps(dict(source='staged', reviewed=True, clips=[item])))
    with pytest.raises(ValueError, match='real camera'): load_manifest(manifest)
    for f in frames: f.camera_id = 'browser-camera'
    recording.write_text(json.dumps({'kind': 'skeleton-recording', 'frames': [f.model_dump() for f in frames]}))
    assert len(load_manifest(manifest)) == 1
    manifest.write_text(json.dumps(dict(source='staged', reviewed=True, clips=[item, {**item, 'split': 'held_out'}])))
    with pytest.raises(ValueError, match='reuse'): load_manifest(manifest)


def test_real_checkpoint_inference_when_installed():
    if not Path('models/stgcnpp-ntu60-2d.pth').exists(): pytest.skip('optional checkpoint not installed')
    pytest.importorskip('torch')
    model = ActionModel('models/stgcnpp-ntu60-2d.pth')
    assert model.parameter_count == 1393016
    result = model.predict(samples())
    assert result['status'] == 'scored' and len(result['top_actions']) == 5
    assert 0 <= result['fall_score'] <= 1
    assert result['latency_ms'] > 0
    # Repeat predictions must be stable: inference mode, no random augmentation/dropout.
    again = model.predict(samples())
    assert again['top_actions'] == result['top_actions']
