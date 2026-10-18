import asyncio
import json
import math
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import select

from backend.app.config import Settings
from backend.app.db import Audit, Database, Event, Notification, Window
from backend.app.detector import evaluate
from backend.app.gemini import GeminiWorker, validate_verdict
from backend.app.notifications import Delivery, NotificationWorker, queue_demo_call
from backend.app.replay import scenario_frames, skeleton
from backend.app.schemas import PoseFrame
from backend.app.service import Guardian


def run(g, name, person='daniel', duration=8):
    results=[]
    for frame in scenario_frames(name,person,duration=duration):
        results.append(g.ingest(frame))
    return results


@pytest.mark.parametrize('scenario,person,hit', [('fall','daniel','fall'),('stumble','daniel','stumble'),
    ('restricted_standing','margaret','restricted_standing'),('sway','daniel','sway'),('bed_exit','margaret','bed_exit')])
def test_actual_pipeline_scenarios(guardian,scenario,person,hit):
    out=run(guardian,scenario,person,duration=9)
    assert any(hit in r.rule_hits for r in out)
    assert len(guardian.events()) == (2 if scenario == 'bed_exit' else 1)
    with guardian.db.sessions() as s:
        assert len(list(s.scalars(select(Window))))>=20


@pytest.mark.parametrize('scenario,person', [('normal','daniel'),('assisted_standing','margaret'),('restricted_standing','daniel')])
def test_negative_scenarios(guardian,scenario,person):
    run(guardian,scenario,person)
    assert guardian.events()==[]


def test_restricted_standing_and_fall_open_independent_alerts(guardian):
    frames = [frame.model_copy(update={'source': 'upload', 'camera_id': 'browser-camera'})
              for frame in scenario_frames('fall', 'margaret')]
    results = [guardian.ingest(frame) for frame in frames]
    events = {event.rule_hits[0]: event for event in guardian.events()}
    assert set(events) == {'restricted_standing', 'fall'}
    assert all(event.status == 'open' and event.notifications for event in events.values())
    assert any(set(result.event_ids) == {events['fall'].id, events['restricted_standing'].id}
               for result in results)
    capture = guardian.capture_result('margaret', frames[0].session_id)
    assert {event['rule_hits'][0] for event in capture['events']} == {'restricted_standing', 'fall'}
    guardian.acknowledge(events['restricted_standing'].id, 'Tester', 'Checked standing')
    assert guardian.event(events['fall'].id).status == 'open'


def test_borderline_hip_drop_is_now_a_possible_fall(guardian):
    results = []
    start = None
    for frame in scenario_frames('normal', 'daniel'):
        if start is None:
            start = frame.timestamp
        elapsed = frame.timestamp - start
        if elapsed >= 2:
            progress = min(1, (elapsed - 2) / .65)
            body = frame.model_dump()
            body['poses'] = [{'track_id': 'subject',
                              'landmarks': skeleton(y=.55 + .13 * progress, angle=math.pi / 2 * progress)}]
            frame = PoseFrame.model_validate(body)
        results.append(guardian.ingest(frame))
    fall_results = [result for result in results if 'fall' in result.rule_hits]
    assert fall_results
    assert any(.35 < result.features['hip_drop'] < .5 for result in fall_results)


def test_multiple_people_do_not_enter_single_person_detector(guardian):
    results = run(guardian, 'assisted_standing', 'margaret')
    assert all(result.quality == 'uncertain' for result in results)
    assert guardian.events() == []


def test_low_visibility_is_uncertain_not_normal(guardian):
    out=run(guardian,'low_visibility')
    assert all(r.quality=='uncertain' for r in out)
    assert not guardian.events()


def test_duplicate_and_reconnect_survive_restart(guardian):
    frames=list(scenario_frames('fall','daniel'))
    for f in frames[:45]: guardian.ingest(f)
    g2=Guardian(Database(guardian.db.engine.url),guardian.settings,guardian.clock)
    for f in frames[40:]: g2.ingest(f)
    assert len(g2.events())==1
    assert g2.ingest(frames[-1]).accepted is False
    g2.acknowledge(g2.events()[0].id,'Tester','Checked')
    g3=Guardian(Database(guardian.db.engine.url),guardian.settings,guardian.clock)
    assert g3.events()[0].status=='acknowledged'


