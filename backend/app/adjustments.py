"""Demo timing, privacy controls, baseline fitting, and evidence views."""
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev
from uuid import uuid4

from fastapi import Depends, HTTPException
from sqlalchemy import delete, func, select, text

from .baseline import FEATURES
from .db import Audit, Candidate, CoverageHour, Event, EventStage, Notification, Person, PersonalBaseline, ProfileVersion, PushSubscription, Stream, Window
from .gemini import event_package
from .schemas import Profile, StrictModel


class ConsentDecision(StrictModel):
    accepted: bool
    gemini: bool = False
    local_preview: bool = False


class ReviewedNormal(StrictModel):
    reviewed_normal: bool


class PurgeRequest(StrictModel):
    confirm_alias: str


def register_adjustment_routes(app, g, operator):
    db, cfg = g.db, g.settings
    auth = [Depends(operator)]

    def person_or_404(session, person_id):
        row = session.get(Person, person_id)
        if not row:
            raise HTTPException(404, 'Profile not found')
        return row

    @app.post('/api/privacy/{person_id}/consent', dependencies=auth)
    def consent(person_id: str, body: ConsentDecision):
        with db.lock, db.sessions() as session:
            row = person_or_404(session, person_id)
            profile = Profile.model_validate(row.body)
            saved = g.save_profile(profile.model_copy(update={'consent': body.accepted, 'gemini_consent': body.accepted and body.gemini}))
        with db.sessions.begin() as session:
            session.add(Audit(id=str(uuid4()), event_id=None, ts=g.clock(), kind='consent',
                              body={'person_id': person_id, **body.model_dump()}))
        return saved

    @app.get('/api/privacy/{person_id}', dependencies=auth)
    def privacy_counts(person_id: str):
        with db.sessions() as session:
            p = person_or_404(session, person_id)
            counts = {}
            for model, name in ((Window, 'windows'), (Candidate, 'candidates'), (EventStage, 'stages'), (CoverageHour, 'coverage'), (Event, 'events'), (Stream, 'streams'), (ProfileVersion, 'profile_versions')):
                counts[name] = session.scalar(select(func.count()).select_from(model).where(model.person_id == person_id))
            counts['keyframes'] = sum(len(e.keyframes) for e in session.scalars(select(Event).where(Event.person_id == person_id)))
            return {'counts': counts, 'consent': p.body.get('consent', False), 'gemini_consent': p.body.get('gemini_consent', False)}

    @app.post('/api/privacy/{person_id}/purge', dependencies=auth)
    def purge(person_id: str, body: PurgeRequest):
        with db.lock, db.sessions.begin() as session:
            p = person_or_404(session, person_id)
            if body.confirm_alias != p.body['alias']:
                raise HTTPException(409, 'Type the displayed alias exactly to confirm deletion')
            events = select(Event.id).where(Event.person_id == person_id)
            counts = {}
            counts['notifications'] = session.execute(delete(Notification).where(Notification.event_id.in_(events))).rowcount
            counts['audits'] = session.execute(delete(Audit).where((Audit.event_id.in_(events)) | (Audit.body['person_id'].as_string() == person_id))).rowcount
            for model, name in ((Window, 'windows'), (Candidate, 'candidates'), (EventStage, 'stages'), (CoverageHour, 'coverage'), (Stream, 'streams'), (Event, 'events'),
                                (ProfileVersion, 'profile_versions'), (PushSubscription, 'subscriptions'), (PersonalBaseline, 'baselines')):
                counts[name] = session.execute(delete(model).where(model.person_id == person_id)).rowcount
            # Keep only an empty revocation marker so startup cannot silently re-seed consent.
            p.version += 1
            p.body = Profile(id=person_id, version=p.version, mode=p.body['mode'], alias='Deleted profile',
                             consent=False, contacts=[], conditions=[], care_notes='').model_dump()
            g.calibrating.discard(person_id)
            g.actions.clear_person(person_id)
            g.forget_streams(person_id)
        aggregate_state = 'not applicable (local SQLite)'
        if db.engine.dialect.name == 'postgresql':
            try:
                with db.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
                    for name in ('hourly_movement', 'daily_baseline', 'hourly_incidents'):
                        if conn.execute(text('SELECT to_regclass(:name)'), {'name': name}).scalar():
                            conn.execute(text(f"CALL refresh_continuous_aggregate('{name}', NULL::timestamptz, NULL::timestamptz)"))
                aggregate_state = 'refreshed'
            except Exception:
                aggregate_state = 'refresh required; aggregate deletion is not yet verified'
        return {'deleted': counts, 'remaining': privacy_counts(person_id)['counts'], 'aggregates': aggregate_state,
                'retained': 'Empty profile identifier with consent revoked; external provider logs/backups are outside this purge.'}

    @app.get('/api/baseline/{person_id}', dependencies=auth)
    def baseline(person_id: str):
        with db.sessions() as session:
            person_or_404(session, person_id)
            b = session.get(PersonalBaseline, person_id)
            windows = session.scalar(select(func.count()).select_from(Window).where(Window.person_id == person_id, Window.calibration == True))
            return {'calibration_windows': windows, 'recording': person_id in g.calibrating,
                    'anchor': b.anchor if b else {}, 'current': b.current if b else {}, 'threshold': b.threshold if b else 3.5,
                    'trigger_enabled': b.trigger_enabled if b else False, 'evidence': b.evidence if b else {},
                    'features': list(FEATURES), 'mad_floors': FEATURES}

    @app.post('/api/baseline/{person_id}/fit', dependencies=auth)
    def fit(person_id: str, body: ReviewedNormal):
        if not body.reviewed_normal:
            raise HTTPException(422, 'Review the calibration recordings as normal before fitting')
        try:
            return g.baseline.calibrate(person_id, g.clock())
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.get('/api/trends/{person_id}', dependencies=auth)
    def trends(person_id: str):
        with db.sessions() as session:
            person_or_404(session, person_id)
            if db.engine.dialect.name == 'postgresql' and session.execute(text("SELECT to_regclass('daily_baseline')")).scalar():
                baseline_row = session.get(PersonalBaseline, person_id)
                # Recent complete buckets can lag the refresh policy. Aggregate
                # yesterday and today from raw rows; older days use Tiger's view.
                today = datetime.fromtimestamp(g.clock(), timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                raw_start = today-timedelta(days=1)
                older = session.execute(text('SELECT bucket, posture, synthetic, window_count, '
                                             'mean_sway, sd_sway, mean_hip_speed, NULL::double precision AS sd_hip_speed '
                                             'FROM daily_baseline WHERE person_id = :person AND bucket < :tail '
                                             'ORDER BY bucket'), {'person': person_id, 'tail': raw_start}).all()
                recent = session.execute(text("SELECT time_bucket(INTERVAL '1 day', ts) AS bucket, features->>'posture' AS posture, "
                                              "COALESCE((features->>'synthetic_trend')::boolean, false) AS synthetic, "
                                              "count(*) AS window_count, avg((features->>'sway')::double precision) AS mean_sway, "
                                              "stddev_pop((features->>'sway')::double precision) AS sd_sway, "
                                              "avg((features->>'hip_speed')::double precision) AS mean_hip_speed, "
                                              "stddev_pop((features->>'hip_speed')::double precision) AS sd_hip_speed "
                                              "FROM pose_windows WHERE person_id = :person AND ts >= :tail AND quality >= .8 "
                                              "AND (COALESCE((features->>'baseline_eligible')::boolean, false) "
                                              "OR COALESCE((features->>'synthetic_trend')::boolean, false)) "
                                              "GROUP BY bucket, posture, synthetic ORDER BY bucket"),
                                         {'person': person_id, 'tail': raw_start}).all()
                points = []
                for bucket, posture, synthetic, count, sway_mean, sway_sd, hip_mean, hip_sd in [*older, *recent]:
                    for feature, value, deviation in (('sway', sway_mean, sway_sd), ('hip_speed', hip_mean, hip_sd)):
                        if value is None:
                            continue
                        anchor = (.04 if synthetic and feature == 'sway' else .2 if synthetic else
                                  baseline_row.anchor.get(posture, {}).get(feature, {}).get('median') if baseline_row else None)
                        points.append({'day': bucket.date().isoformat(), 'posture': posture,
                                       'feature': feature, 'mean': round(value, 4),
                                       'stddev': round(deviation, 4) if deviation is not None else None,
                                       'anchor': anchor, 'gap': round(value-anchor, 4) if anchor is not None else None,
                                       'synthetic': synthetic, 'windows': count})
                return {'points': points, 'aggregation': 'Tiger daily aggregate plus recent raw buckets',
                        'database': 'PostgreSQL/Tiger Data',
                        'note': 'Older hip-speed standard deviation is unavailable in the existing daily aggregate.'}
            rows = list(session.scalars(select(Window).where(Window.person_id == person_id).order_by(Window.ts)))
            b = session.get(PersonalBaseline, person_id)
            groups = defaultdict(list)
            for row in rows:
                if row.quality >= .8 and (row.features.get('baseline_eligible') or row.features.get('synthetic_trend')):
                    groups[(row.ts.strftime('%Y-%m-%d'), row.features.get('posture', 'unknown'), bool(row.features.get('synthetic_trend')))].append(row.features)
            points = []
            for (day, posture, synthetic), values in groups.items():
                for feature in ('sway', 'hip_speed'):
                    numbers = [v[feature] for v in values if isinstance(v.get(feature), (float, int))]
                    if not numbers:
                        continue
                    anchor = .04 if synthetic and feature == 'sway' else .2 if synthetic else (b.anchor.get(posture, {}).get(feature, {}).get('median') if b else None)
                    points.append({'day': day, 'posture': posture, 'feature': feature, 'mean': round(mean(numbers), 4),
                                   'stddev': round(pstdev(numbers), 4), 'anchor': anchor,
                                   'gap': round(mean(numbers)-anchor, 4) if anchor is not None else None,
                                   'synthetic': synthetic, 'windows': len(numbers)})
        return {'points': points, 'aggregation': 'Daily mean and standard deviation; frozen calibration median as reference',
                'database': 'SQLite' if db.engine.dialect.name == 'sqlite' else 'PostgreSQL/Tiger Data',
                'note': 'Displayed values are calculated from retained rows. deploy/tiger.sql defines the matching continuous aggregate for Tiger.'}

    @app.post('/api/demo/trends/{person_id}', dependencies=auth)
    def synthetic_trends(person_id: str):
        if not cfg.demo_enabled:
            raise HTTPException(404, 'Synthetic examples disabled')
        midnight = datetime.fromtimestamp(g.clock(), timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with db.lock, db.sessions.begin() as session:
            person_or_404(session, person_id)
            for day in range(6):
                for i in range(30):
                    ts = midnight-timedelta(days=5-day)+timedelta(seconds=i*2)
                    session.merge(Window(ts=ts, stream_id=f'synthetic-trend:{person_id}', person_id=person_id, quality=1,
                        features={'posture': 'standing', 'sway': .04+day*.012+.002*math.sin(i), 'hip_speed': .2-day*.015,
                                  'synthetic_trend': True, 'baseline_eligible': False}, rule_hits=[], calibration=False, sequence=None))
        return {'synthetic': True, 'days': 6, 'windows': 180, 'included_in_personal_calibration': False}

    @app.get('/api/events/{event_id}/payload-preview', dependencies=auth)
    def payload_preview(event_id: str):
        with db.sessions() as session:
            ev = session.get(Event, event_id)
            if not ev:
                raise HTTPException(404, 'Event not found')
            package, _ = event_package(ev, [])
            return {'package': package, 'skeleton_keyframes': len(ev.keyframes),
                    'external_send_enabled': bool(cfg.gemini_api_key and cfg.gemini_billed_project_confirmed and ev.profile.get('gemini_consent')),
                    'note': 'Skeleton-rendered keyframes may go to Gemini only with per-profile consent and a confirmed billed project.'}

    @app.post('/api/demo/floor/{event_id}', dependencies=auth)
    def floor_example(event_id: str):
        if not cfg.demo_enabled:
            raise HTTPException(404, 'Synthetic examples disabled')
        with db.lock, db.sessions.begin() as session:
            ev = session.get(Event, event_id)
            if not ev or ev.floor_urgency < 1:
                raise HTTPException(409, 'Choose an event with a rule-based floor')
            decision = {'source': 'Simulated downgrade; not a Gemini response', 'proposed': 0,
                        'floor': ev.floor_urgency, 'applied': ev.urgency, 'blocked': True}
            session.add(Audit(id=str(uuid4()), event_id=ev.id, ts=g.clock(), kind='floor_demo', body={'floor_decision': decision}))
        return decision

    @app.get('/api/evidence', dependencies=auth)
    def evidence():
        report_path = Path(__file__).resolve().parents[2]/'docs'/'replay-results.json'
        report = json.loads(report_path.read_text()) if report_path.exists() else None
        with db.sessions() as session:
            windows = session.scalar(select(func.count()).select_from(Window).where(Window.calibration == False,
                ~Window.stream_id.like('synthetic-trend:%')))
            requests = session.scalar(select(func.count()).select_from(Audit).where(Audit.kind == 'gemini_verdict'))
            dismissals = session.scalar(select(func.count()).select_from(Audit).where(Audit.kind == 'feedback', Audit.body['label'].as_string() == 'false_alarm'))
        return {'report': report, 'scored_windows': windows, 'gemini_requests': requests,
                'requests_per_100_windows': round(100*requests/windows, 3) if windows else 0,
                'dismissals_logged': dismissals, 'metric_scope': 'Requests per scored window is a cost/exposure proxy, not accuracy.',
                'external_validation': 'Pending credentials and staged team clips; no prize eligibility is asserted.'}
