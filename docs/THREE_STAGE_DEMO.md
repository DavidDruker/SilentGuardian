# Three-stage demo and deployment

The browser keeps the uploaded video local. After explicit profile consent, the MediaPipe worker extracts landmarks, overlays the skeleton, and sends pose frames over the authenticated socket. Playback waits for the worker to load and automatically tracks a single detected person. At the end, the UI queries the upload session only and shows its analysis status or an explicit no-pose/uncertain result. No video file is uploaded to the API.

After a reviewed upload, **Download skeleton evidence** saves the sent pose frames locally as JSON for the existing `scripts/evaluate_actions.py --manifest` workflow. This export is optional and contains no video pixels.

## Pipeline modes

`DECISION_MODE=enforce` is the default: Rules → ML → Gemini. The rules are deliberately sensitive, so a rule hit is only a candidate. Each candidate (with its profile version, rule hits, features, and two-second skeleton evidence) is scored by the ST-GCN++ model, in-process or on the private ML node, and the versioned policy supports, rejects, or holds it. A rejected or held candidate is recorded but raises no alert. A supported candidate opens an alert immediately, queues notifications, and is sent to Gemini when it is configured and billing is confirmed; there is no per-profile Gemini opt-in. Gemini adds the profile context and an explanation and can raise urgency, never lower it, and a Gemini failure keeps the alert with a fallback verdict.

Two cases use a deterministic safety rule instead of the ML score. Someone motionless on the floor after a drop does not look like the model's "falling" action, so prolonged stillness raises an alert after 30 seconds even when the ML rejected the drop, and escalates at two minutes. If the model is unavailable, falls and stillness still alert. Restricted-hours standing uses the profile's time rule directly and skips the ML.

Alerts are timestamped incidents. Hits of one rule within five seconds of each other (by video or camera time) belong to one alert; a later hit is logged as a new alert. The live status describes only the current moment: once the person is up and settled there is no active alert, while the log keeps every alert with its time. Someone still on the floor after an alerted fall remains an alert, moving or not; after an unconfirmed drop the status shows a watch warning until the stillness rule decides.

`DECISION_MODE=shadow` keeps the earlier behaviour: rules open alerts directly and the ML only scores them; a supporting score sends that alert to Gemini review. Candidates, ML results, Gemini review, notifications, and acknowledgements append stage records to `event_stage_log` in both modes. The Activity page reads incident openings for the heat map and pose-window coverage separately. With Tiger Data, the completed hours come from `hourly_incidents`; the recent raw tail includes the current hour.

The NTU60 model score is not a calibrated probability for this camera or motion. The fall-support cutoff (`FALL_SCORE_THRESHOLD`, 0.4) and staggering cutoff (0.5) are example values: review normal, staged-fall, and difficult false-positive clips and adjust them before relying on the gate. A brief clip that does not produce enough visible pose frames may produce an uncertain result. An absent rule trigger is reported as no alert, never as proof of safety.

## Local verification

From the repository root, run the API test suite and web build. `scripts/migrate_tiger.py` is read-only without `--apply`. On Windows, if pytest cannot use the system temp directory, give it a base directory under `artifacts`.

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q --basetemp .\artifacts\pytest-run
npm.cmd --prefix web run build
```

The one-machine Compose file now runs `guardian`, `rules`, and `ml` as separate services. Only Caddy and the loopback-bound app port are exposed. The rules and ML services require `X-Internal-Token`; use the same unique 32+ character `INTERNAL_SERVICE_TOKEN` on all three nodes in production. The app is the only owner of profiles, consent, Gemini, and notification work. The ML endpoint rejects concurrent inference rather than growing an unbounded queue.

## Three Vultr instances

Build the same repository on each private-network instance. Run only one role per node, with one process per role. Set `RULES_SERVICE_URL=http://RULES_PRIVATE_IP:8001` and `ML_SERVICE_URL=http://ML_PRIVATE_IP:8002` on the app node. The rules and ML instances require the same `INTERNAL_SERVICE_TOKEN`; do not expose ports 8001 or 8002 publicly. Configure VPC firewall rules to allow only the app instance to reach them. Put `DATABASE_URL` and Gemini credentials only on the app node. The ML node needs the official checkpoint and PyTorch CPU runtime; the rules node does not. Caddy on the app node proxies public HTTPS and WebSocket traffic to port 8000. The app still requires one API worker so only one process runs Gemini and notification jobs.

Each node runs one role from its own Compose file, so the app node never builds PyTorch and the workers never hold database or Gemini credentials:

```text
guardian-app:   docker compose -f compose.app.yaml up -d --build
guardian-rules: docker compose -f compose.rules.yaml up -d --build
guardian-ml:    docker compose -f compose.ml.yaml up -d --build
```

Per-node `.env` values (everything else can stay as in `.env.example`):

| Node | Required values |
| --- | --- |
| guardian-app | `DATABASE_URL` (Tiger), Gemini settings, `RULES_SERVICE_URL=http://RULES_VPC_IP:8001`, `ML_SERVICE_URL=http://ML_VPC_IP:8002`, `INTERNAL_SERVICE_TOKEN`, and `ACTION_MODEL_MODE=on`. For a single server without an ML node, leave `ML_SERVICE_URL` empty and set `WITH_ACTION_MODEL=1` so the gate runs in the app image |
| guardian-rules | `INTERNAL_SERVICE_TOKEN`, `PRIVATE_IP=RULES_VPC_IP` |
| guardian-ml | `INTERNAL_SERVICE_TOKEN`, `PRIVATE_IP=ML_VPC_IP` |

The rules and ML ports publish only on `PRIVATE_IP`. Vultr firewall groups filter public traffic, not VPC traffic, so that binding is what keeps 8001/8002 off the internet. Check reachability from the app node with `curl http://RULES_VPC_IP:8001/healthz` and `curl http://ML_VPC_IP:8002/healthz` (the ML node should report `"status":"ready"`). The single-machine `compose.yaml` remains for local use.

Before a Tiger deployment, inspect the actual server with `python -m scripts.migrate_tiger`, then apply `python -m scripts.migrate_tiger --apply`. Rerun `--apply` after updating an existing deployment: it is idempotent and adds the new tables. The migration creates the stage and monitoring-coverage hypertables and the `hourly_incidents` continuous aggregate, and backfills the recent completed buckets. The daily baseline update deletes processed pose windows, so it first stores an hourly coverage summary in `monitoring_coverage`; the Activity grid reads that summary for purged hours and raw windows for the current day. The API backfills stage openings for legacy incidents once at startup. Verify a known incident against the raw stage rows and the Activity cell, including the current hour, on the provisioned service. Profile purge deletes candidates, stages, coverage rows, and relational incident data, then refreshes available aggregates.

No Vultr instances, Tiger service, Gemini project, or real fall video were available to this local implementation. Deployment, real provider delivery, model threshold calibration, performance numbers, and the final staged-video rehearsal need those resources; local tests alone do not establish them.
