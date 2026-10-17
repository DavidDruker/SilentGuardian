"""Tune k on separate clips, evaluate held-out clips, and optionally enable triggers.

No network delivery or camera operation occurs. DATABASE_URL chooses the local or
Tiger database whose baseline is being evaluated. See docs/BASELINE.md for format.
"""
import argparse
import hashlib
import json
import math
import time
from pathlib import Path

from backend.app.baseline import FEATURES, evaluate_clips
from backend.app.config import settings
from backend.app.db import Database, PersonalBaseline


def load_clips(path):
    body = json.loads(Path(path).read_text(encoding='utf-8'))
    if body.get('source') != 'staged' or body.get('reviewed') is not True:
        raise ValueError('Use independently reviewed staged clips; synthetic fixtures cannot unlock triggers')
    clips = body['clips']
    for c in clips:
        if c['label'] not in ('normal', 'abnormal') or not all(c.get(k) for k in ('session', 'person', 'angle')):
            raise ValueError('Every clip needs a session, consenting person alias, angle and label')
        if len(c['windows']) < 3:
            raise ValueError('Each clip needs at least three scored windows')
        for w in c['windows']:
            if not isinstance(w['timestamp'], (int, float)) or not math.isfinite(w['timestamp']):
                raise ValueError('Use finite epoch-second timestamps')
            if any(not isinstance(w['features'].get(k), (int, float)) or not math.isfinite(w['features'][k]) for k in FEATURES):
                raise ValueError('Every scored window needs the six finite feature values')
    if len({c['session'] for c in clips}) != len(clips):
        raise ValueError('Use one contiguous clip per session; session labels must be unique')
    return clips


def validation_report(stats, calibration_sessions, tuning, held_out, max_false_per_hour=2):
    groups = [set(calibration_sessions), {c['session'] for c in tuning}, {c['session'] for c in held_out}]
    if any(groups[a] & groups[b] for a, b in ((0,1),(0,2),(1,2))):
        raise ValueError('Calibration, tuning and held-out sessions must be disjoint')
    threshold, tuned = None, None
    for candidate in [3.5+i*.5 for i in range(34)]:
        result = evaluate_clips(stats, tuning, candidate)
        if result['normal_seconds'] >= 1200 and result['false_alerts_per_hour'] <= max_false_per_hour:
            threshold, tuned = candidate, result
            break
    if threshold is None:
        return {'passed': False, 'reason': 'Tuning requires at least 20 observed normal minutes and an acceptable false-alert rate.'}
    tested = evaluate_clips(stats, held_out, threshold)
    diversity = len({c['person'] for c in held_out}) >= 2 and len({c['angle'] for c in held_out}) >= 2
    passed = bool(diversity and tested['normal_seconds'] >= 1200 and tested['positive_clips'] >= 10
                  and tested['recall'] >= .8 and tested['false_alerts_per_hour'] <= max_false_per_hour)
    return {'passed': passed, 'threshold': threshold, 'tuning': tuned, 'held_out': tested,
            'two_people_two_angles': diversity, 'max_false_alerts_per_hour': max_false_per_hour,
            'scope': 'Personal-baseline persistence only. Staged, not clinical. Does not measure the fixed fall rules.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('person_id'); parser.add_argument('tuning_json'); parser.add_argument('held_out_json')
    parser.add_argument('--enable', action='store_true')
    parser.add_argument('--max-false-per-hour', type=float, default=2)
    args = parser.parse_args()
    if not 0 <= args.max_false_per_hour <= 10:
        raise SystemExit('Set an explicit demo false-alert target between 0 and 10 per hour')
    db = Database(settings.database_url)
    with db.sessions() as session:
        b = session.get(PersonalBaseline, args.person_id)
        if not b:
            raise SystemExit('Fit a reviewed personal baseline first')
        stats, evidence = b.current, b.evidence
    digest = hashlib.sha256(json.dumps(stats, sort_keys=True).encode()).hexdigest()
    report = validation_report(stats, set(evidence.get('anchor_sessions', [])) | set(evidence.get('current_sessions', [])),
                               load_clips(args.tuning_json), load_clips(args.held_out_json), args.max_false_per_hour)
    report['evaluated_at'], report['baseline_sha256'] = time.time(), digest
    print(json.dumps(report, indent=2))
    if args.enable:
        with db.sessions.begin() as session:
            b = session.get(PersonalBaseline, args.person_id, with_for_update=True)
            if not b or hashlib.sha256(json.dumps(b.current, sort_keys=True).encode()).hexdigest() != digest:
                raise SystemExit('Baseline changed during evaluation; re-run against the current version')
            b.trigger_enabled = report['passed']
            if report['passed']:
                b.threshold = report['threshold']
            b.evidence = {**b.evidence, 'validation': report}
        print('Persistent baseline triggers enabled.' if report['passed'] else 'Gate not met; baseline remains score-only.')


if __name__ == '__main__':
    main()
