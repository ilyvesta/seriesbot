# Exivum SeriesBot v16.1

Series catalogue bot for Telegram. Uses the existing MongoDB collections and document fields; no destructive schema migration is performed.

## Render deployment

- Runtime: Docker (repository-root `Dockerfile`).
- Set `API_ID`, `API_HASH`, `BOT_TOKEN`, `ADMINS`, `FILE_CHANNELS`, and `DATABASE_URI`.
- Set `DATABASE_NAME=seriesbot` and optionally `REQUESTS_CHANNEL`.
- Deploy from the repository root. Keep the existing MongoDB database and collections.
- Use `/` for Render liveness checks and `/health` for operational monitoring. `/health` now reports 503 when indexing is degraded; restarting repeatedly cannot repair a durable reservation. See the v16.1 recovery instructions below.

## Main features

Series search and indexing, season/quality navigation, missing-episode quality fallback, episode range navigation, continue watching, requests, reports, force-join, watchlist, discover, statistics, admin settings, audit and reparse.

Filenames are read by `series_parse.py` (release-group tags, hash codes and promos are never part of a show title).

## Notes

The bot requires access to the configured file channels and MongoDB. Test changes against a staging service/database before replacing a live deployment.


## Navigation fixes

Legacy anime entries indexed with season `0` are presented as Season 1 without changing stored MongoDB records. Episode-range navigation now derives its ranges directly from the indexed episode keys.

## Series maintenance commands

Safe order after an update: `/seriesaudit` → `/reparse dry` → review the report. Do not run destructive maintenance unless its preview is understood.

- `/seriesaudit` health report: file and show counts, shows missing from the list, empty shows, biggest shows (an encoder name among them means a bad title).
- `/reparse dry` previews safe metadata refreshes and separately counts filename/title disagreements. `/reparse` may refresh episode and release metadata only when the newly parsed show title normalizes to the already-stored title. It never changes a stored show title, show ID, year, or follows an alias to move a file. Identity disagreements are held for manual review. `/stopreparse` pauses it and `/reparse resume` continues; a plain `/reparse` always starts from the beginning. `/reparse skipped` re-checks skipped channel posts. Unreadable names and title disagreements are left untouched.
- `/rebuildshows` makes the shows list match the files exactly (fixes shows that vanished from search, empty shows and wrong counts).
- `/mergeshow` starts an interactive manual merge. Search for and select the incorrectly catalogued show first (the one to absorb), then search for and select the correct show (the one to keep). Confirm the pair before anything is changed. `/mergeshows dry` remains a read-only candidate report; it never merges automatically. Different years are flagged for manual-only review; punctuation/apostrophe variants can be proposed together; abbreviations and fuzzy title similarities are not treated as identity evidence. Generic device/timestamp titles are excluded.
- `/aliascheck Title One;Title Two` inspects exact title-derived alias mappings. `/aliasremove Title One;Title Two` previews removal of only those alias records; repeat with `/aliasremove confirm ...` to remove them. This does not alter files or show records.
- `/addpromo <text>`, `/delpromo <text>`, `/promos` manage channel/encoder names that appear before titles without brackets. `Brewfiles`, `AnimeRG`, `Judas`, `F-D`, and `PlanetDizi` are protected built-in uploader labels and cannot be accepted as alias destinations. Short titles follow the same parser and indexing rules as other titles. `/shortshows` is a neutral read-only list of three-letter catalogue identities. Reparse never changes established show identity.
- `/dedupewhy Show Title SEASON EPISODE` (read-only) lists every stored copy of an episode, with exact byte sizes and catalogue entry, and says pair by pair why the dedupe did or did not treat them as one file.
- Look-alike removal never loses a better caption: the surviving record always keeps the richest caption from any copy (promo-only captions such as `Join @handle` count as empty).
- `/dedupeseries dry` previews same-size look-alikes; `/dedupeseries` removes later copies from the catalogue only (nothing is deleted from Telegram).
- Search results and browse messages are deleted after the "result_ttl_min" setting (4 minutes by default, 1 in groups).


## Maintenance commands added in this build

- `/renameshow Old exact title | New title` renames a show while retaining its stable ID, files, follows and progress. It refuses to overwrite an existing show/alias.
- `/linkmovie Exact show title` must be sent as a reply to a forwarded movie post from one of the configured series database channels. The movie appears in a separate Movies section; it is not numbered as an episode.
- Automatic channel indexing now queues compact media metadata immediately. Parsing, MongoDB writes and follower notifications run in a background batch worker. If the bounded queue ever fills, Render logs identify the channel/message and `/index` can safely recover from channel history.

Custom promo phrases treat spaces, dots, underscores and hyphens as separators between phrase words. Thus `F - D` matches `F_D_` but does not remove the `FD` suffix in `Tacoma FD`. Bare trailing anime episode numbers such as `Bleach 048.mkv` and `One Piece 836.mkv` are recognized; titles containing `Movie` or `Film` are excluded from that rule.

Legacy Turkish-drama filenames in the form `Esaret 1-@PlanetDizi.mkv`,
`Esaret 2 -@PlanetDizi.mkv`, and `Emanet 232-@PlanetDizi telegram.mp4` are
parsed as season-0 episodes. The episode number is captured before the Telegram
handle is stripped; a four-digit year-like number is not accepted as an episode
by this rule.


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

## Requests, replies and the Donate contact button

On a request message, **Reply** opens a private session: the bot messages the admin who pressed it, and the next private message they type is delivered to every requester ("Delivered to X of Y"). The word `cancel` aborts; it expires after 15 minutes. The admin must have pressed Start in the bot once. `/setcontact @username` sets where the Donate page's "Contact me" button points (`clear` falls back to the first admin's account).

## Captions and the catch-up scan

