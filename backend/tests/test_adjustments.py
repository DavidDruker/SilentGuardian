import asyncio
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.baseline import (FEATURES, blend_statistics, completed_day_cutoff,
                                  fit_statistics, score_statistics, independent_records,
                                  identity_disagrees, fit_identity)
from backend.app.db import Audit, CoverageHour, Event, PersonalBaseline, Stream, Window
from backend.app.gemini import GeminiWorker, event_package
from backend.app.notifications import NotificationWorker
from backend.app.replay import scenario_frames
from backend.app.schemas import BedZone, Pose
from scripts.evaluate_baseline import validation_report


def features(value=0):
    return {**{name: value for name in FEATURES}, 'posture': 'standing'}


IDENTITY = {'shoulders': .52, 'hips': .41, 'arms': 1.04, 'legs': 1.4}


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


def test_identity_requires_independent_reviewed_shape_and_multiple_clear_differences():
    records = [{'session': 'reviewed', 'timestamp': i*2.,
                'features': {'identity_signature': IDENTITY}} for i in range(25)]
    assert fit_identity(records) == IDENTITY
    assert not identity_disagrees(IDENTITY, {**IDENTITY, 'shoulders': 1.2})
    assert identity_disagrees(IDENTITY, {key: value*1.6 for key, value in IDENTITY.items()})


def test_person_check_pauses_mismatched_stream_before_rules_or_learning(guardian):
    from backend.app.detector import sample
    normal = list(scenario_frames('normal', 'daniel'))
    signature = sample(normal[0], next(p for p in guardian.profiles() if p.id == 'daniel'))['identity']
    model = stats()
    with guardian.db.sessions.begin() as s:
        s.add(PersonalBaseline(person_id='daniel', anchor=model, current=model,
            threshold=3.5, trigger_enabled=False, updated_at=guardian.clock(),
            evidence={'identity_anchor': signature, 'identity_current': signature}))
    # Uniform scaling is camera distance, so it must NOT imply another person.
    scaled = []
    for frame in normal[:15]:
        landmarks = frame.poses[0].landmarks
        changed = [point.model_copy(update={'x': .5 + (point.x-.5)*1.7,
                                             'y': .55 + (point.y-.55)*1.7})
                   for point in landmarks]
        scaled.append(frame.model_copy(update={'poses': [Pose(track_id='subject', landmarks=changed)]}))
    assert [guardian.ingest(frame).identity_state for frame in scaled][-1] == 'matched'
    # Widths and leg length change relative to the torso, persistently.
    different = []
    for frame in normal[15:40]:
        changed = []
        for i, point in enumerate(frame.poses[0].landmarks):
            x = .5 + (point.x-.5)*1.8 if i in (11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28) else point.x
            y = .55 + (point.y-.55)*1.7 if i in (25, 26, 27, 28) else point.y
            changed.append(point.model_copy(update={'x': x, 'y': y}))
        different.append(frame.model_copy(update={'poses': [Pose(track_id='subject', landmarks=changed)]}))
    results = [guardian.ingest(frame) for frame in different]
    assert results[-1].identity_state == 'mismatch'
    assert results[-1].quality == 'identity_mismatch'
    assert not guardian.events()
    with guardian.db.sessions() as s:
        stream_id = f'daniel:synthetic-demo:{normal[0].session_id}'
        assert s.get(Stream, stream_id).state['identity_state'] == 'mismatch'
        assert not list(s.scalars(select(Window).where(Window.person_id == 'daniel',
                                                 Window.features['identity_state'].as_string() == 'mismatch')))


