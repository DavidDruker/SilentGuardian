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
from backend.app.replay import scenario_frames
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
    assert len(guardian.events())==1
    with guardian.db.sessions() as s:
        assert len(list(s.scalars(select(Window))))>=20


@pytest.mark.parametrize('scenario,person', [('normal','daniel'),('assisted_standing','margaret'),('restricted_standing','daniel')])
def test_negative_scenarios(guardian,scenario,person):
    run(guardian,scenario,person)
    assert guardian.events()==[]


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


def test_fall_alert_queued_before_ai(guardian):
    guardian.settings.gemini_api_key='not-a-real-key'
    out=run(guardian,'fall')
    ev=guardian.events()[0]
    assert ev.urgency>=2 and ev.notifications
    assert ev.verdict_status=='pending'
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


@pytest.mark.parametrize('failure',['timeout','malformed','raised'])
def test_gemini_failure_preserves_alert(guardian,failure):
    guardian.settings.gemini_api_key='test';guardian.settings.gemini_timeout_seconds=.01
    guardian.settings.gemini_billed_project_confirmed = True
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