def test_urgency_floors_with_continuous_evidence_and_no_gap_credit(guardian):
    p=next(p for p in guardian.profiles() if p.id=='daniel')
    state={}; checkpoints={}
    frames=list(scenario_frames('fall','daniel',duration=126))
    for f in frames:
        state,r,_=evaluate(f,p,state)
        if r and 'prolonged_stillness' in r['rule_hits']:
            seconds=r['features']['still_seconds']
            if seconds>=30: checkpoints.setdefault('30',f.timestamp)
            if seconds>=120: checkpoints.setdefault('120',f.timestamp)
    assert checkpoints['120']-checkpoints['30']==pytest.approx(90,abs=.4)
    # A missing two-minute camera interval resets the continuous stillness timer.
    state={}
    for f in frames[:100]: state,_,_=evaluate(f,p,state)
    later=frames[-1]
    state,r,_=evaluate(later,p,state)
    assert r['features']['still_seconds']==0
    assert 'prolonged_stillness' not in r['rule_hits']


def test_different_profiles_route_same_stumble(guardian):
    run(guardian,'stumble','daniel')
    run(guardian,'stumble','margaret')
    personal=next(e for e in guardian.events() if e.person_id=='daniel')
    care=next(e for e in guardian.events() if e.person_id=='margaret')
    assert personal.urgency==1 and personal.notifications==[]
    assert care.urgency>=2 and any(n.recipient=='Assigned care assistant' for n in care.notifications)


def test_worker_idempotence_and_ack_during_escalation(guardian,clock):
    run(guardian,'stumble')
    eid=guardian.events()[0].id
    worker=NotificationWorker(guardian)
    worker.tick(); assert not guardian.event(eid).notifications
    clock.advance(60); worker.tick(); first=guardian.event(eid)
    assert first.urgency==2 and first.notifications
    worker.tick(); assert guardian.event(eid).notifications==first.notifications
    guardian.acknowledge(eid,'First','Okay')
    guardian.acknowledge(eid,'Second','Duplicate')
    clock.advance(600); worker.tick()
    done=guardian.event(eid)
    assert done.acknowledged_by=='First' and done.next_escalation_at is None
    assert all(n.state in ('simulated','cancelled') for n in done.notifications)


def test_fall_rule_alert_does_not_claim_gemini_review(guardian):
    guardian.settings.gemini_api_key='not-a-real-key'
    out=run(guardian,'fall')
    ev=guardian.events()[0]
    assert ev.urgency>=2 and ev.notifications
    assert ev.verdict_status=='rules_only'
    assert ev.features['hip_drop'] > .5
    assert ev.features['trunk_angle'] > 60


def test_full_pipeline_prolonged_stillness_floor(guardian, clock):
    frames = list(scenario_frames('fall', 'daniel', duration=126))
    for frame in frames:
        guardian.ingest(frame)
        clock.advance(1 / 15)
    event = guardian.events()[0]
    assert event.floor_urgency == 3 and event.urgency == 3
    assert 'prolonged_stillness' in event.rule_hits
    assert event.features['still_seconds'] >= 120
    assert any(n.urgency == 3 for n in event.notifications)


def test_ack_waits_for_inflight_send_then_prevents_future_sends(guardian, clock):
    from threading import Event as Signal, Thread
    from backend.app.db import PushSubscription
    guardian.settings.notification_mode = 'live'
    run(guardian, 'fall')
    eid = guardian.events()[0].id
    with guardian.db.sessions.begin() as s:
        s.add(PushSubscription(id='daniel:son', person_id='daniel', contact_id='son',
                               body={'endpoint': 'https://fcm.googleapis.com/test', 'keys': {}}))
    entered, release, acknowledged = Signal(), Signal(), Signal()
    class Fake:
        sends = 0
        def push(self, subscription, payload):
            self.sends += 1
            entered.set()
            assert release.wait(5)
    delivery = Fake()
    worker = NotificationWorker(guardian, delivery)
    send_thread = Thread(target=worker.tick)
    send_thread.start()
    assert entered.wait(5)
    def ack():
        guardian.acknowledge(eid, 'Concurrent operator', 'Checked')
        acknowledged.set()
    ack_thread = Thread(target=ack)
    ack_thread.start()
    assert not acknowledged.wait(.05)
    release.set()
    send_thread.join(5)
    ack_thread.join(5)
    assert acknowledged.is_set()
    clock.advance(600)
    worker.tick()
    assert delivery.sends == 1


def test_profile_versions_immutable_and_stale_write_rejected(guardian):
    run(guardian,'stumble')
    old=next(p for p in guardian.profiles() if p.id=='daniel')
    updated=guardian.save_profile(old.model_copy(update={'alias':'Daniel demo'}))
    assert updated.version==2 and guardian.events()[0].alias=='Daniel'
    with pytest.raises(ValueError): guardian.save_profile(old)