def test_baseline_requires_three_continuous_windows_and_keeps_rules(guardian):
    from backend.app.detector import sample
    model = stats()
    model['standing']['sway']['median'] = .5
    frames = list(scenario_frames('normal', 'daniel'))
    identity = sample(frames[0], next(p for p in guardian.profiles() if p.id == 'daniel'))['identity']
    with guardian.db.sessions.begin() as session:
        session.add(PersonalBaseline(person_id='daniel', anchor=model, current=model, threshold=3.5,
                                     trigger_enabled=True, evidence={'test_only': True,
                                         'identity_anchor': identity, 'identity_current': identity},
                                     updated_at=guardian.clock()))
    results = [guardian.ingest(f) for f in frames[:40]]
    assert not guardian.events()
    third = next((result for frame in frames[40:60]
                  if (result := guardian.ingest(frame)).rule_hits), None)
    assert third is not None
    assert third.rule_hits == ['personal_baseline']
    assert third.baseline_contributions[0]['feature'] == 'sway'
    assert guardian.events()[0].floor_urgency == 1
    later = frames[70].model_copy(update={'session_id': 'new-baseline-session'})
    assert guardian.ingest(later).rule_hits == []
    # A fixed fall still has its minimum concern floor regardless of baseline state.
    for f in scenario_frames('fall', 'daniel'):
        guardian.ingest(f)
    assert any(e.floor_urgency >= 2 and 'fall' in e.rule_hits for e in guardian.events())


def test_daily_update_blends_previous_baseline_deletes_windows_and_keeps_events(guardian, clock):
    model = stats()
    cutoff = completed_day_cutoff(clock(), 'America/Toronto')
    old = cutoff.timestamp()-3600
    profile = next(p for p in guardian.profiles() if p.id == 'daniel')
    with guardian.db.sessions.begin() as s:
        s.add(PersonalBaseline(person_id='daniel', anchor=model, current=model, threshold=3.5, trigger_enabled=True,
                               evidence={'identity_anchor': IDENTITY, 'identity_current': IDENTITY}, updated_at=clock()))
        for i in range(70):
            eligible = i < 35
            s.add(Window(ts=datetime.fromtimestamp(old-150+i*2, timezone.utc), stream_id='observed',
                         person_id='daniel', quality=1, features={**features(.02 if eligible else 9),
                             'baseline_eligible': eligible, 'identity_state': 'matched' if eligible else 'mismatch',
                             'identity_signature': {key: value*1.1 for key, value in IDENTITY.items()}},
                         rule_hits=[] if eligible else ['fall'], calibration=False, sequence=None))
        s.add(Window(ts=datetime.fromtimestamp(cutoff.timestamp()+60, timezone.utc), stream_id='today',
                     person_id='daniel', quality=1, features={**features(.5), 'baseline_eligible': True},
                     rule_hits=[], calibration=False, sequence=None))
        s.add(Window(ts=datetime.fromtimestamp(old-300, timezone.utc), stream_id='synthetic-trend:daniel',
                     person_id='daniel', quality=1, features={**features(2), 'synthetic_trend': True},
                     rule_hits=[], calibration=False, sequence=None))
        s.add(Event(id='event-kept', person_id='daniel', stream_id='observed', profile=profile.model_dump(),
                    source='live', opened_at=old, observed_at=old, updated_at=old,
                    rule_hits=['fall'], floor_urgency=2, urgency=2, status='acknowledged',
                    summary='Stored event', features={'sway': 9}, keyframes=[],
                    verdict_status='rules_only', verdict=None, next_escalation_at=None))
    result = guardian.baseline.daily_update(clock(), .8, 'America/Toronto')
    assert result['trained_profiles'] == ['daniel'] and result['deleted_windows'] == 70
    with guardian.db.sessions() as s:
        b = s.get(PersonalBaseline, 'daniel')
        assert b.anchor['standing']['sway']['median'] == 0
        assert b.current['standing']['sway']['median'] == pytest.approx(.004)
        assert b.evidence['identity_anchor'] == IDENTITY
        assert b.evidence['identity_current']['shoulders'] == pytest.approx(IDENTITY['shoulders']*1.02)
        assert b.trigger_enabled
        assert s.get(Event, 'event-kept').summary == 'Stored event'
        assert {w.stream_id for w in s.scalars(select(Window))} == {'today', 'synthetic-trend:daniel'}
        # The Activity grid still knows the purged hour was monitored.
        assert [(c.person_id, c.windows) for c in s.scalars(select(CoverageHour))] == [('daniel', 70)]


def test_statistical_blend_adds_new_posture_without_changing_inputs():
    previous = stats()
    daily = {'lying': {name: {'median': 1., 'mad': .2, 'scale': .2, 'count': 30} for name in FEATURES}}
    blended = blend_statistics(previous, daily, .8)
    assert blended['lying']['sway']['median'] == 1
    assert 'lying' not in previous


