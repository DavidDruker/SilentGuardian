# Local verification — 2026-10-17

The credential-free local demo is implemented and checked. The plan's external acceptance condition (both modes on Vultr with Tiger Data and real delivery) remains pending. No cloud resources were created and no real phone call was placed.

## Completed checks

- ST-GCN++ addition: official checkpoint checksum and strict weight loading verified;
  real CPU inference, MediaPipe-to-COCO mapping, shadow isolation, consent/purge
  races, bounded queues and event evidence tested. Full suite: **61 passed** before
  the final queue-priority change; **19 focused model tests passed** afterward,
  including transition preservation and final-result retrieval. The final TypeScript
  and Vite production build passed too.
  The model runs in shadow mode and does not change alerts. No teammate-recording
  accuracy or Vultr throughput has been established. See [ACTION_MODEL.md](ACTION_MODEL.md).
- The model panel was exercised through browser replay and inspected at 390 × 844;
  document width matched scroll width (375 pixels). The temporary viewport was reset.
- Final browser check after queue prioritization: an 8× synthetic fall retained
  a falling-class prediction from the actual transition in event detail; the
  live panel retrieved the final result after replay ended. Later stationary
  synthetic poses were assigned unrelated known action labels, illustrating why
  this pretrained model is still shadow-only. Synthetic class scores are not
  measured fall accuracy. Inference timing varies with machine load.

- Full backend suite after the October 17 adjustments: **44 passed**. Coverage includes baseline warm-up/persistence, calibration review and frozen anchor, held-out gate separation, ROI exclusion, scaled deadlines, dismissal followed by long-lie escalation, category-only Gemini input, consent revocation, deletion and no consent re-seeding. The only warning is a Starlette TestClient deprecation for its current httpx integration.
- TypeScript compilation and Vite production build passed. The build bundles the MediaPipe worker as a classic worker and serves its model and WASM locally.
- `python scripts/dev.py` started the app successfully at `http://localhost:8000` with the final backend. Existing local events and acknowledgements survived the server restart.
- Browser walkthrough: Care replay created an alert; Personal replay created a check-in; acknowledgements stopped the pending sends. A simulated phone request appeared in the event timeline. The scoped trusted-contact page displayed the persisted acknowledgement.
- Actual MediaPipe model initialization and inference succeeded on a masked blank test frame in the browser worker. The camera was not opened. This verifies the masking/runtime path, not pose accuracy on a person.
- Profile saving was exercised in the browser and produced version 2 while preserving the synthetic profile values. Comma-separated editing retained a trailing comma while typing.
- Desktop layout checked at 1280 pixels. The consent and evidence screens were checked at 390 × 844 pixels; document width and scroll width matched (375 pixels with the scrollbar), with no horizontal document overflow. The mobile navigation intentionally scrolls within its own row.
- Browser adjustment walkthrough: 8× long-lie replay reached urgency 3; its simulated downgrade was visibly blocked; rules-only and Gemini fallback panels and category payload preview appeared; acknowledgement survived another server restart. A six-day synthetic drift example was backfilled and plotted with its label. Declining capture consent displayed monitoring paused without opening the camera. The synthetic seed profile was restored for the demo afterward.
- Purge was exercised against disposable test databases, including row counts, keyframe/event removal and prevention of consent re-seeding. The user's working demo data was not purged. Real Tiger aggregate refresh is still unverified.
- Synthetic replay measurements are in `replay-results.json`. They include detector, persistence and dry-run scheduling only. The synthetic fall queues concern 0.667 seconds after scripted onset; nine seconds of normal movement produce zero events. This is not enough data to infer a real-world false-alert rate.

## Not yet verified

| Plan item | Current state | Needed next |
| --- | --- | --- |
| Real camera and staged recordings | Browser runtime and synthetic pose pipeline checked | Consenting team recordings through the local-video/camera path; measure end-to-end latency and false alerts |
| Tiger Data | PostgreSQL models, migration preflight, hypertable and aggregate SQL prepared | Connection string, actual service/version verification, migration and restart checks within the $1,000 total constraint |
| Gemini | Category-only transport, consent/billing guard, validation, evidence checks, timeout and urgency floors tested with mocks | Billed project, API key, profile consent, enabled model and actual response/latency check |
| Web push | Subscription and dispatch path prepared; local behavior tested | VAPID keys, reachable HTTPS origin and real browser receipt |
| Ring-only phone | Dry run and mocked outbound request verified | Voice credentials, explicitly supplied personal number and allowlist; verify handset ringing |
| Vultr | Container, Compose and Caddy configuration prepared | VM/domain/SSH access and Docker execution; Docker is unavailable on this workstation |
| Personalization | Median/MAD baseline, frozen anchor, current update, contribution explanations, Trends and held-out evaluation gate tested; no baseline fitted on real data | Reviewed normal recordings and independent held-out clips from two people/two angles |
| Learned action recognition | Official ST-GCN++ weights running locally in a separate CPU worker; scores/evidence visible; offline comparison script | Evaluate MediaPipe recordings, tune on independent clips, determine whether fine-tuning is necessary; benchmark the Linux/Vultr runtime before deployment |

The exact configuration locations and switches are in [SETUP.md](SETUP.md). The demo walkthrough and measurement limits are in [DEMO.md](DEMO.md). SMS is excluded as requested.
