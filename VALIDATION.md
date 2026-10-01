# Validation — 2026-10-01

## Passed

- Python 3.12.14 with pinned runtime/development dependencies; `pip check` passed.
- **49 tests passed** against real local MongoDB 8.0 and mocked Telegram HTTP transport.
- The exact user-supplied OwO/Yoru caption, alternate emoji, Unicode styled letters, one-line/multiline
  captions, leading-zero IDs, malformed captions and optional theme/value handling are covered.
- Collection idempotence, allowlist/collector switches, catalog isolation, shared-image ambiguity,
  source edits, metadata conflict protection, no-download exact lookup and cache invalidation are covered.
- Reference indexing, resized/compressed images, uniform-border crops, input limits and concurrent
  duplicate lookup downloads are covered.
- Import/export roundtrip, import resume, admin callback authorization, soft delete, opt-in broadcasts,
  paused jobs, instance leases, health checks and webhook failure cleanup are covered.
- Full synthetic flow: Telegram long-poll response → persistent queue → channel collector → MongoDB →
  user photo lookup → outgoing Telegram response. No real Telegram account was contacted by this test.
- `ruff check src tests scripts` and `ruff format --check src tests scripts` passed.
- Railway config validated against the official Railway JSON schema; one replica, sleeping disabled,
  Dockerfile builder, no HTTP healthcheck. The six-field simple env template was validated.
- Railway lifecycle: subprocess SIGTERM test confirms cleanup and successful exit.
- Wheel build passed: `waifu_card_bot-1.0.0-py3-none-any.whl`.
- Docker image build passed. The non-root container ran with a read-only filesystem and /tmp tmpfs;
  supplied-caption parsing and real MongoDB index initialization passed inside the container.
- Container CLI `waifu-bot --help` passed.
- Docker Compose configuration validation passed using `.env.example` without resolving live secrets.

## Synthetic scale check

Command: `TEST_MONGO_URI=mongodb://127.0.0.1:27019 python scripts/benchmark.py`

| Check | Observed |
|---|---:|
| Synthetic catalog size | 100,000 cards |
| Exact lookup samples | 100 |
| Database exact lookup p50 | 0.918 ms |
| Database exact lookup p95 | 1.285 ms |
| 100,000-asset hash scan p50 | 8.891 ms |
| 100,000-asset hash scan p95 | 9.596 ms |
| Two compact hash arrays | 1,600,000 bytes (IDs/overhead excluded) |

Mongo explain inspected one document for each of the tested file, asset-link and card queries using
indexes. These are local synthetic measurements, **not a production SLA**. They exclude Telegram
network time, image download/decode, index loading, concurrent users and real-card accuracy. The full
report is in `docs/benchmark-result.json`.

## Not verified with live services

A real Railway deployment was not performed. Railway account access and user-provided Telegram/MongoDB
variables are required. Docker build, container smoke, Railway schema and lifecycle tests passed locally.


A real bot token, admin identity and source channel were not supplied, so live Telegram authentication,
forward delivery, send permissions and real user end-to-end operation remain to be checked after
configuration. The available emulator catalog has no Telegram Bot API implementation. HTTP fixtures
cover transport contracts; they do not prove provider availability or channel permissions.

No AI service is used. Recognition accuracy across a real card catalog and arbitrary screenshots has
not been measured. Thresholds must be calibrated with your own cards; ambiguous or unknown results
are reported explicitly rather than treated as confirmed editions.

## Reproduce

See README.md for locked dependency installation, local MongoDB tests and `waifu-bot --check`.