- **Caption enrichment.** When a file is indexed (and on `/reparse`), the caption is read for anything the filename lacks: resolution, source, codec, tags, audio languages and encoder. A cut-off encoder is completed from the caption (`x265-Megus` becomes `MeGusta`), and a cut-off title or episode is completed only when the caption describes the same file (same season and episode). Audio languages count only when the caption clearly states them (`Audio: Hindi, English`, a list of 2+ languages, or `[Hindi]`); subtitle languages and words inside titles are ignored. Languages appear on the file buttons, and `/audioline on|off` controls a `🔊 Hindi · English` line above the filename in delivered files.
- **Catch-up scan.** 30 minutes after the last auto-indexed post of a burst, the bot re-reads that burst's message-id range and indexes anything that slipped through (known files are ignored). It also scans once after every startup, from just before the newest message it last handled, which covers any time it was offline. It never runs while indexing/maintenance is running, and it DMs the first admin only if it recovered files. `/catchup [window]` runs it on demand (default: the latest 300 ids per channel plus anything newer; up to 5000).

## Languages, date-based shows, /skipped and /adopt

- **Other languages.** Season and episode words are recognised in German (Staffel/Folge), French (Saison/Épisode), Spanish (Temporada/Capítulo, T02E05), Italian, Portuguese, Dutch, Polish, Swedish, Turkish and Russian, also inside brackets such as `(Staffel 9, Folge 01)`. `E10E11` is read as a double episode.
- **Date-based shows** (wrestling, daily shows: `Show.2026.09.16`, `Show 31.01.2025`) become episodes of "season" = the year, labelled with the date, newest first, with no missing-episode or go-to-episode logic. One-off events without a date (UFC cards, award shows) and unnumbered specials stay skipped; `/adopt` can place them.
- **`/skipped`** opens the series skip browser: unreadable titles and same-size look-alikes (each with an "open post" link; look-alikes show the file that was kept). `/series_skipped N` is the plain-text list.
- **`/adopt [dry] CHANNEL:FROM-TO | Show title | Season N`** gives a run of unreadable skipped posts to one show, reading each episode number from the filename (`57. Eternal Leave` = episode 57). The title must match an existing show exactly, or a new show is created. Always run it with `dry` first. `/reparse skipped` re-reads every skipped post with the current parser, so improved parsing recovers files automatically.


## Revision: filename normalisation, classification and review system

### Filename pipeline
`series_parse.parse_series` normalises the filename **before** extracting episodes: (1) known uploader prefix removal, (2) obfuscation repair for known programmes, (3) number-first release groups, then promo/bracket cleaning and episode extraction. Small explicit registries live in `registries.py`; nothing is solved with a global "remove these words" list.

- **Known uploaders** (prefix only, whole prefix removed): `@Givenaway`, `@MovArchive`, `@moviezday` (the `@` is required) and `ADrama_Lovers` (optional `@`, a `_`, `.` or space must follow). `Lovers` is never a promo word, so `The Lovers`, `Lovers in Paris` and `My Lovers` are untouched. Add a new uploader by adding one `Uploader(...)` line.
- **Release group `[AI]`** (number-first anime): `[AI]08-World's End Harem`, `[AI] 1012 - One Piece` give absolute episodes 8 and 1012. The tag is only recognised as a leading bracket, so titles containing the letters AI are unaffected. The parser returns the title as written (e.g. `Strongest Sage`); map it to a longer catalogue title with the existing alias / `/mergeshow` tools - the parser never invents catalogue identities.
- **Turkish**: `Kalp_Yarası_32_Bölüm` is episode 32 (number before `Bölüm`). `..._28_Bölüm_5_Parça` / `_6_Parça` are two **file parts** (`part` field, label "Part 5") of episode 28, not episodes. A year inside the title is kept for this convention (`Survivor 2022`). Display corrections (`Uc Kurus` -> `Üç Kuruş`, `BTS In the Soop`) come from `TITLE_CORRECTIONS`; identity is unaffected because ids are computed from normalised text.
- **Multi-episode** `S01E01E02E03` = episodes 1, 2, 3 of one file; `S01E01-E03` is a range. **OVA/special**: `Show - 00 [OVA]` keeps `special=True`, label `OVA 00`.
- **Dated programmes**: `AEW.Dynamite.02.23.2022`, `AEW_Rampage_02_25_2022`, `2nd September 2024`. Month-first numeric dates are used only for US programmes (`US_DATE_CUES`); others stay day-first. Obfuscated names of registered programmes (`WW3_M0nd@y_N1ght_R@w`) are repaired to `WWE Monday Night Raw`. Quality copies of one broadcast share show + date and are separate files of one episode. Undated one-offs (e.g. "NXT Title Match Special") stay skipped; no date or number is invented.
- **Legacy numbering is not a parser rule.** `One Piece 411` stays absolute episode 411. `sabrina.412_proper_dvdrip` is held as a *manual adoption candidate* (registry `LEGACY_SSEE_TITLES`) and placed with `/adopt ... | Season 4 legacy`.

### Skipped-file classification
Every unindexed post gets a **category** and **reason code** on its skip record (`series_skips`); classification is only a label and never creates or changes a show or file.

| Category | Reason codes |
|---|---|
| Likely films (`film`) | `likely_standalone_film` (title + year + release metadata, no episode marker, not an existing show) |
| Unrecognised series | `unsupported_episode_format`, `manual_adoption_candidate` (historical skip reasons remain stored) |
| Other / ambiguous | `documentary`, `ambiguous_special`, `no_episode_marker` |
| Unreadable | `insufficient_title_information` (and `legacy_unclassified` for older records until `/reclassify`) |
| Look-alikes | `possible_duplicate` |

Commands (admins only; callbacks re-check admin rights on the server): `/skippedexport` (or the **Export everything (.txt)** button in `/skipped`) sends one plain-text file with every skip record: name, category, reason code, post link and, for look-alikes only, the kept copy and its link; read-only, nothing is removed. `/skipped` (browser: category lists, filters, 8 per page, detail view with reason code, single remove with confirmation, "remove all in list" with an explicit count confirmation), `/skipsearch [f|r|a|u|l] words`, `/skipinfo CHANNEL:MESSAGE`, `/filmreview` (the likely-films review list - there is no film catalogue), `/reclassify dry|confirm` (re-sorts labels of existing skip records; shows/files untouched).

