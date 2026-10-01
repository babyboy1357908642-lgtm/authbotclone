# Architecture

## Data flow

1. Long polling persists each Telegram update as an idempotent Mongo job before advancing the offset.
2. One update worker processes channel posts/edits, admin commands and user lookup submissions.
3. Trusted channel captions are parsed locally. The OwO format is normalized with Unicode NFKC;
   emoji are decorative. Raw captions are preserved unchanged. No AI or external recognition API exists.
4. Cards are identified by catalog + external card ID. Every Telegram photo size is registered.
5. Reference image indexing runs separately. One source image is downloaded for each new asset;
   SHA-256/pHash/dHash are stored. Repeated ready-asset indexing skips the download.
6. Lookup workers query Telegram identifiers first; the cache is bounded and versioned against DB changes.
7. On an exact miss, download the input once, try identical bytes, then compact in-memory hash arrays.
8. Near candidates require both pHash and dHash agreement; the gap is calculated between distinct cards.
   Unknown inputs are not auto-added to the trusted catalog.

## Mongo collections

| Collection | Key / purpose |
|---|---|
| cards | Deterministic SHA-256 key; unique catalog_id + external_card_id; normalized name/anime indexes |
| assets | Reference image hashes, hash/preprocessing version, indexing state |
| telegram_files | `_id = file_unique_id`; per-bot reusable file IDs; asset reference |
| card_assets | Unique card/asset pair; trusted relation and contributing sources |
| source_messages | Unique channel/message pair; raw caption, origin, parser result, candidates |
| source_channels | Numeric channel ID, enabled flag, catalog, optional label aliases |
| jobs | Durable updates/lookups/index/import/export/reindex/broadcast, leases and retries |
| import_rows | Staged validated records and conflict records |
| settings | Poll offset, collector switch, cache generation, lookup counters |
| users | Numeric user ID and explicit broadcast subscription |
| instances | Bot identity lease, one active process per bot/database |
| audit_logs | Numeric admin actor, action, target and previous card metadata |

Unparsed sources do not create incomplete cards. Thus every document in cards has a nonempty external
ID and a normal compound unique index is sufficient. The Mongo `_id` index provides exact file lookup.

## Writes and recovery

- Writes are idempotent upserts and bulk writes. This build runs on standalone MongoDB as well as Atlas;
  it does not require transactions or a replica set.
- A successful source collection can be retried after partial failure. It converges on the same card,
  files, source and links. Source edits remove obsolete source contributions while keeping other sources
  and manually imported links.
- Canonical card metadata is never silently replaced by new conflicting forwards. Deleted cards are not
  automatically reactivated. Admin review is required.
- A multi-collection mutation is not a transactional snapshot. A lookup during an edit can briefly see
  an older mapping; persistent source links/cache generation converge when the job completes or retries.
- A claimed job has a 120-second lease, renewed every 30 seconds. Unexpected process termination can
  delay resumption until expiry. Failed jobs retry with backoff, then remain visible for admin retry.
- Finished jobs expire after 7 days. Pending/failed jobs do not expire automatically. Review/resolve
  old failures and archive audit/source records according to your own retention needs.
- Import resumes from its last completed row. A row committed just before a crash may be repeated;
  upserts prevent duplicate catalog records. Conflicts remain in import_rows for inspection.
- Admin confirmation tokens live in memory for 5 minutes, tied to the requesting user/chat. After a
  restart, prepare the action again. Already queued jobs remain durable.
- Broadcast sends can repeat after an uncertain network outcome or crash between send and checkpoint.
- Railway redeploys are supported with SIGTERM cleanup and a bounded wait for the previous bot lease.
- The bot lease does not coordinate deployments using different Mongo databases. Never run the same
  Telegram bot token against two separate databases simultaneously.

## Concurrency and scale

PyMongo Async shares one connection pool per process. Lookup concurrency is configurable; indexing
and bulk tasks have separate workers. Pillow/hash computation uses asyncio.to_thread so CPU work does
not execute directly in the event loop. Hash arrays use 16 raw bytes per asset for two 64-bit hashes,
plus asset IDs and array/query overhead. Reference arrays refresh after catalog/index changes.

The first fallback after a generation change reloads the current reference hash index. Concurrent
refreshes share a lock; subsequent lookups reuse arrays. Under sustained ingestion, frequent refreshes
can cost more than steady-state searches. For much larger datasets or high query rates, split catalog
and image-index generations and adopt a dedicated candidate index after measuring workload.

Nearest candidates are bounded to 50 assets. This is approximate candidate retrieval, not a guarantee
that every visually related image is considered. Crowded datasets with many aliases/identical images
need threshold/candidate calibration. Exact file lookup has no hash-search dependency.

## Operations

- Keep one process running continuously; Telegram does not retain bot updates indefinitely.
- /sources changes are stored in Mongo. Environment channel IDs are bootstrap defaults and are re-added
  if absent on a later startup. To permanently remove a bootstrap source, also remove it from .env.
- /collector persists its global switch; the environment value only initializes a new database.
- Configure Mongo authentication/TLS for externally reachable deployments. Bundled compose exposes no
  Mongo host port and is intended for a private single-host deployment.
- Logs omit Telegram token-bearing URLs and provider data. Do not enable raw HTTP debug logging.
- --check initializes indexes/bootstrap settings, then checks Telegram identity and source membership.
- The Docker health check verifies Mongo reachability and a live bot process lease. It does not prove
  Telegram internet connectivity or successful matching; monitor /stats, /jobs and process logs too.

## Scope

This is a collector/lookup service for forwarded cards, not a user-account automation client that claims
cards from other bots. Telegram command-based admin controls are included; a separate web dashboard is
not included. Channel history scraping and arbitrary screenshot segmentation are not implemented.
