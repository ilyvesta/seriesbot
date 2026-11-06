# Exivum Series Bot

Series catalogue bot for Telegram. Uses the existing MongoDB collections and document fields; no destructive schema migration is performed.

## Render deployment

- Runtime: Docker (repository-root `Dockerfile`).
- Set `API_ID`, `API_HASH`, `BOT_TOKEN`, `ADMINS`, `FILE_CHANNELS`, and `DATABASE_URI`.
- Set `DATABASE_NAME=seriesbot` and optionally `REQUESTS_CHANNEL`.
- Deploy from the repository root. Keep the existing MongoDB database and collections.

## Main features

Series search and indexing, season/quality navigation, missing-episode quality fallback, episode range navigation, continue watching, requests, reports, force-join, watchlist, discover, statistics, admin settings, audit and reparse.

Filenames are read by `series_parse.py` (release-group tags, hash codes and promos are never part of a show title).

## Notes

The bot requires access to the configured file channels and MongoDB. Test changes against a staging service/database before replacing a live deployment.


## Navigation fixes

Legacy anime entries indexed with season `0` are presented as Season 1 without changing stored MongoDB records. Episode-range navigation now derives its ranges directly from the indexed episode keys.

## Series maintenance commands

Safe order after an update: `/seriesaudit` → `/reparse dry` → `/reparse` → `/dedupeseries dry` → `/dedupeseries`.

- `/seriesaudit` health report: file and show counts, shows missing from the list, empty shows, biggest shows (an encoder name among them means a bad title).
- `/reparse dry` shows what would change without writing. `/reparse` re-reads every stored filename with the current parser (database only, bulk writes, pauses between batches so the bot keeps answering), then rebuilds the shows list from the files. `/stopreparse` pauses it and `/reparse resume` continues; a plain `/reparse` always starts from the beginning. `/reparse skipped` also re-checks skipped channel posts. Unreadable names are left untouched.
- `/rebuildshows` makes the shows list match the files exactly (fixes shows that vanished from search, empty shows and wrong counts).
- `/mergeshow` starts an interactive manual merge. Search for and select the incorrectly catalogued show first (the one to absorb), then search for and select the correct show (the one to keep). Confirm the pair before anything is changed. `/mergeshows dry` remains a read-only candidate report; it never merges automatically. Different years are flagged for manual-only review; punctuation/apostrophe variants can be proposed together; abbreviations and fuzzy title similarities are not treated as identity evidence. Generic device/timestamp titles are excluded.
- `/aliascheck Title One;Title Two` inspects exact title-derived alias mappings. `/aliasremove Title One;Title Two` previews removal of only those alias records; repeat with `/aliasremove confirm ...` to remove them. This does not alter files or show records.
- `/addpromo <text>`, `/delpromo <text>`, `/promos` manage channel/encoder names that appear before titles without brackets. After adding one, run `/reparse`.
- `/dedupeseries dry` previews same-size look-alikes; `/dedupeseries` removes later copies from the catalogue only (nothing is deleted from Telegram).
- Search results and browse messages are deleted after the "result_ttl_min" setting (4 minutes by default, 1 in groups).


## Maintenance commands added in this build

- `/renameshow Old exact title | New title` renames a show while retaining its stable ID, files, follows and progress. It refuses to overwrite an existing show/alias.
- `/linkmovie Exact show title` must be sent as a reply to a forwarded movie post from one of the configured series database channels. The movie appears in a separate Movies section; it is not numbered as an episode.
- Automatic channel indexing now queues compact media metadata immediately. Parsing, MongoDB writes and follower notifications run in a background batch worker. If the bounded queue ever fills, Render logs identify the channel/message and `/index` can safely recover from channel history.

Custom promo phrases treat spaces, dots, underscores and hyphens as separators between phrase words. Thus `F - D` matches `F_D_` but does not remove the `FD` suffix in `Tacoma FD`. Bare trailing anime episode numbers such as `Bleach 048.mkv` and `One Piece 836.mkv` are recognized; titles containing `Movie` or `Film` are excluded from that rule.

Legacy Turkish-drama filenames in the form `Esaret 1-@PlanetDizi.mkv` and
`Esaret 2 -@PlanetDizi.mkv` are parsed as season-0 episodes. The episode number
is captured before the Telegram handle is stripped; a four-digit year-like
number is not accepted as an episode by this rule.


## Manual show identity review and merging

- `/mergeshows dry` builds a read-only list of catalogue entries with normalized title matches. It never merges shows.
- Each candidate lists the exact catalogue SIDs. Different-year variants remain candidates for the owner to inspect.
- `/mergeshow` starts the interactive workflow: search/select the entry to absorb, then search/select the entry to keep. The second selection is the survivor and retains its display title and year. The bot asks for confirmation before merging.
- A merge affects only that explicitly named pair. For a candidate group with three or more entries, merge one pair, review the result, then decide whether another pair should be merged.
- The confirmation step rechecks that both catalogue entries still exist.

## Catalogue search and multi-show merging

- Series search keeps exact-title matches at the top but continues to show other plausible catalogue matches. If only one result exists, it opens directly; if multiple results exist, users choose from the paginated list.
- Admin merge syntax: `/mergeshow Survivor title | Absorbed title | Another absorbed title`. The first exact catalogue title survives; every later exact title is absorbed into it. Use `Title (2021)` when multiple catalogue entries share the same title but have different years. The bot displays a confirmation before writing anything. Up to 8 catalogue entries can be selected in one operation.
- A merge reassigns series file records, separately linked movie records, watchlists, progress and tagged requests to the survivor, and creates aliases for absorbed show IDs. It does not delete Telegram channel posts or automatically delete duplicate file records. Linked movie records remain in the separate Movies section.

## /mergeshow syntax

`/mergeshow Survivor | Absorbed | Absorbed` (up to 8). **Matching is literal only**: a title must equal an entry's stored title exactly (case-sensitive); there is no fuzzy or punctuation-insensitive matching and no fallback. The first title survives and keeps its title and year. A bare title that matches several entries uses the one with no year; otherwise add `(2005)` or `(no year)`, or use `sid:SID`. `/mergeshows dry` is a read-only suggestion list and is never used by the command. Confirmations expire after 10 minutes. `/aliasremove` keeps `/renameshow` aliases unless you add the word `force`.
