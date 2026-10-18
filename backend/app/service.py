import time
import json
import threading
import httpx
import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, func, or_, select

from .db import Audit, Candidate, Event, EventStage, Notification, Person, ProfileVersion, Stream, Window
from .detector import evaluate
from .baseline import BaselineEngine
from .actions import ActionWorker
from .schemas import EventOut, IngestResult, NotificationOut, Profile
from .seed import demo_profiles
from .stages import append_stage
from .schemas import RulesRequest, RulesResponse
from .schemas import MLRequest
from .decision import decide, POLICY_VERSION


# Hits of one rule within this gap are one incident. A later hit, after the person
# has settled, is logged as a new alert with its own timestamp.
INCIDENT_GAP_SECONDS = 5
FLOOR_RULES = ('fall', 'prolonged_stillness')
# Contextual rules that keep firing for as long as the situation lasts; one log
# entry per capture session is enough.
ONCE_PER_SESSION = ('restricted_standing',)

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
        self.actions = ActionWorker(self)
        self._event_locks, self._event_locks_guard = {}, threading.Lock()
        # Live per-stream detector state stays in memory (one API worker). The streams
        # table is a restart checkpoint written at most ~3 times a second, not per frame.
        self.streams = {}

    def stream_state(self, s, stream_id, person_id):
        cached = self.streams.get(stream_id)
        if cached is None:
            row = s.get(Stream, stream_id)
            cached = self.streams[stream_id] = {'state': row.state if row else {}, 'person_id': person_id,
                                                'saved_ts': None, 'uncertain_ts': None}
        return cached

    def checkpoint_stream(self, s, stream_id, cached, frame_ts, now):
        if cached['saved_ts'] is not None and 0 <= frame_ts-cached['saved_ts'] < .3:
            return
        row = s.get(Stream, stream_id)
        if row is None:
            s.add(Stream(id=stream_id, person_id=cached['person_id'], state=cached['state'], updated_at=now))
        else:
            row.state, row.updated_at = cached['state'], now
        cached['saved_ts'] = frame_ts

    def forget_streams(self, person_id=None, older_than=None):
        with self.db.lock:
            for key in [k for k, c in self.streams.items() if (person_id and c['person_id'] == person_id)
                        or (older_than is not None and (c['saved_ts'] or 0) < older_than)]:
                del self.streams[key]

    def event_lock(self, event_id):
        """Serializes delivery with acknowledgement per event, so provider I/O never holds the DB lock."""
        with self._event_locks_guard:
            return self._event_locks.setdefault(event_id, threading.Lock())

    def seed(self):
        with self.db.lock, self.db.sessions.begin() as s:
            for p in demo_profiles():
                if not s.get(Person, p.id):
                    s.add(Person(id=p.id, version=1, body=p.model_dump()))
                    s.add(ProfileVersion(person_id=p.id, version=1, body=p.model_dump(), created_at=self.clock(), author='Synthetic seed'))

    def backfill_stages(self):
        """Make pre-migration incidents visible in the new time-series activity view."""
        with self.db.lock, self.db.sessions.begin() as s:
            opened_ids = select(EventStage.event_id).where(EventStage.stage == 'incident_opened')
            legacy = s.scalars(select(Event).where(Event.source != 'demo_call',
                            Event.status != 'pending_review', Event.verdict_status != 'privacy_blocked',
                            ~Event.id.in_(opened_ids)))
            for ev in legacy:
                append_stage(s, person_id=ev.person_id, candidate_id=ev.features.get('candidate_id'),
                             event_id=ev.id, stage='incident_opened', outcome='legacy_backfill',
                             source=ev.source, when=ev.opened_at, urgency=ev.urgency,
                             policy_version='legacy-rules')

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
            self.actions.clear_person(p.id)
            return Profile.model_validate(body)

    def evaluate_rules(self, frame, profile, state, stream_id):
        if not self.settings.rules_service_url:
            return evaluate(frame, profile, state)
        body = RulesRequest(correlation_id=stream_id, profile_version=profile.version,
                            frame=frame, profile=profile, state=state)
        try:
            response = httpx.post(self.settings.rules_service_url.rstrip('/') + '/internal/evaluate',
                                  json=body.model_dump(),
                                  headers={'x-internal-token': self.settings.internal_service_token},
                                  timeout=2)
            response.raise_for_status()
            scored = RulesResponse.model_validate(response.json())
            if scored.correlation_id != stream_id:
                raise ValueError('Rules response correlation mismatch')
            return scored.state, scored.result, scored.reason
        except httpx.HTTPError as exc:
            raise ValueError('Rules service unavailable') from exc

    def score_candidate(self, candidate, samples):
        if not self.settings.ml_service_url:
            # One-node deployment: gate with the locally loaded model.
            model = self.actions.model
            if model is None or candidate.quality != 'good':
                return None
            try:
                return {**model.predict(samples), 'mode': 'enforce'}
            except Exception:
                return None
        try:
            body = MLRequest(correlation_id=candidate.id, candidate_id=candidate.id,
                             idempotency_key=candidate.idempotency_key,
                             profile_version=candidate.profile_version,
                             observed_at=candidate.observed_at, rule_hits=candidate.rule_hits,
                             quality=candidate.quality, samples=samples)
            response = httpx.post(self.settings.ml_service_url.rstrip('/') + '/internal/predict',
                                  json=body.model_dump(),
                                  headers={'x-internal-token': self.settings.internal_service_token},
                                  timeout=self.settings.model_timeout_seconds)
            response.raise_for_status()
            result = response.json()
            if result.get('candidate_id') != candidate.id or result.get('profile_version') != candidate.profile_version:
                return None
            return {**result, 'mode': 'enforce'}
        except (httpx.HTTPError, ValueError):
            return None

    def ingest(self, frame):
        now = self.clock()
        if frame.source == 'live' and abs(frame.timestamp-now) > 15:
            raise ValueError('Camera clock differs from server by more than 15 seconds')
        if frame.source == 'replay' and not self.settings.demo_enabled:
            raise ValueError('Synthetic replay is disabled')
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
            cached = self.stream_state(s, stream_id, p.id)
            state, result, reason = self.evaluate_rules(frame, p, cached['state'], stream_id)
            cached['state'] = state
            if result is None:
                self.checkpoint_stream(s, stream_id, cached, frame.timestamp, now)
                return IngestResult(accepted=reason != 'duplicate_or_out_of_order', reason=reason, quality='buffering')
            if result['quality'] == 'uncertain':
                # Uncertain frames arrive at camera rate. Persist them at score cadence only.
                if cached['uncertain_ts'] is not None and 0 <= frame.timestamp-cached['uncertain_ts'] < 1/3-.001:
                    self.checkpoint_stream(s, stream_id, cached, frame.timestamp, now)
                    return IngestResult(accepted=True, reason=reason, quality='uncertain')
                cached['uncertain_ts'] = frame.timestamp
            features, hits = result['features'], result['rule_hits']
            samples = result['samples']
            window_ready = len(samples) >= 3 and samples[-1]['t']-samples[0]['t'] >= 1.8
            baseline = self.baseline.score(s, p.id, features) if window_ready else {
                'score': None, 'anchor_score': None, 'contributions': [], 'state': 'warming up or uncertain', 'flag': False}
            continuous = state.get('baseline_at') is not None and frame.timestamp-state['baseline_at'] <= .75
            state['baseline_count'] = (state.get('baseline_count', 0) if continuous else 0)+1 if baseline['flag'] and result['quality'] == 'good' else 0
            state['baseline_at'] = frame.timestamp
            cached['state'] = dict(state)
            self.checkpoint_stream(s, stream_id, cached, frame.timestamp, now)
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
            recent_events = list(s.scalars(select(Event).where(
                Event.stream_id == stream_id,
                or_(Event.status.in_(('open', 'pending_review')),
                    Event.updated_at >= now - 60/frame.demo_time_scale),
            ).order_by(Event.opened_at.desc())))
            latest_by_rule = {}
            for previous in recent_events:
                if previous.rule_hits:
                    latest_by_rule.setdefault(previous.rule_hits[0], previous)
            active_by_rule = {hit: event for hit, event in latest_by_rule.items()
                              if event.status in ('open', 'pending_review')}
            gate_prediction = None
            decision = None
            alerted = []
            incidents = state.setdefault('incident_by_rule', {})
            for hit in dict.fromkeys(hits):
                if hit == 'prolonged_stillness' and features.get('still_seconds', 0) >= 120:
                    floor = 3
                elif hit in ('fall', 'prolonged_stillness', 'restricted_standing', 'bed_exit'):
                    floor = 2
                else:
                    floor = 1
                if p.mode == 'care' and hit == 'stumble' and p.mobility_aid != 'none':
                    floor = max(floor, 2)
                # Group by observation time, so an upload groups the same at any speed.
                incident = incidents.get(hit)
                if incident is None or frame.timestamp-incident['last'] > INCIDENT_GAP_SECONDS:
                    incident = incidents[hit] = {'event': None, 'last': frame.timestamp}
                incident['last'] = frame.timestamp
                ev = s.get(Event, incident['event']) if incident['event'] else None
                candidate_cooldown = (self.settings.decision_mode == 'enforce' and
                                      frame.timestamp-state.get('last_candidate_at_by_rule', {}).get(hit, -1e12) < 2)
                already_logged = hit in ONCE_PER_SESSION and hit in state.get('logged_once', [])
                if ev is None and not candidate_cooldown and not already_logged:
                    candidate_id = str(uuid4())
                    idempotency_key = hashlib.sha256(
                        f'{stream_id}:{hit}:{frame.timestamp:.6f}:{p.version}'.encode()).hexdigest()
                    candidate = Candidate(id=candidate_id, idempotency_key=idempotency_key,
                                          person_id=p.id, stream_id=stream_id, profile_version=p.version,
                                          observed_at=frame.timestamp, created_at=now, source=frame.source,
                                          quality=result['quality'], rule_hits=[hit], features=dict(features),
                                          samples=list(samples), decision='shadow', reason='rules_shadow',
                                          model_result=None, event_id=None)
                    s.add(candidate)
                    candidate_stages = [append_stage(s, person_id=p.id, candidate_id=candidate_id, event_id=None,
                                 stage='candidate', outcome='rule_hit', source=frame.source, when=now,
                                  urgency=floor, details={'rule_hits': [hit]},
                                 policy_version=POLICY_VERSION if self.settings.decision_mode == 'enforce' else 'rules-shadow-v1')]
                    model_result = None
                    policy = None
                    if self.settings.decision_mode == 'enforce' and hit == 'restricted_standing':
                        candidate.decision, candidate.reason = 'rules_only', 'restricted_hours_rule'
                        candidate_stages.append(append_stage(s, person_id=p.id, candidate_id=candidate_id,
                                     event_id=None, stage='candidate_decision', outcome='rules_only',
                                     source=frame.source, when=now, urgency=floor,
                                     policy_version=POLICY_VERSION, details={'reason': candidate.reason}))
                    elif self.settings.decision_mode == 'enforce':
                        state.setdefault('last_candidate_at_by_rule', {})[hit] = frame.timestamp
                        cached['state'] = dict(state)
                        model_result = self.score_candidate(candidate, samples)
                        gate_prediction = model_result
                        policy = decide([hit], floor, model_result, self.settings.fall_score_threshold)
                        decision = policy.action
                        candidate.decision, candidate.reason, candidate.model_result = policy.action, policy.reason, model_result
                        candidate_stages.append(append_stage(s, person_id=p.id, candidate_id=candidate_id, event_id=None,
                                     stage='ml_result', outcome='scored' if model_result else 'unavailable',
                                     source=frame.source, when=now, urgency=floor,
                                     policy_version=POLICY_VERSION,
                                     details={'model': model_result.get('model') if model_result else None,
                                              'latency_ms': model_result.get('latency_ms') if model_result else None}))
                        candidate_stages.append(append_stage(s, person_id=p.id, candidate_id=candidate_id, event_id=None,
                                     stage='candidate_decision', outcome=policy.action,
                                     source=frame.source, when=now, urgency=floor,
                                     policy_version=POLICY_VERSION, details={'reason': policy.reason}))
                    keys = result['samples']
                    keyframes = [{'timestamp': x['t'], 'landmarks': x['landmarks']} for x in (keys[0], keys[len(keys)//2], keys[-1])] if keys else []
                    if policy is None or policy.action in ('supported', 'fallback_open'):
                        # The alert opens now with the ML-backed urgency. Gemini reviews every
                        # ML-supported or safety-rule alert afterwards and can only raise that
                        # urgency, never hold or lower it.
                        review = bool(policy and policy.action in ('supported', 'fallback_open')
                                      and self.settings.gemini_api_key and self.settings.gemini_billed_project_confirmed)
                        verdict_status = ('pending' if review
                                           else 'model_fallback' if policy and policy.action == 'fallback_open'
                                           else 'model_supported_no_gemini' if policy else 'rules_only')
                        ev = Event(id=str(uuid4()), person_id=p.id, stream_id=stream_id, profile=p.model_dump(),
                                   source=frame.source, opened_at=now, observed_at=frame.timestamp, updated_at=now,
                                   rule_hits=[hit], floor_urgency=floor, urgency=floor,
                                   status='open', summary=LABELS[hit],
                                   features={**features, 'candidate_id': candidate_id,
                                             **({'action_prediction_json': json.dumps(model_result)} if model_result else {})},
                                   keyframes=keyframes, verdict_status=verdict_status,
                                   next_escalation_at=now+(p.check_in_seconds if floor == 1 and p.mode == 'personal' and p.check_in_first else 180)/frame.demo_time_scale)
                        s.add(ev)
                        s.flush()
                        active_by_rule[hit] = ev
                        latest_by_rule[hit] = ev
                        candidate.event_id = ev.id
                        for stage_row in candidate_stages:
                            stage_row.event_id = ev.id
                        append_stage(s, person_id=p.id, candidate_id=candidate_id, event_id=ev.id,
                                     stage='incident_opened', outcome=policy.action if policy else 'rules_shadow',
                                     source=frame.source, when=now, urgency=floor,
                                     policy_version=policy.policy_version if policy else 'rules-shadow-v1')
                        self.queue(s, ev, now)
                        incident['event'] = ev.id
                        alerted.append(hit)
                        if hit in ONCE_PER_SESSION:
                            state.setdefault('logged_once', []).append(hit)
                        if hit in FLOOR_RULES:
                            state['floor_alert_id'] = ev.id
                elif ev is not None and ev.status in ('open', 'pending_review'):
                    old = ev.urgency
                    ev.floor_urgency = max(ev.floor_urgency, floor)
                    ev.urgency = max(ev.urgency, floor)
                    ev.features, ev.updated_at = event_features(ev.features, features), now
                    if ev.urgency > old:
                        self.queue(s, ev, now)
                    alerted.append(hit)
                    if hit in FLOOR_RULES:
                        state['floor_alert_id'] = ev.id
            # The live concern describes this moment only. Lying on the floor (outside
            # the bed) is never "fine": it is an alert after an alerted fall or stillness,
            # and a watch warning otherwise. Once up for 3 s the floor alert clears.
            concern = None
            if result['quality'] == 'good':
                down = features.get('posture') == 'lying' and not features.get('in_bed')
                if down:
                    state.pop('up_since', None)
                else:
                    state['up_since'] = state.get('up_since') or frame.timestamp
                    if frame.timestamp - state['up_since'] >= 3:
                        state.pop('floor_alert_id', None)
                concern = ('on_floor' if down and state.get('floor_alert_id') else
                           next(iter(alerted), None) or ('on_floor_watch' if down else None))
            cached['state'] = dict(state)
            if alerted:
                # Persist the incident link now (not at checkpoint cadence) so a restart
                # cannot log the same incident twice.
                row = s.get(Stream, stream_id)
                if row is not None:
                    row.state = cached['state']
            active_events = list(active_by_rule.values())
            if result['quality'] == 'good':
                # Keep collecting evidence only for incidents still in progress; a
                # logged alert from earlier in the session is left as it was.
                current = {incident['event'] for incident in incidents.values()
                           if incident['event'] and frame.timestamp-incident['last'] <= INCIDENT_GAP_SECONDS}
                current.add(state.get('floor_alert_id'))
                for ev in active_events:
                    if ev.id in current:
                        ev.features = event_features(ev.features, features)
            active_events.sort(key=lambda ev: (ev.urgency, ev.opened_at), reverse=True)
            prediction = (gate_prediction or {'status': 'not_requested', 'mode': 'enforce'}
                           if self.settings.decision_mode == 'enforce' else
                           self.actions.observe(stream_id, p, samples, result['quality'],
                                                [ev.id for ev in active_events if 'restricted_standing' not in ev.rule_hits],
                                                [hit for hit in hits if hit != 'restricted_standing'],
                                                'synthetic' if frame.camera_id == 'synthetic-demo' else frame.source))
            return IngestResult(accepted=True, reason=reason, quality=result['quality'], features=features,
                                rule_hits=hits, event_id=active_events[0].id if active_events else None,
                                event_ids=[ev.id for ev in active_events],
                                anomaly_score=baseline['score'], anchor_score=baseline['anchor_score'],
                                baseline_contributions=baseline['contributions'], baseline_state=baseline['state'],
                                action_prediction=prediction, concern=concern, candidate_decision=decision)

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
            append_stage(s, person_id=ev.person_id, candidate_id=ev.features.get('candidate_id'),
                         event_id=ev.id, stage='notification_queued', outcome=c['channel'],
                         source=ev.source, when=now, urgency=ev.urgency)

    def acknowledge(self, event_id, author, note):
        with self.event_lock(event_id), self.db.lock, self.db.sessions.begin() as s:
            ev = s.get(Event, event_id, with_for_update=True)
            if not ev:
                raise KeyError(event_id)
            if ev.status != 'acknowledged':
                ev.status, ev.acknowledged_at, ev.acknowledged_by = 'acknowledged', self.clock(), author
                ev.acknowledgement_note, ev.next_escalation_at = note, None
                for n in s.scalars(select(Notification).where(Notification.event_id == ev.id, Notification.state.in_(['pending', 'retry']))):
                    n.state = 'cancelled'
                s.add(Audit(id=str(uuid4()), event_id=ev.id, ts=self.clock(), kind='acknowledgement', body={'author': author, 'note': note}))
                append_stage(s, person_id=ev.person_id, candidate_id=ev.features.get('candidate_id'),
                             event_id=ev.id, stage='acknowledged', outcome='acknowledged',
                             source=ev.source, when=self.clock(), urgency=ev.urgency)
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
                        baseline_contributions=json.loads(ev.features.get('baseline_contributions_json', '[]')),
                        action_prediction=json.loads(ev.features.get('action_prediction_json', 'null')))

    def events(self, person_id=None):
        with self.db.sessions() as s:
            q = select(Event).where(Event.status != 'pending_review').order_by(Event.opened_at.desc()).limit(100)
            if person_id:
                q = q.where(Event.person_id == person_id)
            return [self.event_out(s, ev) for ev in s.scalars(q)]

    def event(self, event_id):
        with self.db.sessions() as s:
            ev = s.get(Event, event_id)
            if ev is None:
                raise KeyError(event_id)
            return self.event_out(s, ev)

    def capture_result(self, person_id, session_id):
        stream_id = f'{person_id}:browser-camera:{session_id}'
        with self.db.sessions() as s:
            if s.get(Person, person_id) is None:
                raise KeyError(person_id)
            windows = s.scalar(select(func.count()).select_from(Window).where(
                Window.stream_id == stream_id)) or 0
            events = list(s.scalars(select(Event).where(Event.stream_id == stream_id,
                                                       Event.source == 'upload')))
            # Lead with a movement event that reached ML/Gemini when a separate
            # contextual rule alert also exists in the same upload.
            events.sort(key=lambda event: (
                event.verdict_status in ('pending', 'validated', 'fallback', 'model_supported_no_gemini'),
                event.urgency, event.opened_at), reverse=True)
            candidate_filter = Candidate.event_id == events[0].id if events else Candidate.stream_id == stream_id
            candidate = s.scalar(select(Candidate).where(candidate_filter).order_by(Candidate.observed_at.desc()).limit(1))
            return {'session_id': session_id, 'scored_windows': windows,
                    'status': 'reviewing' if any(event.status == 'pending_review' or event.verdict_status == 'pending'
                                                 for event in events) else
                              'uncertain' if not events and candidate and candidate.decision == 'hold' else 'ready',
                    'candidate_decision': candidate.decision if candidate else None,
                    'decision_reason': candidate.reason if candidate else None,
                    'event': self.event_out(s, events[0]).model_dump() if events else None,
                    'events': [self.event_out(s, event).model_dump() for event in events]}

    def cleanup(self):
        now = self.clock()
        with self.db.lock, self.db.sessions.begin() as s:
            for p in s.scalars(select(Person)):
                cutoff = datetime.fromtimestamp(now-p.body['retain_days']*86400, timezone.utc)
                s.execute(delete(Window).where(Window.person_id == p.id, Window.ts < cutoff))
                s.execute(delete(Candidate).where(Candidate.person_id == p.id, Candidate.observed_at < cutoff.timestamp()))
                s.execute(delete(EventStage).where(EventStage.person_id == p.id, EventStage.ts < cutoff))
                s.execute(delete(Stream).where(Stream.person_id == p.id, Stream.updated_at < now-86400))
                for ev in s.scalars(select(Event).where(Event.person_id == p.id, Event.opened_at < cutoff.timestamp())):
                    ev.keyframes = []
            self.forget_streams(older_than=now-86400)