def test_reviewed_calibration_fits_and_preserves_first_anchor(client, clock):
    auth(client)
    g = client.app.state.guardian
    assert client.post('/api/baseline/daniel/fit', json={'reviewed_normal':False}).status_code == 422
    assert client.post('/api/baseline/daniel/fit', json={'reviewed_normal':True}).status_code == 422
    with g.db.sessions.begin() as s:
        for i in range(64):
            s.add(Window(ts=datetime.fromtimestamp(clock()-i*2, timezone.utc), stream_id=f'camera-session-{i//32}',
                         person_id='daniel', quality=1, features={**features(.02), 'identity_signature': IDENTITY},
                         rule_hits=[], calibration=True, sequence=None))
    fitted = client.post('/api/baseline/daniel/fit', json={'reviewed_normal':True})
    assert fitted.status_code == 200 and fitted.json()['anchor_frozen']
    with g.db.sessions.begin() as s:
        assert not list(s.scalars(select(Window).where(Window.calibration == True)))
        for i in range(64):
            s.add(Window(ts=datetime.fromtimestamp(clock()-500-i*2, timezone.utc), stream_id=f'recalibration-{i//32}',
                         person_id='daniel', quality=1, features={**features(.04), 'identity_signature': IDENTITY},
                         rule_hits=[], calibration=True, sequence=None))
    assert client.post('/api/baseline/daniel/fit', json={'reviewed_normal':True}).status_code == 200
    b = client.get('/api/baseline/daniel').json()
    assert b['anchor']['standing']['sway']['median'] == .02
    assert b['current']['standing']['sway']['median'] == .04
    assert not b['trigger_enabled']


def test_second_person_remains_uncertain_even_with_mask(guardian):
    frame = next(scenario_frames('restricted_standing', 'margaret'))
    bystander = Pose(track_id='outside', landmarks=[p.model_copy(update={'x': p.x+.25}) for p in frame.poses[0].landmarks])
    unmasked = frame.model_copy(update={'poses': frame.poses+[bystander]})
    assert guardian.ingest(unmasked).quality == 'uncertain'
    masked = unmasked.model_copy(update={'session_id': 'masked', 'region': BedZone(x1=.3,y1=0,x2=.65,y2=1)})
    assert guardian.ingest(masked).quality == 'uncertain'
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


def test_gemini_package_includes_care_context_without_profile_opt_in(guardian):
    guardian.settings.gemini_api_key = 'test'
    guardian.settings.gemini_billed_project_confirmed = True
    guardian.settings.decision_mode = 'enforce'
    guardian.score_candidate = lambda candidate, samples: {
        'status': 'scored', 'model': 'test', 'fall_score': .9,
        'staggering_score': .01, 'latency_ms': 1,
    }
    p = next(p for p in guardian.profiles() if p.id == 'daniel')
    p = guardian.save_profile(p.model_copy(update={'gemini_consent':True, 'care_notes':'private-note-secret'}))
    for f in scenario_frames('fall', 'daniel'): guardian.ingest(f)
    with guardian.db.sessions() as s:
        pending = s.scalar(select(Event).where(Event.verdict_status == 'pending'))
        assert pending is not None and pending.status == 'open'
        package, refs = event_package(pending, [])
    encoded = json.dumps(package)
    assert all(secret not in encoded for secret in ('Daniel','contacts'))
    assert package['profile']['conditions'] == ['BPPV', 'Orthostatic hypotension']
    assert package['profile']['care_notes'] == 'private-note-secret'
    assert package['profile']['medication_flags'] == []
    assert package['profile']['mobility_aid'] == 'none'
    assert package['profile']['risk_categories'] == ['balance_support']
    guardian.save_profile(p.model_copy(update={'gemini_consent':False}))
    worker = GeminiWorker(guardian)
    sent = []
    async def review(package, frames):
        sent.append(package)
        return 'not valid JSON'
    worker.request = review
    asyncio.run(worker.tick())
    # Gemini needs no per-profile opt-in, and a failed review keeps the alert open.
    event = guardian.events()[0]
    assert sent and event.verdict_status == 'fallback' and event.status == 'open'


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
