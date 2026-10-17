import json
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from sqlalchemy import select

from .db import Audit, Event, Notification, PushSubscription
from .security import contact_token
from .service import event_scale


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
        # Serialize ack and send. A send already in flight finishes before ack commits;
        # no new send starts after acknowledgement. Provider delivery is at-least-once.
        with g.db.lock, g.db.sessions.begin() as s:
            n = s.get(Notification, nid, with_for_update=True)
            ev = s.get(Event, n.event_id, with_for_update=True)
            if n.state not in ('pending', 'retry'):
                return
            if ev.status != 'open':
                n.state = 'cancelled'
                return
            n.attempts += 1
            if g.settings.notification_mode == 'dry_run':
                n.state, n.sent_at = 'simulated', now
                return
            try:
                if n.channel == 'call':
                    day_start = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                    calls = list(s.scalars(select(Notification).where(Notification.channel == 'call', Notification.sent_at >= day_start, Notification.state.in_(['provider_accepted', 'unknown']))))
                    if len(calls) >= g.settings.call_daily_limit or any(now-c.sent_at < g.settings.call_cooldown_seconds for c in calls):
                        n.state, n.last_error = 'suppressed', 'Demo call cooldown or daily limit reached'
                        return
                    target = g.settings.demo_call_to if n.contact_id == 'demo_phone' else next(c['phone'] for c in ev.profile['contacts'] if c['id'] == n.contact_id)
                    # Commit a reservation before making a non-idempotent call request.
                    # If the process crashes, do not call again automatically.
                    n.state, n.sent_at = 'unknown', now
                    s.flush()
                    s.commit()
                    sid = self.delivery.call(target)
                    with g.db.sessions.begin() as saved:
                        row = saved.get(Notification, nid)
                        row.state, row.provider_id = 'provider_accepted', sid
                    return
                sub = s.scalar(select(PushSubscription).where(PushSubscription.person_id == ev.person_id, PushSubscription.contact_id == n.contact_id))
                if not sub:
                    raise ValueError('No browser subscribed for this contact')
                token = contact_token(g.settings.signing_secret, ev.id, n.contact_id)
                payload = {'title': f'Silent Guardian · Level {ev.urgency}', 'event_id': ev.id,
                           'body': f"{ev.profile['alias']}: a movement event needs a check.",
                           'url': f'{g.settings.public_url}/#contact/{ev.id}/{n.contact_id}/{token}'}
                self.delivery.push(sub.body, payload)
                n.state, n.sent_at = 'provider_accepted', now
            except Exception as exc:
                # Error classes only: provider exceptions can contain phone numbers or secrets.
                if n.channel == 'call':
                    with g.db.sessions.begin() as saved:
                        row = saved.get(Notification, nid)
                        row.last_error = f'{type(exc).__name__}: inspect provider call log before retrying'
                        row.state = 'configuration_required' if isinstance(exc, ValueError) else 'unknown'
                        if isinstance(exc, ValueError):
                            row.sent_at = None
                            row.last_error = str(exc)
                    return
                n.last_error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                n.state = 'retry'
                n.due_at = now+min(300, 15*2**min(n.attempts, 4))


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
