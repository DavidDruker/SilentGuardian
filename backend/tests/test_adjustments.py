import asyncio
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.baseline import FEATURES, fit_statistics, score_statistics, independent_records
from backend.app.db import Audit, Event, PersonalBaseline, Window
from backend.app.gemini import GeminiWorker, event_package
from backend.app.notifications import NotificationWorker
from backend.app.replay import scenario_frames
from backend.app.schemas import BedZone, Pose
from scripts.evaluate_baseline import validation_report


def features(value=0):
    return {**{name: value for name in FEATURES}, 'posture': 'standing'}


def stats():
    return fit_statistics([{'session': 'calibration', 'timestamp': i*2., 'features': features()} for i in range(35)])


def test_robust_statistics_ignore_overlap_and_resist_outlier():
    records = [{'session': 'a', 'timestamp': i/3, 'features': features()} for i in range(240)]
    records[-1]['features'] = features(999)
    assert len(independent_records(records)) == 40
    fitted = fit_statistics(records)
    assert fitted['standing']['sway']['median'] == 0
    score, contributions = score_statistics(fitted, {**features(), 'sway': .1})
    assert score == pytest.approx(6.745)
    assert contributions[0]['feature'] == 'sway' and contributions[0]['ratio'] is None
    assert score_statistics(fitted, {**features(), 'posture': 'lying'}) == (None, [])


def test_baseline_requires_three_continuous_windows_and_keeps_rules(guardian):
    model = stats()
    model['standing']['sway']['median'] = .5
    with guardian.db.sessions.begin() as session:
        session.add(PersonalBaseline(person_id='daniel', anchor=model, current=model, threshold=3.5,
                                     trigger_enabled=True, evidence={'test_only': True}, updated_at=guardian.clock()))
    frames = list(scenario_frames('normal', 'daniel'))
    results = [guardian.ingest(f) for f in frames[:40]]
    assert not guardian.events()
    third = guardian.ingest(frames[40])
    assert third.rule_hits == ['personal_baseline']
    assert third.baseline_contributions[0]['feature'] == 'sway'
    assert guardian.events()[0].floor_urgency == 1
    later = frames[45].model_copy(update={'session_id': 'new-baseline-session'})
    assert guardian.ingest(later).rule_hits == []
    # A fixed fall still has its minimum concern floor regardless of baseline state.
    for f in scenario_frames('fall', 'daniel'):
        guardian.ingest(f)
    assert any(e.floor_urgency >= 2 and 'fall' in e.rule_hits for e in guardian.events())


def test_current_update_freezes_anchor_and_excludes_flagged(guardian, clock):
    model = stats()
    with guardian.db.sessions.begin() as s:
        s.add(PersonalBaseline(person_id='daniel', anchor=model, current=model, threshold=3.5, trigger_enabled=True,
                               evidence={}, updated_at=clock()))
        for i in range(70):
            eligible = i < 35
            s.add(Window(ts=datetime.fromtimestamp(clock()-150+i*2, timezone.utc), stream_id='observed',
                         person_id='daniel', quality=1, features={**features(.02 if eligible else 9), 'baseline_eligible': eligible},
                         rule_hits=[] if eligible else ['fall'], calibration=False, sequence=None))
    assert guardian.baseline.refresh_current('daniel', clock())
    with guardian.db.sessions() as s:
        b = s.get(PersonalBaseline, 'daniel')
        assert b.anchor['standing']['sway']['median'] == 0
        assert b.current['standing']['sway']['median'] == .02
        assert not b.trigger_enabled


def test_reviewed_calibration_fits_and_preserves_first_anchor(client, clock):
    auth(client)
    g = client.app.state.guardian
    assert client.post('/api/baseline/daniel/fit', json={'reviewed_normal':False}).status_code == 422
    assert client.post('/api/baseline/daniel/fit', json={'reviewed_normal':True}).status_code == 422
    with g.db.sessions.begin() as s:
        for i in range(64):
            s.add(Window(ts=datetime.fromtimestamp(clock()-i*2, timezone.utc), stream_id=f'camera-session-{i//32}',
                         person_id='daniel', quality=1, features=features(.02), rule_hits=[], calibration=True, sequence=None))
    fitted = client.post('/api/baseline/daniel/fit', json={'reviewed_normal':True})
    assert fitted.status_code == 200 and fitted.json()['anchor_frozen']
    with g.db.sessions.begin() as s:
        for w in s.scalars(select(Window)):
            w.features = features(.04)
    assert client.post('/api/baseline/daniel/fit', json={'reviewed_normal':True}).status_code == 200
    b = client.get('/api/baseline/daniel').json()
    assert b['anchor']['standing']['sway']['median'] == .02
    assert b['current']['standing']['sway']['median'] == .04
    assert not b['trigger_enabled']


def test_mask_excludes_nearby_bystander_as_helper(guardian):
    frame = next(scenario_frames('restricted_standing', 'margaret'))
    bystander = Pose(track_id='outside', landmarks=[p.model_copy(update={'x': p.x+.25}) for p in frame.poses[0].landmarks])
    unmasked = frame.model_copy(update={'poses': frame.poses+[bystander]})
    assert 'restricted_standing' not in guardian.ingest(unmasked).rule_hits
    masked = unmasked.model_copy(update={'session_id': 'masked', 'region': BedZone(x1=.3,y1=0,x2=.65,y2=1)})
    assert 'restricted_standing' in guardian.ingest(masked).rule_hits
    outside = masked.model_copy(update={'session_id': 'outside-subject', 'region': BedZone(x1=0,y1=0,x2=.2,y2=1)})
    assert guardian.ingest(outside).quality == 'uncertain'


