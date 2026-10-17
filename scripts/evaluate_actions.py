"""Offline comparison of ST-GCN++ and fixed fall rules. Never sends notifications.

Use --synthetic for runtime smoke measurements, or --manifest for reviewed
camera/local-video skeleton recordings. Scores and a chosen threshold do not
enable alerts or establish clinical accuracy. See docs/ACTION_MODEL.md.
"""
import argparse
import hashlib
import json
from pathlib import Path
from statistics import median

from backend.app.action_model import ActionModel, CHECKPOINT_SHA256, MODEL_ID, PREPROCESSING
from backend.app.detector import evaluate
from backend.app.replay import scenario_frames
from backend.app.schemas import PoseFrame
from backend.app.seed import demo_profiles


def load_manifest(path):
    path = Path(path)
    body = json.loads(path.read_text(encoding='utf-8'))
    if body.get('reviewed') is not True or body.get('source') != 'staged':
        raise ValueError('Manifest must identify reviewed staged recordings')
    clips, sessions, hashes = [], set(), set()
    for item in body['clips']:
        if item.get('label') not in ('normal', 'fall') or item.get('split') not in ('tuning', 'held_out'):
            raise ValueError('Each clip requires normal/fall label and tuning/held_out split')
        if not item.get('person_alias') or not item.get('angle'):
            raise ValueError('Each clip needs a consenting person alias and camera angle')
        raw = (path.parent/item['recording']).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        recording = json.loads(raw)
        if recording.get('kind') != 'skeleton-recording':
            raise ValueError('Use the browser Download skeleton recording export')
        frames = [PoseFrame.model_validate(f) for f in recording['frames']]
        identities = {(f.person_id, f.camera_id, f.session_id, f.monitored_track_id) for f in frames}
        if len(identities) != 1 or any(f.camera_id != 'browser-camera' or f.demo_time_scale != 1 for f in frames):
            raise ValueError('Each recording must contain one real camera/local-video session at 1x')
        identity = next(iter(identities))
        if identity in sessions or digest in hashes:
            raise ValueError('Do not reuse sessions or recordings across clips/splits')
        if any(b.timestamp <= a.timestamp for a, b in zip(frames, frames[1:])):
            raise ValueError('Recording timestamps must increase')
        sessions.add(identity); hashes.add(digest)
        if item['label'] == 'fall':
            onset = item.get('onset_seconds')
            if not isinstance(onset, (int, float)) or not 0 <= onset < frames[-1].timestamp-frames[0].timestamp:
                raise ValueError('Fall clips require onset_seconds within the recording')
        clips.append(({**item, 'sha256': digest}, frames))
    if not clips:
        raise ValueError('Manifest contains no clips')
    return clips


def score_clip(model, item, frames, threshold):
    profile = demo_profiles()[1].model_copy(update={'id': frames[0].person_id})
    state, rows = {}, []
    for frame in frames:
        state, result, _ = evaluate(frame, profile, state)
        if result is None:
            continue
        row = dict(at_seconds=round(frame.timestamp-frames[0].timestamp, 4),
                   rule_fall='fall' in result['rule_hits'], quality=result['quality'])
        try:
            prediction = model.predict(result['samples'])
            row.update(fall_score=prediction['fall_score'], latency_ms=prediction['latency_ms'],
                       top_action=prediction['top_actions'][0])
        except ValueError:
            row['fall_score'] = None
        rows.append(row)
    count, active, episodes, observed, previous = 0, False, 0, 0., None
    onset = item.get('onset_seconds', 0)
    first_model, first_rule = None, None
    for row in rows:
        t, score = row['at_seconds'], row['fall_score']
        continuous = previous is not None and 0 < t-previous <= .75
        if score is not None and continuous:
            observed += t-previous
        if not continuous or score is None:
            count, active = 0, False
        count = count+1 if score is not None and score >= threshold else 0
        flagged = count >= 3
        if flagged and not active:
            episodes += 1
        if flagged and t >= onset and first_model is None: first_model = t-onset
        if row['rule_fall'] and t >= onset and first_rule is None: first_rule = t-onset
        active = flagged
        previous = t if score is not None else None
    measured = [r for r in rows if r['fall_score'] is not None]
    latencies = sorted(r['latency_ms'] for r in measured)
    return dict(label=item['label'], split=item.get('split', 'synthetic'),
                recording=item.get('recording', 'generated fixture'), person_alias=item.get('person_alias'),
                angle=item.get('angle'), sha256=item.get('sha256'),
                model_observed_seconds=round(observed, 3), total_scored_windows=len(rows),
                model_windows=len(measured), max_fall_score=max((r['fall_score'] for r in measured), default=None),
                model_episodes=episodes, model_detected=first_model is not None,
                rule_detected=first_rule is not None,
                model_onset_to_detection_s=first_model if item['label'] == 'fall' else None,
                rule_onset_to_detection_s=first_rule if item['label'] == 'fall' else None,
                inference_median_ms=median(latencies) if latencies else None,
                inference_p95_ms=latencies[min(len(latencies)-1, int(.95*len(latencies)))] if latencies else None,
                windows=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--synthetic', action='store_true')
    group.add_argument('--manifest')
    parser.add_argument('--checkpoint', default='models/stgcnpp-ntu60-2d.pth')
    parser.add_argument('--threshold', type=float, default=.5)
    parser.add_argument('--output', default='artifacts/action-model-report.json')
    args = parser.parse_args()
    if not 0 < args.threshold < 1: parser.error('threshold must be between 0 and 1')
    if args.synthetic:
        clips = [(dict(label='fall' if name == 'fall' else 'normal', recording=name,
                       onset_seconds=2 if name == 'fall' else 0),
                  list(scenario_frames(name, 'daniel', 1792217400, duration=6)))
                 for name in ('normal', 'fall', 'low_visibility')]
    else:
        clips = load_manifest(args.manifest)
    model = ActionModel(args.checkpoint)
    results = [score_clip(model, item, frames, args.threshold) for item, frames in clips]
    summaries = {}
    for split in {r['split'] for r in results}:
        members = [r for r in results if r['split'] == split]
        normal = [r for r in members if r['label'] == 'normal']
        falls = [r for r in members if r['label'] == 'fall']
        seconds = sum(r['model_observed_seconds'] for r in normal)
        summaries[split] = dict(normal_observed_seconds=seconds, fall_clips=len(falls),
            model_detected_fall_clips=sum(r['model_detected'] for r in falls),
            rule_detected_fall_clips=sum(r['rule_detected'] for r in falls),
            model_false_episodes=sum(r['model_episodes'] for r in normal),
            model_false_episodes_per_observed_hour=(sum(r['model_episodes'] for r in normal)*3600/seconds) if seconds else None)
    report = dict(model=MODEL_ID, checkpoint_sha256=CHECKPOINT_SHA256, preprocessing=PREPROCESSING,
        source='synthetic' if args.synthetic else 'reviewed staged recordings', threshold=args.threshold,
        persistence_windows=3, alert_authority=False, summaries=summaries, clips=results,
        limitations=['No alert policy is changed by this report.',
            'Synthetic data verifies execution only; it cannot establish accuracy.',
            'Choose threshold on tuning clips; do not tune against held-out results.',
            'Inference timing excludes camera, network, database and notification delivery.',
            'MediaPipe visibility and two-second sampling differ from checkpoint training.',
            'Class scores are not calibrated fall probabilities; no unknown/normal class exists.'])
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'report': str(output), 'source': report['source'], 'summaries': summaries}, indent=2))


if __name__ == '__main__': main()
