"""Timestamp-based, illustrative pose heuristics. Thresholds require room calibration."""
import math
from datetime import datetime
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

import numpy as np

from .schemas import PoseFrame, Profile

KEYS = (0, 11, 12, 15, 16, 23, 24, 27, 28)
MODEL_KEYS = (11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28)
BRIDGE_SECONDS = 3


def midpoint(points, a, b):
    return ((points[a].x + points[b].x) / 2, (points[a].y + points[b].y) / 2)


def quiet_hours(profile, timestamp):
    hour = datetime.fromtimestamp(timestamp, ZoneInfo(profile.timezone)).hour
    a, b = profile.quiet_start, profile.quiet_end
    return a <= hour < b if a < b else hour >= a or hour < b


def sample(frame: PoseFrame, profile: Profile):
    if frame.region:
        region = frame.region
        poses = [p for p in frame.poses if region.x1 <= midpoint(p.landmarks, 23, 24)[0] <= region.x2
                 and region.y1 <= midpoint(p.landmarks, 23, 24)[1] <= region.y2]
        frame = frame.model_copy(update={'poses': poses})
    selected = next((p for p in frame.poses if p.track_id == frame.monitored_track_id), None)
    if selected is None or frame.tracking_ambiguous:
        return None
    points = selected.landmarks
    quality = sum(points[k].visibility >= .6 for k in KEYS) / len(KEYS)
    if quality < .8:
        return None
    hip = midpoint(points, 23, 24)
    shoulder = midpoint(points, 11, 12)
    torso = math.dist(hip, shoulder)
    if torso < .045:
        return None
    angle = math.degrees(math.atan2(abs(hip[0] - shoulder[0]), abs(hip[1] - shoulder[1])))
    height = max(p.y for p in points) - min(p.y for p in points)
    width = max(p.x for p in points) - min(p.x for p in points)
    posture = 'lying' if angle > 60 and width > height * .9 else 'standing' if angle < 35 and height / torso > 2.3 else 'sitting'
    zone = profile.bed_zone
    in_bed = bool(zone and zone.x1 <= hip[0] <= zone.x2 and zone.y1 <= hip[1] <= zone.y2)
    helper = False
    for other in frame.poses:
        if other.track_id == frame.monitored_track_id:
            continue
        if min(other.landmarks[k].visibility for k in (11, 12, 23, 24)) < .6:
            continue
        helper |= math.dist(hip, midpoint(other.landmarks, 23, 24)) / torso < 2.0
    vector = [v for k in MODEL_KEYS for v in ((points[k].x - hip[0]) / torso, (points[k].y - hip[1]) / torso)]
    return dict(t=frame.timestamp, x=hip[0], y=hip[1], torso=torso, angle=angle,
                posture=posture, quality=quality, in_bed=in_bed, helper=helper,
                wrist=[(points[k].x, points[k].y) for k in (15, 16)],
                ankles=[points[k].y for k in (27, 28)], vector=vector,
                landmarks=[p.model_dump() for p in points])


def sequence(samples):
    if len(samples) < 3 or samples[-1]['t'] - samples[0]['t'] < 1.5:
        return None
    ts = [s['t'] for s in samples]
    target = np.linspace(ts[-1] - 2, ts[-1], 60)
    values = np.array([s['vector'] for s in samples])
    return np.stack([np.interp(target, ts, values[:, i]) for i in range(24)], axis=1).tolist()


