"""Explainable, posture-specific baselines. No neural model or clinical claim."""
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from .db import CoverageHour, PersonalBaseline, Window

# Six directly observed features. Speeds are torso-normalized proxies, not gait diagnoses.
# Floors regularize zero/near-zero MAD at the sensor's practical resolution.
FEATURES = {'sway': .01, 'hip_speed': .03, 'trunk_angle': 2.,
            'motion_energy': .04, 'wrist_speed': .08, 'ankle_change': .08}


def independent_records(records):
    last, kept = {}, []
    for r in sorted(records, key=lambda x: x['timestamp']):
        stream = r['session']
        if r['timestamp'] - last.get(stream, -1e20) < 2:
            continue
        last[stream] = r['timestamp']
        kept.append(r)
    return kept


def fit_statistics(records, minimum=30):
    groups = defaultdict(list)
    for row in independent_records(records):
        if row['features'].get('posture') in ('standing', 'sitting', 'lying'):
            groups[row['features']['posture']].append(row)
    result = {}
    for posture, rows in groups.items():
        if len(rows) < minimum:
            continue
        stats = {}
        for name, floor in FEATURES.items():
            values = [float(r['features'][name]) for r in rows if isinstance(r['features'].get(name), (int, float))]
            if len(values) < minimum:
                continue
            center = median(values)
            mad = median(abs(x-center) for x in values)
            stats[name] = {'median': center, 'mad': mad, 'scale': max(mad, floor), 'count': len(values)}
        if len(stats) == len(FEATURES):
            result[posture] = stats
    return result


def score_statistics(stats, features):
    posture = features.get('posture')
    if posture not in stats:
        return None, []
    contributions = []
    for name, row in stats[posture].items():
        value = features.get(name)
        if not isinstance(value, (int, float)):
            return None, []
        z = .6745 * (value-row['median']) / row['scale']
        contributions.append({'feature': name, 'value': value, 'median': round(row['median'], 4),
                              'mad': round(row['mad'], 4), 'scale': round(row['scale'], 4),
                              'z': round(z, 3), 'ratio': round(value / row['median'], 2) if abs(row['median']) > 1e-6 else None})
    contributions.sort(key=lambda r: abs(r['z']), reverse=True)
    return max((abs(r['z']) for r in contributions), default=0), contributions


def blend_statistics(previous, daily, previous_weight=.8):
    """Blend one completed day into the current baseline without moving the anchor."""
    result = {posture: {name: dict(values) for name, values in features.items()}
              for posture, features in previous.items()}
    daily_weight = 1-previous_weight
    for posture, features in daily.items():
        if posture not in result:
            result[posture] = {name: dict(values) for name, values in features.items()}
            continue
        for name, new in features.items():
            old = result[posture].get(name)
            if old is None:
                result[posture][name] = dict(new)
                continue
            blended_median = previous_weight*old['median'] + daily_weight*new['median']
            blended_mad = previous_weight*old['mad'] + daily_weight*new['mad']
            result[posture][name] = {
                'median': blended_median,
                'mad': blended_mad,
                'scale': max(blended_mad, FEATURES[name]),
                'count': new['count'],
            }
    return result


def completed_day_cutoff(now, timezone_name):
    local = datetime.fromtimestamp(now, ZoneInfo(timezone_name))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def window_records(rows):
    return [{'session': r.stream_id, 'timestamp': r.ts.replace(tzinfo=timezone.utc).timestamp(), 'features': r.features}
            for r in rows if r.quality >= .8 and not r.features.get('synthetic_trend') and 'synthetic-demo' not in r.stream_id]