### Retention
Skip records are **never deleted automatically**: the old 20,000-record cap and daily prune were removed, there is no TTL index, and the lists are read with skip/limit cursors (never fully loaded). A record leaves only when its post is indexed/adopted or an administrator removes it. The skip browser screens are not scheduled for chat auto-deletion; only the temporary Telegram message may be cleaned up, never the record.

### /adopt and reparse safety
- `/adopt dry CH:FROM-TO | Show | Season N [legacy]` previews (show, new-vs-existing, similar shows, episode assignments, already-catalogued files, files without a number). Without `dry` it stores the plan and shows **Confirm / Cancel**; nothing is written until Confirm, and Confirm recomputes the plan and aborts if anything changed. It only touches skip records inside the range; already-catalogued files are never overwritten. Adopted files carry `adopted=true`.
- `/reparse` still never changes show identity. It additionally skips adopted records and holds (never writes) readings that would weaken stored data: episode->pack, absolute->season numbering, lost special flag, lost multi-episode info. Re-running it is idempotent. `/reparse skipped` walks skip records with a stable keyset cursor and only refreshes labels for posts that still do not parse.

### Tests
`python -m unittest discover -s tests -v`. The suite uses an in-memory async Mongo fake (`tests/support.py`) and stubs `pymongo`/`pyrogram`/`aiohttp` only when they are not installed; with `pip install -r requirements.txt` the real packages are used for imports. No test touches a real database.


## Three-letter titles, identity repair and short all-caps titles

- **Three-letter titles.** No short-title blacklist or allow-list is used. `/allowshort` is retired (informational response only); previous settings and skips are left intact.
- **`/identities`** is the reviewed repair for the identity conflicts `/reparse` holds. It scans read-only and lists the conflicts grouped by the show each record would be indexed under today (count + 2 example raw filenames, 8 groups per page; `/identities page 2`). `/identities apply <Proposed Title>` shows a confirmation; only that group's records move after you press Confirm. Each record is re-read first: adopted files are never touched, and a reading that would weaken stored data (episode→pack, lost special/multi-episode info) is held back. The report gives planned / moved / held / skipped counts, emptied source shows, and any new same-size look-alikes. A group that targets an uploader/promo title, or has more than 500 records is refused unless the command ends with the word `force`. Add `(2021)` or `(no year)` if two groups share a title. The scan is kept in memory for 30 minutes (lost on restart); the confirmation expires after 10 minutes. Nothing is deleted from Telegram.
- **Short all-caps titles** (5 letters or fewer across the whole title: NCIS, FBI, SWAT) are kept as typed instead of being title-cased. Longer all-caps titles (`BLEACH`) and all-lowercase titles (`ncis`) are still title-cased. Shows already catalogued under a title-cased spelling keep it until you run `/renameshow Ncis | NCIS`.


## Fixes from live use (promo stripping, export, promo-prefixed shows)

- **Promo stripping order.** Site names such as `BLAXUP.COM` / `AVADL.CoM` were matched while underscores were still word characters, so `Poldark_S01E08_720p_BLAXUP.COM` was swallowed whole and read as "unreadable"; `@handle.` prefixes did the same. Handles (`@\w+`, no dots) are now removed first, then underscores become spaces, then site names are matched. Unknown leading handles are stripped in two recognised styles: dot style (`@korean_series.The.Silent.Sea.E01`, `@Media2x.Los.Internacionales.S01E07`) and underscore style (`@MoviezzClub_Big_Time_Rush_S01E01`). `@ADrama_Lovers-Search.WWW...` is a promo with no title: it stays unreadable unless the caption names the series.
- **Anime variants.** `One Piece - 0492A` / `0492B` = two parts of episode 492 (`part`, label "Part A/B"); `One Piece - 1004.5` = a special "Episode 1004.5" kept separate from episode 1004; `One Piece - Movie 14` goes to the film review list (`franchise_movie`), never to an episode.
- **/skippedexport [u|a|f|r|l|reset].** The export is now streamed to disk in numbered `.txt` parts of 15,000 records (memory is one batch), reports progress, has a hard timeout on the database pass and on every upload, always releases its lock, and a lock older than 20 minutes is replaced automatically. `/skippedexport reset` clears it manually. A category letter exports just that list.
- **/fixpromoshows dry|confirm.** Reparse never renames shows, so a show stored as `Brewfiles Band Of Brothers` stayed that way. This command proposes a rename/merge only when the title starts with a built-in/custom promo label **and** every sampled file now reads as exactly the clean title **and** none of the filenames contain the label. Apply uses the normal merge (episodes unchanged, hits kept, old show id kept as an alias). Shows that fail those checks are left for `/mergeshow`.


## Fixing held identity conflicts: /fixtitles

Reparse never renames a show, so a show whose stored title disagrees with what its files now read as stays "held". `/fixtitles` resolves them in evidence-based classes; every class is previewed first and nothing is applied unless you name the class.

- `/fixtitles dry` - counts and examples per class. Changes nothing. `/fixtitles list <class> [page]` browses a class.
- **label** - a show titled exactly like an uploader label (`Brewfiles`). Its files legitimately belong to many series, so each readable file moves to the show it reads as; unreadable files stay. Apply: `/fixtitles confirm label`.
- **contained** - the stored and newly read titles share their words or letters (`'s Beautiful Now` -> `It's Beautiful Now`, `Monarca Temporada 2 Capitulo` -> `Monarca`). Requires every sampled file to agree on one new title. Uses the normal merge (episodes kept, hits kept, old id kept as an alias). Apply: `/fixtitles confirm contained`.
- **other** - consistent evidence but unrelated titles. Review only; fix by hand with `/mergeshow`. It cannot be applied by `/fixtitles`.
- Generic device names and uploader-only labels are not legitimate show titles.
- After applying, run `/reparse` again: files whose show now matches get their metadata refreshed and the held count drops.

## Other changes in this revision

