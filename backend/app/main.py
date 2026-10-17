import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import timezone
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from .config import Settings, settings
from .db import Audit, Database, Event, Person, ProfileVersion, PushSubscription, Stream, Window
from .gemini import GeminiWorker
from .notifications import NotificationWorker, queue_demo_call, valid_push_endpoint
from .replay import SCENARIOS, scenario_frames
from .schemas import Acknowledgement, EventOut, Feedback, IngestResult, Login, PoseFrame, Profile, PushRegistration
from .security import contact_token, session_token, valid_session
from .service import Guardian

log = logging.getLogger('guardian')


def create_app(cfg: Settings = settings, db=None, clock=time.time):
    cfg.validate_runtime()
    database = db or Database(cfg.database_url)
    guardian = Guardian(database, cfg, clock)
    notifications, gemini = NotificationWorker(guardian), GeminiWorker(guardian)
    worker_health = {'notifications': None, 'gemini': None}

    async def notify_loop():
        while True:
            try:
                await asyncio.to_thread(notifications.tick)
                worker_health['notifications'] = time.time()
            except Exception as exc:
                log.error('Notification worker failed: %s', type(exc).__name__)
            await asyncio.sleep(.25)

    async def verdict_loop():
        while True:
            try:
                await gemini.tick()
                worker_health['gemini'] = time.time()
            except Exception as exc:
                log.error('Verdict worker failed: %s', type(exc).__name__)
            await asyncio.sleep(.5)

    async def cleanup_loop():
        while True:
            await asyncio.sleep(3600)
            await asyncio.to_thread(guardian.cleanup)

    @asynccontextmanager
    async def lifespan(app):
        database.create()
        guardian.seed()
        jobs = [asyncio.create_task(f()) for f in (notify_loop, verdict_loop, cleanup_loop)] if cfg.worker_enabled else []
        yield
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    app = FastAPI(title='Silent Guardian API', version='0.1.0', lifespan=lifespan)
    app.state.guardian, app.state.notifications, app.state.gemini = guardian, notifications, gemini
    origins = set(cfg.allowed_origins.split(','))
    login_attempts = defaultdict(deque)

    @app.middleware('http')
    async def request_policy(request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('origin') and request.headers['origin'] not in origins:
            return JSONResponse({'detail': 'Origin not allowed'}, 403)
        if int(request.headers.get('content-length', '0')) > 256_000:
            return JSONResponse({'detail': 'Request too large'}, 413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Permissions-Policy'] = 'camera=(self), microphone=()'
        if request.url.path.startswith('/api'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    def operator(request: Request):
        bearer = request.headers.get('authorization', '').removeprefix('Bearer ')
        if hmac.compare_digest(bearer, cfg.operator_token) or valid_session(cfg.signing_secret, request.cookies.get('guardian_session')):
            return True
        raise HTTPException(401, 'Sign in to Silent Guardian')

    @app.get('/healthz')
    def health():
        with database.sessions() as s:
            s.execute(text('SELECT 1'))
        healthy = not cfg.worker_enabled or worker_health['notifications'] is None or time.time()-worker_health['notifications'] < 30
        return JSONResponse({'status': 'ok' if healthy else 'degraded', 'database': 'connected', 'notification_worker': worker_health['notifications']}, status_code=200 if healthy else 503)

    @app.get('/api/config')
    def config():
        return {'demo_enabled': cfg.demo_enabled, 'notification_mode': cfg.notification_mode,
                'gemini_configured': bool(cfg.gemini_api_key), 'push_public_key': cfg.vapid_public_key,
                'calls_configured': bool(cfg.calls_enabled and cfg.demo_call_to and cfg.twilio_account_sid and cfg.twilio_auth_token and cfg.twilio_from_number),
                'database': 'local SQLite' if cfg.database_url.startswith('sqlite') else 'PostgreSQL',
                'personalization': 'median_mad', 'gemini_billed_project_confirmed': cfg.gemini_billed_project_confirmed,
                'tiger_budget_limit': cfg.tiger_budget_limit}

    @app.post('/api/session')
    def login(body: Login, request: Request, response: Response):
        attempts = login_attempts[request.client.host if request.client else 'unknown']
        now = time.time()
        while attempts and now-attempts[0] > 60:
            attempts.popleft()
        if len(attempts) >= 10:
            raise HTTPException(429, 'Try again in one minute')
        attempts.append(now)
        if not hmac.compare_digest(body.token, cfg.operator_token):
            raise HTTPException(401, 'Invalid access token')
        response.set_cookie('guardian_session', session_token(cfg.signing_secret), httponly=True, secure=cfg.app_env == 'production', samesite='strict', max_age=43200)
        return {'authenticated': True}

    @app.delete('/api/session')
    def logout(response: Response):
        response.delete_cookie('guardian_session')
        return {'authenticated': False}

    @app.get('/api/profiles', response_model=list[Profile], dependencies=[Depends(operator)])
    def profiles():
        return guardian.profiles()

    @app.put('/api/profiles/{person_id}', response_model=Profile, dependencies=[Depends(operator)])
    def save_profile(person_id: str, profile: Profile):
        if person_id != profile.id:
            raise HTTPException(422, 'Profile id mismatch')
        try:
            return guardian.save_profile(profile)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.get('/api/profiles/{person_id}/versions', dependencies=[Depends(operator)])
    def versions(person_id: str):
        with database.sessions() as s:
            return [{'version': p.version, 'created_at': p.created_at, 'author': p.author, 'profile': p.body}
                    for p in s.scalars(select(ProfileVersion).where(ProfileVersion.person_id == person_id).order_by(ProfileVersion.version.desc()))]

    @app.get('/api/events', response_model=list[EventOut], dependencies=[Depends(operator)])
    def events(person_id: str | None = None):
        return guardian.events(person_id)

    @app.get('/api/events/{event_id}', response_model=EventOut, dependencies=[Depends(operator)])
    def event_detail(event_id: str):
        try:
            return guardian.event(event_id)
        except KeyError:
            raise HTTPException(404, 'Event not found')

    @app.post('/api/events/{event_id}/ack', response_model=EventOut, dependencies=[Depends(operator)])
    def ack(event_id: str, body: Acknowledgement):
        try:
            guardian.acknowledge(event_id, body.author, body.note)
            return guardian.event(event_id)
        except KeyError:
            raise HTTPException(404, 'Event not found')

    @app.post('/api/events/{event_id}/feedback', dependencies=[Depends(operator)])
    def feedback(event_id: str, body: Feedback):
        with database.lock, database.sessions.begin() as s:
            ev = s.get(Event, event_id)
            if not ev:
                raise HTTPException(404, 'Event not found')
            if body.label == 'false_alarm' and ev.status == 'open':
                ev.status, ev.next_escalation_at = 'dismissed', None
                from .db import Notification
                for n in s.scalars(select(Notification).where(Notification.event_id == ev.id, Notification.state.in_(['pending', 'retry']))):
                    n.state = 'cancelled'
                # Only the reviewed event interval can enter later baseline updates.
                for w in s.scalars(select(Window).where(Window.stream_id == ev.stream_id)):
                    if ev.observed_at-2 <= w.ts.replace(tzinfo=timezone.utc).timestamp() <= ev.observed_at+2:
                        w.features = {**w.features, 'baseline_eligible': True, 'reviewed_false_alarm': True}
            s.add(Audit(id=str(uuid4()), event_id=event_id, ts=clock(), kind='feedback', body=body.model_dump()))
        return {'saved': True}

    def contact_auth(event_id, contact_id, request):
        candidate = request.headers.get('authorization', '').removeprefix('Bearer ')
        expected = contact_token(cfg.signing_secret, event_id, contact_id)
        if not hmac.compare_digest(candidate, expected):
            raise HTTPException(401, 'Invalid contact link')
        with database.sessions() as s:
            ev = s.get(Event, event_id)
            if not ev or clock()-ev.opened_at > 7*86400:
                raise HTTPException(404, 'Contact link expired or event unavailable')
            contact = next((c for c in ev.profile['contacts'] if c['id'] == contact_id), None)
            if not contact:
                raise HTTPException(403, 'Contact is not assigned to this event')
            return ev, contact

    @app.get('/api/contact/{event_id}/{contact_id}')
    def contact_view(event_id: str, contact_id: str, request: Request):
        ev, contact = contact_auth(event_id, contact_id, request)
        return {'id': ev.id, 'alias': ev.profile['alias'], 'summary': ev.summary, 'urgency': ev.urgency,
                'status': ev.status, 'opened_at': ev.opened_at, 'contact_name': contact['name'],
                'source': ev.source, 'acknowledged_by': ev.acknowledged_by}

    @app.post('/api/contact/{event_id}/{contact_id}/ack')
    def contact_ack(event_id: str, contact_id: str, request: Request, body: Acknowledgement):
        ev, contact = contact_auth(event_id, contact_id, request)
        guardian.acknowledge(event_id, contact['name'], body.note)
        return {'status': 'acknowledged'}

    @app.get('/api/events/{event_id}/contact-link/{contact_id}', dependencies=[Depends(operator)])
    def contact_link(event_id: str, contact_id: str):
        with database.sessions() as s:
            ev = s.get(Event, event_id)
            if not ev or not any(c['id'] == contact_id for c in ev.profile['contacts']):
                raise HTTPException(404, 'Contact not found')
        token = contact_token(cfg.signing_secret, event_id, contact_id)
        return {'url': f'{cfg.public_url}/#contact/{event_id}/{contact_id}/{token}'}

    @app.post('/api/push', dependencies=[Depends(operator)])
    def register_push(body: PushRegistration):
        if not valid_push_endpoint(body.endpoint) or set(body.keys) != {'auth', 'p256dh'} or any(len(v)>500 for v in body.keys.values()):
            raise HTTPException(422, 'Invalid push subscription')
        with database.sessions.begin() as s:
            person = s.get(Person, body.person_id)
            if not person or not any(c['id'] == body.contact_id for c in person.body['contacts']):
                raise HTTPException(404, 'Contact not found')
            s.merge(PushSubscription(id=f'{body.person_id}:{body.contact_id}', person_id=body.person_id,
                                     contact_id=body.contact_id, body={'endpoint': body.endpoint, 'keys': body.keys}))
        return {'subscribed': True}

    @app.post('/api/frames', response_model=IngestResult, dependencies=[Depends(operator)])
    def frame_ingest(body: PoseFrame):
        try:
            return guardian.ingest(body)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.websocket('/ws/pose')
    async def pose_socket(ws: WebSocket):
        if ws.headers.get('origin') not in origins or not valid_session(cfg.signing_secret, ws.cookies.get('guardian_session')):
            await ws.close(code=4401)
            return
        await ws.accept()
        binding, count, tick = None, 0, time.monotonic()
        try:
            while True:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=30)
                count += 1
                if time.monotonic()-tick >= 1:
                    tick, count = time.monotonic(), 1
                if len(raw) > 64000 or count > 60:
                    await ws.close(code=4408)
                    return
                if not valid_session(cfg.signing_secret, ws.cookies.get('guardian_session')):
                    await ws.close(code=4401)
                    return
                try:
                    frame = PoseFrame.model_validate_json(raw)
                    ident = (frame.person_id, frame.camera_id, frame.session_id, frame.monitored_track_id, frame.source, frame.demo_time_scale)
                    if binding and ident != binding:
                        raise ValueError('Start a new connection to change monitored identity')
                    binding = ident
                    result = await asyncio.to_thread(guardian.ingest, frame)
                    await ws.send_json(result.model_dump())
                except ValueError:
                    await ws.send_json({'error': 'Invalid pose frame, profile, timestamp, or session binding'})
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass

    @app.get('/api/demo/scenarios', dependencies=[Depends(operator)])
    def scenarios():
        return SCENARIOS if cfg.demo_enabled else []

    @app.get('/api/demo/scenarios/{name}', response_model=list[PoseFrame], dependencies=[Depends(operator)])
    def replay(name: str, person_id: str = 'daniel', time_scale: float = 1):
        if not cfg.demo_enabled or name not in SCENARIOS:
            raise HTTPException(404, 'Scenario unavailable')
        if time_scale not in (1, 8):
            raise HTTPException(422, 'Choose normal time or 8x simulated time')
        frames = list(scenario_frames(name, person_id, duration=130 if name == 'long_lie' else 9 if name == 'sway' else 8))
        # 5 timestamped frames per simulated second keeps 8x below the 60 fps socket cap.
        return [f.model_copy(update={'demo_time_scale': time_scale}) for f in frames[::3 if time_scale > 1 else 1]]

    @app.post('/api/demo/call/{request_id}', dependencies=[Depends(operator)])
    def demo_call(request_id: UUID):
        if cfg.notification_mode == 'live' and not (cfg.calls_enabled and cfg.demo_call_to):
            raise HTTPException(409, 'Configure CALLS_ENABLED and the allowlisted DEMO_CALL_TO first')
        event_id = queue_demo_call(guardian, request_id)
        return {'event_id': event_id, 'mode': cfg.notification_mode}

    @app.get('/api/monitoring', dependencies=[Depends(operator)])
    def monitoring():
        with database.sessions() as s:
            return [{'person_id': x.person_id, 'last_seen': x.updated_at, 'quality': 'good' if x.state.get('last_good') == x.state.get('last_timestamp') else 'uncertain',
                     'source': 'replay' if 'synthetic-demo' in x.id else 'live', 'online': clock()-x.updated_at<3}
                    for x in s.scalars(select(Stream).order_by(Stream.updated_at.desc()).limit(20))]

    @app.post('/api/calibration/{person_id}/{action}', dependencies=[Depends(operator)])
    def calibration(person_id: str, action: str):
        if action not in ('start', 'stop') or person_id not in {p.id for p in guardian.profiles()}:
            raise HTTPException(422, 'Invalid calibration request')
        if action == 'start':
            guardian.calibrating.add(person_id)
        else:
            guardian.calibrating.discard(person_id)
        return {'recording': action == 'start', 'experimental': True}

    @app.get('/api/calibration/{person_id}/export', dependencies=[Depends(operator)])
    def export_calibration(person_id: str):
        with database.sessions() as s:
            rows = list(s.scalars(select(Window).where(Window.person_id == person_id, Window.calibration == True).order_by(Window.ts).limit(10000)))
            data = {'person_id': person_id, 'confirmed_normal_required': True,
                    'windows': [{'session': w.stream_id, 'timestamp': w.ts.isoformat(), 'posture': w.features.get('posture'), 'features': w.features} for w in rows]}
        return JSONResponse(data, headers={'Content-Disposition': 'attachment; filename="calibration.json"'})

    from .adjustments import register_adjustment_routes
    register_adjustment_routes(app, guardian, operator)
    static = Path(__file__).resolve().parents[2] / 'web' / 'dist'
    if static.exists():
        app.mount('/', StaticFiles(directory=static, html=True), name='web')
    return app


app = create_app()
