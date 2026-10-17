import json
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from sqlalchemy import select

from .db import Audit, Event, Notification, PushSubscription
from .security import contact_token
from .service import event_scale
from .stages import append_stage


def valid_push_endpoint(endpoint):
    url = urlparse(endpoint)
    host = url.hostname or ''
    return url.scheme == 'https' and not url.username and url.port in (None, 443) and (
        host == 'fcm.googleapis.com' or host == 'updates.push.services.mozilla.com' or
        host.endswith('.notify.windows.com') or host.endswith('.push.apple.com'))


class Delivery:
    def __init__(self, settings):
        self.settings = settings

    def call(self, destination):
        cfg = self.settings
        allowed = {x.strip() for x in cfg.call_test_allowlist.split(',') if x.strip()}
        if not cfg.calls_enabled or destination not in allowed:
            raise ValueError('Call destination is not enabled and allowlisted')
        if not all((cfg.twilio_account_sid, cfg.twilio_auth_token, cfg.twilio_from_number)):
            raise ValueError('Twilio Voice credentials and caller number are required')
        # Inline TwiML avoids a public webhook; no speech, recording, or media.
        with httpx.Client(timeout=10) as client:
            res = client.post(f'https://api.twilio.com/2010-04-01/Accounts/{cfg.twilio_account_sid}/Calls.json',
                              auth=(cfg.twilio_account_sid, cfg.twilio_auth_token),
                              data={'To': destination, 'From': cfg.twilio_from_number,
                                    'Twiml': '<Response><Hangup/></Response>', 'Timeout': '15'})
            res.raise_for_status()
            return res.json()['sid']

    def push(self, subscription, payload):
        from pywebpush import webpush
        if not valid_push_endpoint(subscription['endpoint']):
            raise ValueError('Unsupported push service')
        if not self.settings.vapid_private_key:
            raise ValueError('Web push is not configured')
        webpush(subscription_info=subscription, data=json.dumps(payload),
                vapid_private_key=self.settings.vapid_private_key,
                vapid_claims={'sub': self.settings.vapid_subject}, timeout=10)


