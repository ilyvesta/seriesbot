# Exivum Series Bot

Series catalogue bot for Telegram. Uses the existing MongoDB collections and document fields; no destructive schema migration is performed.

## Render deployment

- Runtime: Docker (repository-root `Dockerfile`).
- Set `API_ID`, `API_HASH`, `BOT_TOKEN`, `ADMINS`, `FILE_CHANNELS`, and `DATABASE_URI`.
- Set `DATABASE_NAME=seriesbot` and optionally `REQUESTS_CHANNEL`.
- Deploy from the repository root. Keep the existing MongoDB database and collections.

## Main features

Series search and indexing, season/quality navigation, missing-episode quality fallback, episode range navigation, continue watching, requests, reports, force-join, watchlist, discover, statistics, admin settings, audit and reparse.

`/reparse` re-reads stored filenames and checks saved skipped channel posts in batches. It does not fetch already indexed Telegram messages. Stored-file scanning uses short keyset-paginated queries rather than one long-lived MongoDB cursor, and saves a resume checkpoint after each batch. Use `/stopreparse` to stop safely after the current batch; run `/reparse` again to resume.

## Notes

The bot requires access to the configured file channels and MongoDB. Test changes against a staging service/database before replacing a live deployment.


## Navigation fixes

Legacy anime entries indexed with season `0` are presented as Season 1 without changing stored MongoDB records. Episode-range navigation now derives its ranges directly from the indexed episode keys.

## Series maintenance commands

- `/mergeshows dry` previews automatic merges based on cleaned filename/caption titles; `/mergeshows` applies them. Use `/stopmergeshows` to stop after the current safe boundary. Reparse and show merging cannot run concurrently.
- `/mergeshow` starts an admin-guided wrong-show → real-show merge.
- `/addpromo <text>`, `/delpromo <text>`, and `/promos` manage custom filename/caption promo tokens. After adding a token, run `/mergeshows dry` and then `/mergeshows` if the preview is correct.
- `/dedupeseries dry` previews same-size look-alike catalog duplicates; `/dedupeseries` removes later duplicates from the catalogue only. Telegram channel posts are never deleted.

Search and navigation keep per-message result state, and episode lists are file-based so multi-episode uploads appear once.
