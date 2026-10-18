# Learned action recognition on the backend

Silent Guardian runs the official **ST-GCN++ NTU60 2D joint checkpoint** on the
backend CPU. Capture and event detail show predictions beside the existing
detector. By default (`DECISION_MODE=enforce`) it is the **alert gate**: a rule
hit is a candidate, and only a candidate whose rule the model supports becomes
an alert. Stillness on the floor after a drop, and an unavailable model, fall
back to the deterministic rule. Supported alerts go to Gemini review with the
scores as evidence; Gemini may raise urgency but never lower it.

## Run

```powershell
python scripts/dev.py --with-action-model --build
```

This installs pinned CPU PyTorch into the project virtual environment, downloads
and verifies the official checkpoint, builds the interface and starts the server.
Later launches can use `python scripts/dev.py`. The base app runs without the
optional runtime/checkpoint; the interface reports that setup is required.

Root `.env` settings (defaults are also in `.env.example`):

```dotenv
ACTION_MODEL_MODE=on
ACTION_MODEL_PATH=models/stgcnpp-ntu60-2d.pth
ACTION_MODEL_THREADS=2
```

`off` disables inference. There is deliberately no unvalidated `live` mode.
Authenticated `/api/action-model` reports readiness, queue counts, failures,
completed predictions and `alert_authority: false`.

## Model and input contract

- ST-GCN++, 1,393,016 parameters, 60 NTU action classes.
- Weights: `stgcnpp_8xb16-joint-u100-80e_ntu60-xsub-keypoint-2d_20221228-86e1e77a.pth`.
- SHA-256: `86e1e77a7b27dd53c6ccc1600a4cc3c321ec8e04d17f25a41fbbb7896026945a`.
- [Official configuration and weights](https://github.com/open-mmlab/mmaction2/blob/v1.2.0/configs/skeleton/stgcnpp/README.md).
- Code: inference subset of MMAction2 v1.2.0, Apache-2.0. Attribution and full
  license are in `backend/app/vendor/stgcn`. No NTU recordings are downloaded or
  redistributed; check dataset conditions before acquiring training data.
- Loading uses `weights_only=True`, pinned SHA-256 and strict parameter matching.
  The runtime never substitutes random weights for a missing checkpoint.

MediaPipe's 33 landmarks map to COCO's 17 joints using indices
`0,2,5,7,8,11,12,13,14,15,16,23,24,25,26,27,28`. Each joint contains normalized
x/y and visibility; MediaPipe z is **not** the confidence channel. Coordinates
use upstream PreNormalize2D: `2*x-1` and `2*y-1`. The second person is zero-padded
as in upstream FormatGCNInput. Classification runs only when one person is visible.

The online adapter interpolates approximately two seconds to 100 time positions.
It requires eight or more poses spanning at least 1.8 seconds, no gap over 250 ms,
and sufficient body-joint visibility. Uncertain tracking invalidates queued work
and older results. This is an **online adaptation**, not the upstream full-clip,
ten-clip benchmark protocol. It does not inherit published benchmark accuracy.
MediaPipe visibility also differs from the training pose estimator's scores.

Class 42 (zero based) is falling, class 41 is staggering. Those scores and the
top five classes are retained. Scores are softmax outputs, **not calibrated
probabilities of harm**. There is no universal normal/unknown class: the network
must rank known actions even for unfamiliar movement. Dataset labels that infer
medical conditions are replaced with observable-movement descriptions in output.
Personal median/MAD calibration remains a separate component.

## Runtime and privacy

Inference runs in a separate worker thread outside the ingestion/database lock.
It keeps one pending window per stream, up to 32 streams, replacing old pending
windows under load. It reuses the detector's existing two-second pose buffer.
The first complete window associated with a new event is preserved and prioritized
so accelerated replay cannot replace its transition with a later static pose.
Accelerated replay can still skip other windows; evaluate timing/accuracy at 1x.
Only prediction summaries are added to scored windows and events; no additional
video or raw skeleton archive is created. Live results include their observation
timestamp and age, rather than implying instantaneous classification.
Event detail retains the snapshot with the highest falling score, explicitly
labelled as such; each scored window retains its own prediction for comparison.

Profile changes and consent revocation clear queued/cached predictions. A result
computed during deletion or consent revocation is discarded before persistence.
Per-profile purge removes predictions with window/event rows. A running CPU
calculation cannot be undone, but its result cannot recreate deleted rows.
No external inference service receives the model's input.

## Evaluate actual recordings

Use **Download skeleton recording** after a camera/local-video capture. Collect
separate reviewed normal and safely staged fall clips from consenting teammates.
Create a manifest beside those JSON recordings:

```json
{
  "source": "staged",
  "reviewed": true,
  "clips": [
    {"recording": "normal-front.json", "label": "normal", "split": "tuning", "person_alias": "a", "angle": "front"},
    {"recording": "fall-side.json", "label": "fall", "split": "held_out", "person_alias": "b", "angle": "side", "onset_seconds": 2.5}
  ]
}
```

This shows the format, not a sufficient validation set. Include fast sitting,
bending, intentional lying down and occlusion. Use two people/two angles and
enough ordinary activity to estimate false alerts. The project's 20–30 minute
minimum is a starting point, not a statistical guarantee. Keep tuning and
held-out sessions disjoint. The loader rejects duplicate sessions/recordings and
generated fixture streams.

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate_actions --manifest recordings/manifest.json --threshold 0.5 --output artifacts/action-evaluation.json
```

The report compares falling scores, inference time, fixed fall-rule decisions,
three-window persistent model episodes, observed exposure and onset latency.
`0.5` is an evaluation probe, not a validated alert threshold. Choose a threshold
on tuning clips, freeze it, then inspect held-out results. The script never
enables alerts or sends notifications. It does not fine-tune the checkpoint.
Fine-tuning on MediaPipe-derived data may be needed if transfer is poor.

For a credential-free execution check:

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate_actions --synthetic
```

`docs/action-model-smoke.json` records the initial synthetic run. It verifies
execution, not accuracy. Inference timing excludes camera, network, database
and provider delivery latency. No real staged dataset has been evaluated yet.

## Vultr

Compose defaults to `WITH_ACTION_MODEL=1`: the Docker build installs CPU PyTorch
and fetches/verifies the checkpoint into the image. For a smaller rules-only
build set `WITH_ACTION_MODEL=0` and `ACTION_MODEL_MODE=off`. Weights are not
committed to Git. Run one API process as before. Windows CPU inference is tested;
the Linux image and Vultr latency still await VM access. No GPU is assumed and
the $1,000 Tiger Data constraint is unchanged.