def test_consent_stops_ingestion(guardian):
    p=next(p for p in guardian.profiles() if p.id=='daniel')
    guardian.save_profile(p.model_copy(update={'consent':False}))
    with pytest.raises(ValueError,match='paused'):run(guardian,'fall')


def verdict_json(**kwargs):
    return json.dumps(dict(verdict='fall',urgency=0,confidence=.8,summary='A possible fall was observed.',
                          evidence=[dict(ref='rule.fall',detail='Fall rule fired')],recommended_actions=['Please check on the person.'],**kwargs))


def test_gemini_schema_evidence_and_floor():
    assert validate_verdict(verdict_json(),{'rule.fall'},3).urgency==3
    with pytest.raises(ValueError):validate_verdict('invalid',{'rule.fall'},2)
    with pytest.raises(ValueError):validate_verdict(verdict_json(),{'metric.unknown'},2)
    with pytest.raises(ValueError):validate_verdict(verdict_json().replace('possible fall','stroke'),{'rule.fall'},2)


def test_gemini_transport_validates_multipart_response(guardian, monkeypatch):
    received = {}
    expected = verdict_json()
    async def post(self, url, **kwargs):
        received.update(url=url, **kwargs)
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [
            {'thought': True, 'text': 'Internal reasoning is not a verdict.'},
            {'text': expected[:30]}, {'text': expected[30:]}
        ]}}]}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    guardian.settings.gemini_api_key = 'test'
    guardian.settings.gemini_billed_project_confirmed = True
    body = asyncio.run(GeminiWorker(guardian).request({'available_evidence': ['rule.fall']}, []))
    assert validate_verdict(body, {'rule.fall'}, 2).urgency == 2
    assert received['headers']['x-goog-api-key'] == 'test'
    assert received['url'].endswith('/gemini-3.8-flash:generateContent')
    assert received['json']['generationConfig']['responseFormat']['text']['mimeType'] == 'application/json'
    # Gemini 3 defaults to high thinking, which can outlast the review timeout.
    assert received['json']['generationConfig']['thinkingConfig'] == {'thinkingLevel': 'low'}


@pytest.mark.parametrize('failure',['timeout','malformed','raised'])
def test_gemini_failure_preserves_alert(guardian,failure):
    guardian.settings.gemini_api_key='test';guardian.settings.gemini_timeout_seconds=.01
    guardian.settings.gemini_billed_project_confirmed = True
    guardian.settings.decision_mode = 'enforce'
    guardian.score_candidate = lambda candidate, samples: {
        'status': 'scored', 'model': 'test', 'fall_score': .9,
        'staggering_score': .01, 'latency_ms': 1,
    }
    p = next(p for p in guardian.profiles() if p.id == 'daniel')
    guardian.save_profile(p.model_copy(update={'gemini_consent': True}))
    run(guardian,'fall')
    w=GeminiWorker(guardian)
    async def fake(*args):
        if failure=='timeout':await asyncio.sleep(1)
        if failure=='raised':raise RuntimeError('failure')
        return 'not valid JSON'
    w.request=fake
    asyncio.run(w.tick())
    ev=guardian.events()[0]
    assert ev.urgency>=2 and ev.verdict_status=='fallback' and ev.notifications
    assert ev.features['gemini_error']  # the reason is kept for the alert log


def test_manual_call_dry_run_idempotent(guardian):
    from uuid import uuid4
    request=uuid4();eid=queue_demo_call(guardian,request)
    assert queue_demo_call(guardian,request)==eid
    NotificationWorker(guardian).tick()
    assert len(guardian.event(eid).notifications)==1
    assert guardian.event(eid).notifications[0].state=='simulated'


def test_replay_evaluator_cannot_inherit_live_delivery(monkeypatch):
    from scripts.evaluate_replays import offline_guardian
    monkeypatch.setenv('NOTIFICATION_MODE', 'live')
    monkeypatch.setenv('CALLS_ENABLED', 'true')
    monkeypatch.setenv('DEMO_CALL_TO', '+15005550009')
    g = offline_guardian()
    run(g, 'fall')
    class NoExternalDelivery:
        def push(self, *args):
            pytest.fail('Offline evaluation attempted web push')
        def call(self, *args):
            pytest.fail('Offline evaluation attempted a phone call')
    NotificationWorker(g, NoExternalDelivery()).tick()
    assert g.events()[0].notifications[0].state == 'simulated'
    assert g.settings.demo_call_to == '' and not g.settings.calls_enabled


