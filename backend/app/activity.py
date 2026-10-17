"""Bounded activity queries backed by the incident stage log and pose windows."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, HTTPException
from sqlalchemy import func, select, text

from .db import CoverageHour, Event, EventStage, Person, Window


def register_activity_routes(app, guardian, operator):
    auth = [Depends(operator)]
    db = guardian.db

    def incident_rows(session, events):
        ids = [e.id for e in events]
        stage_rows = list(session.scalars(select(EventStage).where(EventStage.event_id.in_(ids)).order_by(EventStage.ts))) if ids else []
        stages = defaultdict(list)
        for row in stage_rows:
            stages[row.event_id].append({'ts': row.ts.replace(tzinfo=timezone.utc).timestamp(),
                                         'stage': row.stage, 'outcome': row.outcome,
                                         'urgency': row.urgency, 'details': row.details})
        return [{'id': e.id, 'observed_at': e.observed_at, 'opened_at': e.opened_at,
                 'summary': e.summary, 'rules': e.rule_hits, 'source': e.source,
                 'urgency': e.urgency, 'status': e.status,
                 'gemini_status': e.verdict_status, 'stages': stages[e.id]}
                for e in events]

    def profile_zone(session, person_id, requested):
        person = session.get(Person, person_id)
        if not person:
            raise HTTPException(404, 'Profile not found')
        name = requested or person.body.get('timezone', 'America/Toronto')
        try:
            return name, ZoneInfo(name)
        except ZoneInfoNotFoundError:
            raise HTTPException(422, 'Invalid IANA time zone')

    @app.get('/api/activity/{person_id}/timeline', dependencies=auth)
    def timeline(person_id: str, from_ts: float, to_ts: float, limit: int = 50, offset: int = 0):
        if not 0 < to_ts-from_ts <= 31*86400 or not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, 'Choose a range of at most 31 days, limit 1-100, and nonnegative offset')
        with db.sessions() as s:
            profile_zone(s, person_id, None)
            predicate = (Event.person_id == person_id, Event.opened_at >= from_ts,
                         Event.opened_at < to_ts, Event.source != 'demo_call',
                         Event.id.in_(select(EventStage.event_id).where(EventStage.stage == 'incident_opened')))
            total = s.scalar(select(func.count()).select_from(Event).where(*predicate)) or 0
            events = list(s.scalars(select(Event).where(*predicate).order_by(Event.opened_at.desc()).offset(offset).limit(limit)))
            return {'total': total, 'limit': limit, 'offset': offset,
                    'incidents': incident_rows(s, events)}

    @app.get('/api/activity/{person_id}/cell', dependencies=auth)
    def cell(person_id: str, day: str, hour: int, tz: str | None = None,
             limit: int = 100, offset: int = 0):
        if not 0 <= hour < 24 or not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, 'Invalid hour or pagination')
        try:
            local_day = datetime.strptime(day, '%Y-%m-%d').date()
        except ValueError:
            raise HTTPException(422, 'Day must use YYYY-MM-DD')
        with db.sessions() as s:
            _, zone = profile_zone(s, person_id, tz)
            start = datetime.combine(local_day, datetime.min.time(), zone).astimezone(timezone.utc)
            end = datetime.combine(local_day+timedelta(days=1), datetime.min.time(), zone).astimezone(timezone.utc)
            openings = s.execute(select(EventStage.event_id, EventStage.ts).where(
                EventStage.person_id == person_id, EventStage.stage == 'incident_opened',
                EventStage.source != 'demo_call', EventStage.ts >= start,
                EventStage.ts < end).order_by(EventStage.ts.desc()))
            ids = [event_id for event_id, stamp in openings
                   if stamp.replace(tzinfo=timezone.utc).astimezone(zone).hour == hour]
            selected = ids[offset:offset+limit]
            found = {e.id: e for e in s.scalars(select(Event).where(Event.id.in_(selected)))}
            return {'total': len(ids), 'limit': limit, 'offset': offset,
                    'incidents': incident_rows(s, [found[event_id] for event_id in selected if event_id in found])}

    @app.get('/api/activity/{person_id}/heatmap', dependencies=auth)
    def heatmap(person_id: str, tz: str | None = None, days: int = 7):
        if days != 7:
            raise HTTPException(422, 'The activity grid shows seven days')
        with db.sessions() as s:
            zone_name, zone = profile_zone(s, person_id, tz)
            today = datetime.fromtimestamp(guardian.clock(), zone).date()
            first = today-timedelta(days=6)
            start = datetime.combine(first, datetime.min.time(), zone).astimezone(timezone.utc)
            end = datetime.combine(today+timedelta(days=1), datetime.min.time(), zone).astimezone(timezone.utc)
            counts = defaultdict(int)
            raw_start = start
            if (db.engine.dialect.name == 'postgresql' and
                    s.execute(text("SELECT to_regclass('hourly_incidents')")).scalar()):
                # The policy excludes the newest hour. Query the last two hours raw
                # to include an unrefreshed completed bucket and the current bucket.
                current_hour = datetime.fromtimestamp(guardian.clock(), timezone.utc).replace(minute=0, second=0, microsecond=0)
                raw_start = max(start, current_hour-timedelta(hours=1))
                completed = s.execute(text('SELECT bucket, incident_count FROM hourly_incidents '
                                           'WHERE person_id = :person AND bucket >= :start AND bucket < :tail'),
                                      {'person': person_id, 'start': start, 'tail': raw_start})
                for bucket_ts, number in completed:
                    local = bucket_ts.replace(tzinfo=timezone.utc).astimezone(zone)
                    counts[(local.date().isoformat(), local.hour)] += number
            # Incident openings are separate from pose-window coverage. The raw tail
            # includes the current partial bucket without waiting for a refresh.
            opened = s.scalars(select(EventStage.ts).where(
                EventStage.person_id == person_id, EventStage.stage == 'incident_opened',
                EventStage.source != 'demo_call', EventStage.ts >= raw_start, EventStage.ts < end))
            for ts in opened:
                local = ts.replace(tzinfo=timezone.utc).astimezone(zone)
                counts[(local.date().isoformat(), local.hour)] += 1
            # SQL grouping bounds the coverage result to <= 169 UTC buckets. A local
            # hour may appear twice on the fall DST transition; combine those buckets.
            bucket = (func.date_trunc('hour', Window.ts) if db.engine.dialect.name == 'postgresql'
                      else func.strftime('%Y-%m-%dT%H:00:00', Window.ts))
            coverage_rows = s.execute(select(bucket, func.count()).where(
                Window.person_id == person_id, Window.ts >= start, Window.ts < end,
                Window.quality >= .8).group_by(bucket))
            # Hours already purged by the daily baseline update keep a coverage summary.
            kept_rows = s.execute(select(CoverageHour.bucket, CoverageHour.windows).where(
                CoverageHour.person_id == person_id, CoverageHour.bucket >= start, CoverageHour.bucket < end))
            coverage = defaultdict(int)
            for stamp, number in [*coverage_rows, *kept_rows]:
                utc = (datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp)
                local = utc.replace(tzinfo=timezone.utc).astimezone(zone)
                coverage[(local.date().isoformat(), local.hour)] += number
            rows = []
            for day_index in range(7):
                day = (first+timedelta(days=day_index)).isoformat()
                rows.append({'day': day, 'hours': [
                    {'hour': hour, 'incidents': counts[(day, hour)],
                     'covered': coverage[(day, hour)] > 0}
                    for hour in range(24)]})
            return {'timezone': zone_name, 'meaning': 'incident openings', 'days': rows}
