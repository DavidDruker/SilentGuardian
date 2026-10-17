import time
import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, select

from .db import Audit, Event, Notification, Person, ProfileVersion, Stream, Window
from .detector import evaluate
from .baseline import BaselineEngine
from .schemas import EventOut, IngestResult, NotificationOut, Profile
from .seed import demo_profiles


LABELS = {'fall': 'Possible fall, assessing. Please check on the person.',
          'prolonged_stillness': 'Stillness observed after a possible fall. Please check now.',
          'restricted_standing': 'Standing without a nearby helper during restricted hours.',
          'bed_exit': 'Movement out of the bed zone during restricted hours.',
          'stumble': 'A brief movement pattern consistent with a stumble was observed.',
          'sway': 'Sustained unsteadiness while standing was observed.',
          'personal_baseline': 'Movement remained outside the reviewed personal baseline for three consecutive windows.'}


def event_scale(ev):
    return ev.features.get('demo_time_scale', 1) if ev.source == 'replay' else 1


def event_features(previous, current):
    """Keep trigger peaks as evidence while posture and quality track the latest window."""
    merged = {**previous, **current}
    for name in ('hip_drop', 'trunk_angle', 'sway', 'wrist_speed', 'ankle_change', 'still_seconds'):
        if name in current:
            merged[name] = max(previous.get(name, 0) or 0, current[name] or 0)
    if previous.get('anomaly_score') is not None and (current.get('anomaly_score') is None or previous['anomaly_score'] > current['anomaly_score']):
        for name in ('anomaly_score', 'anchor_score', 'baseline_contributions_json', 'baseline_posture'):
            merged[name] = previous.get(name)
    return merged