- `WeTV`, `iQIYI`, `Viki` are release metadata and `WEB_DL` is one token (no more `Where Dreams Begin WeTV DL`).
- `@TVSeries_FC.The World at war`: a handle may contain underscores when a dot or space follows it.
- Number-first anime `07 - Demon Slayer [1080p] [Sub] @Animax_Industry` is read as episode 7, but only when the name has both a resolution bracket and a sub/dub/dual bracket. A bare `14 - Transformation` stays unidentified.
- `/skippedexport` parts are now at most about 380 KB so each fits in one Telegram upload chunk (Render's connection to Telegram's upload servers drops large uploads). Each part is retried 3 times; a failed part does not stop the others, and the final message lists which parts failed.

## /dbsize
`/dbsize` (admin, read-only) shows per-collection data and index sizes, the percentage of the 512 MB Atlas M0 limit in use, and how much of the catalogue is stored captions versus filenames.


## Two databases as one (DATABASE_URI_2)

Set `DATABASE_URI_2` (and optionally `DATABASE_NAME_2`, default = `DATABASE_NAME`; `DATABASE_SWITCH_MB`, default 450) on Render to add a second MongoDB database (e.g. a second free Atlas cluster). Leave them unset and the bot behaves exactly as before.

- **Reads** look in both databases and return one combined answer (search, browsing, counts, pagination, exports, look-alike checks, reparse).
- **Writes** go to the database that already holds the document. A document that exists in neither goes to the *active* database. Nothing is written to both, so a file already in the first database is never copied into the second, and newly indexed duplicates are prevented by a shared catalogue reservation. Existing duplicate records are not automatically changed. The same-size look-alike rule works across both databases.
- **Active database** = the first until the primary cluster's logical size (all databases on it, as Atlas counts) reaches `DATABASE_SWITCH_MB`; then new documents go to the second. A write rejected with "over your space quota" flips to the second and retries once. It returns to the first only when it falls 25 MB below the limit. The choice is stored in the second database, so restarts keep it.
- `/dbswitch status|primary|secondary|auto` shows or forces where NEW documents go (reads always use both). `/dbsize` reports each database and the cluster's other databases.
- Supported operations are exactly those the bot uses; anything else raises an error instead of answering from half the data. Only `$match` + `$group` (+`$sort/$limit/$skip`) aggregates are supported.
- Limits: pre-existing duplicate IDs may exist; use `/catalogueaudit` to identify them without changing records; order of bulk writes across the two databases is not preserved; reads cost two queries.

## Conventions added after reading real skip exports

