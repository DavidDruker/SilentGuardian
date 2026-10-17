# Silent Guardian

A working local movement-monitoring demo with equal Care and Personal flows: browser-local pose estimation, authenticated skeleton streaming, deterministic event detection, persistent alerts, acknowledgement, and optional Gemini, web push and ring-only phone calls.

## Run

Requires Python 3.11+ and Node 20.19+ (Node 24 recommended). From this repository:

```powershell
python scripts/dev.py
```

The launcher installs dependencies and the official MediaPipe model on first run, builds the UI, and serves **http://localhost:8000**. Choose **Open the local demo**. Later source edits can be rebuilt with `python scripts/dev.py --build`. Stop with Ctrl+C. Local data persists in ignored `data/guardian.db`.

Two seeded synthetic profiles: Margaret in Care mode and Daniel in Personal mode. Replay **stumble** in both to see direct care-team routing versus a personal check-in. Open event detail to acknowledge, inspect evidence, or open the limited trusted-contact view. The **Care team** / **Trusted circle** page includes a ring-only phone test; delivery is simulated until configured. SMS is not included.

## Configuration and current limits

Copy `.env.example` to root `.env` when credentials are available. See [the exact configuration map](docs/SETUP.md), including the **$1,000 total Tiger Data budget**, phone allowlist, deployment steps and unverified external acceptance checks. No paid resources or real calls have been created.

The app uses SQLite locally and PostgreSQL/Tiger Data through the same models. `deploy/tiger.sql` defines a pose-window hypertable and hourly aggregate. A single-process FastAPI server serves the React/Vite build and runs durable notification and verdict workers. Deployment files target a small Vultr VM with Caddy HTTPS. Run one API worker/replica.

Live video stays on the browser. Two poses can be detected; the selected subject is tracked geometrically and ambiguity is surfaced. Quality-gated two-second windows are evaluated about three times per second. Current rules cover falls, stillness after a fall, stumbles, sustained sway, bed exits and restricted standing. They are starting heuristics, not validated health monitoring. Walker detection, real multi-person identity, clinical use, IP cameras and broad analytics remain outside this demo.

Personalization uses an explainable median/MAD baseline per posture, a frozen calibration anchor, per-feature contributions and an evaluated three-window persistence gate. It remains score-only until held-out staged evidence passes. [Baseline collection and evaluation](docs/BASELINE.md) documents its assumptions and feature proxies. No trained baseline or clinical performance is claimed.

The [October 17 adjustments](docs/ADJUSTMENTS.md) also add an 8× simulated replay clock, monitored-area masking, consent/pause/delete controls, human Real concern/False alarm review, and a rules/Gemini second-assessment status in event detail. The synthetic Trends and Evidence screens were removed from the demo UI; their APIs and the measured limits remain in the docs.

## Verify

The optional **ST-GCN++ backend classifier** runs in shadow mode with official
pretrained weights. Use `python scripts/dev.py --with-action-model --build` to
install it. Event detail shows the learned falling and staggering scores beside the rules;
model predictions do not yet control alerts. [Model setup and evaluation](docs/ACTION_MODEL.md)
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
| `web/src` | Care station, Personal home, capture, profiles, event/contact views |
| `scripts` | Local launcher, schema export, replay evaluation, Tiger migration |
| `deploy` | TLS proxy and Tiger Data SQL |

## Integration references

[MediaPipe browser guide](https://developers.google.com/edge/mediapipe/solutions/vision/pose_landmarker/web_js), [Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output), [Tiger Data hypertables](https://www.tigerdata.com/docs/reference/timescaledb/hypertables/create_hypertable), and [Twilio Call API](https://www.twilio.com/docs/voice/api/call-resource).

The idea and architecture were planned in advance. Application code, schemas and prompts were authored in this workspace with OpenAI Codex assistance; the supplied blueprint appendices are not committed. The ST-GCN++ inference subset and pretrained checkpoint are third-party OpenMMLab work, attributed in `backend/app/vendor/stgcn/NOTICE.md`, not newly authored code. Compliance with the event's development window and use of pretrained components awaits organizer confirmation and timestamp review. Demonstration profiles and bundled motion fixtures are synthetic. This is an engineering prototype, not a medical device or an emergency service.