def test_real_call_request_is_ring_only_and_allowlisted(monkeypatch):
    received={}
    def post(self,url,**kwargs):
        received.update(kwargs)
        return httpx.Response(201,json={'sid':'CA_test'},request=httpx.Request('POST',url))
    monkeypatch.setattr(httpx.Client,'post',post)
    cfg=Settings.model_construct(calls_enabled=True,twilio_account_sid='AC_test',twilio_auth_token='test',twilio_from_number='+15005550006',call_test_allowlist='+15005550009')
    assert Delivery(cfg).call('+15005550009')=='CA_test'
    assert received['data']['Twiml']=='<Response><Hangup/></Response>'
    assert received['data']['Timeout']=='15' and 'Record' not in received['data']
    with pytest.raises(ValueError):Delivery(cfg).call('+15005550008')


def test_call_throttles_and_uncertain_request_not_retried(guardian,clock):
    from uuid import uuid4
    guardian.settings.notification_mode='live'
    class Fake:
        calls=0
        def call(self,to):
            self.calls+=1
            raise httpx.ReadTimeout('unknown acceptance')
    fake=Fake();worker=NotificationWorker(guardian,fake)
    eid=queue_demo_call(guardian,uuid4());worker.tick();worker.tick()
    assert fake.calls==1 and guardian.event(eid).notifications[0].state=='unknown'
    other=queue_demo_call(guardian,uuid4());worker.tick()
    assert fake.calls==1 and guardian.event(other).notifications[0].state=='suppressed'


def test_live_timestamp_and_replay_gate(guardian,clock):
    f=next(scenario_frames('normal','daniel'))
    with pytest.raises(ValueError):guardian.ingest(f.model_copy(update={'source':'live','timestamp':clock()-100}))
    guardian.settings.demo_enabled=False
    with pytest.raises(ValueError):guardian.ingest(f)


def test_recovery_ends_the_prior_fall_stillness_episode(guardian):
    p = next(p for p in guardian.profiles() if p.id == 'daniel')
    fall = list(scenario_frames('fall', 'daniel', duration=5))
    state = {}
    for f in fall:
        state, _, _ = evaluate(f, p, state)
    assert state['fall_seen']
    for f in scenario_frames('normal', 'daniel', start=fall[-1].timestamp+1/15, duration=5):
        state, _, _ = evaluate(f, p, state)
    assert not state['fall_seen']


def test_tiger_migration_splits_statements_outside_comments():
    from pathlib import Path
    from scripts.migrate_tiger import statements
    parts = statements(Path('deploy/tiger.sql').read_text())
    assert len(parts) == 9
    assert any("create_hypertable('event_stage_log'" in p for p in parts)
    assert any("create_hypertable('monitoring_coverage'" in p for p in parts)
    assert any('hourly_incidents' in p for p in parts)
    assert all(p.split()[0].upper() in ('SELECT', 'CREATE') and '--' not in p for p in parts)
    assert statements('-- a; b\nSELECT 1;\n-- c;\nSELECT 2;') == ['SELECT 1', 'SELECT 2']


def test_synthetic_seeds_allow_gemini_and_disclaimers_pass_validation(guardian):
    assert all(p.gemini_consent for p in guardian.profiles())
    disclaimer = verdict_json().replace('A possible fall was observed.', 'Observation only, not a diagnosis.')
    assert validate_verdict(disclaimer, {'rule.fall'}, 2).urgency == 2
    with pytest.raises(ValueError, match='Diagnostic'):
        validate_verdict(verdict_json().replace('possible fall', 'possible stroke'), {'rule.fall'}, 2)


def test_ingest_is_not_blocked_by_inflight_push(guardian):
    import time
    from threading import Event as Signal, Thread
    from backend.app.db import PushSubscription
    guardian.settings.notification_mode = 'live'
    run(guardian, 'fall')
    with guardian.db.sessions.begin() as s:
        s.add(PushSubscription(id='daniel:son', person_id='daniel', contact_id='son',
                               body={'endpoint': 'https://fcm.googleapis.com/test', 'keys': {}}))
    entered, release = Signal(), Signal()
    class Slow:
        def push(self, subscription, payload):
            entered.set()
            assert release.wait(5)
    send_thread = Thread(target=NotificationWorker(guardian, Slow()).tick)
    send_thread.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        for frame in scenario_frames('normal', 'margaret', duration=1):
            guardian.ingest(frame)
        assert time.monotonic()-started < .5
    finally:
        release.set()
        send_thread.join(5)
    sent = [n for n in guardian.events(person_id='daniel')[0].notifications if n.channel == 'push' and n.state == 'provider_accepted']
    assert len(sent) == 1