class BaselineEngine:
    def __init__(self, db):
        self.db = db

    def score(self, session, person_id, features):
        baseline = session.get(PersonalBaseline, person_id)
        if not baseline:
            return {'score': None, 'anchor_score': None, 'contributions': [], 'state': 'uncalibrated', 'flag': False}
        score, contributions = score_statistics(baseline.current, features)
        anchor, _ = score_statistics(baseline.anchor, features)
        return {'score': score, 'anchor_score': anchor, 'contributions': contributions,
                'state': 'unseen posture' if score is None else 'validated trigger' if baseline.trigger_enabled else 'score only · needs held-out evaluation',
                'flag': bool(baseline.trigger_enabled and score is not None and score > baseline.threshold),
                'threshold': baseline.threshold}

    def calibrate(self, person_id, now):
        with self.db.lock, self.db.sessions.begin() as session:
            rows = list(session.scalars(select(Window).where(Window.person_id == person_id, Window.calibration == True).order_by(Window.ts)))
            records = window_records(rows)
            stats = fit_statistics(records)
            if not stats or len({r['session'] for r in records}) < 2:
                raise ValueError('Need two reviewed normal sessions and at least 30 non-overlapping two-second windows per supported posture.')
            existing = session.get(PersonalBaseline, person_id)
            if existing:
                existing.current, existing.updated_at = stats, now
                existing.trigger_enabled = False
                existing.evidence = {**existing.evidence, 'current_recalibrated': now, 'validation': None,
                                     'current_sessions': sorted({r['session'] for r in records})}
            else:
                session.add(PersonalBaseline(person_id=person_id, anchor=stats, current=stats,
                    threshold=3.5, trigger_enabled=False, updated_at=now,
                    evidence={'anchor_created': now, 'reviewed_normal': True, 'validation': None,
                              'anchor_sessions': sorted({r['session'] for r in records}),
                              'current_sessions': sorted({r['session'] for r in records})}))
            # The fitted statistics are enough for operation. Do not keep the raw
            # calibration movement windows after they have been consumed.
            session.execute(delete(Window).where(Window.person_id == person_id, Window.calibration == True))
        return {'postures': list(stats), 'trigger_enabled': False, 'anchor_frozen': True}

    def daily_update(self, now, previous_weight=.8, timezone_name='America/Toronto'):
        """Blend a completed local day into each baseline, then purge its windows.

        Event rows contain their own evidence and are intentionally unaffected.
        Synthetic fixtures and the current local day's data remain untouched.
        """
        cutoff = completed_day_cutoff(now, timezone_name)
        local_zone = ZoneInfo(timezone_name)
        summary = {'cutoff': cutoff.isoformat(), 'trained_profiles': [],
                   'skipped_profiles': [], 'trained_days': [], 'skipped_days': [],
                   'deleted_windows': 0,
                   'previous_weight': previous_weight}
        with self.db.lock, self.db.sessions.begin() as session:
            filters = (
                Window.ts < cutoff,
                Window.calibration == False,
                ~Window.stream_id.like('synthetic-trend:%'),
                ~Window.stream_id.like('%:synthetic-demo:%'),
            )
            rows = list(session.scalars(select(Window).where(*filters).order_by(Window.person_id, Window.ts)))
            grouped = defaultdict(list)
            for row in rows:
                observed = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=timezone.utc)
                grouped[(row.person_id, observed.astimezone(local_zone).date())].append(row)
            for (person_id, day), person_rows in sorted(grouped.items(), key=lambda item: item[0][1]):
                baseline = session.get(PersonalBaseline, person_id)
                eligible = [row for row in person_rows if row.features.get('baseline_eligible')]
                records = window_records(eligible)
                stats = fit_statistics(records) if baseline else {}
                if stats:
                    baseline.current = blend_statistics(baseline.current, stats, previous_weight)
                    baseline.updated_at = now
                    baseline.evidence = {
                        **baseline.evidence,
                        'last_daily_update': now,
                        'last_daily_cutoff': cutoff.timestamp(),
                        'last_daily_training_day': day.isoformat(),
                        'daily_previous_weight': previous_weight,
                        'daily_windows_used': len(independent_records(records)),
                        'daily_postures': sorted(stats),
                    }
                    if person_id not in summary['trained_profiles']:
                        summary['trained_profiles'].append(person_id)
                    summary['trained_days'].append({'person_id': person_id, 'day': day.isoformat()})
                else:
                    if person_id not in summary['skipped_profiles']:
                        summary['skipped_profiles'].append(person_id)
                    summary['skipped_days'].append({'person_id': person_id, 'day': day.isoformat()})
            # Keep hourly monitoring coverage for the Activity grid once the raw
            # windows are gone; incident openings live in the stage log already.
            coverage = defaultdict(int)
            for row in rows:
                if row.quality >= .8:
                    observed = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=timezone.utc)
                    coverage[(observed.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0), row.person_id)] += 1
            for key, count in coverage.items():
                kept = session.get(CoverageHour, key)
                if kept:
                    kept.windows += count
                else:
                    session.add(CoverageHour(bucket=key[0], person_id=key[1], windows=count))
            if rows:
                summary['deleted_windows'] = session.execute(delete(Window).where(*filters)).rowcount
        return summary


def evaluate_clips(stats, clips, threshold):
    """Count persistent alert episodes on non-calibration, independently labelled clips."""
    normal_seconds = 0
    false_alerts = detected = positives = 0
    for clip in clips:
        windows = sorted(clip['windows'], key=lambda w: w['timestamp'])
        if not windows:
            continue
        if clip['label'] == 'normal':
            # Credit observed time only; disjoint recordings are not hours of exposure.
            normal_seconds += sum(min(.75, max(0, b['timestamp']-a['timestamp'])) for a, b in zip(windows, windows[1:]))
        else:
            positives += 1
        count, alerted, last, cooldown_until = 0, False, None, -1e20
        for row in windows:
            score, _ = score_statistics(stats, row['features'])
            if last is not None and row['timestamp']-last > .75:
                count = 0
            count = count+1 if score is not None and score > threshold else 0
            last = row['timestamp']
            if count >= 3 and row['timestamp'] >= cooldown_until:
                if clip['label'] == 'normal':
                    false_alerts += 1
                alerted = True
                cooldown_until = row['timestamp']+60
        detected += int(alerted and clip['label'] != 'normal')
    return {'normal_seconds': round(normal_seconds, 2), 'false_alerts': false_alerts,
            'false_alerts_per_hour': false_alerts*3600/normal_seconds if normal_seconds else None,
            'detected': detected, 'positive_clips': positives,
            'recall': detected/positives if positives else None}
