"""Bounded asynchronous shadow inference, independent of alert decisions.

At most one queued window per stream and 32 streams total. Overload replaces old
windows rather than building a latency backlog. No inference runs under DB locks.
"""
from collections import OrderedDict
from datetime import datetime, timezone
import json
import hashlib
import httpx
from threading import RLock
import time
from uuid import uuid4

from .action_model import ActionModel, MODEL_ID, PREPROCESSING, prepare_sequence
from .db import Candidate, Event, Person, Window
from .decision import POLICY_VERSION, decide
from .stages import append_stage
from .schemas import MLRequest


class ActionWorker:
    def __init__(self, guardian):
        self.g = guardian
        self.lock = RLock()
        self.pending, self.streams = OrderedDict(), OrderedDict()
        self.model = None
        self.state = 'disabled' if guardian.settings.action_model_mode == 'off' else 'not_loaded'
        self.error = None
        self.completed, self.replaced, self.failed = 0, 0, 0
        self.closed = False

    def load(self):
        if self.state == 'disabled' or self.closed:
            return
        if self.g.settings.ml_service_url:
            self.state, self.error = 'ready', None
            return
        self.state = 'loading'
        try:
            self.model = ActionModel(self.g.settings.action_model_path, self.g.settings.action_model_threads)
            self.state, self.error = 'ready', None
        except Exception as exc:
            self.state = 'unavailable'
            self.error = f'{type(exc).__name__}: run the model setup command and restart'

    def status(self):
        with self.lock:
            return dict(model=MODEL_ID, mode=self.g.settings.action_model_mode, state=self.state,
                        error=self.error, preprocessing=PREPROCESSING, queued=len(self.pending),
                        completed=self.completed, replaced=self.replaced, failed=self.failed,
                        alert_authority=self.g.settings.decision_mode == 'enforce', parameters=getattr(self.model, 'parameter_count', None))

    def latest(self, stream_id):
        with self.lock:
            record = self.streams.get(stream_id)
            if record and time.monotonic()-record['seen'] <= 120:
                result = record['result']
                if result:
                    return {**result, 'age_seconds': round(max(0, record['submitted']-result['observed_at']), 3)}
        return None

    def clear_person(self, person_id):
        with self.lock:
            for key in list(self.streams):
                if self.streams[key]['person_id'] == person_id:
                    self.streams.pop(key, None)
                    self.pending.pop(key, None)

    def close(self):
        with self.lock:
            self.closed = True
            self.streams.clear()
            self.pending.clear()

    def observe(self, stream_id, profile, samples, quality, event_ids, rule_hits, source):
        event_ids = [event_ids] if isinstance(event_ids, str) else list(event_ids or [])
        if self.state != 'ready' or self.closed:
            return dict(status=self.state, mode='shadow', model=MODEL_ID)
        try:
            if quality != 'good':
                raise ValueError('Uncertain tracking')
            prepare_sequence(samples)
        except ValueError:
            with self.lock:
                self.streams.pop(stream_id, None)
                self.pending.pop(stream_id, None)
            return dict(status='warming_up' if quality == 'good' else 'uncertain', mode='shadow', model=MODEL_ID)
        now, observed = time.monotonic(), samples[-1]['t']
        with self.lock:
            for key in list(self.streams):
                if now-self.streams[key]['seen'] > 120:
                    self.streams.pop(key)
                    self.pending.pop(key, None)
            previous = self.streams.get(stream_id)
            if previous is None or previous['version'] != profile.version:
                previous = dict(person_id=profile.id, version=profile.version, token=str(uuid4()),
                                result=None, submitted=-1, seen=now, last_events=[])
                self.streams[stream_id] = previous
            previous['seen'] = now
            self.streams.move_to_end(stream_id)
            while len(self.streams) > 32:
                key, _ = self.streams.popitem(last=False)
                self.pending.pop(key, None)
            # Detector emits ~3 scored windows/sec; cap inference at that cadence too.
            if observed-previous['submitted'] >= .30:
                pending = self.pending.get(stream_id)
                # Preserve a new event's first complete window even under 8x replay
                # overload; a later static pose must not erase its transition.
                if pending and pending['event_transition']:
                    pending['event_ids'] = list(dict.fromkeys(pending['event_ids'] + event_ids))
                    pending['rule_hits'] = list(dict.fromkeys(pending['rule_hits'] + list(rule_hits)))
                    previous['last_events'] = pending['event_ids']
                else:
                    if pending:
                        self.replaced += 1
                    transition = any(event_id not in previous['last_events'] for event_id in event_ids)
                    self.pending[stream_id] = dict(stream_id=stream_id, person_id=profile.id,
                        version=profile.version, token=previous['token'], samples=samples,
                        event_ids=event_ids, rule_hits=list(rule_hits), source=source,
                        event_transition=transition)
                    if transition:
                        previous['last_events'] = event_ids
                        self.pending.move_to_end(stream_id, last=False)
                previous['submitted'] = observed
            result = previous['result']
            if result and 0 <= observed-result['observed_at'] <= 2:
                return {**result, 'age_seconds': round(observed-result['observed_at'], 3)}
            return dict(status='pending', mode='shadow', model=MODEL_ID)

    def promote(self, session, event, person, result):
        """Send a rule alert to Gemini once a shadow ML score supports its rule."""
        if event.verdict_status != 'rules_only' or event.status != 'open':
            return
        candidate = session.get(Candidate, event.features.get('candidate_id'))
        policy = decide(event.rule_hits, event.floor_urgency, result, self.g.settings.fall_score_threshold)
        if not candidate or candidate.event_id != event.id or policy.action != 'supported':
            return
        cfg = self.g.settings
        review = bool(cfg.gemini_api_key and cfg.gemini_billed_project_confirmed)
        candidate.decision, candidate.reason, candidate.model_result = 'supported', policy.reason, result
        event.verdict_status = 'pending' if review else 'model_supported_no_gemini'
        append_stage(session, person_id=event.person_id, candidate_id=candidate.id, event_id=event.id,
                     stage='candidate_decision', outcome='supported', source=event.source,
                     when=self.g.clock(), urgency=event.urgency, policy_version=POLICY_VERSION,
                     details={'reason': policy.reason, 'mode': 'shadow', 'gemini': review})

    def tick(self):
        with self.lock:
            if not self.pending or (self.model is None and not self.g.settings.ml_service_url) or self.closed:
                return False
            _, job = self.pending.popitem(last=False)
        try:
            if self.g.settings.ml_service_url:
                request_id = str(uuid4())
                body = MLRequest(correlation_id=request_id, candidate_id=request_id,
                                 idempotency_key=hashlib.sha256(
                                     f"{job['stream_id']}:{job['samples'][-1]['t']:.6f}".encode()).hexdigest(),
                                 profile_version=job['version'], observed_at=job['samples'][-1]['t'],
                                 rule_hits=job['rule_hits'], quality='good', samples=job['samples'])
                response = httpx.post(self.g.settings.ml_service_url.rstrip('/') + '/internal/predict',
                                      json=body.model_dump(),
                                      headers={'x-internal-token': self.g.settings.internal_service_token},
                                      timeout=self.g.settings.model_timeout_seconds)
                response.raise_for_status()
                result = response.json()
            else:
                result = self.model.predict(job['samples'])
            result.update(rule_hits=job['rule_hits'], source=job['source'])
        except Exception:
            with self.lock:
                self.failed += 1
                record = self.streams.get(job['stream_id'])
                if record and record['token'] == job['token']:
                    record['result'] = dict(status='inference_failed', mode='shadow', model=MODEL_ID,
                                            observed_at=job['samples'][-1]['t'])
            return True
        # Same lock order as ingestion and consent/purge: DB, then worker.
        with self.g.db.lock, self.g.db.sessions.begin() as session, self.lock:
            record = self.streams.get(job['stream_id'])
            person = session.get(Person, job['person_id'])
            if (self.closed or not record or record['token'] != job['token'] or not person
                    or person.version != job['version'] or not person.body.get('consent')):
                return True
            ts = datetime.fromtimestamp(result['observed_at'], timezone.utc)
            window = session.get(Window, (ts, job['stream_id']))
            if window is None:
                return True
            # The model cannot create, escalate, or suppress an event. A score that
            # supports the event's rule does send it on to Gemini review.
            encoded = json.dumps(result)
            window.features = {**window.features, 'action_prediction_json': encoded}
            for event_id in job['event_ids']:
                event = session.get(Event, event_id)
                if event and event.stream_id == job['stream_id']:
                    previous = json.loads(event.features.get('action_prediction_json', 'null'))
                    if previous is None or result['fall_score'] > previous['fall_score']:
                        event.features = {**event.features, 'action_prediction_json': encoded}
                        append_stage(session, person_id=event.person_id,
                                     candidate_id=event.features.get('candidate_id'), event_id=event.id,
                                     stage='ml_result', outcome='shadow_scored', source=event.source,
                                     when=self.g.clock(), urgency=event.urgency,
                                     details={'model': result['model'], 'latency_ms': result['latency_ms'],
                                              'fall_score': round(result['fall_score'], 4)})
                    self.promote(session, event, person, result)
            if record['result'] is None or record['result']['observed_at'] <= result['observed_at']:
                record['result'] = result
            self.completed += 1
        return True
