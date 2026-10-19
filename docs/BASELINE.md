# Personal baseline and evaluation

The LSTM path has been replaced by posture-specific median/MAD statistics. No model is pretrained and no baseline accuracy is claimed. Fixed fall, stillness and restriction rules remain independent.

## Features and score

The six features are lateral sway, hip speed, trunk angle, motion energy, wrist speed and ankle-change rate. Distances and speeds use torso length for scale. These are visible-pose proxies; this implementation does **not** claim measured gait speed, step-interval variability or sit-to-stand timing. Per-posture statistics cover standing, sitting and lying only when enough observations exist.

For each feature, `z = 0.6745 × (value - median) / max(MAD, resolution_floor)`. The score is the largest absolute z-score. Each contribution includes the value, median, MAD, regularized scale and signed z-score. Near-zero medians produce no ratio. Resolution floors are exposed by the baseline API. An unseen posture is uncalibrated, not normal.

The first reviewed calibration is the frozen anchor. The current baseline then updates automatically after each completed local day. Eligible daily windows must pass the profile's person check, be quality-gated, and be unflagged or explicitly reviewed as a false alarm; synthetic and uncertain activity is excluded. By default each update keeps 80% of the previous current baseline and gives the new day 20%. A previously validated trigger remains enabled while the frozen anchor remains available for comparison.

The person check stores median shoulder width, hip width, arm length and leg length divided by the visible torso length. It needs clear, upright poses from reviewed recordings. Several dimensions must differ by more than 40% from both the original reference and the current reference across three independent readings before the stream is marked as a mismatch. Automatic learning also needs a consistent upright reading in the preceding 30 seconds. The current reference blends daily at 80/20; the original stays fixed. Pose shape cannot prove identity. Similar body proportions, occlusion, clothing, posture and camera angle limit this check; a mismatch should prompt a human review.

After the daily update, all processed non-calibration pose windows are deleted, including windows that were rejected for training. Event rows are stored separately and are not deleted by this process: the event keeps its rule hits, peak features, keyframes, verdict, acknowledgement, notifications and audit history. Synthetic fixture rows and the current unfinished day are excluded from the purge. The worker checks hourly and uses `PERSONALIZATION_TIMEZONE` to determine the local day boundary.

## Collect and fit

1. Use the capture consent screen. Only synthetic data or consenting teammates belong in this demonstration.
2. Start the camera or a local video, then use **Person check → Record normal movement**. Capture ordinary movement in at least two separate sessions, including enough clear upright footage. Stop recording between sessions. Calibration does not raise incidents.
3. Review the footage as normal, stop recording, and select **Fit reviewed profile**. The confirmation is an operator attestation. Fitting keeps only non-overlapping two-second observations, needs at least 30 per supported posture, two sessions overall and 20 clear upright identity observations. It deletes the consumed calibration windows.
4. Inspect the anchor/current medians and live feature contributions through the baseline API. The movement anomaly trigger is **score-only** until the held-out evaluation below passes. Defaults are `k=3.5` and three consecutive scored windows; a missing/uncertain interval resets persistence. No daily operator action is required after calibration.

## Tune and test separately

The exported calibration contains feature windows. Prepare separate labelled tuning and held-out files from recorded staged clips through the same pose pipeline. Each needs at least 20 observed minutes of ordinary movement; use independent sessions, at least two consenting people and two angles in the held-out set, plus at least ten abnormal clips. Include hard negatives (fast sitting, bending, intentional bed lying) and an uncertain/occluded case. Keep aliases, not real names, in evaluation files.

Format:

```json
{
  "source": "staged",
  "reviewed": true,
  "clips": [{
    "session": "unique-capture-session",
    "person": "teammate-a",
    "angle": "front",
    "label": "normal",
    "windows": [{
      "timestamp": 1792217400.0,
      "features": {
        "posture": "standing", "sway": 0.02, "hip_speed": 0.1,
        "trunk_angle": 4, "motion_energy": 0.03,
        "wrist_speed": 0.1, "ankle_change": 0.1
      }
    }]
  }]
}
```

The example has one window solely to show the format; real clips need a continuous series. `label` is `normal` or `abnormal`. Epoch-second timestamps must be finite. Session IDs must be disjoint across calibration, tuning and held-out data. Synthetic fixtures cannot unlock triggers. Capture session IDs are included in the downloaded skeleton recording; derive feature windows from those same sessions rather than inventing session splits.

Export an actual camera/local-video session directly from retained rows (use its full stream ID from the recording's person, camera and session fields). Add separate clips with `--append`; do not reuse the same footage across datasets. The `--reviewed` flag records your explicit review, not an automated judgement. The exporter rejects generated-fixture streams and preserves uncertainty as gaps rather than crediting them as observation time.

```powershell
.\.venv\Scripts\python.exe -m scripts.export_session "daniel:browser-camera:SESSION_UUID" tuning.json --label normal --person teammate-a --angle front --reviewed
```

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate_baseline daniel tuning.json held-out.json
# Only after reviewing the result, apply the computed gate:
.\.venv\Scripts\python.exe -m scripts.evaluate_baseline daniel tuning.json held-out.json --enable
```

The script selects the smallest threshold from 3.5 to 20 in increments of 0.5 that meets the tuning false-alert target. The default engineering target is at most two false alert episodes/hour; change it explicitly with `--max-false-per-hour`. Held-out gating requires the same target, at least 80% abnormal-clip recall, ten abnormal clips, sufficient observed normal duration, and two people/two angles. The score covers the personal baseline only, not total system/fall-detector accuracy. Long gaps do not count as observation exposure. Reports distinguish a persistent episode from individual abnormal windows.

A pretrained ST-GCN++ action classifier now runs separately in shadow mode;
[ACTION_MODEL.md](ACTION_MODEL.md) describes its comparison tools. It recognizes
dataset action classes, not an individual's normal movement. It has not been
fine-tuned or validated on teammate recordings and does not replace this baseline.

## Trends

The Trends screen was removed from the demo UI; `GET /api/trends/{person_id}` still returns daily means and standard deviations against the frozen calibration median. **Load synthetic drift example** writes six labelled days of sample rows into the configured database, segregated from observed data and excluded from calibration. It does not provision Tiger Data or imply measured decline.

The API computes these values from retained rows. `deploy/tiger.sql` additionally defines a daily continuous aggregate using mean/stddev instead of relying on `percentile_cont` support. Actual migration and aggregate refresh still require verification on the provisioned Tiger service. Seven-day retention applies to the small demo; a longer drift study needs a deliberate retention and budget decision.