class NotificationWorker:
    def __init__(self, guardian, delivery=None):
        self.g = guardian
        self.delivery = delivery or Delivery(guardian.settings)

    def tick(self):
        g, now = self.g, self.g.clock()
        with g.db.lock, g.db.sessions.begin() as s:
            for ev in s.scalars(select(Event).where(Event.status == 'open').with_for_update()):
                if ev.next_escalation_at is not None and now >= ev.next_escalation_at:
                    if ev.urgency == 1:
                        ev.urgency = 2
                        ev.summary = 'Check-in was not acknowledged. Please check on the person.'
                    g.queue(s, ev, now, repeat=True)
                    ev.next_escalation_at = now+60/event_scale(ev)
        with g.db.sessions() as s:
            due = list(s.scalars(select(Notification.id).where(Notification.state.in_(['pending', 'retry']), Notification.due_at <= now).limit(20)))
        for nid in due:
            self.send(nid)

    def send(self, nid):
        g, now = self.g, self.g.clock()
        with g.db.sessions() as s:
            n = s.get(Notification, nid)
            if n is None:
                return
            event_id = n.event_id
        # Serialize ack and send per event. A send already in flight finishes before ack
        # commits; no new send starts after acknowledgement. Provider delivery is
        # at-least-once. Only the event lock is held during provider I/O, so frame
        # ingest and other events never wait on a slow push or call.
        with g.event_lock(event_id):
            job = self.claim(nid, now)
            if job is None:
                return
            result, error = None, None
            try:
                result = self.delivery.call(job[1]) if job[0] == 'call' else self.delivery.push(job[1], job[2])
            except Exception as exc:
                error = exc
            self.record(nid, job[0], result, error, now)

    def claim(self, nid, now):
        """Validates and reserves a notification under the DB lock; returns the provider job, if any."""
        g = self.g
        with g.db.lock, g.db.sessions.begin() as s:
            n = s.get(Notification, nid, with_for_update=True)
            if n is None or n.state not in ('pending', 'retry'):
                return None
            ev = s.get(Event, n.event_id, with_for_update=True)
            if ev is None or ev.status != 'open':
                n.state = 'cancelled'
                return None
            n.attempts += 1
            if g.settings.notification_mode == 'dry_run':
                n.state, n.sent_at = 'simulated', now
                append_stage(s, person_id=ev.person_id, candidate_id=ev.features.get('candidate_id'),
                             event_id=ev.id, stage='notification_result', outcome='simulated',
                             source=ev.source, when=now, urgency=ev.urgency,
                             details={'channel': n.channel})
                return None
            if n.channel == 'call':
                day_start = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                calls = list(s.scalars(select(Notification).where(Notification.channel == 'call', Notification.sent_at >= day_start, Notification.state.in_(['provider_accepted', 'unknown']))))
                if len(calls) >= g.settings.call_daily_limit or any(now-c.sent_at < g.settings.call_cooldown_seconds for c in calls):
                    n.state, n.last_error = 'suppressed', 'Demo call cooldown or daily limit reached'
                    return None
                target = g.settings.demo_call_to if n.contact_id == 'demo_phone' else next((c.get('phone') for c in ev.profile['contacts'] if c['id'] == n.contact_id), None)
                if target is None:
                    n.state, n.last_error = 'configuration_required', 'No phone number for this contact'
                    return None
                # Commit a reservation before making a non-idempotent call request.
                # If the process crashes, do not call again automatically.
                n.state, n.sent_at = 'unknown', now
                return ('call', target)
            sub = s.scalar(select(PushSubscription).where(PushSubscription.person_id == ev.person_id, PushSubscription.contact_id == n.contact_id))
            if not sub:
                n.last_error, n.state = 'No browser subscribed for this contact', 'retry'
                n.due_at = now+min(300, 15*2**min(n.attempts, 4))
                return None
            token = contact_token(g.settings.signing_secret, ev.id, n.contact_id)
            payload = {'title': f'Silent Guardian · Level {ev.urgency}', 'event_id': ev.id,
                       'body': f"{ev.profile['alias']}: a movement event needs a check.",
                       'url': f'{g.settings.public_url}/#contact/{ev.id}/{n.contact_id}/{token}'}
            return ('push', sub.body, payload)

    def record(self, nid, channel, result, error, now):
        g = self.g
        with g.db.lock, g.db.sessions.begin() as s:
            n = s.get(Notification, nid, with_for_update=True)
            if n is None:
                return  # Purged while the provider request was in flight.
            ev = s.get(Event, n.event_id)
            # Error classes only: provider exceptions can contain phone numbers or secrets.
            if channel == 'call':
                if error is None:
                    n.state, n.provider_id = 'provider_accepted', result
                elif isinstance(error, ValueError):
                    n.state, n.sent_at, n.last_error = 'configuration_required', None, str(error)
                else:
                    n.state, n.last_error = 'unknown', f'{type(error).__name__}: inspect provider call log before retrying'
                if ev:
                    append_stage(s, person_id=ev.person_id, candidate_id=ev.features.get('candidate_id'),
                                 event_id=ev.id, stage='notification_result', outcome=n.state,
                                 source=ev.source, when=now, urgency=ev.urgency,
                                 details={'channel': channel})
                return
            if error is None:
                n.state, n.sent_at = 'provider_accepted', now
                if ev:
                    append_stage(s, person_id=ev.person_id, candidate_id=ev.features.get('candidate_id'),
                                 event_id=ev.id, stage='notification_result', outcome=n.state,
                                 source=ev.source, when=now, urgency=ev.urgency,
                                 details={'channel': channel})
                return
            n.last_error = str(error) if isinstance(error, ValueError) else type(error).__name__
            n.state = 'retry'
            n.due_at = now+min(300, 15*2**min(n.attempts, 4))
            if ev:
                append_stage(s, person_id=ev.person_id, candidate_id=ev.features.get('candidate_id'),
                             event_id=ev.id, stage='notification_result', outcome='retry',
                             source=ev.source, when=now, urgency=ev.urgency,
                             details={'channel': channel})


def queue_demo_call(guardian, request_id):
    """Idempotent manual call button; same request id never generates another call."""
    g, now = guardian, guardian.clock()
    eid = str(request_id)
    with g.db.lock, g.db.sessions.begin() as s:
        if s.get(Event, eid):
            return eid
        profile = next(p for p in g.profiles() if p.id == 'daniel').model_dump()
        ev = Event(id=eid, person_id='daniel', stream_id='manual-call', profile=profile, source='demo_call', opened_at=now,
                   observed_at=now, updated_at=now, rule_hits=[], floor_urgency=0, urgency=2, status='open',
                   summary='Manually requested ring-only demo call.', features={}, keyframes=[], verdict_status='not_requested', next_escalation_at=None)
        s.add(ev)
        s.add(Notification(id=eid+':demo_phone', event_id=eid, recipient='Demo phone', contact_id='demo_phone',
                           channel='call', urgency=2, due_at=now, state='pending', attempts=0))
        s.add(Audit(id=str(uuid4()), event_id=eid, ts=now, kind='manual_call_requested', body={}))
    return eid