def evaluate(frame: PoseFrame, profile: Profile, state: dict):
    t = frame.timestamp
    if t <= state.get('last_timestamp', 0):
        return state, None, 'duplicate_or_out_of_order'
    state = dict(state)
    state['last_timestamp'] = t
    current = sample(frame, profile)
    if current is None:
        # A person lying on the floor often hides wrists or ankles for a moment. Keep the
        # stillness timer through at most BRIDGE_SECONDS of low-visibility frames; the
        # next good frame confirms it (same place, still on the floor) or resets it.
        gap_from = state.get('gap_from') or (state['samples'][-1]['t'] if state.get('samples') else t)
        keep = state.get('fall_seen') and state.get('still_since') is not None and t - gap_from <= BRIDGE_SECONDS
        state.update(samples=[], sway_since=None, recovery_since=None, last_good=None, gap_from=gap_from)
        if not keep:
            state.update(still_since=None, still_anchor=None)
        return state, dict(quality='uncertain', features={}, rule_hits=[], samples=[]), 'low_visibility_or_ambiguous'
    prior = state.get('samples', [])
    gap_from = state.pop('gap_from', None)
    if gap_from is not None and state.get('still_since') is not None:
        anchor = state.get('still_anchor')
        state['bridged'] = bool(anchor and t - gap_from <= BRIDGE_SECONDS and current['posture'] == 'lying' and not current['in_bed']
                                and math.dist((current['x'], current['y']), anchor) / current['torso'] <= .1)
        if not state['bridged']:
            state.update(still_since=None, still_anchor=None)
    # Never bridge missing observations to claim continuous motion/stillness.
    if prior and t - prior[-1]['t'] > .75:
        prior = []
        state.update(still_since=None, still_anchor=None, sway_since=None, recovery_since=None, bridged=False)
    samples = [s for s in prior if t - s['t'] <= 2] + [current]
    state.update(samples=samples, last_good=t)
    if t - state.get('last_score', 0) < 1 / 3 - .001:
        return state, None, 'buffering'
    state['last_score'] = t
    scale = mean(s['torso'] for s in samples)
    steps = [(a, b, b['t'] - a['t']) for a, b in zip(samples, samples[1:]) if b['t'] > a['t']]
    speed = mean(math.hypot(b['x']-a['x'], b['y']-a['y']) / scale / dt for a, b, dt in steps) if steps else 0
    motion = mean(np.linalg.norm(np.array(b['vector'])-np.array(a['vector'])) / math.sqrt(24) / dt for a, b, dt in steps) if steps else 0
    drop = max(((current['y']-s['y']) / scale for s in samples if t-s['t'] <= 1), default=0)
    sway = pstdev(s['x'] for s in samples) / scale
    wrist_speed = max((max(math.dist(a['wrist'][i], b['wrist'][i]) for i in (0, 1)) / scale / dt for a, b, dt in steps), default=0)
    ankle_change = max((abs((b['ankles'][0]-b['ankles'][1])-(a['ankles'][0]-a['ankles'][1])) / scale / dt for a, b, dt in steps), default=0)
    hits = []
    if drop > .5 and current['posture'] == 'lying' and not current['in_bed']:
        hits.append('fall')
        state['fall_seen'] = True
    on_floor = current['posture'] == 'lying' and not current['in_bed']
    if state.get('fall_seen') and not on_floor:
        state['recovery_since'] = state.get('recovery_since') or t
        if t - state['recovery_since'] >= 3:
            state['fall_seen'] = False
    else:
        state['recovery_since'] = None
    # Recent .6 s isolates stillness from the initial impact still in the 2 s window.
    recent = [s for s in samples if t-s['t'] <= .6]
    still = on_floor and len(recent) >= 3 and max(math.dist((s['x'],s['y']), (current['x'],current['y'])) / scale for s in recent) < .04 and all(np.linalg.norm(np.array(s['vector'])-np.array(current['vector'])) < .3 for s in recent)
    # Right after a bridged gap there are too few samples to judge stillness; hold the timer.
    settling = state.get('bridged') and on_floor and len(recent) < 3
    if still and state.get('fall_seen'):
        state['still_since'] = state.get('still_since') or t
        state.update(still_anchor=(current['x'], current['y']), bridged=False)
    elif not settling:
        state.update(still_since=None, still_anchor=None, bridged=False)
    still_for = t-state['still_since'] if state.get('still_since') else 0
    if still_for >= 30:
        hits.append('prolonged_stillness')
    if current['posture'] == 'standing' and sway > .13 and wrist_speed > 2 and ankle_change > .5:
        hits.append('stumble')
    if current['posture'] == 'standing' and sway > .10:
        state['sway_since'] = state.get('sway_since') or t
        if t-state['sway_since'] >= 5:
            hits.append('sway')
    else:
        state['sway_since'] = None
    at_night = quiet_hours(profile, t)
    if profile.restrict_night_standing and at_night and current['posture'] == 'standing' and not current['helper']:
        hits.append('restricted_standing')
    if profile.bed_zone and at_night and profile.restrict_night_standing and not current['in_bed'] and not current['helper'] and any(s['in_bed'] for s in samples):
        hits.append('bed_exit')
    features = dict(hip_drop=round(drop, 3), trunk_angle=round(current['angle'], 1), sway=round(sway, 3),
                    motion_energy=round(float(motion), 3), hip_speed=round(speed, 3), wrist_speed=round(wrist_speed, 3),
                    ankle_change=round(ankle_change, 3), posture=current['posture'], helper_present=current['helper'],
                    in_bed=current['in_bed'], still_seconds=round(still_for, 2), visibility=round(current['quality'], 2))
    return state, dict(quality='good', features=features, rule_hits=hits, samples=samples), 'scored'