class Guardian:
    def __init__(self, db, settings, clock=time.time):
        self.db, self.settings, self.clock = db, settings, clock
        self.calibrating = set()
        self.baseline = BaselineEngine(db)

    def seed(self):
        with self.db.lock, self.db.sessions.begin() as s:
            for p in demo_profiles():
                if not s.get(Person, p.id):
                    s.add(Person(id=p.id, version=1, body=p.model_dump()))
                    s.add(ProfileVersion(person_id=p.id, version=1, body=p.model_dump(), created_at=self.clock(), author='Synthetic seed'))

    def profiles(self):
        with self.db.sessions() as s:
            return [Profile.model_validate(p.body) for p in s.scalars(select(Person).order_by(Person.id))]

    def save_profile(self, p):
        with self.db.lock, self.db.sessions.begin() as s:
            person = s.get(Person, p.id, with_for_update=True)
            if person and person.version != p.version:
                raise ValueError('Profile changed since it was opened; reload before saving.')
            body = p.model_copy(update={'version': person.version + 1 if person else 1}).model_dump()
            if person:
                person.version, person.body = body['version'], body
            else:
                s.add(Person(id=p.id, version=body['version'], body=body))
            s.add(ProfileVersion(person_id=p.id, version=body['version'], body=body, created_at=self.clock()))
            return Profile.model_validate(body)

    def ingest(self, frame):
        now = self.clock()
        if frame.source == 'replay' and not self.settings.demo_enabled:
            raise ValueError('Synthetic replay is disabled')
        if frame.source == 'live' and abs(frame.timestamp-now) > 15:
            raise ValueError('Camera clock differs from server by more than 15 seconds')
        if frame.demo_time_scale != 1 and (frame.source != 'replay' or not self.settings.demo_enabled):
            raise ValueError('Accelerated time is only available for labelled demo replays')
        stream_id = f'{frame.person_id}:{frame.camera_id}:{frame.session_id}'
        with self.db.lock, self.db.sessions.begin() as s:
            person = s.get(Person, frame.person_id)
            if not person:
                raise ValueError('Unknown profile')
            p = Profile.model_validate(person.body)
            if not p.consent:
                raise ValueError('Monitoring consent is paused')
            stream = s.get(Stream, stream_id, with_for_update=True)
            if stream is None:
                stream = Stream(id=stream_id, person_id=p.id, state={}, updated_at=now)
                s.add(stream)
            state, result, reason = evaluate(frame, p, stream.state)
            stream.updated_at = now
            if result is None:
                stream.state = state
                return IngestResult(accepted=reason != 'duplicate_or_out_of_order', reason=reason, quality='buffering')
            features, hits = result['features'], result['rule_hits']
            samples = result['samples']
            window_ready = len(samples) >= 3 and samples[-1]['t']-samples[0]['t'] >= 1.8
            baseline = self.baseline.score(s, p.id, features) if window_ready else {
                'score': None, 'anchor_score': None, 'contributions': [], 'state': 'warming up or uncertain', 'flag': False}
            continuous = state.get('baseline_at') is not None and frame.timestamp-state['baseline_at'] <= .75
            state['baseline_count'] = (state.get('baseline_count', 0) if continuous else 0)+1 if baseline['flag'] and result['quality'] == 'good' else 0
            state['baseline_at'] = frame.timestamp
            stream.state = dict(state)
            if state['baseline_count'] >= 3:
                hits.append('personal_baseline')
            features.update(anomaly_score=baseline['score'], anchor_score=baseline['anchor_score'],
                            baseline_posture=features.get('posture'),
                            demo_time_scale=frame.demo_time_scale,
                            baseline_eligible=window_ready and not hits and not baseline['flag'] and result['quality'] == 'good',
                            baseline_contributions_json=json.dumps(baseline['contributions']))
            # Persist at score cadence, including uncertain windows. No raw video is accepted.
            s.merge(Window(ts=datetime.fromtimestamp(frame.timestamp, timezone.utc), stream_id=stream_id, person_id=p.id,
                           quality=features.get('visibility', 0), features=features, rule_hits=hits,
                           calibration=p.id in self.calibrating and window_ready and not hits and result['quality'] == 'good',
                           sequence=None))
            recent = s.scalar(select(Event).where(Event.stream_id == stream_id).order_by(Event.opened_at.desc()).limit(1))
            active = recent if recent and recent.status == 'open' else None
            ev = active
            if hits:
                floor = 3 if features.get('still_seconds', 0) >= 120 else 2 if any(h in hits for h in ('fall', 'prolonged_stillness', 'restricted_standing', 'bed_exit')) else 1
                if p.mode == 'care' and 'stumble' in hits and p.mobility_aid != 'none':
                    floor = max(floor, 2)
                cooldown = recent and now-recent.updated_at < 60/frame.demo_time_scale and set(hits).issubset(recent.rule_hits) and floor <= recent.floor_urgency
                if not ev and not cooldown:
                    keys = result['samples']
                    keyframes = [{'timestamp': x['t'], 'landmarks': x['landmarks']} for x in (keys[0], keys[len(keys)//2], keys[-1])] if keys else []
                    ev = Event(id=str(uuid4()), person_id=p.id, stream_id=stream_id, profile=p.model_dump(),
                               source=frame.source, opened_at=now, observed_at=frame.timestamp, updated_at=now,
                               rule_hits=hits, floor_urgency=floor, urgency=floor, status='open', summary=LABELS[hits[0]],
                               features=features, keyframes=keyframes, verdict_status='pending' if self.settings.gemini_api_key else 'rules_only',
                               next_escalation_at=now+(p.check_in_seconds if floor == 1 and p.mode == 'personal' and p.check_in_first else 180)/frame.demo_time_scale)
                    s.add(ev)
                    s.flush()
                    self.queue(s, ev, now)
                elif ev:
                    old = ev.urgency
                    ev.rule_hits = list(dict.fromkeys(ev.rule_hits+hits))
                    ev.floor_urgency = max(ev.floor_urgency, floor)
                    ev.urgency = max(ev.urgency, floor)
                    ev.features, ev.updated_at = event_features(ev.features, features), now
                    if ev.urgency > old:
                        ev.summary = LABELS['prolonged_stillness'] if 'prolonged_stillness' in hits else LABELS[hits[0]]
                        self.queue(s, ev, now)
            if ev and result['quality'] == 'good':
                ev.features = event_features(ev.features, features)
            return IngestResult(accepted=True, reason=reason, quality=result['quality'], features=features,
                                rule_hits=hits, event_id=ev.id if ev else None, anomaly_score=baseline['score'], anchor_score=baseline['anchor_score'],
                                baseline_contributions=baseline['contributions'], baseline_state=baseline['state'])

    def queue(self, s, ev, now, repeat=False):
        if ev.status != 'open' or ev.urgency < 1:
            return
        p = Profile.model_validate(ev.profile)
        if p.mode == 'personal' and p.check_in_first and ev.urgency == 1:
            return
        contacts = [c.model_dump() for c in p.contacts]
        if self.settings.demo_call_to and self.settings.calls_enabled and ev.urgency >= 2:
            contacts += [dict(id='demo_phone', name='Demo phone', channel='call', min_urgency=2, delay_seconds=0)]
        for c in contacts:
            if ev.urgency < c['min_urgency']:
                continue
            bucket = int((now-ev.opened_at)//(60/event_scale(ev))) if repeat and c['channel'] == 'push' else 0
            # Calls are once per event even if urgency rises. Push updates share event threads.
            stage = 'call' if c['channel'] == 'call' else str(ev.urgency)
            nid = f"{ev.id}:{c['id']}:{stage}:{bucket}"
            if s.get(Notification, nid):
                continue
            due = now if ev.urgency == 3 else max(now, ev.opened_at+c['delay_seconds']/event_scale(ev))
            s.add(Notification(id=nid, event_id=ev.id, recipient=c['name'], contact_id=c['id'], channel=c['channel'],
                               urgency=ev.urgency, due_at=due, state='pending', attempts=0))

    def acknowledge(self, event_id, author, note):
        with self.db.lock, self.db.sessions.begin() as s:
            ev = s.get(Event, event_id, with_for_update=True)
            if not ev:
                raise KeyError(event_id)
            if ev.status != 'acknowledged':
                ev.status, ev.acknowledged_at, ev.acknowledged_by = 'acknowledged', self.clock(), author
                ev.acknowledgement_note, ev.next_escalation_at = note, None
                for n in s.scalars(select(Notification).where(Notification.event_id == ev.id, Notification.state.in_(['pending', 'retry']))):
                    n.state = 'cancelled'
                s.add(Audit(id=str(uuid4()), event_id=ev.id, ts=self.clock(), kind='acknowledgement', body={'author': author, 'note': note}))
            return ev.id

    def event_out(self, s, ev):
        reviews = list(s.scalars(select(Audit).where(Audit.event_id == ev.id).order_by(Audit.ts.desc())))
        review = next((a.body.get('label') for a in reviews if a.kind == 'feedback'), None)
        decision = next((a.body.get('floor_decision') for a in reviews if a.body.get('floor_decision')), None)
        notifications = [NotificationOut(id=n.id, recipient=n.recipient, channel=n.channel, urgency=n.urgency, state=n.state,
                                        attempts=n.attempts, due_at=n.due_at, last_error=n.last_error)
                         for n in s.scalars(select(Notification).where(Notification.event_id == ev.id).order_by(Notification.due_at))]
        return EventOut(id=ev.id, person_id=ev.person_id, alias=ev.profile['alias'], mode=ev.profile['mode'],
                        profile_version=ev.profile['version'], source=ev.source, opened_at=ev.opened_at, observed_at=ev.observed_at,
                        updated_at=ev.updated_at, rule_hits=ev.rule_hits, floor_urgency=ev.floor_urgency, urgency=ev.urgency,
                        status=ev.status, summary=ev.summary, verdict_status=ev.verdict_status, verdict=ev.verdict,
                        features=ev.features, keyframes=ev.keyframes, acknowledged_at=ev.acknowledged_at,
                        acknowledged_by=ev.acknowledged_by, next_escalation_at=ev.next_escalation_at, notifications=notifications,
                        rule_summary=' '.join(LABELS[h] for h in ev.rule_hits if h in LABELS) or ev.summary,
                        human_review=review, floor_decision=decision,
                        baseline_contributions=json.loads(ev.features.get('baseline_contributions_json', '[]')))

    def events(self, person_id=None):
        with self.db.sessions() as s:
            q = select(Event).order_by(Event.opened_at.desc()).limit(100)
            if person_id:
                q = q.where(Event.person_id == person_id)
            return [self.event_out(s, ev) for ev in s.scalars(q)]

    def event(self, event_id):
        with self.db.sessions() as s:
            ev = s.get(Event, event_id)
            if ev is None:
                raise KeyError(event_id)
            return self.event_out(s, ev)

    def cleanup(self):
        now = self.clock()
        with self.db.lock, self.db.sessions.begin() as s:
            for p in s.scalars(select(Person)):
                cutoff = datetime.fromtimestamp(now-p.body['retain_days']*86400, timezone.utc)
                s.execute(delete(Window).where(Window.person_id == p.id, Window.ts < cutoff))
                s.execute(delete(Stream).where(Stream.person_id == p.id, Stream.updated_at < now-86400))
                for ev in s.scalars(select(Event).where(Event.person_id == p.id, Event.opened_at < cutoff.timestamp())):
                    ev.keyframes = []