def test_demo_time_scales_deadlines_but_rejects_live_scaling(guardian, clock):
    for frame in scenario_frames('stumble', 'daniel'):
        guardian.ingest(frame.model_copy(update={'demo_time_scale': 8}))
    ev = guardian.events()[0]
    assert ev.next_escalation_at-clock() == 7.5
    clock.advance(7.4); NotificationWorker(guardian).tick()
    assert guardian.event(ev.id).urgency == 1
    clock.advance(.2); NotificationWorker(guardian).tick()
    assert guardian.event(ev.id).urgency == 2
    with pytest.raises(ValueError, match='Accelerated'):
        guardian.ingest(frame.model_copy(update={'source':'live', 'timestamp':clock(), 'demo_time_scale':8}))


def auth(client):
    assert client.post('/api/session', json={'token':'local-demo-only'}).status_code == 200


def test_dismissal_logged_but_continuous_long_lie_reopens(client):
    auth(client)
    g = client.app.state.guardian
    frames = list(scenario_frames('long_lie', 'daniel', duration=130))[::3]
    for frame in frames[:35]: g.ingest(frame)
    event = g.events()[0]
    assert client.post(f'/api/events/{event.id}/feedback', json={'label':'false_alarm'}).status_code == 200
    assert g.event(event.id).status == 'dismissed'
    assert g.event(event.id).human_review == 'false_alarm'
    for frame in frames[35:]: g.ingest(frame)
    assert any(e.status == 'open' and e.floor_urgency == 3 for e in g.events())
    assert all(n.state not in ('pending','retry') for n in g.event(event.id).notifications)


def test_gemini_package_is_categories_only_and_revocation_blocks_pending(guardian):
    guardian.settings.gemini_api_key = 'test'
    guardian.settings.gemini_billed_project_confirmed = True
    p = next(p for p in guardian.profiles() if p.id == 'daniel')
    p = guardian.save_profile(p.model_copy(update={'gemini_consent':True, 'care_notes':'private-note-secret'}))
    for f in scenario_frames('fall', 'daniel'): guardian.ingest(f)
    with guardian.db.sessions() as s:
        package, refs = event_package(s.get(Event, guardian.events()[0].id), [])
    encoded = json.dumps(package)
    assert all(secret not in encoded for secret in ('private-note-secret','BPPV','Orthostatic','Daniel','contacts','medication_flags'))
    assert package['profile']['risk_categories'] == ['balance_support']
    guardian.save_profile(p.model_copy(update={'gemini_consent':False}))
    worker = GeminiWorker(guardian)
    async def forbidden(*args): pytest.fail('Revoked profile must not reach external transport')
    worker.request = forbidden
    asyncio.run(worker.tick())
    assert guardian.events()[0].verdict_status == 'privacy_blocked'


def test_purge_removes_associated_data_and_does_not_reseed_consent(client):
    auth(client)
    g = client.app.state.guardian
    for frame in scenario_frames('fall', 'daniel'): g.ingest(frame)
    eid = g.events()[0].id
    client.post('/api/privacy/daniel/consent', json={'accepted':True,'local_preview':True})
    before = client.get('/api/privacy/daniel').json()['counts']
    assert before['windows'] and before['events'] and before['keyframes']
    assert client.post('/api/privacy/daniel/purge', json={'confirm_alias':'wrong'}).status_code == 409
    result = client.post('/api/privacy/daniel/purge', json={'confirm_alias':'Daniel'}).json()
    assert not any(result['remaining'].values())
    assert result['deleted']['audits'] >= 1
    assert client.get(f'/api/events/{eid}').status_code == 404
    g.seed()
    p = next(p for p in g.profiles() if p.id == 'daniel')
    assert not p.consent and p.alias == 'Deleted profile' and not p.contacts
    with pytest.raises(ValueError, match='consent'):
        g.ingest(next(scenario_frames('normal', 'daniel')))


def test_floor_demonstration_and_synthetic_trends_are_labelled(client):
    auth(client); g = client.app.state.guardian
    for f in scenario_frames('fall', 'daniel'): g.ingest(f)
    ev = g.events()[0]
    decision = client.post(f'/api/demo/floor/{ev.id}').json()
    assert decision['blocked'] and 'Simulated' in decision['source'] and g.event(ev.id).urgency == ev.urgency
    before = client.get('/api/evidence').json()['scored_windows']
    assert client.post('/api/demo/trends/daniel').json()['synthetic']
    points = client.get('/api/trends/daniel').json()['points']
    assert len([p for p in points if p['synthetic']]) == 12
    assert client.get('/api/evidence').json()['scored_windows'] == before


def test_validation_requires_disjoint_reviewed_exposure_and_diversity():
    normal = [{'timestamp':i*.5, 'features':features()} for i in range(2402)]
    tuning = [{'session':'tune', 'person':'a', 'angle':'front', 'label':'normal', 'windows':normal}]
    held = [{'session':'normal-test', 'person':'b', 'angle':'side', 'label':'normal', 'windows':normal}]
    held += [{'session':f'positive-{j}', 'person':'a' if j%2 else 'b', 'angle':'front' if j%2 else 'side',
              'label':'abnormal', 'windows':[{'timestamp':i*.5, 'features':features(1)} for i in range(4)]} for j in range(10)]
    report = validation_report(stats(), ['calibration'], tuning, held)
    assert report['passed'] and report['threshold'] == 3.5 and report['held_out']['recall'] == 1
    assert not validation_report(stats(), ['calibration'], tuning, held[:1])['passed']
    with pytest.raises(ValueError, match='disjoint'):
        validation_report(stats(), ['normal-test'], tuning, held)
