# Silent Guardian configuration and deployment

The local app works without cloud credentials. Real Gemini verdicts, Tiger Data persistence, Vultr hosting, web push, and phone calls have **not** been verified against external services. SMS is intentionally omitted. No resources have been purchased or provisioned.

## Configuration locations

Copy `.env.example` to **`.env` in the repository root**. Never commit `.env`; the browser must not receive provider credentials. For Vultr, put the same `.env` beside `compose.yaml` on the VM, restricted to its owner. Restart the app after changes. These are the fields to update when credentials become available:

| Integration | Values in root `.env` | Where to obtain them |
| --- | --- | --- |
| Tiger Data | `DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:PORT/DB?sslmode=require` | Provisioned service connection details; URL-encode special characters in credentials |
| Gemini | `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_BILLED_PROJECT_CONFIRMED=true` | An API project associated with active billing; verify billing and model access, then record per-profile Gemini consent in the app |
| Vultr | `PUBLIC_URL`, `ALLOWED_ORIGINS`, `APP_ENV=production`, `DEMO_ENABLED=false`, `DOMAIN` | VM hostname/domain and DNS. SSH access is needed to deploy; the app does not need a Vultr API key |
| Operator sign-in | `OPERATOR_TOKEN`, `SIGNING_SECRET` | Generate two independent random secrets of at least 32 characters; replace the local defaults |
| Web push | `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_SUBJECT` | Generate a VAPID key pair; use an appropriate `mailto:` contact for the subject |
| Ring-only phone call | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `DEMO_CALL_TO`, `CALL_TEST_ALLOWLIST` | Twilio Voice account, voice-capable caller number, and the user's explicitly supplied personal destination in E.164 format |
| Delivery switch | `NOTIFICATION_MODE=live`, `CALLS_ENABLED=true` | Set only after configuration is checked and the team is ready for real delivery |

Phone numbers are never inferred from David's profile or account information. `DEMO_CALL_TO` must exactly match an entry in the comma-separated `CALL_TEST_ALLOWLIST`. Trial accounts may require a verified destination and may add a provider announcement if someone answers; verify the account's trial restrictions.

