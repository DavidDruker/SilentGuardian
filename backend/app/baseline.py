"""Explainable, posture-specific baselines. No neural model or clinical claim."""
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median

from sqlalchemy import select

from .db import PersonalBaseline, Window

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
        return {'postures': list(stats), 'trigger_enabled': False, 'anchor_frozen': True}

    def refresh_current(self, person_id, now):
        with self.db.lock, self.db.sessions.begin() as session:
            baseline = session.get(PersonalBaseline, person_id)
            if not baseline:
                return False
            cutoff = datetime.fromtimestamp(now-7*86400, timezone.utc)
            rows = list(session.scalars(select(Window).where(Window.person_id == person_id, Window.ts >= cutoff).order_by(Window.ts)))
            eligible = [r for r in rows if r.features.get('baseline_eligible') and not r.features.get('synthetic_trend')]
            stats = fit_statistics(window_records(eligible))
            if not stats:
                return False
            baseline.current = {**baseline.current, **stats}
            baseline.updated_at = now
            # A new distribution needs its own validation before affecting alerts.
            baseline.trigger_enabled = False
            baseline.evidence = {**baseline.evidence, 'current_refreshed': now, 'validation': None,
                                'current_sessions': sorted({r.stream_id for r in eligible})}
            return True


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