`1x1` / `1X4` / `1×46` / `1973x16`, `S01S01` (encoder typo), `S03X02`, `N серия` / `N episode` / `capítulo 08p2`, `Bölüm-3`, `slug-style-season-1-episode-3`, `#ADL.E16.END.Title.2009`, `Ep-01 [480p] Title`, `FMA OVA - 02&03`, `01_Title_Saison`, `SRV28-2022_2` (Survivor), `=ENG_SUB=<CJK>_Title_01_<CJK>`, `house.of.lies.409.hdtv-lol` (SSEE only with hdtv/pdtv/dsr; `dvdrip` and anime stay manual/absolute), WWE decorated names (`📂 WWE Monday Night Raw #1624 (08.07.2024) …`, day/month decided by the programme's air weekday), `One Piece - 0492A/B` (parts), `1004.5` (special). Subtitles/audio/images (`.srt`, `.flac` ...) are never indexed and are labelled `non_video_file`; bloopers/highlights/interviews are `extras_or_featurette`; device names (`video_2026-11-11_00-11-23`) are unreadable.

New admin tools: `/skipruns [page]` (runs of same-shape consecutive skipped posts with a ready `/adopt dry` command - you only supply the show title), `/skipclear <code>` (bulk-remove records of `non_video_file`, `extras_or_featurette` or `insufficient_title_information` after an explicit count confirmation), `/adopt` now refuses subtitle/archive files.


## Third review: formats, WWE, delivery, runs, pages

- **More formats.** `S4.01. Title [720p Dual] @Handle`, `S2- 5 Title [720p]`, uploaders that put the marker first (`@DramaOST.E01.Title.480p.WEBDL.viki`, `[TIF]_S01_E01_Title`, `[YDF] E01 Title`: compact `E<number> Title` is recognised directly, with or without an uploader; bare `Episode 01 - Title` still requires other evidence), `ss03e01`, `[848x480] Ver Capitulo 28 Show Online Gratis`, `=ENG_SUB=<CJK>_Title_01_<cast>`, `...mp4_PART_00001` / ` PART-2` / `.part001` (file parts), a known release group after the tech tokens (`... 01 1080p x265 pseudo`). Codecs `x264/x265` are never episodes. Titles with a digit (`The 100`, `NOS4`) are no longer treated as three-letter fragments; plain three-letter titles (`MSW`, `PyP`, `OMB`) no longer need `/allowshort`.
- **WWE is one show.** Every WWE programme (`Monday Night Raw`, `SmackDown`, `NXT`, ...) is an episode of the show `WWE`; the programme and date ride in the episode title. `/wwemerge dry|confirm` folds the existing `WWE ...` shows into it (then `/reparse`).
- **Specials.** Episode 00 / S01E00 and `Show - Movie N` are indexed as specials of the show and appear under the season's Specials button. Subtitle files with a show and episode are indexed again and shown on the season's subtitles screen (v5 had stopped that; reverted).
- **Richer copy wins.** When two copies are the same file (same show, episode, size, resolution, codec) the copy whose name/caption carries more information improves the stored record. The stored post, ids and look-alike bookkeeping do not change.
- **Caption transfer.** The source post's caption lines travel with the delivered file in monospace, after links, `@handles` and promo/"join" lines are removed (Settings > Caption transfer).
- **Groups.** Search results in a group are never replaced by the join message; the join check happens in the DM when a file is chosen. Someone who already started the bot and joined the channels gets the file in their DM with a "File sent to your DM" toast and no redirect; anyone else is sent to the bot once. Result messages delete after their own group timer (default 7 minutes, Settings > Group results).
- **Joining.** A pending join request counts as joined (stored, survives restarts). When someone joins a force-join channel the bot notices at once and carries on with what they asked for in their DM.
- **Images.** `/preview [welcome|discover|about|help|donate|survey|mylist]` now shows the chosen page with its own image (it used to always show the welcome page).
- **Settings** shows all delivery, group, join, catalogue and database options with a live summary.
- **Pages.** The skipped browser has First / Last / Random / -10 / +10, and opening or removing a record returns to the same list and page. "Find missing episodes" returns to the page you were on. Admins get "episodes missing at every resolution" on each season (all resolutions combined, with request buttons).
- **Tools.** `/reparse skipped` re-reads ONLY the skipped records (names are judged locally; only newly readable posts are fetched); `/reparse all skipped` does both. `/singleshows [page|export]` lists shows with exactly one file and that file's name. `/autoruns dry|confirm` adopts runs whose files all carry the same show name; `/skipruns` pre-fills that title.

## Series digest control panel (manual)

The digest is no longer sent automatically. Open it from `/seriesdigest`:

- **Preview & send digest** builds what would go out (shows with newly indexed episodes since the last cutoff that are Digest Shows or 2026+), newest first. Tap a number to select a show, then **Top / Up / Down / Drop**. **Add / swap in** lists dropped shows and other shows with new files: when the digest is full the one you pick replaces the selected show, otherwise it goes on top. **Send to channel** posts it.
- The post has no date: the newest show is first (that is what a notification previews) and the caption is the **footer line at the bottom** (`Footer text` to change, `none` to remove).
- **Shows per digest** caps the list (default 15, max 30).
- Only each show's **newest season** is listed, and only episodes newer than what the show already had before the window. Back-filled older seasons or older episodes never appear; a show with nothing newsworthy is left out.
- **Edit last post** reopens the sent post with shows indexed since marked 🆕 on top. Reorder or drop as before, then **Update the post**: the same Telegram message is edited, and the cutoff moves to that moment, so anything indexed after it goes in the next digest.
- **Skip this window** moves the cutoff without posting (for example after a bulk-indexing day).
- Show titles in the post link to the show in the bot. Set `series_digest_auto` to `true` in settings only if you ever want the old automatic 10:10 send back.

## Dedupe: one code path, with reasons

`/dedupeseries`, `/dedupeseries dry`, the post-merge cleanup and `/dedupewhy` all use the same per-show routine (`dedupe_show`): the files of a show are grouped by exact byte size + season + episodes + special/part, then compared with the compatibility rule. Nothing depends on a stored key or on paging through the whole collection, and the list of shows comes from the files themselves.

- Two copies are the same file when the bytes, show, episode and part match and nothing contradicts it (resolution, codec family, source, audio languages, dubbed vs subtitled). A different file **extension** no longer keeps them apart: identical bytes under an `.mp4` and an `.mkv` name are one file.
- The report lists, for same-size files that were deliberately kept as separate variants, how many were kept apart and why (for example `different resolution ×12`). With `0` removed, it also points you to `/dedupewhy Show SEASON EPISODE`.



## v15.1 corrective release

- `/episodepreview` is an admin-only read-only preview of the formerly unsupported `E<number> Title` convention. Existing records are not adopted at startup.
- `/catalogueaudit` checks the two databases without moving, merging or deleting records. It reports duplicate identities and look-alikes, potentially duplicated shows, conflicting aliases and manual titles, unresolved file/show links, and major collection data/index sizes. The scan is batched and can take time on a large catalogue. It is not a consistent transaction snapshot.
- Catalogue identity mutations share a reservation document in the existing secondary `_federation` collection. No additional collection is needed. All running SeriesBot workers must use v15.1; stop any old instances before deploying. In the event of an interrupted process leaving `catalogue_write` behind, stop all writers, confirm the owning process is no longer running, and only then have an authorised database administrator remove that one reservation document. Never remove an active reservation. Failure to acquire is fail-closed.
- `/dbsize` reports per-database and combined logical sizes and visible connection totals. Different usernames do not establish separate Atlas clusters. If the two application databases share the same cluster they share one 512 MiB M0 quota. The existing 450 MiB routing threshold and hysteresis are unchanged; routing is not a capacity solution for a shared cluster. Inspect the two Atlas connection hosts and project/cluster identity before assuming separate capacity.
- Media inspection uses authoritative container language and duration fields, with FFprobe only supplementing video attributes. Unknown languages remain Unknown. No full-file fallback is permitted; small files that cannot be partially inspected are reported unavailable.
- Deployment: replace the existing SeriesBot repository files with the contents of this ZIP, keep current Render configuration and existing MongoDB databases, stop any other indexing workers, then deploy the existing Docker service. Do not run a full reparse or an automatic adoption. Check `/health`, `/dbsize` and the read-only previews.

## v15.2 — Media Information connection recovery

- The complete inspection has a 90-second deadline, including Telegram message lookup, media retrieval, retries and probing. Each chunk attempt has a maximum of 55 seconds (subject to the overall deadline). At most two retries per chunk are allowed for transient failures; each waits one second and consumes the same shared request budget. Permanent errors are not retried.
- The inspector starts with one 1 MiB header chunk. If more metadata is needed, it extends the head or the MP4 tail one chunk at a time, never re-fetching a successfully retrieved chunk. A maximum of 12 head and 12 tail chunks may be requested, subject to the combined budget and leaving at least one original chunk unread. No full-file fallback.
- The 24 MiB ceiling is an application-level request budget: each attempted chunk costs 1 MiB, including failed attempts and the shorter final chunk. Kurigram's own internal MTProto retry traffic is not exposed through `stream_media`, so this is not a hard network-wire byte counter. File size itself is not limited to 24 MiB.
- One inspection may run at a time. The user is notified that inspection may take up to 90 seconds. Render logs under `seriesbot.mediainfo` now show the stage (`head`, `tail`, `container probe`), chunk offset, attempt, time, requested/received bytes and final outcome. A `transport failure` or retrieval `timeout` indicates the media connection failed; `metadata unavailable` indicates the bounded inspection did not locate usable metadata.
- No new environment variables, dependencies, database changes or hosting requirements. Deploy over v15.1 using the same Render service; no reparse is needed. Try the same 512 MB and 47 MB files after deployment. If either still fails, provide the `media info start` through `media info end` log entries and adjacent Pyrogram warnings.

## v15.3 — Smaller Telegram ranges, language labels and file footer

- Media Information prefers 128 KiB aligned `upload.GetFile` requests using the existing pinned Kurigram 2.2.25 session API, with one SDK attempt per request, an 18-second RPC timeout and a 35-second per-attempt ceiling. A transient failure switches the next attempt to the regular endpoint of the same DC (or back to the media endpoint); a successful route is retained for the rest of that inspection. No session reset or separate streaming service is required. The older bounded 1 MiB stream implementation remains for clients without the range API. Unexpected redirects are not followed by the raw reader. Live Telegram support for the alternate endpoint and smaller ranges must be verified after deployment.
- The 90-second overall deadline, single-flight guard, 24 MiB application request budget (including failed attempts), 12 MiB limit per head/tail region and no-full-download policy are unchanged. With the smaller raw ranges, metadata can sometimes be read with just 128 KiB. This is not a network-wire byte meter; SDK protocol-level traffic is not included. Logs identify transport, DC, endpoint type and requested range.
- MKV track language priority: explicit modern tag, otherwise legacy tag, then an unambiguous embedded track name (e.g. English or English (SDH)). Never use a filename or an implicit English default. Track lists no longer have a fixed 16-subtitle/12-audio cap; exceptionally large lists are bounded for Telegram's message limit and report omitted rows.
- `/setfooter` now applies the existing saved setting to newly delivered series-file captions. Reply to formatted text or send `/setfooter Your text`; `/setfooter off` removes it. Clean-file supporters are exempt. New footer inputs are limited to 600 HTML characters to preserve filename space. Old oversized values are rendered as bounded plain text. Existing messages are not edited. Digest footer settings remain separate.
- No changes to database schema, configuration, dependencies, hosting or indexing. Stop other old workers and deploy the complete v15.3 ZIP through the existing GitHub/Render workflow; no reparse is needed. Test the failing file again and check `seriesbot.mediainfo` logs for `transport=range`, `route=media`/`route=regular`, and the final received/requested byte counts.

## v15.4 media update

- Raw Telegram ranges start at 128 KiB and grow to aligned requests up to 1 MiB; failed larger requests shrink and switch same-DC endpoint.
- MP4 top-level atom offsets and MKV element sizes / validated SeekHead targets avoid sequential media payload retrieval.
- 120-second overall deadline; 24 MiB application request budget including retries; one active inspection; no full-file fallback.
- Verified complete MKV tracks can be returned if duration retrieval fails. Duration is explicitly unavailable when not verified.
- Legacy stream fallback remains unchanged apart from deadline. No new dependencies, environment variables or database changes.
- Tests use simulated Telegram sessions; latency regression estimates 2.5 seconds per request rather than waiting on real network. Live Telegram and Docker not verified. No incremental progress UI added; acknowledgement reflects new deadline.
- Targeted suite: 36 passed. Full suite did not finish within the execution window.


## Supporter and Media Information update

- Home and Donate always offer Supporters Wall, even when empty. Active supporters may opt in/out; the wall uses their Telegram display name, not payment details. Existing Mongo query cap of 100 names remains; pages show 10 names. Group search replies identify active supporters with a star and their display name; this does not modify Telegram user tags. Wall consent and group recognition are separate.
- `/supporters` opens the wall; `/request` opens a supporter-only custom request prompt; `/donationclaim` opens the user's pending claim. Reply specifically to the bot-authored prompt. Ordinary private text remains search text, even with a pending claim. Ignore the prompt to stop the workflow; old prompts may still be replied to explicitly. Donor/admin correspondence is only relayed when explicitly addressed to an authorized prompt (or an existing admin claim card). No automatic approval.
- `/request`: one title per line, max 20 per batch, 80 characters per title; additional batches allowed without a daily quota for active supporters. Existing request storage, duplicate detection and admin review actions are used. Series users can use Season N Episode N or S01E02. Regular users retain the existing request path. Admin title text is copyable monospace.
- `/donor claims` lists up to 20 pending claims; `/donor claim CLAIM_ID` opens a specific claim for review. Verify the payment independently; historical submitted flags may not mean a receipt was delivered. A donor can send proof again as an explicit follow-up using `/donationclaim`. No existing records automatically resolved, migrated or deleted.
- Media report uses existing retrieved data only. MKV omitted language is marked English (container default), explicit undefined remains Unknown unless an unambiguous track name identifies a language; MP4 unspecified remains Unknown. Duration seconds, average bitrate, audio channels/sample rate, codec/track names, optional flags, aspect/bit depth and container technical details are shown only when available. Existing range retrieval, fallback probe behavior, budget (24 MiB), 120s timeout and single inspection are unchanged. No extra probing or downloads. No new packages, collections or environment variables.
- Deploy from repository root using existing Dockerfile, configuration and database(s). No catalogue reparse needed. Check wall opt-in/out, normal search with a pending claim, explicit donor follow-up, custom requests, supporter group reply and media info after deployment. Live Telegram, payment settlement, Atlas and Docker integration were not verified locally.

## v16 — Anime announcements and independent force-join profiles

**Opt-in.** Upgrading does not create announcements for existing records, change general force join, or require a reparse. No extra bot, hosting service, binary image storage, dependency or environment variable is needed.

### Setup and publish an episode

1. In the existing bot's private admin chat, open `/anime`. Select **Add show**, reply to the setup prompt with the catalogue title, and choose the exact matching show (including its stable ID). Alternatively use `/anime add SID` with the 12-character catalogue ID. Similar names are never mapped automatically.
2. In the show's settings, choose **Destination** and reply with the channel's numeric ID (private channels) or `@username` (public channels). Add the bot as an administrator with **Post Messages** permission first. The bot validates channel type, access and posting rights before saving. It revalidates the destination before publishing.
3. Choose **Image**, then reply to the specific prompt with a Telegram photo (landscape, square or portrait; send as a photo, not a document). The bot stores only its `file_id`. You can replace the photo at any time. Use **Caption template**, **Description**, **Cour**, and **Arc** to customize plain-text captions. `reset` restores the default template or clears optional text. Configure cour/arc labels in full, e.g. `Cour 2` or `Final Arc`. No missing season is fabricated.
4. Choose **Enable**. The show must have a validated destination. An image can be added later, but an announcement cannot be published without it. Only genuinely new single-episode files delivered through the live automatic-indexing worker create candidates. Manual `/index`, catch-up (including startup recovery), reparse, adopt and existing records never trigger historical announcements. Use the manual queue for any missed episode.
5. Open `/anime queue` (or **Announcement queue** in `/anime`). Select the candidate. The bot sends the actual proposed photo, caption and episode URL button to your private chat, followed by controls with the destination, exact URL and status. **Send announcement** is explicit approval. Missing image, disabled show, removed episode, changed destination or a stale preview prevents sending.
6. To queue an already indexed episode manually use `/anime queue SID SEASON EPISODE` or **Queue episode** in show settings. Use the exact *stored* season (0 for unnumbered/absolute numbering), and the actual episode number. Explicitly enumerated multi-episode files can be selected manually, but packs, specials and incomplete parts are not automatically mapped or offered as standalone episode versions.
7. **Publication history** includes sent and cancelled records. **Reannounce** and **Restore** require confirmation and create a fresh pending revision, not an immediate send. **Change destination** on a pending candidate changes the show's destination for future announcements, creates or selects that episode's record for the new channel, and cancels the previous candidate; a newly created candidate preserves its caption/image overrides. An existing target record keeps its state, including sent/review. Other pending episodes at the old destination stay pending but cannot publish until explicitly moved or cancelled. Removing a configuration never removes catalogue files or history.

Caption templates accept only `{title}`, `{season}`, `{episode}`, `{episode_line}`, `{cour}`, `{arc}`, `{description}`. `{episode_line}` is recommended because it omits unknown seasons. Captions are escaped and checked against the 1024-character Telegram limit. A per-candidate caption override is complete plain text. No HTML markup or arbitrary Python expressions are accepted in a template.

### Independent force join

- `/forcesub list` (or `/forcesub general list`) shows your unchanged general profile. Existing commands retain their meaning.
- `/forcesub anime list` shows the anime profile.
- `/forcesub anime add CHANNEL [invite link]` adds or updates a channel.
- `/forcesub anime remove CHANNEL` removes by ID, username or listed number.
- `/forcesub anime replace OLD NEW [invite link]` validates the replacement before saving; the general profile also supports `replace`.
- `/forcesub anime off` clears only anime force join. Each profile has up to four channels. Admin permissions, invite link generation, pending join requests, auto-approval, the membership cache, supporter bypass and the existing fail-open behaviour when a membership check is unavailable are preserved.

Only `ae_<sid>_<season>_<episode>` start payloads use the anime profile. The exact episode menu shows all valid stored versions, paginated if needed. It uses the original catalogue and delivery routine; its buttons are bound to the message, owner, episode and anime profile, and recheck that profile before file delivery. It intentionally does not navigate to unrelated shows/seasons. Old links (`q_`, `show_`, `s_`, `sm_`, `f_`), manually typed searches and the normal home route stay general. As in v15.5, ordinary `/start` shows the home screen; general join checks occur at the existing search/file-entry points. A user can have different anime and general messages open at once; no permanent anime classification is stored. Anime join prompts expire after the existing five-minute pending interval, and message-scoped sessions have bounded lifetime/capacity; reopen the announcement after expiry or restart.

### Operational notes

- Configuration is stored in `settings.main.anime_announcements` (up to 30 shows) and `settings.main.anime_force_subs`. General `force_subs` is not overwritten. One small document per show/season/episode/destination uses the already-existing `series_maintenance` namespace with an `anime:` ID and `record_type: anime_announcement`. No third database or new application collection is introduced. Federation routes existing record writes to their owning database, and new identity upserts use its existing catalogue reservation. The queue is read only when an admin opens it; there is no new polling task.
- Before a send the bot persists `review` as a durable uncertainty barrier. Success records Telegram's returned message ID and timestamp. Timeouts, crashes or unsuccessful recording remain `review` (or `sent` if the result write actually succeeded). Never blindly retry: open the destination and check whether the post exists. Use **Mark published** if found or **Review and retry** if absent; the latter only reopens the candidate, requiring another explicit approval. Manual confirmation without a returned message ID records a confirmation time, not an invented original send time. The last previous message ID/time is retained when explicitly reannouncing. Publication RPC has a 40-second application deadline.
- If live candidate creation fails after the catalogue insertion, the file remains indexed; the Render log identifies the show and you can recover it with `/anime queue SID SEASON EPISODE`. A merged/deleted show ID is not guessed or redirected; configure its surviving ID explicitly. Run one bot instance as before; do not run two competing production workers with different settings caches.
- Deployment: replace the SeriesBot repository with the ZIP contents at repository root; keep the existing Dockerfile, env vars, databases and same bot token. Test with a private staging destination and a normal Telegram account before enabling the live channels. Existing FilmBot and Media Information are unchanged.


## v16.1 — Federation reliability and indexing recovery

### Recover the reservation left by an old deployment

`E11000 ... _federation ... catalogue_write` means a catalogue write reservation exists, **not** that a media file is a duplicate. The reservation lives in the configured secondary database. It is deliberately not a time-expiring lease: an old or unresponsive worker may still have an in-flight write. Removing a live reservation can permit conflicting writes to the two databases.

1. Stop all old SeriesBot processes (including any other services or local instances) that use the same databases. Wait for the old deployment to terminate and for in-flight MongoDB operations to finish. Do not deploy old and new writers together. Deploy the new ZIP to the same service. The new instance can read the catalogue and accept admin commands even if the old reservation remains.
2. In the bot's private admin chat run `/lockstatus`. It shows the exact reservation owner, worker identity, last activity and suggested recovery command. Legacy reservations show an unknown worker. A quiet heartbeat is not proof that an owner is dead.
3. Only after independently confirming **every other writer has stopped**, copy the exact owner command shown: `/lockrecover OWNER stopped`. The command refuses a reservation held by this process, one with activity in the last 120 seconds, a mismatched owner or a reservation changed during verification. The comparison and deletion target just the observed reservation; no catalogue or federation state is deleted. A recent reservation can be checked again once quiet. If a forced interruption happened in the current process, stop it and restart before reviewing its reservation. If an old reservation has malformed metadata, use the prior Atlas recovery procedure only after stopping all writers.
4. Run `/lockstatus` again. It may show a new, legitimate reservation while automatic indexing resumes; do not clear that one. `/indexretry` replays up to three due or backed-off saved batches on demand once the catalogue is available. The existing catch-up worker also replays up to three due batches per minute when idle (first pass after startup warm-up). Known files are deduplicated.
5. The new journal cannot reconstruct v16 failures that occurred before this release. For the reported 27 November 2026 log, run `/index -1004363838987 597263 597285` once the reservation is clear. Extend the range if additional failures are present. No full reparse or migration is needed. Verify the new logs report successful indexing.

### Commands (private admin chat)

- `/lockstatus` — read-only reservation details and pending recovery counts.
- `/lockrecover OWNER stopped` — explicit stopped-worker recovery, never automatic expiry. Do not use on a live worker.
- `/indexretry list` — first ten journal batches, channel and message ranges, missing-review IDs. A listed min–max is a summary, not a claim that every ID in that interval was a failed file.
- `/indexretry` — replay up to three saved batches; normal recovery excludes missing-review entries. The command overrides temporary backoff.
- `/indexretry review` — also try previously unavailable messages. If Telegram cannot fetch a post or it no longer has media, its reference remains in review. If a channel fetch raises an error, keep the row and back off five minutes, allowing other batches through.
- `/indexretry acknowledge REVIEW_ID confirm` — only after reviewing a permanently unavailable post, explicitly dismiss that missing-review journal entry. This removes a recovery reference, not a catalogue record or Telegram file. Pending, non-review entries cannot be dismissed with this command.

### Operation and limits

Each automatic worker batch (at most 100 queued posts) first writes a small ID-only journal in the existing `series_maintenance` collection, pinned to the secondary database when federation is enabled. The prefix `index-recovery:` and record type `index_recovery_v161` isolate it from anime publication records and other maintenance state. It contains only message/channel IDs and recovery metadata. The journal is removed after successful ingestion, including durable skips. If recording fails, the current batch stays in memory until recording succeeds. If a reservation is busy, the batch is retained and the worker backs off. Other transient errors have four attempts before journal recovery takes over. Overflow references are journalled when possible. Recovery never creates historical anime announcements; use `/anime queue SID SEASON EPISODE` manually. Normal live indexing still creates candidates from genuinely new records only.

The reservation now records a process UUID, host/PID and heartbeat (30 seconds while held). A completed operation can retry its own failed lock release. Graceful shutdown gives the queue a short drain opportunity, stops Telegram input, waits for cancelled tasks and attempts to journal remaining queued references. A forced cancellation inside a mutation retains the reservation for stopped-worker review rather than risking another writer starting while MongoDB finishes an uncertain operation. Catch-up failures are rescheduled, bounded scans no longer stop at an empty gap when an explicit ending ID is supplied, and the saved watermark is held back while a known catch-up range or unrecorded overflow remains.

The `/health` JSON includes `indexing_ok`, `indexing_error`, `index_recovery_batches`, `index_unrecorded_channels`, `index_journal_check_ok`, `index_batch_active` and `index_catchup_failed_channels`. HTTP 503 means an operational problem, including journal backlog or review items; `/` remains a simple liveness endpoint so a durable lock does not cause a restart loop. The startup message and heartbeat expose indexing problems; auto-index failure alerts are throttled to once every 15 minutes.

No new databases, collections, environment variables, services, dependencies or polling workers. Normal live batches add one small journal upsert and one delete (plus a heartbeat only for long-lived reservations). Journal size depends on the unresolved backlog; a batch of 100 channel/message references is on the order of a few KiB of BSON, before Atlas index/storage overhead. When both MongoDB and the process fail before a queued item can be journalled, exact durability cannot be guaranteed; source channel catch-up and manual `/index` remain necessary. This is not an exactly-once distributed transaction, nor a guarantee that an old worker is dead. Deploy one active writer only. Production Telegram, Atlas and Render shutdown timing must be verified after deployment.

## v16.2 — Image flexibility, clean Media Information and query reuse

- Any Telegram photo orientation/aspect ratio is accepted. Use a direct reply to the image setup prompt. Telegram document IDs cannot be reused as photo IDs; send through the photo picker instead. No file download, conversion or resize is added by the bot. Existing stored image IDs are retained.
- Preview reports missing photos, disabled shows and destination mismatches separately. A configured candidate override still applies only to that candidate; the show-level Image button sets the reusable default.
- Raw audio/subtitle track names and arbitrary muxing/writing application strings are no longer shown. Track labels still participate in existing conservative language detection; known codec labels, languages, numeric technical properties, flags and creation timestamps remain visible. Unrecognized codec IDs are displayed as Unknown codec instead of arbitrary text. Actual filename is unchanged, including any original filename branding. The media transport, 24 MiB budget, 120-second deadline and single-flight guard are unchanged.
- Small multi-result searches reuse their first page for the session snapshot. Large searches retain their full bounded snapshot. Linked movie lists use the existing 15-minute, 1,000-entry catalogue cache, invalidated on writes and reassignment. Empty force-join profiles skip the unnecessary supporter-bypass lookup; nonempty profiles retain the existing checks and cache. No additional DB collection, environment variable, dependency or background worker.
- Performance review: federated search counts and sorted cursors query both databases; live indexing invalidates search pages/counts; search statistics and user progress/watchlist operations add database round trips. v16.1 journal writes and reservation checks protect recovery and were not removed. Live latency has not been measured; the query reductions are local regression results, not a production speed guarantee.
- Deploy normally over v16.1, with no reparse or migration. Live Telegram and Atlas tests remain unverified. See CHANGELOG-v16.2.md.