The Gemini default is `gemini-3.8-flash`, a stable model listed for new projects in [Google's model catalog](https://ai.google.dev/gemini-api/docs/models). The adapter follows the documented [Generate Content structured-output REST format](https://ai.google.dev/gemini-api/docs/generate-content/structured-output). Confirm access and measure its response time with the actual API project; the independent rule-based alert remains active during the 15-second AI deadline.

Gemini receives category flags, detector metrics, event history and skeleton-rendered keyframes, not aliases, age, contact details, conditions or free-text care notes. This demo requires a confirmed billed project and explicit profile consent before sending. Google's [Gemini terms](https://ai.google.dev/gemini-api/terms) distinguish paid/unpaid data handling and prohibit clinical/medical-advice use; category reduction is data minimization, not anonymization. The app remains a staged, nonclinical demonstration. Consent revocation blocks pending requests; it cannot retract a request already sent.

**Privacy → Delete** removes that profile's application rows, keyframes and baseline, leaves an empty consent-revocation marker, and attempts to refresh existing Tiger aggregates. The response reports whether aggregate refresh succeeded. Provider logs, local downloads and database backups are outside the app purge. Test deletion on a disposable staged profile before demonstrating it on Tiger.

## The phone demonstration

1. Configure the Voice fields above and keep `NOTIFICATION_MODE=dry_run` for the first check.
2. Open **Demo setup → Simulate demo call**. The event timeline must say **simulated**.
3. Once ready, set live delivery and enable calls; restart. **Ring demo phone** creates one outbound call. A concern/urgent event also queues one call to the configured demo destination.
4. The request allows about 15 seconds of ringing; if answered, inline `<Response><Hangup/></Response>` ends the call. There is no application speech, recording, or audio stream. Carrier/provider timing may vary.
5. A successful REST response is labelled **provider accepted**, which does not prove the handset rang. Check the actual phone and provider log. Timeout/crash ambiguity is labelled **unknown** and is not automatically retried.

The default persistent limits are five requests per UTC day and five minutes between calls. Calls are once per event, including urgency upgrades. Browser push continues on the escalation schedule until acknowledgement. Acknowledgement prevents future sends but cannot retract a call already accepted by the provider.

## Tiger Data budget of $1,000

Treat $1,000 as the total maximum for this project until the user specifies currency and duration. Do not assume it is a monthly allowance. `TIGER_BUDGET_LIMIT=1000` displays that constraint; **it does not enforce a cloud billing cap**.

Use one small Performance service for the demo, without replicas, IO boosts, support upgrades, or long-lived forks. Verify its region-specific quote, billing currency, tax, and any credits before provisioning. The official pricing page currently lists entry compute from $30/month and hourly consumption billing; this is an indicative starting point, not a quote for the chosen service. Storage and add-ons cost extra. Use the provider console's accrued charges and forecast, reserve headroom for taxes/currency, and stop expanding well below the total limit. Pause compute when the demo is idle; retained storage can still incur charges. Keep seven-day pose retention for this small demo, review actual growth, and remove disposable demo data only with the owner's authorization. See [Tiger Data pricing](https://www.tigerdata.com/pricing) and [service management](https://www.tigerdata.com/docs/use-timescale/latest/services/service-management/).

The application stores profiles, events, verdict audits, notifications and acknowledgements in relational tables. Only `pose_windows` becomes a hypertable. Run these commands from the repo root after setting the actual Tiger connection string:

```powershell
.\.venv\Scripts\python.exe -m scripts.migrate_tiger
# Inspect the provisioned version and deploy/tiger.sql before applying:
.\.venv\Scripts\python.exe -m scripts.migrate_tiger --apply
```

The first command is read-only. It checks the actual TimescaleDB version and `by_range` availability; the second creates the core schema, hypertable and hourly continuous aggregate. Syntax is based on [Tiger's hypertable API](https://www.tigerdata.com/docs/reference/timescaledb/hypertables/create_hypertable) and [continuous aggregates](https://www.tigerdata.com/docs/build/continuous-aggregates/create-a-continuous-aggregate), but must be verified against the provisioned service. Aggregate refresh excludes the current hour. Retention runs hourly in the app; do not use this single-process prototype for unattended clinical service.

## Vultr deployment

Use a small Linux VM with Docker Engine and Compose, a domain pointing at it, and ingress for SSH from the operator's network plus ports 80/443. No GPU or Kubernetes is required for this demo. Do not expose port 8000 publicly. Set `.env` to production, disable synthetic replay, and generate strong secrets. The app checks these at startup.

```sh
docker compose --profile cloud up --build -d
docker compose exec guardian python -m scripts.migrate_tiger
docker compose exec guardian python -m scripts.migrate_tiger --apply
curl --fail https://YOUR_DOMAIN/healthz
```

Run **one API worker/replica**, as specified by the Dockerfile. The notification worker and camera ingestion share a local lock, with PostgreSQL event row locks as an additional guard. This intentionally does not claim multi-replica scheduling support. Caddy provides TLS and WebSocket proxying. Browser camera and push require a secure context (localhost works for local tests).

For a hosted synthetic demonstration, use a separate development deployment with strong credentials, HTTPS and an explicit trusted audience. Production mode deliberately disables synthetic replay. Load a staged local video only in a development/demo deployment; real camera capture works in either mode.

Acceptance after credentials arrive: verify health, apply the migration to the real service, run Margaret/Daniel scenarios, check the actual phone, acknowledge from a trusted-contact browser, restart the service and verify the event remains acknowledged. Docker is not installed in the current workstation, so the image and Compose deployment have not been executed here.
