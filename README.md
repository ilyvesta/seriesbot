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

Safe order after an update: `/seriesaudit` → `/reparse dry` → review the report. Do not run destructive maintenance unless its preview is understood.

- `/seriesaudit` health report: file and show counts, shows missing from the list, empty shows, biggest shows (an encoder name among them means a bad title).
- `/reparse dry` previews safe metadata refreshes and separately counts filename/title disagreements. `/reparse` may refresh episode and release metadata only when the newly parsed show title normalizes to the already-stored title. It never changes a stored show title, show ID, year, or follows an alias to move a file. Identity disagreements are held for manual review. `/stopreparse` pauses it and `/reparse resume` continues; a plain `/reparse` always starts from the beginning. `/reparse skipped` re-checks skipped channel posts. Unreadable names and title disagreements are left untouched.
- `/rebuildshows` makes the shows list match the files exactly (fixes shows that vanished from search, empty shows and wrong counts).
- `/mergeshow` starts an interactive manual merge. Search for and select the incorrectly catalogued show first (the one to absorb), then search for and select the correct show (the one to keep). Confirm the pair before anything is changed. `/mergeshows dry` remains a read-only candidate report; it never merges automatically. Different years are flagged for manual-only review; punctuation/apostrophe variants can be proposed together; abbreviations and fuzzy title similarities are not treated as identity evidence. Generic device/timestamp titles are excluded.
- `/aliascheck Title One;Title Two` inspects exact title-derived alias mappings. `/aliasremove Title One;Title Two` previews removal of only those alias records; repeat with `/aliasremove confirm ...` to remove them. This does not alter files or show records.
- `/addpromo <text>`, `/delpromo <text>`, `/promos` manage channel/encoder names that appear before titles without brackets. `Brewfiles`, `AnimeRG`, `Judas`, `F-D`, and `PlanetDizi` are protected built-in uploader labels and cannot be accepted as alias destinations. Suspicious one-word three-letter titles (for example `Emt`, `Fhb`, `Vts`) are not automatically indexed or accepted as alias destinations; a small allow-list preserves common genuine acronyms such as FBI, CSI and SNL. `/shortshows` is a read-only report of existing suspicious three-letter catalogue identities. Reparse never changes established show identity.
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
| Unrecognised series | `unsupported_episode_format`, `manual_adoption_candidate`, `suspicious_short_title` |
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

- **Three-letter titles.** A single three-letter title is a suspicious fragment (`Emt`, `Fhb`, `Vts`) unless it is on the allow-list, which is the built-in list (`registries.BUILTIN_SHORT_TITLES`: ordinary words such as You, Zoo, Sky, first names such as Ted and Ray, and acronyms such as FBI, CSI, SNL) merged with an admin list. Function words (The, And, For) and release tokens (Web, Raw, Dub, Rip) are deliberately not on the built-in list. `/allowshort Word [Word...]` adds to the admin list (stored in the `short_allow` setting, exactly three letters A–Z each); `/allowshort` alone lists both lists; `/allowshort remove Word` removes an admin word. New files index normally from the next post; posts already skipped for a short title are picked up by `/reparse skipped`.
- **`/identities`** is the reviewed repair for the identity conflicts `/reparse` holds. It scans read-only and lists the conflicts grouped by the show each record would be indexed under today (count + 2 example raw filenames, 8 groups per page; `/identities page 2`). `/identities apply <Proposed Title>` shows a confirmation; only that group's records move after you press Confirm. Each record is re-read first: adopted files are never touched, and a reading that would weaken stored data (episode→pack, lost special/multi-episode info) is held back. The report gives planned / moved / held / skipped counts, emptied source shows, and any new same-size look-alikes. A group that targets an uploader/promo title, a suspicious short title, or has more than 500 records is refused unless the command ends with the word `force`. Add `(2021)` or `(no year)` if two groups share a title. The scan is kept in memory for 30 minutes (lost on restart); the confirmation expires after 10 minutes. Nothing is deleted from Telegram.
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
- Suspicious three-letter fragments (`BYL`, `YE`, `VTS`) and generic device names are never proposed.
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
- **Writes** go to the database that already holds the document. A document that exists in neither goes to the *active* database. Nothing is written to both, so a file already in the first database is never copied into the second, and nothing can show up twice. The same-size look-alike rule works across both databases.
- **Active database** = the first until the primary cluster's logical size (all databases on it, as Atlas counts) reaches `DATABASE_SWITCH_MB`; then new documents go to the second. A write rejected with "over your space quota" flips to the second and retries once. It returns to the first only when it falls 25 MB below the limit. The choice is stored in the second database, so restarts keep it.
- `/dbswitch status|primary|secondary|auto` shows or forces where NEW documents go (reads always use both). `/dbsize` reports each database and the cluster's other databases.
- Supported operations are exactly those the bot uses; anything else raises an error instead of answering from half the data. Only `$match` + `$group` (+`$sort/$limit/$skip`) aggregates are supported.
- Limits: it assumes a document id never exists in both databases (the write rules guarantee it); order of bulk writes across the two databases is not preserved; reads cost two queries.

## Conventions added after reading real skip exports

`1x1` / `1X4` / `1×46` / `1973x16`, `S01S01` (encoder typo), `S03X02`, `N серия` / `N episode` / `capítulo 08p2`, `Bölüm-3`, `slug-style-season-1-episode-3`, `#ADL.E16.END.Title.2009`, `Ep-01 [480p] Title`, `FMA OVA - 02&03`, `01_Title_Saison`, `SRV28-2022_2` (Survivor), `=ENG_SUB=<CJK>_Title_01_<CJK>`, `house.of.lies.409.hdtv-lol` (SSEE only with hdtv/pdtv/dsr; `dvdrip` and anime stay manual/absolute), WWE decorated names (`📂 WWE Monday Night Raw #1624 (08.07.2024) …`, day/month decided by the programme's air weekday), `One Piece - 0492A/B` (parts), `1004.5` (special). Subtitles/audio/images (`.srt`, `.flac` ...) are never indexed and are labelled `non_video_file`; bloopers/highlights/interviews are `extras_or_featurette`; device names (`video_2026-11-11_00-11-23`) are unreadable.

New admin tools: `/skipruns [page]` (runs of same-shape consecutive skipped posts with a ready `/adopt dry` command - you only supply the show title), `/skipclear <code>` (bulk-remove records of `non_video_file`, `extras_or_featurette` or `insufficient_title_information` after an explicit count confirmation), `/adopt` now refuses subtitle/archive files.


## Third review: formats, WWE, delivery, runs, pages

- **More formats.** `S4.01. Title [720p Dual] @Handle`, `S2- 5 Title [720p]`, uploaders that put the marker first (`@DramaOST.E01.Title.480p.WEBDL.viki`, `[TIF]_S01_E01_Title`, `[YDF] E01 Title`: a leading marker is trusted with a year, a leading `[GROUP]` tag, a resolution token or a known uploader; bare `Episode 01 - Title` is still left for `/adopt`), `ss03e01`, `[848x480] Ver Capitulo 28 Show Online Gratis`, `=ENG_SUB=<CJK>_Title_01_<cast>`, `...mp4_PART_00001` / ` PART-2` / `.part001` (file parts), a known release group after the tech tokens (`... 01 1080p x265 pseudo`). Codecs `x264/x265` are never episodes. Titles with a digit (`The 100`, `NOS4`) are no longer treated as three-letter fragments; plain three-letter titles (`MSW`, `PyP`, `OMB`) still need `/allowshort`.
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