def test_stream_state_is_checkpointed_not_rewritten_per_frame(guardian):
    from sqlalchemy import event as sa_event
    writes = []
    def count(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith(('UPDATE STREAMS', 'INSERT INTO STREAMS')):
            writes.append(statement)
    sa_event.listen(guardian.db.engine, 'before_cursor_execute', count)
    frames = list(scenario_frames('normal', 'daniel', duration=6, session_id='checkpoint'))
    low = list(scenario_frames('low_visibility', 'daniel', start=frames[-1].timestamp+1, duration=3, session_id='checkpoint'))
    for f in frames+low:
        guardian.ingest(f)
    sa_event.remove(guardian.db.engine, 'before_cursor_execute', count)
    span = (frames[-1].timestamp-frames[0].timestamp)+(low[-1].timestamp-low[0].timestamp)
    assert len(writes) <= span*3.4+2 and len(writes) < len(frames+low)/3
    with guardian.db.sessions() as s:
        assert len(list(s.scalars(select(Window).where(Window.quality == 0)))) <= 3*3.4+1


def occlude(frame):
    pose = frame.poses[0].model_dump()
    pose['landmarks'] = [{**l, 'visibility': .1} for l in pose['landmarks']]
    return PoseFrame.model_validate({**frame.model_dump(), 'poses': [pose]})


def first_long_lie(frames, profile, gap=None):
    state = {}
    for f in frames:
        if gap and gap[0] <= f.timestamp-frames[0].timestamp < gap[1]:
            f = occlude(f)
        state, r, _ = evaluate(f, profile, state)
        if r and 'prolonged_stillness' in r['rule_hits']:
            return f.timestamp-frames[0].timestamp
    return None


def test_long_lie_survives_brief_occlusion_but_not_long_gaps(guardian):
    p = next(p for p in guardian.profiles() if p.id == 'daniel')
    frames = list(scenario_frames('fall', 'daniel', duration=45))
    clear = first_long_lie(frames, p)
    assert clear is not None
    brief = first_long_lie(frames, p, gap=(10, 12))
    assert brief is not None and clear <= brief <= clear+1
    long = first_long_lie(frames, p, gap=(10, 15))
    assert long is None or long >= clear+4


def test_brief_dropout_keeps_window_but_long_gap_resets():
    from backend.app.detector import evaluate
    from backend.app.seed import demo_profiles
    profile = demo_profiles()[1]
    frames = list(scenario_frames('normal', profile.id, 1792217400))[:31]
    state = {}
    for frame in frames[:20]:
        state, _, _ = evaluate(frame, profile, state)
    kept = len(state['samples'])
    blank = frames[20].model_copy(update={'poses': []})
    state, result, reason = evaluate(blank, profile, state)
    assert (reason, result, len(state['samples'])) == ('brief_dropout', None, kept)
    late = frames[20].model_copy(update={'poses': [], 'timestamp': frames[19].timestamp + 1})
    state, result, reason = evaluate(late, profile, state)
    assert result['quality'] == 'uncertain' and state['samples'] == []


def test_posture_holds_until_reading_clearly_changes():
    from backend.app.detector import classify_posture
    # 38 degrees is between the standing (35) and hold (42) thresholds.
    assert classify_posture(38, .6, .2, .2) == 'sitting'
    assert classify_posture(38, .6, .2, .2, previous='standing') == 'standing'
    assert classify_posture(45, .6, .2, .2, previous='standing') == 'sitting'
    assert classify_posture(55, .3, .3, .2, previous='lying') == 'lying'


def test_gemini_reply_keeps_valid_citations_and_trims_long_text():
    body = json.loads(verdict_json())
    body['evidence'].append({'ref': 'metric.invented', 'detail': 'Not in the package'})
    body['summary'] = 'x' * 900
    verdict = validate_verdict(json.dumps(body), {'rule.fall'}, 2)
    assert [e.ref for e in verdict.evidence] == ['rule.fall'] and len(verdict.summary) == 400


def test_failed_review_records_the_reason():
    from backend.app.gemini import describe_failure
    request = httpx.Request('POST', 'https://example.test')
    response = httpx.Response(404, json={'error': {'message': 'models/x is not found'}}, request=request)
    error = httpx.HTTPStatusError('not found', request=request, response=response)
    assert describe_failure(error) == 'Gemini API returned HTTP 404: models/x is not found'
    assert describe_failure(asyncio.TimeoutError()) == 'Timed out waiting for Gemini'
