# Silent Guardian

A local movement-monitoring MVP with Care and Personal modes: live metrics, camera or local-video capture, persistent alerts, acknowledgement, and optional Gemini assessment.

## Run

Requires Python 3.11+ and Node 20.19+ (Node 24 recommended). From this repository:

```powershell
python scripts/dev.py
```

The launcher installs dependencies and the official MediaPipe model on first run, builds the UI, and serves **http://localhost:8000**. Sign in with the configured operator token (the local development default is `local-demo-only`), create a profile, then start the camera or choose a video. Later source edits can be rebuilt with `python scripts/dev.py --build`. Stop with Ctrl+C. Local data persists in ignored `data/guardian.db`.

Synthetic profiles and replay controls are off by default. Existing synthetic records in an older local database are hidden from the MVP UI, without deleting stored data. Set `DEMO_ENABLED=true` only for fixture-driven testing through the API.

## Configuration and current limits

Copy `.env.example` to root `.env` when credentials are available. See [the exact configuration map](docs/SETUP.md), including the **$1,000 total Tiger Data budget**, phone allowlist, deployment steps and unverified external acceptance checks. No paid resources or real calls have been created.

The app uses SQLite locally and PostgreSQL/Tiger Data through the same models. `deploy/tiger.sql` defines pose-window and event-stage hypertables with hourly aggregates. The app, rules, and ML services have separate entrypoints; the app alone runs Gemini and notification workers. The Compose file runs all three roles locally, while `compose.app.yaml`, `compose.rules.yaml` and `compose.ml.yaml` run one role per Vultr instance ([deployment guide](docs/THREE_STAGE_DEMO.md)). Run one app worker/replica.

Live video stays on the browser. Monitoring starts automatically when one person is visible. A second detected person makes the frame uncertain and pauses scoring until only one remains. Quality-gated two-second windows are evaluated about three times per second. Each triggered rule can open its own alert, so a restricted-standing alert does not hide a later fall. Current rules cover falls, stillness after a fall, stumbles, sustained sway, bed exits and restricted standing. They are starting heuristics, not validated health monitoring. Walker detection, real identity verification, clinical use, IP cameras and broad analytics remain outside this demo.

Uploading a video plays the clip with a local MediaPipe skeleton overlay and shows its session-scoped analysis status at the end; it does **not** train or fine-tune the action model. By default, rules open alerts and ML runs only for comparison, so no Gemini verdict is requested. In validated enforcement mode, a rule hit goes to ML first; only an ML-supported candidate can reach Gemini for contextual review, with profile consent and configured credentials. The Activity page shows incident openings, stage history, and monitoring coverage. After an initial reviewed calibration, the per-person statistical baseline updates automatically from each completed day of eligible normal activity. By default it keeps 80% of the previous baseline and gives the completed day 20%, then deletes the processed pose windows. See [baseline collection and evaluation](docs/BASELINE.md). No clinical performance is claimed.

The earlier experimental controls and synthetic evidence remain available through the API where applicable; the MVP screen shows capture, metrics and alert actions.

## Verify

The optional **ST-GCN++ backend classifier** runs in shadow mode with official
pretrained weights. Use `python scripts/dev.py --with-action-model --build` to
install it. Predictions are recorded for evaluation but do not control alerts.
[Model setup and evaluation](docs/ACTION_MODEL.md)
documents the adapter, CPU runtime, licensing and validation limits.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q
.\.venv\Scripts\python.exe -m scripts.evaluate_replays
.\.venv\Scripts\python.exe -m scripts.export_openapi
npm.cmd --prefix web run types
npm.cmd --prefix web run build
```

`web/src/api-types.ts` is generated from FastAPI's committed `openapi.json`. The backend suite covers scenario routing, uncertain visibility, duplicate windows, WebSocket reconnects, profile versions, urgency timing, Gemini failure/grounding, persistent acknowledgement, and safe call dispatch. [Replay results](docs/replay-results.json) describe synthetic-landmark measurements, not camera or provider latency.

See [the verification record](docs/VERIFICATION.md) for completed local checks and the remaining external acceptance steps.

## Project map

| Path | Purpose |
| --- | --- |
| `backend/app` | API contracts, persistence, rules, events, Gemini, notifications |
| `backend/tests` | Integration and failure-path tests |
| `backend/app/baseline.py` | Robust posture-specific statistics and frozen-anchor comparison |
| `backend/app/action_model.py`, `actions.py` | Pretrained ST-GCN++ adapter and bounded shadow-inference worker |
| `web/src` | Monitoring dashboard, capture, profiles, alert/contact views |
| `scripts` | Local launcher, schema export, replay evaluation, Tiger migration |
| `deploy` | TLS proxy and Tiger Data SQL |

## Integration references

[MediaPipe browser guide](https://developers.google.com/edge/mediapipe/solutions/vision/pose_landmarker/web_js), [Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output), [Tiger Data hypertables](https://www.tigerdata.com/docs/reference/timescaledb/hypertables/create_hypertable), and [Twilio Call API](https://www.twilio.com/docs/voice/api/call-resource).

The idea and architecture were planned in advance. Application code, schemas and prompts were authored in this workspace with OpenAI Codex assistance; the supplied blueprint appendices are not committed. The ST-GCN++ inference subset and pretrained checkpoint are third-party OpenMMLab work, attributed in `backend/app/vendor/stgcn/NOTICE.md`, not newly authored code. Compliance with the event's development window and use of pretrained components awaits organizer confirmation and timestamp review. Demonstration profiles and bundled motion fixtures are synthetic. This is an engineering prototype, not a medical device or an emergency service.
