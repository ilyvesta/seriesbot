"""Series-specific persistent catalog. Uses the bot's own Mongo database only.

Records are bounded and queried by show/season; no collection is loaded wholesale.
"""
from __future__ import annotations
import hashlib, re, time, asyncio, functools
from contextlib import asynccontextmanager
from collections import OrderedDict
from dataclasses import dataclass, asdict, replace
from normalize import normalize, core_key
from parse import parse_series, strip_title_groups, enrich_from_caption, caption_languages
from series_parse import NON_INDEXABLE_EXTS, is_generic_media_name
from series_classify import (AMBIGUOUS, FILM, LOOKALIKE, LOOKALIKE_REASON, SERIES, SKIP_CATEGORIES, UNRECOGNISED,
                             UNREADABLE, Classification, classify_name)

VIDEO_EXTS = {"mkv", "mp4", "avi", "mov", "m4v", "webm", "ts", "wmv", "flv", "mpg", "mpeg"}

# These uploader/release labels must never become catalogue show identities, even
# when an admin has not added them to the editable promo settings.
BUILTIN_PROMOS = ("Brewfiles", "AnimeRG", "Judas", "F-D", "PlanetDizi")

def effective_promos(promos=()):
    values=list(BUILTIN_PROMOS)
    values.extend(str(x).strip() for x in (promos or ()) if str(x).strip())
    return tuple(dict.fromkeys(values))
SUB_EXTS = {"srt", "ass", "ssa", "vtt", "sub", "idx"}


def show_id(title: str, year: int | None = None) -> str:
    key = core_key(normalize(title)) + (f"-{year}" if year else "")
    return hashlib.sha1(key.encode()).hexdigest()[:12]

def richness(x) -> int:
    """How much usable information a copy of a file carries (SeriesFile or stored document)."""
    g=(lambda k,d=None: x.get(k,d)) if isinstance(x,dict) else (lambda k,d=None: getattr(x,k,d))
    name=str(g("name","") or "")
    score=len([t for t in re.split(r"[\s._\-\[\]()]+",name) if t])+len(name)//20
    score+=3*bool(g("group"))+2*bool(g("source"))+2*bool(g("codec"))+2*len(g("languages",()) or ())+len(g("tags",()) or ())
    score+=2*bool(g("ep_title"))+min(len(str(g("caption","") or ""))//40,5)
    return int(score)


_RICH_FIELDS=("name","caption","source","codec","group","languages","tags","ext")


_CODEC_NORM = {"hevc": "x265", "h265": "x265", "h.265": "x265", "x265": "x265",
               "avc": "x264", "h264": "x264", "h.264": "x264", "x264": "x264",
               "xvid": "xvid", "divx": "xvid"}


def _norm_codec(value) -> str:
    raw = str(value or "").strip().casefold()
    return _CODEC_NORM.get(raw, raw)


def _field(x, key: str, default=None):
    return x.get(key, default) if isinstance(x, dict) else getattr(x, key, default)


def duplicate_key(r: "SeriesFile") -> str | None:
    """Stable look-alike bucket key; codec/resolution/source/group are checked via compatibility."""
    if not r.size or r.size <= 0 or (r.ext or "").casefold() in SUB_EXTS:
        return None
    eps = ",".join(map(str, sorted(set(r.episodes))))
    raw = "|".join((
        r.sid,
        str(getattr(r, "kind", "episode") or "episode"),
        str(r.season),
        eps,
        str(int(r.size)),
        str(bool(getattr(r, "special", False))),
        (r.special_label or "").casefold(),
    ))
    if getattr(r, "part", None):
        raw += f"|p{int(r.part)}"          # file parts of one episode are not look-alikes
    return hashlib.sha1(raw.encode()).hexdigest()


def duplicate_conflict(a, b) -> str | None:
    """Why two records must NOT be treated as the same file, or None when nothing contradicts it.

    Exactly the same size in bytes, show, season, episodes, part and extension are required. Resolution, codec family,
    and source may be unknown on one side (a cut-off or renamed copy) but never present and different on both. Audio
    languages must match exactly (a copy with a language tag is never merged into an untagged one): that is what keeps
    real language variants apart."""
    sa, sb = int(_field(a, "size", 0) or 0), int(_field(b, "size", 0) or 0)
    if sa <= 0 or sb <= 0: return "no size recorded"
    if sa != sb: return f"different size in bytes ({sa:,} vs {sb:,})"
    if str(_field(a, "sid", "")) != str(_field(b, "sid", "")): return "stored under different shows"
    if str(_field(a, "kind", "episode")) != str(_field(b, "kind", "episode")): return "different kind (episode/pack)"
    if int(_field(a, "season", 0) or 0) != int(_field(b, "season", 0) or 0): return "different season"
    if sorted(set(_field(a, "episodes", ()) or ())) != sorted(set(_field(b, "episodes", ()) or ())): return "different episodes"
    if bool(_field(a, "special", False)) != bool(_field(b, "special", False)): return "special vs regular episode"
    if str(_field(a, "special_label", "") or "").casefold() != str(_field(b, "special_label", "") or "").casefold(): return "different special label"
    if _field(a, "part", None) != _field(b, "part", None): return "different file part"
    res_a = str(_field(a, "resolution", "") or "").casefold(); res_b = str(_field(b, "resolution", "") or "").casefold()
    if res_a and res_b and res_a != res_b: return f"different resolution ({res_a} vs {res_b})"
    cod_a, cod_b = _norm_codec(_field(a, "codec", None)), _norm_codec(_field(b, "codec", None))
    if cod_a and cod_b and cod_a != cod_b: return f"different codec ({cod_a} vs {cod_b})"
    src_a = str(_field(a, "source", "") or "").casefold(); src_b = str(_field(b, "source", "") or "").casefold()
    if src_a and src_b and src_a != src_b: return f"different source ({src_a} vs {src_b})"
    lang_a = {str(x).casefold() for x in (_field(a, "languages", ()) or ()) if x}
    lang_b = {str(x).casefold() for x in (_field(b, "languages", ()) or ()) if x}
    if lang_a != lang_b: return "different audio languages" + (" (one copy has a language tag, the other none)" if not (lang_a and lang_b) else "")
    tags_a = {str(x).casefold() for x in (_field(a, "tags", ()) or ()) if x}
    tags_b = {str(x).casefold() for x in (_field(b, "tags", ()) or ()) if x}
    has_dual = ("dual audio" in tags_a or "multi audio" in tags_a or "dual audio" in tags_b or "multi audio" in tags_b)
    if not has_dual and (("dubbed" in tags_a and "esub" in tags_b) or ("dubbed" in tags_b and "esub" in tags_a)):
        return "dubbed vs subtitled"
    return None


def are_duplicate_candidates(a, b) -> bool:
    """Return True if two same-bucket records represent the same underlying file rather than distinct variants."""
    return duplicate_conflict(a, b) is None


_CAP_JUNK = re.compile(r"(?i)\b(join|subscribe|follow|share|channel|telegram|powered|uploaded|upload by|donate|support us|request|click|download|link|group|admin|credit)\b|@\w+|(?:https?://|www\.|t\.me/)\S+")


def caption_value(caption) -> int:
    """How much real information a caption carries: letters left after promo lines, @handles and links are ignored.
    'Join @exivum' is worth 0; an episode title and synopsis is worth a lot."""
    total = 0
    for line in str(caption or "").splitlines():
        if _CAP_JUNK.search(line): continue
        total += len(re.findall(r"[^\W\d_]", line))
    return total


@dataclass
class SeriesFile:
    uid: str
    sid: str
    show: str
    year: int | None
    kind: str
    season: int
    episodes: tuple[int, ...]
    absolute: bool
    ep_title: str
    channel: int
    message_id: int
    size: int = 0
    name: str = ""
    resolution: str | None = None
    source: str | None = None
    codec: str | None = None
    languages: tuple[str, ...] = ()
    group: str | None = None
    tags: tuple[str, ...] = ()
    ext: str | None = None
    caption: str = ""
    indexed_at: int = 0
    special: bool = False
    special_label: str = ""
    part: int | None = None        # file part of one episode (Turkish "5 Parça"); the episode itself is unchanged
    adopted: bool = False          # placed by an administrator (/adopt): reparse must never rewrite it
    manual_title: bool = False     # show title was manually corrected (/renameshow / /locktitle): identity survives reparse

    def as_doc(self) -> dict:
        d = asdict(self)
        d["_id"] = d.pop("uid")
        d["dupe_key"] = duplicate_key(self)
        d["episodes"] = list(self.episodes); d["languages"] = list(self.languages); d["tags"] = list(self.tags)
        return d

    @classmethod
    def from_doc(cls, d: dict) -> "SeriesFile":
        return cls(d.get("_id", d.get("uid", "")), d["sid"], d["show"], d.get("year"), d["kind"],
                   int(d.get("season", 0)), tuple(d.get("episodes", ())), bool(d.get("absolute", False)),
                   d.get("ep_title", ""), int(d["channel"]), int(d["message_id"]), int(d.get("size", 0)),
                   d.get("name", ""), d.get("resolution"), d.get("source"), d.get("codec"),
                   tuple(d.get("languages", ())), d.get("group"), tuple(d.get("tags", ())), d.get("ext"), d.get("caption", ""), int(d.get("indexed_at", 0) or 0), bool(d.get("special", False)), d.get("special_label", ""),
                   (int(d["part"]) if d.get("part") else None), bool(d.get("adopted", False)),
                   bool(d.get("manual_title", False)))


def _strip_custom_promos(value: str, promos=()) -> str:
    """Strip configured uploader tokens only as complete, delimited phrases.

    Separators inside a configured phrase are interchangeable (F - D matches
    F_D), but at least one separator is required between its words. This keeps
    a token such as F-D from matching the ordinary word/suffix "FD".
    """
    text=(value or "").strip()
    promos=effective_promos(promos)
    m=re.search(r"(?i)(\.[a-z0-9]{2,5})$",text)
    ext=m.group(1) if m else ""
    if ext: text=text[:-len(ext)]
    # Some PlanetDizi posts append "telegram" after the uploader handle.
    # Remove that suffix before stripping the uploader token, otherwise the
    # leftover word can interfere with the episode-number parse.
    text=re.sub(r"(?i)(@[^\s]+|planetdizi)\s+telegram$", r"\1", text)
    for phrase in sorted((str(x).strip() for x in (promos or ()) if str(x).strip()),key=len,reverse=True):
        words=[w for w in re.split(r"[\s._-]+",phrase) if w]
        if not words: continue
        token=r"[\s._-]+".join(re.escape(w) for w in words)
        # A leading promo must end at a real delimiter or closing bracket.
        text=re.sub(r"(?i)^\s*(?:[\[({]\s*)?"+token+r"(?:\s*[\])}])?\s*(?:[-–—|:_.]+\s*|\s+)", "", text)
        # A trailing promo must be a separate delimited phrase, never a suffix
        # embedded in the final title word.
        text=re.sub(r"(?i)(?:\s+|[-–—|:_.]+)\s*(?:[\[({]\s*)?"+token+r"(?:\s*[\])}])?\s*$", "", text)
        # Promo between title and an episode/quality marker.
        text=re.sub(r"(?i)(?:\s+|[-–—|:_.]+)\s*(?:[\[({]\s*)?"+token+r"(?:\s*[\])}])?\s*(?:[-–—|:_.]+\s*)?(?=(?:s\d{1,2}(?:[._ -]*e|$)|season\b|ep(?:isode)?\b|e\d{1,4}\b|#\d{1,4}\b|\d{1,2}x\d{1,4}\b|(?:2160p|1440p|1080p|720p|480p|x265|x264|hevc|avc)\b))", " ", text)
    return re.sub(r"\s+"," ",text).strip()+ext


def alias_title_is_safe(value: str, promos=()) -> bool:
    """Reject uploader labels as alias destinations."""
    promos=effective_promos(promos)
    title=" ".join(str(value or "").split()).strip()
    if not title: return False
    clean=strip_title_groups(_strip_custom_promos(title,promos),promos)
    if clean.casefold()!=title.casefold(): return False
    key=core_key(normalize(title))
    promo_keys={core_key(normalize(str(x))) for x in (promos or ()) if str(x).strip()}
    return key not in promo_keys


def _parse_series_text(value: str, promos=(), rules=None):
    """Try caption lines independently so multi-line promo text cannot hide the title."""
    promos=effective_promos(promos)
    for line in (value or "").splitlines():
        line=_strip_custom_promos(line.strip(),promos)
        if not line: continue
        parsed=parse_series(line, rules=rules)
        if parsed.kind in ("episode","pack") and parsed.show:
            return parsed
    return parse_series(_strip_custom_promos((value or "").strip(),promos), rules=rules)


def _resolve_parsed(name: str, caption: str, promos=(), rules=None):
    """The single place that turns (filename, caption) into a SeriesParsed. Returns (parsed, generic_name).

    Indexing, reparse and classification all go through here so they can never disagree. When neither the
    filename nor the caption yields an episode, the filename's own result is returned so its ``hint``
    (e.g. manual_adoption_candidate or ambiguous_dual_number) survives.
    """
    promos=effective_promos(promos)
    name=(name or "").strip(); caption=(caption or "").strip()
    generic_name = is_generic_media_name(name)
    if generic_name:
        parsed=_parse_series_text(caption,promos,rules=rules) if caption else parse_series("",rules=rules)
    else:
        parsed=parse_series(_strip_custom_promos(name or caption,promos), rules=rules)
    if parsed.kind not in ("episode", "pack") and caption:
        from_caption = _parse_series_text(caption,promos,rules=rules)
        if from_caption.kind in ("episode","pack") and from_caption.show or not parsed.hint:
            parsed = replace(from_caption, from_caption=True)
    return parsed, generic_name


def from_message(m, promos=(), aliases=None, rules=None) -> SeriesFile | None:
    promos=effective_promos(promos)
    media = getattr(m, "document", None) or getattr(m, "video", None) or getattr(m, "audio", None)
    if not media:
        return None
    name = (getattr(media, "file_name", None) or "").strip()
    caption = (getattr(m, "caption", None) or "").strip()
    parsed, generic_name = _resolve_parsed(name, caption, promos, rules=rules)
    if parsed.kind not in ("episode", "pack") or not parsed.show:
        return None
    if caption and name and not generic_name:
        parsed = enrich_from_caption(parsed, name, caption)      # fill / repair from the caption
    ext = (parsed.ext or (name.rsplit(".", 1)[-1] if "." in name else "")).lower() or None
    uid = getattr(media, "file_unique_id", None)
    if not uid:
        return None
    parsed = replace(parsed, show=strip_title_groups(parsed.show, promos))
    sid = show_id(parsed.show, parsed.year)
    manual_flag = False
    alias=(aliases or {}).get(sid)
    if alias:
        if isinstance(alias,(tuple,list)):
            alias_title=str(alias[1] or parsed.show)
            # A stale alias must not reintroduce a configured uploader token
            # that the filename parser has just removed (e.g. Judas Bleach).
            if alias_title_is_safe(alias_title,promos):
                sid=str(alias[0]); parsed=replace(parsed,show=alias_title,year=alias[2] if len(alias)>2 else parsed.year)
                manual_flag = bool(alias[3]) if len(alias) > 3 else False
        else:
            sid=str(alias)
    return SeriesFile(str(uid), sid, parsed.show, parsed.year, parsed.kind, parsed.season, parsed.episodes,
                      parsed.absolute, parsed.ep_title, int(m.chat.id), int(m.id), int(getattr(media, "file_size", 0) or 0),
                      name[:500], parsed.resolution, parsed.source, parsed.codec, tuple(parsed.languages), parsed.group,
                      tuple(parsed.tags), ext, caption[:1000], time.time_ns(), parsed.special, parsed.special_label,
                      parsed.part, False, manual_flag)

def adopt_episode_number(name: str, legacy_season: int | None = None):
    """Season (or None) and episode number for a file whose name has no show title: '57. Eternal Leave',
    'Episode 12', 'E05 - Title', 'S01E05'. Returns None if no episode number can be read."""
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", (name or "").strip())
    if legacy_season:
        # Explicit, admin-requested decoding of legacy SSEE numbers (412 -> S04E12). Only used by
        # "/adopt ... | Season 4 legacy", and only when the number's leading digits equal that season.
        lm = re.search(r"(?<![A-Za-z0-9])0*(\d{3,4})(?![A-Za-z0-9])", base)
        if lm:
            n = int(lm.group(1))
            if n // 100 == int(legacy_season) and n % 100 > 0:
                return int(legacy_season), n % 100
        return None
    p = parse_series("Show " + base)
    if p.kind == "episode" and p.episodes and not p.special:
        return (p.season or None), int(p.episodes[0])
    m = re.match(r"^\s*(?:ep(?:isode)?\.?\s*)?0*(\d{1,4})(?!\d)\s*(?:[.\-–):_]|\s|$)", base, re.I)
    if m and not 1900 <= int(m.group(1)) <= 2099 and int(m.group(1)) > 0:
        return None, int(m.group(1))
    m = re.search(r"(?i)(?<![A-Za-z0-9])(?:ep(?:isode)?\.?|e)\s*0*(\d{1,4})(?![A-Za-z0-9])", base)
    if m and int(m.group(1)) > 0:
        return None, int(m.group(1))
    return None


def adopted_record(m, title, year, season, episode, aliases=None, promos=(), legacy=False):
    """Build a catalogue record for a post whose show was chosen by the admin (/adopt)."""
    from series_parse import _meta, SeriesParsed
    media = getattr(m, "document", None) or getattr(m, "video", None) or getattr(m, "audio", None)
    uid = getattr(media, "file_unique_id", None) if media else None
    if not uid: return None
    name = (getattr(media, "file_name", None) or "").strip(); caption = (getattr(m, "caption", None) or "").strip()
    res, source, codec, langs, tags = _meta(name + " " + caption)
    ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower() or None
    parsed = SeriesParsed(raw=name, kind="episode", show=title, year=year, season=int(season or 0), episodes=(int(episode),),
                          absolute=not season, resolution=res, source=source, codec=codec, languages=caption_languages(caption) or (),
                          group=None, tags=tags, ext=ext)
    if caption and name: parsed = enrich_from_caption(parsed, name, caption)
    sid = show_id(title, year)
    alias = (aliases or {}).get(sid)
    if alias and isinstance(alias, (tuple, list)) and alias_title_is_safe(str(alias[1] or title), promos):
        sid = str(alias[0]); parsed = replace(parsed, show=str(alias[1] or title), year=alias[2] if len(alias) > 2 else year)
    return SeriesFile(str(uid), sid, parsed.show, parsed.year, "episode", parsed.season, parsed.episodes, parsed.absolute, "",
                      int(m.chat.id), int(m.id), int(getattr(media, "file_size", 0) or 0), name[:500], parsed.resolution,
                      parsed.source, parsed.codec, tuple(parsed.languages), parsed.group, tuple(parsed.tags), ext,
                      caption[:1000], time.time_ns(), False, "", None, True)


def _catalogue_mutation(fn):
    """Keep identity mutations in the same serialization domain as indexing."""
    @functools.wraps(fn)
    async def wrapped(self, *args, **kwargs):
        # Dry operations are strictly read-only, including reservation state.
        if fn.__name__ == "dedupe_show" and (kwargs.get("dry_run") or (len(args) > 1 and args[1])):
            return await fn(self, *args, **kwargs)
        if fn.__name__ == "dedupe_series" and (kwargs.get("dry_run") or (args and args[0])):
            return await fn(self, *args, **kwargs)
        if fn.__name__ == "reclassify_skips" and (kwargs.get("dry") or (args and args[0])):
            return await fn(self, *args, **kwargs)
        async with self._catalogue_write():
            return await fn(self, *args, **kwargs)
    return wrapped


class MongoSeriesCatalog:
    def __init__(self, db):
        self.files=db.series_files; self.shows=db.series_shows; self.skips=db.series_skips; self.watch=db.series_watchlists; self.requests=db.requests; self.progress=db.series_progress; self.user_stats_col=db.series_user_stats; self.aliases=db.series_show_aliases
        self.maintenance_state=db.series_maintenance; self.movies=db.series_movies
        self._add_lock = asyncio.Lock()
        self._add_owner = None
        self._db = db
        self._cache=OrderedDict(); self._alias_cache=(0.0,{})
        self._cache_ttl = 900.0
        self._cache_maxsize = 1000
    def _cache_prune(self, now: float) -> None:
        expired = [k for k, (exp, _) in self._cache.items() if exp <= now]
        for k in expired:
            self._cache.pop(k, None)
    def _cache_get(self,key,now=None):
        ts_now=time.monotonic() if now is None else now
        item=self._cache.get(key)
        if not item: return None
        if item[0]<=ts_now:
            self._cache.pop(key,None); return None
        self._cache.move_to_end(key); return item[1]
    def _cache_set(self,key,value,now=None):
        ts_now=time.monotonic() if now is None else now
        self._cache[key]=(ts_now+self._cache_ttl,value); self._cache.move_to_end(key)
        if len(self._cache)>self._cache_maxsize:
            self._cache_prune(ts_now)
            while len(self._cache)>self._cache_maxsize: self._cache.popitem(last=False)
    def _invalidate_show(self,sid):
        sid_s = str(sid)
        self._cache.pop(("show",sid_s),None)
        self._cache.pop(("files",sid_s,None),None)
        self._cache.pop(("movies",sid_s),None)
        # Search pages/counts and any show-scoped caches are cleared together.
        for key in list(self._cache):
            if key[0] in ("search","count") or (key[0]=="files" and str(key[1])==sid_s):
                self._cache.pop(key,None)
    async def setup(self):
        await self.files.create_index([("sid",1),("season",1),("episodes",1)])
        await self.files.create_index("dupe_key", sparse=True)
        await self.files.create_index([("channel",1),("message_id",1)])
        await self.files.create_index([("indexed_at",1)])
        await self.shows.create_index([("key",1)])
        await self.shows.create_index([("year",1)])
        await self.shows.create_index([("added",-1)])
        await self.shows.create_index([("hits",-1)])
        await self.skips.create_index([("at",-1)])
        await self.skips.create_index([("reason",1),("at",-1)])
        await self.skips.create_index([("category",1),("at",-1)])
        await self.skips.create_index([("channel",1),("message_id",1)])
        await self.migrate_skip_categories()
        await self.watch.create_index([("uid",1)],unique=True)
        await self.progress.create_index([("uid",1),("sid",1)],unique=True)
        await self.user_stats_col.create_index("uid",unique=True)
        await self.aliases.create_index("sid")
        await self.movies.create_index([("sid",1),("indexed_at",-1)])
        await self.movies.create_index([("channel",1),("message_id",1)],unique=True)
    @asynccontextmanager
    async def _catalogue_write(self):
        from multidb import FederatedDB
        if isinstance(self._db, FederatedDB):
            async with self._db.catalogue_write():
                yield
        elif self._add_owner is asyncio.current_task():
            yield
        else:
            async with self._add_lock:
                self._add_owner = asyncio.current_task()
                try:
                    yield
                finally:
                    self._add_owner = None

    @_catalogue_mutation
    async def add(self, records: list[SeriesFile], include_added: bool = False):
        return await self._add_locked(records, include_added)

    async def _add_locked(self, records, include_added=False):
        if not records: return (0,0,0,[]) if include_added else (0,0,0)
        # Resolve fresh aliases and manual title protections in bounded batches.
        # Do not let an old cached alias override a manual adoption.
        records = await self._canonicalize_batch(records)
        from pymongo import UpdateOne
        unique={r.uid:r for r in records}
        # Both identity and look-alike queries use the existing federated collection.
        known={d["_id"] async for d in self.files.find({"_id":{"$in":list(unique)}},{"_id":1})}
        fresh=[]; skipped_dupes=[]; dupes=0; seen_keys=set()
        candidates=[r for uid,r in unique.items() if uid not in known]
        candidates.sort(key=lambda r:(int(r.indexed_at or 0),int(r.message_id),str(r.uid)))
        dkeys=list({dk for r in candidates if (dk:=duplicate_key(r))})
        existing_by_key={}
        if dkeys:
            async for d in self.files.find({"dupe_key":{"$in":dkeys}},{"_id":1,"dupe_key":1,"sid":1,"kind":1,"season":1,"episodes":1,"size":1,"ext":1,"special":1,"special_label":1,"part":1,"resolution":1,"codec":1,"source":1,"languages":1,"tags":1,"name":1,"caption":1,"group":1,"ep_title":1}):
                existing_by_key.setdefault(d["dupe_key"],[]).append(d)
        # Records stored before the duplicate key changed carry the OLD key, so a lookup by key alone would miss them.
        # Also find stored copies by what makes two files the same (show, season, episodes, size) and file them under
        # the CURRENT key; are_duplicate_candidates() then makes the real decision, exactly as for key matches.
        sids={r.sid for r in candidates}; sizes={int(r.size) for r in candidates if int(r.size or 0)>0}
        if sids and sizes:
            seen_ids={d["_id"] for lst in existing_by_key.values() for d in lst}
            async for d in self.files.find({"sid":{"$in":list(sids)},"size":{"$in":list(sizes)}},{"_id":1,"dupe_key":1,"sid":1,"kind":1,"season":1,"episodes":1,"size":1,"ext":1,"special":1,"special_label":1,"part":1,"resolution":1,"codec":1,"source":1,"languages":1,"tags":1,"name":1,"caption":1,"group":1,"ep_title":1}):
                if d["_id"] in seen_ids: continue
                from types import SimpleNamespace
                key=duplicate_key(SimpleNamespace(sid=d.get("sid"),kind=d.get("kind","episode"),season=d.get("season",0),
                                                  episodes=tuple(d.get("episodes") or ()),size=d.get("size",0),ext=d.get("ext"),
                                                  special=d.get("special",False),special_label=d.get("special_label",""),part=d.get("part")))
                if key: existing_by_key.setdefault(key,[]).append(d)
        fresh_by_key={}
        for r in candidates:
            dk=duplicate_key(r)
            if dk:
                matched_prev=next((p for p in fresh_by_key.get(dk,()) if are_duplicate_candidates(p,r)),None)
                if matched_prev is not None:
                    if richness(r)>richness(matched_prev)+1:
                        # same file twice in one batch: the copy with MORE information stays, the other becomes the look-alike
                        fresh[fresh.index(matched_prev)]=r
                        lst=fresh_by_key[dk]; lst[lst.index(matched_prev)]=r
                        skipped_dupes.append((matched_prev,r.uid)); dupes+=1; continue
                    skipped_dupes.append((r,matched_prev.uid)); dupes+=1; continue
                matched_old=next((d for d in existing_by_key.get(dk,()) if are_duplicate_candidates(d,r)),None)
                if matched_old is not None:
                    skipped_dupes.append((r,matched_old["_id"])); dupes+=1; continue
                fresh_by_key.setdefault(dk,[]).append(r)
            fresh.append(r)
        enriched_sids=set()
        if skipped_dupes:
            # a later copy that carries MORE information improves the stored record (same file, richer description);
            # the stored post and ids never change, so links, progress and look-alike bookkeeping stay valid
            keeps=list({k for _,k in skipped_dupes if k!="same-batch"})
            old={d["_id"]:d async for d in self.files.find({"_id":{"$in":keeps}})} if keeps else {}
            ups=[]
            for r,k in skipped_dupes:
                od=old.get(k)
                if not od: continue
                fields={}
                if richness(r)>richness(od)+1:
                    fields={f:getattr(r,f) for f in _RICH_FIELDS if getattr(r,f,None) not in (None,"",())}
                    for f in ("languages","tags"):
                        if f in fields: fields[f]=list(fields[f])
                if caption_value(getattr(r,"caption",""))>caption_value(od.get("caption")):
                    fields["caption"]=r.caption                       # the richer caption always wins
                if fields:
                    ups.append(UpdateOne({"_id":k},{"$set":fields}))
                    old[k]={**od,**fields}
                    if od.get("sid"):
                        enriched_sids.add(str(od["sid"]))
            if ups: await self.files.bulk_write(ups,ordered=False)
        if skipped_dupes:
            skip_ops=[]
            for r,kept_uid in skipped_dupes:
                skip_ops.append(UpdateOne({"_id":f"{r.channel}:{r.message_id}"},{"$set":{"channel":r.channel,"message_id":r.message_id,"kept_uid":str(kept_uid),"name":r.name or r.show,"reason":LOOKALIKE_REASON,"category":LOOKALIKE,"code":"possible_duplicate","title_guess":(r.show or "")[:120],"at":time.time()}},upsert=True))
            await self.skips.bulk_write(skip_ops,ordered=False)
        if fresh:
            ops=[]
            for r in fresh:
                doc=r.as_doc(); doc.pop("_id",None)
                ops.append(UpdateOne({"_id":r.uid},{"$set":doc},upsert=True))
            await self.files.bulk_write(ops,ordered=False)
            await self.skips.delete_many({"_id":{"$in":[f"{r.channel}:{r.message_id}" for r in fresh]}})
        per_show={}
        for r in fresh:
            d=per_show.setdefault(r.sid,{"title":r.show,"year":r.year,"key":core_key(normalize(r.show)),"n":0})
            d["n"]+=1
        for sid,d in per_show.items():
            await self.shows.update_one({"_id":sid},{"$setOnInsert":{"title":d["title"],"year":d["year"],"key":d["key"],"added":time.time(),"hits":0},"$inc":{"count":d["n"]}},upsert=True)
        for sid in (set(per_show) | enriched_sids):
            self._invalidate_show(sid)
        result=(len(fresh),len(known),dupes)
        return (*result,fresh) if include_added else result
    def _search_filter(self,query):
        q=core_key(normalize(query)); tokens=[re.escape(x) for x in q.split() if x]
        if not q: return {},q
        return {"$or":[{"key":{"$regex":"^"+re.escape(q)}},{"key":{"$regex":".*"+".*".join(tokens)+".*"}}]},q
    async def search_shows(self, query: str, limit: int = 10, offset: int = 0) -> list[dict]:
        filt,q=self._search_filter(query)
        if not q: return []
        key=("search",q,int(limit),int(offset)); cached=self._cache_get(key)
        if cached is not None: return [dict(x) for x in cached]
        projection={"title":1,"year":1,"count":1,"added":1,"key":1}
        # Exact catalogue identities always lead, but do not suppress other
        # plausible candidates. This also keeps same-title/different-year rows visible.
        exact_count=await self.shows.count_documents({"key":q})
        rows=[]
        skip=max(0,int(offset)); take=max(1,int(limit))
        if skip<exact_count:
            exact_cur=self.shows.find({"key":q},projection).sort([("year",1),("title",1)]).skip(skip).limit(take)
            exact_rows=[d async for d in exact_cur]
            rows.extend(exact_rows)
            take-=len(exact_rows)
            skip=0
        else:
            skip-=exact_count
        if take>0:
            other_filter={"$and":[filt,{"key":{"$ne":q}}]}
            other_cur=self.shows.find(other_filter,projection).sort([("key",1),("year",1),("title",1)]).skip(skip).limit(take)
            rows.extend([d async for d in other_cur])
        self._cache_set(key,rows); return rows
    async def count_search_shows(self,query):
        filt,q=self._search_filter(query)
        if not q: return 0
        key=("count",q); cached=self._cache_get(key)
        if cached is not None: return cached
        n=await self.shows.count_documents(filt); self._cache_set(key,n); return n
    async def get_show(self,sid):
        key=("show",str(sid)); cached=self._cache_get(key)
        if cached is not None: return dict(cached)
        d=await self.shows.find_one({"_id":sid})
        if d: self._cache_set(key,d)
        return d
    async def show_files(self, sid: str, season: int | None = None) -> list[SeriesFile]:
        key=("files",str(sid),None)
        cached=self._cache_get(key)
        if cached is None:
            cur=self.files.find({"sid":sid},batch_size=500)
            out=[SeriesFile.from_doc(d) async for d in cur]
            out.sort(key=lambda f:(f.season,f.episodes[0] if f.episodes else 9999,f.kind,f.name.lower()))
            cached=tuple(out)
            self._cache_set(key,cached)
        if season is not None:
            return [f for f in cached if f.season==season]
        return list(cached)

    @staticmethod
    def indexed_at_sec(val) -> float:
        v = float(val or 0)
        return v / 1_000_000_000.0 if v > 1e12 else v

    async def series_digest_candidates(self, since_ts: float, due_ts: float) -> tuple[dict[str, list[SeriesFile]], dict[str, dict]]:
        """Return (files_by_sid, shows_by_sid) for episodes/packs indexed in (since_ts, due_ts]."""
        since_ns = int(float(since_ts) * 1_000_000_000)
        due_ns = int(float(due_ts) * 1_000_000_000)
        filt = {"$or": [
            {"indexed_at": {"$gt": since_ns, "$lte": due_ns}},
            {"indexed_at": {"$gt": float(since_ts), "$lte": float(due_ts)}},
        ]}
        by_sid: dict[str, list[SeriesFile]] = {}
        async for d in self.files.find(filt, batch_size=500):
            ts_sec = self.indexed_at_sec(d.get("indexed_at"))
            if float(since_ts) < ts_sec <= float(due_ts):
                rec = SeriesFile.from_doc(d)
                by_sid.setdefault(str(rec.sid), []).append(rec)
        if not by_sid:
            return {}, {}
        shows = {
            str(d["_id"]): d
            async for d in self.shows.find({"_id": {"$in": list(by_sid)}}, {"title": 1, "year": 1, "key": 1, "count": 1, "hits": 1, "added": 1})
        }
        return by_sid, shows
    # ------------------------------------------------------------------ digest control panel queries
    @staticmethod
    def _window_filter(since_ts: float, due_ts: float) -> dict:
        """indexed_at is stored either as nanoseconds (new records) or seconds (old ones): match both."""
        return {"$or": [
            {"indexed_at": {"$gt": int(float(since_ts) * 1_000_000_000), "$lte": int(float(due_ts) * 1_000_000_000)}},
            {"indexed_at": {"$gt": float(since_ts), "$lte": float(due_ts)}},
        ]}

    async def digest_pool(self, since_ts: float, due_ts: float) -> dict[str, dict]:
        """{sid: {"n": files, "newest": seconds}} for everything indexed in (since, due]. Grouped in the database,
        so a huge bulk-indexing window never has to be loaded into memory."""
        cur = await self.files.aggregate([
            {"$match": self._window_filter(since_ts, due_ts)},
            {"$group": {"_id": "$sid", "n": {"$sum": 1}, "newest": {"$max": "$indexed_at"}}},
        ], allowDiskUse=True)
        out: dict[str, dict] = {}
        async for g in cur:
            if g.get("_id") is None: continue
            out[str(g["_id"])] = {"n": int(g.get("n") or 0), "newest": self.indexed_at_sec(g.get("newest"))}
        return out

    async def digest_window_files(self, sid: str, since_ts: float, due_ts: float) -> list[SeriesFile]:
        filt = {"sid": str(sid), **self._window_filter(since_ts, due_ts)}
        out = []
        async for d in self.files.find(filt):
            if str(d.get("ext") or "").casefold() in SUB_EXTS: continue
            out.append(SeriesFile.from_doc(d))
        return out

    async def latest_season(self, sid: str) -> int | None:
        """The show's newest season in the WHOLE catalogue (specials ignored), not just in the digest window."""
        rows = await self.files.find({"sid": str(sid), "special": {"$ne": True}}, {"season": 1}).sort("season", -1).limit(1).to_list(length=1)
        return int(rows[0].get("season") or 0) if rows else None

    async def max_episode_before(self, sid: str, season: int, since_ts: float) -> int | None:
        """Highest episode number already in that season BEFORE the window opened (None = the season is new)."""
        before = {"$or": [
            {"indexed_at": {"$gt": 1e12, "$lte": int(float(since_ts) * 1_000_000_000)}},
            {"indexed_at": {"$lte": float(since_ts)}},
            {"indexed_at": None},
        ]}
        best = None
        async for d in self.files.find({"sid": str(sid), "season": int(season), **before}, {"episodes": 1}):
            for e in (d.get("episodes") or ()):
                best = int(e) if best is None else max(best, int(e))
        return best

    # ------------------------------------------------------------------ skipped posts
    # Retention rule: a skip record is NEVER deleted automatically - no cap, no expiry, no pruning.
    # It disappears only when it is processed (indexed/adopted) or an administrator removes it.
    @staticmethod
    def _skip_fields(name, cls: Classification) -> dict:
        return {"name":str(name or "(no filename)")[:500],"category":cls.category,"code":cls.code,"reason":cls.reason,
                "title_guess":(cls.title or "")[:120],"classified_at":time.time()}

    async def classify_skip(self, name, caption="", promos=()) -> Classification:
        """Classify one unindexed media post. Pure labelling: touches no catalogue record."""
        cls=classify_name(name,caption,promos)
        if cls.category==SERIES:                       # indexer refused it (e.g. missing file id): never call it a series
            cls=Classification(UNRECOGNISED,"unsupported_episode_format",cls.title,cls.year)
        elif cls.category==FILM and cls.title:
            key=core_key(normalize(cls.title))
            if key and await self.shows.find_one({"key":key},{"_id":1}):
                cls=classify_name(name,caption,promos,known_show=True)
                if cls.category==SERIES: cls=Classification(AMBIGUOUS,"no_episode_marker",cls.title,cls.year)
        return cls

    async def add_skip_record(self,r,reason=LOOKALIKE_REASON):
        key=f"{r.channel}:{r.message_id}"
        cls=Classification(LOOKALIKE,"possible_duplicate",r.show or "")
        await self.skips.update_one({"_id":key},{"$set":{"channel":r.channel,"message_id":r.message_id,**self._skip_fields(r.name or r.show,cls),"reason":reason,"at":time.time()}},upsert=True)

    async def add_skip_payloads(self, payloads, reason=None, promos=()):
        if not payloads: return 0
        from pymongo import UpdateOne
        ops=[]
        for p in payloads:
            ch=int(p["chat_id"]); mid=int(p["message_id"])
            name=p.get("file_name") or p.get("caption") or "(no filename)"
            cls=await self.classify_skip(p.get("file_name") or "",p.get("caption") or "",promos)
            ops.append(UpdateOne({"_id":f"{ch}:{mid}"},{"$set":{"channel":ch,"message_id":mid,**self._skip_fields(name,cls),"at":time.time()}},upsert=True))
        if ops: await self.skips.bulk_write(ops,ordered=False)
        return len(ops)

    async def add_skip(self,m,reason=None,promos=()):
        media=getattr(m,"document",None) or getattr(m,"video",None) or getattr(m,"audio",None)
        fname=getattr(media,"file_name",None) or ""; cap=getattr(m,"caption",None) or ""
        cls=await self.classify_skip(fname,cap,promos)
        await self.skips.update_one({"_id":f"{m.chat.id}:{m.id}"},{"$set":{"channel":m.chat.id,"message_id":m.id,**self._skip_fields(fname or cap,cls),"at":time.time()}},upsert=True)
        return cls

    @staticmethod
    def _skip_filter(category=None, query=None) -> dict:
        f={}
        if category: f["category"]=category
        q=(query or "").strip()
        if q: f["name"]={"$regex":re.escape(q),"$options":"i"}
        return f

    async def skip_counts(self):
        out={c:await self.skips.count_documents({"category":c}) for c in SKIP_CATEGORIES}
        out["total"]=sum(out.values())
        out["unlabelled"]=await self.skips.count_documents({"category":{"$exists":False}})
        out["total"]+=out["unlabelled"]
        return out

    async def skip_count(self,category=None,query=None) -> int:
        return await self.skips.count_documents(self._skip_filter(category,query))

    async def skip_page(self,category,limit,offset,query=None):
        """One page of skip records, newest first. Uses skip/limit on the cursor - never loads the collection."""
        cur=self.skips.find(self._skip_filter(category,query)).sort([("at",-1),("_id",1)]).skip(int(offset)).limit(int(limit))
        return [d async for d in cur]

    async def get_skip(self,skip_id):
        return await self.skips.find_one({"_id":str(skip_id)})

    async def remove_skip(self,skip_id) -> bool:
        """Explicit single-record removal (administrator action). Does not touch files or shows."""
        r=await self.skips.delete_one({"_id":str(skip_id)})
        return bool(getattr(r,"deleted_count",0))

    async def remove_skips_in_category(self,category,expected_count) -> int | None:
        """Bulk removal, only for a confirmed count. Returns None (nothing deleted) if the count changed."""
        if category not in SKIP_CATEGORIES: return None
        if await self.skips.count_documents({"category":category})!=int(expected_count): return None
        r=await self.skips.delete_many({"category":category})
        return int(getattr(r,"deleted_count",0))

    async def iter_skips(self,category=None,query=None,after_id=None,batch=200):
        """Keyset-paginated iteration (bounded memory) for exports and reclassification."""
        last=after_id; size=max(1,min(int(batch),500))
        while True:
            f=self._skip_filter(category,query)
            if last is not None: f["_id"]={"$gt":last}
            docs=await self.skips.find(f).sort("_id",1).limit(size).to_list(length=size)
            if not docs: return
            yield docs
            last=docs[-1]["_id"]
            if len(docs)<size: return

    async def skips_after(self,after_id=None,limit=20):
        """The next `limit` skip records after a keyset position (stable while records are removed)."""
        f={} if after_id is None else {"_id":{"$gt":after_id}}
        return await self.skips.find(f).sort("_id",1).limit(int(limit)).to_list(length=int(limit))

    async def skips_in_range(self,channel,lo,hi):
        return [d async for d in self.skips.find({"channel":int(channel),"message_id":{"$gte":int(lo),"$lte":int(hi)},"category":{"$ne":LOOKALIKE},"reason":{"$ne":LOOKALIKE_REASON}}).sort("message_id",1)]

    async def clear_skip(self, channel, message_id):
        """A post was processed into the catalogue: its skip record is no longer needed."""
        await self.skips.delete_one({"_id":f"{channel}:{message_id}"})

    async def skipped_rows(self,limit=12,offset=0):
        return [d async for d in self.skips.find({},batch_size=limit).sort("at",-1).skip(offset).limit(limit)]

    async def file_brief(self,uid):
        return await self.files.find_one({"_id":str(uid)},{"name":1,"channel":1,"message_id":1})

    async def files_brief_many(self,uids) -> dict:
        """{uid: {name, channel, message_id}} for a bounded batch of file ids (used by the skipped-results export)."""
        ids=[str(u) for u in dict.fromkeys(uids) if u]
        if not ids: return {}
        docs=await self.files.find({"_id":{"$in":ids}},{"name":1,"channel":1,"message_id":1}).to_list(length=len(ids))
        return {str(d["_id"]):d for d in docs}

    async def migrate_skip_categories(self) -> dict:
        """Idempotent, non-destructive: label records stored before classification existed."""
        a=await self.skips.update_many({"category":{"$exists":False},"reason":LOOKALIKE_REASON},
                                       {"$set":{"category":LOOKALIKE,"code":"possible_duplicate"}})
        b=await self.skips.update_many({"category":{"$exists":False}},
                                       {"$set":{"category":UNREADABLE,"code":"legacy_unclassified"}})
        return {"lookalike":int(getattr(a,"modified_count",0)),"legacy":int(getattr(b,"modified_count",0))}

    async def reclassify_skips(self,dry=False,promos=(),progress=None) -> dict:
        """Re-run the classifier over skip records. Only the label fields of the skip record change."""
        from pymongo import UpdateOne
        scanned=changed=0; moves={}; examples=[]
        async for docs in self.iter_skips(batch=300):
            ops=[]
            for d in docs:
                if d.get("category")==LOOKALIKE: continue
                scanned+=1
                cls=await self.classify_skip(d.get("name") or "","",promos)
                if (d.get("category"),d.get("code"))==(cls.category,cls.code): continue
                changed+=1; k=(d.get("category") or "?",cls.category); moves[k]=moves.get(k,0)+1
                if len(examples)<8: examples.append((str(d.get("name"))[:60],cls.category,cls.code))
                if not dry: ops.append(UpdateOne({"_id":d["_id"]},{"$set":{"category":cls.category,"code":cls.code,"reason":cls.reason,"title_guess":(cls.title or "")[:120],"classified_at":time.time()}}))
            if ops: await self.skips.bulk_write(ops,ordered=False)
        return {"scanned":scanned,"changed":changed,"moves":moves,"examples":examples,"dry":dry}

    # persisted admin state (survives restarts; lives in the maintenance collection)
    async def save_skip_view(self,uid,category,query):
        await self.maintenance_state.update_one({"_id":f"skipview:{int(uid)}"},{"$set":{"category":category,"query":query,"updated":time.time()}},upsert=True)
    async def get_skip_view(self,uid):
        return await self.maintenance_state.find_one({"_id":f"skipview:{int(uid)}"}) or {}
    async def save_adopt_plan(self,uid,plan):
        await self.maintenance_state.update_one({"_id":f"adopt:{int(uid)}"},{"$set":{"plan":plan,"created":time.time()}},upsert=True)
    async def get_adopt_plan(self,uid,max_age=900):
        d=await self.maintenance_state.find_one({"_id":f"adopt:{int(uid)}"})
        if not d or time.time()-float(d.get("created",0))>max_age: return None
        return d.get("plan")
    async def clear_adopt_plan(self,uid):
        await self.maintenance_state.delete_one({"_id":f"adopt:{int(uid)}"})

    async def bump_hit(self,sid): await self.shows.update_one({"_id":sid},{"$inc":{"hits":1}})
    async def recent_shows(self,limit=12): return [d async for d in self.shows.find({},batch_size=limit).sort("added",-1).limit(limit)]
    async def popular_shows(self,limit=12): return [d async for d in self.shows.find({},batch_size=limit).sort([("hits",-1),("count",-1)]).limit(limit)]
    async def watchlist(self,uid):
        d=await self.watch.find_one({"uid":uid}); return list(d.get("sids",[])) if d else []
    async def save_watchlist(self,uid,sids): await self.watch.update_one({"uid":uid},{"$set":{"sids":list(dict.fromkeys(sids))}},upsert=True)
    async def followers(self,sid): return [int(d["uid"]) async for d in self.watch.find({"sids":sid},{"uid":1})]
    async def tag_request(self, key: str, sid: str):
        """Attach a stable show reference to series season/episode requests."""
        await self.requests.update_one({"_id":str(key)},{"$set":{"sid":str(sid)}})
    async def bump_user_stat(self,uid,key):
        if key not in ("searches","downloads"): return
        await self.user_stats_col.update_one({"uid":int(uid)},{"$inc":{key:1}},upsert=True)
    async def user_stats(self,uid):
        d=await self.user_stats_col.find_one({"uid":int(uid)},{"_id":0,"searches":1,"downloads":1}) or {}
        return d
    async def get_progress(self,uid,sid):
        d=await self.progress.find_one({"uid":int(uid),"sid":str(sid)},{"_id":0,"season":1,"episode":1,"resolution":1}); return d
    async def save_progress(self,uid,sid,season,episode,resolution=None):
        await self.progress.update_one({"uid":int(uid),"sid":str(sid)},{"$set":{"season":int(season),"episode":int(episode),"resolution":resolution,"updated":time.time()}},upsert=True)
    @_catalogue_mutation
    async def remove_file(self,uid):
        d=await self.files.find_one_and_delete({"_id":uid})
        if not d: return None
        r=SeriesFile.from_doc(d)
        await self.shows.update_one({"_id":r.sid},{"$inc":{"count":-1}})
        await self.shows.delete_one({"_id":r.sid,"count":{"$lte":0},"manual_title":{"$ne":True}})
        self._invalidate_show(r.sid)
        return r
    async def _canonicalize_batch(self, records):
        direct = {r.sid: r.sid for r in records}
        for _ in range(8):
            ids = list(set(direct.values()))
            rows = {str(d["_id"]): str(d["sid"]) async for d in self.aliases.find({"_id": {"$in": ids}}, {"sid": 1}) if d.get("sid")}
            nxt = {old: rows.get(target, target) for old, target in direct.items()}
            if nxt == direct: break
            direct = nxt
        shows = {}
        async for d in self.shows.find({"_id": {"$in": list(set(direct.values()))}}, {"title": 1, "year": 1, "manual_title": 1}):
            sid = str(d["_id"])
            if sid not in shows or d.get("manual_title"):
                shows[sid] = d
        promos = ()
        if any(k != v for k, v in direct.items()):
            setting = await self._db.settings.find_one({"_id": "main"}, {"series_promos": 1}) or {}
            promos = setting.get("series_promos", ())
        result = []
        for r in records:
            target = direct[r.sid]
            show = shows.get(target)
            manual = bool((show or {}).get("manual_title"))
            if show and (manual or (target != r.sid and not r.adopted and alias_title_is_safe(show.get("title"), promos))):
                r = replace(r, sid=target, show=show.get("title") or r.show, year=show.get("year", r.year), manual_title=manual or r.manual_title)
            result.append(r)
        return result

    async def canonicalize_record(self, record):
        return (await self._canonicalize_batch([record]))[0]

    @_catalogue_mutation
    async def rename_show(self, sid: str, new_title: str, year=None):
        """Rename a show in place, preserving its stable sid and all user references."""
        new_title=" ".join(str(new_title or "").split()).strip()
        if not new_title or len(new_title)>110: raise ValueError("Title must contain 1–110 characters.")
        old=await self.shows.find_one({"_id":str(sid)})
        if not old: raise ValueError("Show not found.")
        year=old.get("year") if year is None else year
        new_sid=show_id(new_title,year)
        if new_sid!=str(sid):
            collision=await self.shows.find_one({"_id":new_sid},{"_id":1,"title":1})
            alias=await self.aliases.find_one({"_id":new_sid},{"sid":1})
            if collision or (alias and str(alias.get("sid"))!=str(sid)):
                raise ValueError("That title already belongs to another show or alias. Merge/resolve the existing entry first; nothing was renamed.")
        await self.files.update_many({"sid":str(sid)},{"$set":{"show":new_title,"year":year,"manual_title":True}})
        await self.shows.update_one(
            {"_id":str(sid)},
            {"$set":{
                "title":new_title,
                "year":year,
                "key":core_key(normalize(new_title)),
                "manual_title":True,
                "title_source":"manual",
                "manual_title_at":time.time(),
                "manual_original_title":old.get("manual_original_title") or old.get("title"),
            }},
        )
        # Keep the original stable ID mapped to itself so filenames that still
        # parse under the pre-rename title resolve to the renamed catalogue entry.
        await self.aliases.update_one({"_id":str(sid)},{"$set":{"sid":str(sid),"updated":time.time()}},upsert=True)
        if new_sid!=str(sid):
            await self.aliases.update_one({"_id":new_sid},{"$set":{"sid":str(sid),"updated":time.time()}},upsert=True)
        self.clear_cache()
        return old.get("title","Series"),new_title

    @_catalogue_mutation
    async def lock_show_title(self, sid: str) -> dict:
        """Mark an existing show's current title as a durable manual title override."""
        d = await self.shows.find_one({"_id": str(sid)})
        if not d:
            raise ValueError("Show not found.")
        title = str(d.get("title") or sid)
        year = d.get("year")
        await self.shows.update_one(
            {"_id": str(sid)},
            {"$set": {"manual_title": True, "title_source": "manual", "manual_title_at": time.time()}},
        )
        await self.files.update_many({"sid": str(sid)}, {"$set": {"manual_title": True}})
        await self.aliases.update_one({"_id": str(sid)}, {"$set": {"sid": str(sid), "updated": time.time()}}, upsert=True)
        self.clear_cache()
        return {"sid": str(sid), "title": title, "year": year}

    async def manual_title_map(self) -> dict[str, dict]:
        """Return {sid: {title, year, title_source}} for every show protected by a manual title override."""
        out: dict[str, dict] = {}
        async for d in self.shows.find({"manual_title": True}, {"title": 1, "year": 1, "manual_title": 1, "title_source": 1}):
            out[str(d["_id"])] = d
        return out

    async def alias_map(self):
        """Build the compatibility map with bounded Mongo round trips, not N+1 queries."""
        now=time.monotonic()
        if now-self._alias_cache[0]<60: return self._alias_cache[1]
        rows=[row async for row in self.aliases.find({},batch_size=500)]
        direct={str(r.get("_id","")):str(r.get("sid","")) for r in rows if r.get("_id") and r.get("sid")}
        targets=set(direct.values())
        # Resolve alias chains in memory; fetch all canonical show documents at once.
        mapping={}
        for old in direct:
            target=old; seen=set()
            for _ in range(8):
                nxt=direct.get(target,target)
                if nxt==target or target in seen: break
                seen.add(target); target=nxt
            if target: mapping[old]=target
        canonical=set(mapping.values())
        shows={str(d["_id"]):d async for d in self.shows.find({"_id":{"$in":list(canonical)}},{"title":1,"year":1,"manual_title":1})} if canonical else {}
        result={}
        for old,target in mapping.items():
            d=shows.get(target)
            if d: result[old]=(target,d.get("title"),d.get("year"),bool(d.get("manual_title",False)))
        self._alias_cache=(now,result)
        return result

    @_catalogue_mutation
    async def remove_alias_ids(self, sids):
        """Remove only explicitly selected alias source IDs; never touch files/shows."""
        ids=list(dict.fromkeys(str(x) for x in (sids or ()) if x))
        if not ids: return 0
        result=await self.aliases.delete_many({"_id":{"$in":ids}})
        self.clear_cache()
        return int(getattr(result,"deleted_count",0))

    async def resolve_alias(self, sid: str):
        current=str(sid); seen=set()
        for _ in range(8):
            if current in seen: break
            seen.add(current)
            row=await self.aliases.find_one({"_id":current},{"sid":1})
            target=str(row.get("sid")) if row and row.get("sid") else current
            if target==current: break
            current=target
        return current
    async def get_file(self, uid: str):
        d=await self.files.find_one({"_id":uid}); return SeriesFile.from_doc(d) if d else None
    @_catalogue_mutation
    async def merge_show_ids(self, survivor: str, removed: list[str], title: str, year: int | None):
        removed=list(set(removed)-{survivor})
        if not removed: return 0
        old=await self.shows.find_one({"_id":survivor}) or {}
        keep_manual=bool(old.get("manual_title",False))
        if keep_manual:
            title, year = old.get("title") or title, old.get("year")
        file_set={"sid":survivor,"show":title,"year":year}
        if keep_manual:
            file_set["manual_title"]=True
        moved=await self.files.update_many({"sid":{"$in":removed}},{"$set":file_set})
        await self.movies.update_many({"sid":{"$in":removed}},{"$set":{"sid":survivor}})
        # The duplicate identity includes the show ID; refresh keys after moving files.
        from pymongo import UpdateOne
        async for records,_last_id in self.iter_file_batches(500,query={"sid":survivor}):
            key_ops=[]
            for rec in records:
                key=duplicate_key(rec)
                if key: key_ops.append(UpdateOne({"_id":rec.uid},{"$set":{"dupe_key":key}}))
                else: key_ops.append(UpdateOne({"_id":rec.uid},{"$unset":{"dupe_key":""}}))
            if key_ops: await self.files.bulk_write(key_ops,ordered=False)
        for sid in removed:
            await self.aliases.update_one({"_id":sid},{"$set":{"sid":survivor,"updated":time.time()}},upsert=True)
        # Merge user follows without creating duplicate watchlist entries.
        async for row in self.watch.find({"sids":{"$in":removed}}, {"uid":1,"sids":1}):
            sids=list(dict.fromkeys([survivor if x in removed else x for x in row.get("sids",[])]))
            await self.watch.update_one({"uid":row["uid"]},{"$set":{"sids":sids}})
        # Preserve existing progress for the survivor; otherwise transfer the newest removed record.
        async for row in self.progress.find({"sid":{"$in":removed}}):
            await self.progress.update_one({"uid":row["uid"],"sid":survivor},{"$setOnInsert":{"season":row.get("season",0),"episode":row.get("episode",0),"resolution":row.get("resolution"),"updated":row.get("updated",0)}},upsert=True)
        await self.progress.delete_many({"sid":{"$in":removed}})
        await self.requests.update_many({"sid":{"$in":removed}},{"$set":{"sid":survivor}})
        # Request documents are keyed by free-form request text in this bot and do not store show IDs.
        total=await self.files.count_documents({"sid":survivor})
        old=await self.shows.find_one({"_id":survivor}) or {}
        hits=int(old.get("hits",0))
        async for row in self.shows.find({"_id":{"$in":removed}},{"hits":1}): hits+=int(row.get("hits",0))
        await self.shows.update_one({"_id":survivor},{"$set":{"title":title,"year":year,"key":core_key(normalize(title)),"count":total,"hits":hits},"$setOnInsert":{"added":time.time()}},upsert=True)
        await self.shows.delete_many({"_id":{"$in":removed}})
        self._invalidate_show(survivor)
        for sid in removed:
            self._invalidate_show(sid)
        self._alias_cache=(0.0,{})
        return int(moved.modified_count)

    @staticmethod
    def _cluster_duplicates(items: list[dict]) -> tuple[list[dict], list[dict]]:
        """Keep the richest (ties: earliest) copy of each distinct variant; return (kept, losers)."""
        ordered = sorted(items, key=lambda d: (-richness(d), int(d.get("indexed_at") or 0), int(d.get("message_id") or 0), str(d["_id"])))
        kept: list[dict] = []
        losers: list[dict] = []
        for d in ordered:
            winner = next((k for k in kept if are_duplicate_candidates(k, d)), None)
            if winner is not None:
                d["_kept"] = winner["_id"]
                losers.append(d)
            else:
                kept.append(d)
        return kept, losers

    async def _carry_captions(self, kept: list[dict], losers: list[dict]) -> int:
        """A removed copy may hold the better caption: the surviving record always ends up with the richest one."""
        from pymongo import UpdateOne
        by_id = {d["_id"]: d for d in kept}
        best: dict = {}
        for d in losers:
            k = d.get("_kept")
            if k not in by_id: continue
            v = caption_value(d.get("caption"))
            if v > max(caption_value(by_id[k].get("caption")), best.get(k, (0, ""))[0]):
                best[k] = (v, d.get("caption") or "")
        if not best: return 0
        await self.files.bulk_write([UpdateOne({"_id": k}, {"$set": {"caption": c}}) for k, (_v, c) in best.items()], ordered=False)
        return len(best)

    async def explain_duplicates(self, title: str, season: int, episode: int) -> dict:
        """Read-only: every stored copy of one episode (across ALL catalogue entries matching the title) and, for each
        pair, whether the dedupe would treat them as one file and, if not, the exact reason."""
        shows = await self.search_shows(title, 10)
        recs = []
        for sh in shows:
            sid = str(sh["_id"])
            async for d in self.files.find({"sid": sid, "season": int(season)}):
                if int(episode) in set(d.get("episodes") or ()):
                    d["_show_title"] = sh.get("title"); d["_show_year"] = sh.get("year")
                    recs.append(d)
        recs.sort(key=lambda d: (int(d.get("indexed_at") or 0), int(d.get("message_id") or 0)))
        pairs = []
        for i in range(len(recs)):
            for j in range(i + 1, len(recs)):
                why = duplicate_conflict(recs[i], recs[j])
                pairs.append((i, j, why))
        return {"shows": shows, "records": recs, "pairs": pairs}

    @_catalogue_mutation
    async def dedupe_show(self, sid: str, dry_run: bool = False) -> dict:
        """Deduplicate ONE show. This is the single code path for every dedupe (the full run, the post-merge cleanup and
        the /dedupewhy diagnostic all use the same grouping and the same compatibility rule).

        Files are grouped by exact size + episode identity computed on the fly (nothing depends on a stored key or on
        paging through the whole collection). Dry runs write nothing. Same-size copies that are NOT merged are tallied
        by the reason they were kept apart."""
        from pymongo import UpdateOne
        sid = str(sid)
        key_projection = {"_id": 1, "sid": 1, "show": 1, "kind": 1, "channel": 1, "message_id": 1,
                          "season": 1, "episodes": 1, "size": 1, "resolution": 1, "codec": 1,
                          "source": 1, "languages": 1, "tags": 1, "group": 1, "ep_title": 1,
                          "caption": 1, "ext": 1, "special": 1, "special_label": 1, "part": 1,
                          "dupe_key": 1, "indexed_at": 1, "name": 1}
        scanned = 0
        groups: dict[str, list[dict]] = {}
        key_ops = []
        last = None
        while True:
            q = {"sid": sid}
            if last is not None:
                q["_id"] = {"$gt": last}
            docs = await self.files.find(q, key_projection).sort("_id", 1).limit(400).to_list(length=400)
            if not docs:
                break
            for d in docs:
                scanned += 1
                key = duplicate_key(SeriesFile.from_doc(d))
                if key != d.get("dupe_key"):
                    key_ops.append(UpdateOne({"_id": d["_id"]}, {"$set": {"dupe_key": key}} if key else {"$unset": {"dupe_key": ""}}))
                if key:
                    groups.setdefault(key, []).append(d)
            last = docs[-1]["_id"]
        if key_ops and not dry_run:
            for off in range(0, len(key_ops), 400):
                await self.files.bulk_write(key_ops[off:off + 400], ordered=False)
        removed = 0; captions_upgraded = 0; kept_apart: dict[str, int] = {}; examples: list[str] = []
        for items in groups.values():
            if len(items) < 2:
                continue
            _kept, losers = self._cluster_duplicates(items)
            for other in _kept[1:]:                              # same size + episode, yet deliberately kept as separate files
                why = (duplicate_conflict(_kept[0], other) or "unrelated").split(" (")[0].split(" - ")[0]
                kept_apart[why] = kept_apart.get(why, 0) + 1
            if losers:
                removed += len(losers)
                for d in losers:
                    if d.get("show") and d.get("show") not in examples and len(examples) < 12: examples.append(d.get("show"))
                if dry_run:
                    continue
                captions_upgraded += await self._carry_captions(_kept, losers)
                for offset in range(0, len(losers), 200):
                    chunk = losers[offset:offset + 200]
                    skip_ops = [UpdateOne({"_id": f"{d.get('channel')}:{d.get('message_id')}"},
                                          {"$set": {"channel": d.get("channel"), "message_id": d.get("message_id"),
                                                    "kept_uid": str(d.get("_kept", "")), "name": d.get("name") or d.get("show"),
                                                    "reason": LOOKALIKE_REASON, "category": LOOKALIKE,
                                                    "code": "possible_duplicate", "at": time.time()}}, upsert=True)
                                for d in chunk]
                    if skip_ops:
                        await self.skips.bulk_write(skip_ops, ordered=False)
                    await self.files.delete_many({"_id": {"$in": [d["_id"] for d in chunk]}})
        if removed and not dry_run:
            await self.refresh_show_counts([sid])
        self._invalidate_show(sid)
        return {"scanned": scanned, "removed": removed, "captions_upgraded": captions_upgraded,
                "kept_apart": kept_apart, "examples": examples, "dry": bool(dry_run)}

    @_catalogue_mutation
    async def dedupe_series(self, dry_run=False, progress=None, batch_size=400):
        """Find/remove catalogue look-alikes in every show without changing Telegram posts (one show at a time)."""
        import asyncio
        scanned = removed = shows_done = 0
        examples: list[str] = []; kept_apart: dict[str, int] = {}
        # Every show that has files, taken from the files themselves (a missing show entry can never hide duplicates).
        cur = await self.files.aggregate([{"$group": {"_id": "$sid"}}], allowDiskUse=True)
        sids = sorted({str(g["_id"]) async for g in cur if g.get("_id") is not None})
        for sid in sids:
            r = await self.dedupe_show(sid, dry_run=dry_run)
            scanned += r["scanned"]; removed += r["removed"]; shows_done += 1
            for t in r["examples"]:
                if t not in examples and len(examples) < 12: examples.append(t)
            for k, v in r["kept_apart"].items(): kept_apart[k] = kept_apart.get(k, 0) + v
            if progress and shows_done % 100 == 0:
                try: await progress.edit_text(f"{'Read-only duplicate check' if dry_run else 'Removing look-alikes'}… {shows_done:,}/{len(sids):,} shows, {scanned:,} files checked; {removed:,} duplicates {'found' if dry_run else 'removed'}.")
                except Exception: pass
            await asyncio.sleep(0.01)
        self.clear_cache()
        out = {"scanned": scanned, "removed": removed, "examples": examples, "kept_apart": kept_apart, "shows": shows_done}
        if dry_run: out["dry"] = True
        return out

    async def add_movie(self, sid: str, channel: int, message_id: int, name: str = "", size: int = 0):
        show=await self.shows.find_one({"_id":str(sid)},{"_id":1})
        if not show: raise ValueError("Parent show not found.")
        raw=f"{int(channel)}:{int(message_id)}"
        key=hashlib.sha1(raw.encode()).hexdigest()[:18]
        await self.movies.update_one({"_id":key},{"$set":{"sid":str(sid),"channel":int(channel),"message_id":int(message_id),"name":str(name or "Movie")[:300],"size":int(size or 0),"indexed_at":time.time()}},upsert=True)
        self._invalidate_show(sid)
        # Reattaching the same source message may move it from another show.
        # This rare admin action clears all cached movie lists after the write.
        for cache_key in list(self._cache):
            if cache_key[0]=="movies": self._cache.pop(cache_key,None)
        return key

    async def show_movies(self, sid: str):
        key=("movies",str(sid))
        cached=self._cache_get(key)
        if cached is None:
            cached=tuple([d async for d in self.movies.find({"sid":str(sid)}).sort("indexed_at",-1).limit(100)])
            self._cache_set(key,cached)
        return [dict(d) for d in cached]

    async def shows_with_exact_title(self, title: str, limit: int = 50) -> list:
        """Catalogue entries whose stored title is EXACTLY this text (case-sensitive, no fuzzy matching)."""
        return [d async for d in self.shows.find({"title": title}).limit(limit)]

    async def sample_files_by_show(self, text: str, limit: int = 6) -> list:
        """A few stored records whose show title contains the text (for the /showinfo diagnostic)."""
        cur=self.files.find({"show":{"$regex":re.escape(text),"$options":"i"}}).limit(limit)
        return [SeriesFile.from_doc(d) for d in await cur.to_list(length=limit)]

    async def iter_file_batches(self,batch_size:int=100,after_id=None,query=None,projection=None):
        """Yield bounded pages using a fresh query each time; never keep a long-lived Mongo cursor."""
        size=max(1,min(int(batch_size),500))
        last=after_id
        while True:
            filters=dict(query or {})
            if last is not None:
                filters["_id"]={"$gt":last}
            cursor=self.files.find(filters,projection).sort("_id",1).limit(size)
            docs=await cursor.to_list(length=size)
            if not docs:
                break
            records=[SeriesFile.from_doc(d) for d in docs]
            last=docs[-1]["_id"]
            yield records,last
            if len(docs)<size:
                break

    async def iter_files(self,batch_size:int=500,after_id=None):
        """Compatibility iterator backed by short keyset-paginated queries."""
        async for records,_last in self.iter_file_batches(batch_size,after_id):
            for record in records:
                yield record


    async def save_merge_review(self, candidates, scanned=0):
        """Persist a fresh, explicitly reviewable merge-candidate list."""
        await self.maintenance_state.update_one(
            {"_id": "merge_review"},
            {"$set": {"candidates": candidates, "scanned": int(scanned), "created": time.time()}},
            upsert=True,
        )

    async def get_merge_review(self):
        return await self.maintenance_state.find_one({"_id": "merge_review"})

    async def remove_merge_review_candidate(self, candidate_id):
        row = await self.get_merge_review() or {}
        candidates = [c for c in row.get("candidates", []) if str(c.get("id")) != str(candidate_id)]
        await self.maintenance_state.update_one(
            {"_id": "merge_review"},
            {"$set": {"candidates": candidates, "updated": time.time()}},
            upsert=True,
        )
        return len(candidates)

    async def get_reparse_checkpoint(self):
        row=await self.maintenance_state.find_one({"_id":"reparse"})
        return row.get("last_id") if row else None

    async def set_reparse_checkpoint(self,last_id):
        await self.maintenance_state.update_one({"_id":"reparse"},{"$set":{"last_id":last_id,"updated":time.time()}},upsert=True)

    async def clear_reparse_checkpoint(self):
        await self.maintenance_state.delete_one({"_id":"reparse"})

    async def counts(self): return {"files":await self.files.count_documents({}),"shows":await self.shows.count_documents({})}
    def clear_cache(self):
        self._cache.clear(); self._alias_cache=(0.0,{})

    @_catalogue_mutation
    async def bulk_update_files(self, news: list) -> int:
        """Write several re-parsed records in ONE round trip (shows are rebuilt afterwards)."""
        if not news: return 0
        from pymongo import UpdateOne
        ops=[]
        touched=set()
        current = {d["_id"]: d async for d in self.files.find({"_id": {"$in": [n.uid for n in news]}}, {"sid": 1, "show": 1, "year": 1, "manual_title": 1, "adopted": 1})}
        sids = list({d.get("sid") for d in current.values() if d.get("sid")})
        locks = {d["_id"]: d async for d in self.shows.find({"_id": {"$in": sids}, "manual_title": True}, {"title": 1, "year": 1})}
        for n in await self._canonicalize_batch(news):
            old = current.get(n.uid)
            if not old or old.get("adopted"): continue
            lock = locks.get(old.get("sid"))
            if old.get("manual_title") or lock:
                n = replace(n, sid=old["sid"], show=(lock or {}).get("title") or old.get("show"), year=(lock or old).get("year"), manual_title=True)
            doc=n.as_doc(); doc.pop("_id",None)
            ops.append(UpdateOne({"_id":n.uid},{"$set":doc}))
            if n.sid:
                touched.add(str(n.sid))
        if ops: await self.files.bulk_write(ops,ordered=False)
        for sid in touched:
            self._invalidate_show(sid)
        return len(ops)

    @_catalogue_mutation
    async def rebuild_shows(self, progress=None) -> dict:
        """Make the shows collection match the files exactly: one doc per show id.

        Fixes shows that vanished (files with no show doc), empty shows and wrong
        counts. Hits/added dates of existing shows are kept."""
        from pymongo import UpdateOne
        cur=await self.files.aggregate([{"$group":{"_id":"$sid","title":{"$first":"$show"},"year":{"$first":"$year"},
                                                   "count":{"$sum":1},"added":{"$min":"$indexed_at"}}}],allowDiskUse=True)
        have={}; created=fixed=0; ops=[]
        existing={}
        async for d in self.shows.find({},{"count":1,"title":1,"year":1,"manual_title":1,"title_source":1}):
            if d["_id"] not in existing or d.get("manual_title"):
                existing[d["_id"]] = d
        async for g in cur:
            sid=g["_id"]; have[sid]=1
            prev=existing.get(sid) or {}
            is_manual=bool(prev.get("manual_title",False))
            title=(prev.get("title") if is_manual and prev.get("title") else None) or g.get("title") or ""
            year=prev.get("year") if is_manual else g.get("year")
            if not title: continue
            if sid not in existing: created+=1
            elif int(prev.get("count",0))!=int(g["count"]): fixed+=1
            added=(g.get("added") or 0)/1e9 or time.time()
            set_doc={"title":title,"year":year,"key":core_key(normalize(title)),"count":int(g["count"])}
            if is_manual:
                set_doc["manual_title"]=True
                set_doc["title_source"]=prev.get("title_source") or "manual"
            ops.append(UpdateOne({"_id":sid},{"$set":set_doc,
                                              "$setOnInsert":{"added":added,"hits":0}},upsert=True))
            if len(ops)>=500:
                await self.shows.bulk_write(ops,ordered=False); ops=[]
        if ops: await self.shows.bulk_write(ops,ordered=False)
        stale=[sid for sid in existing if sid not in have and not existing[sid].get("manual_title")]
        if not have and existing: stale=[]        # never wipe the shows list if the files look empty
        removed=0
        for i in range(0,len(stale),500):
            r=await self.shows.delete_many({"_id":{"$in":stale[i:i+500]}}); removed+=r.deleted_count
        self.clear_cache()
        return {"shows":len(have),"created":created,"count_fixed":fixed,"removed_empty":removed}

    async def audit(self) -> dict:
        """Cheap health report: sizes, show/file mismatches and the biggest shows."""
        files=await self.files.count_documents({}); shows=await self.shows.count_documents({})
        cur=await self.files.aggregate([{"$group":{"_id":"$sid","n":{"$sum":1},"title":{"$first":"$show"},"year":{"$first":"$year"}}}],allowDiskUse=True)
        groups=[g async for g in cur]
        ids={d["_id"] async for d in self.shows.find({},{"_id":1})}
        orphans=sum(1 for g in groups if g["_id"] not in ids)
        sids={g["_id"] for g in groups}
        empty=sum(1 for i in ids if i not in sids)
        top=sorted(groups,key=lambda g:-g["n"])[:15]
        singles=sum(1 for g in groups if g["n"]==1)
        return {"files":files,"shows":shows,"sids":len(groups),"orphan_sids":orphans,"empty_shows":empty,
                "single_file_shows":singles,"top":[(g.get("title"),g.get("year"),g["n"]) for g in top]}

    async def cross_database_audit(self) -> dict:
        """Read-only bounded cross-database audit, no reservations or writes.

        Look-alikes use the same compatibility predicate as indexing, including
        legacy keys. Results are observations, not a transaction snapshot.
        """
        from multidb import FederatedDB
        if not isinstance(self._db, FederatedDB):
            return {"configured": False}
        p, q = self._db.primary, self._db.secondary
        out = {"configured": True, "primary": {}, "secondary": {}, "duplicate_uids": 0,
               "lookalike_records": 0, "duplicate_skips": 0, "potential_duplicate_shows": 0,
               "conflicting_aliases": 0, "conflicting_manual_titles": 0, "unmatched_files": 0}
        for label, db in (("primary", p), ("secondary", q)):
            for name in ("series_files", "series_shows", "series_skips", "series_show_aliases"):
                n = await db[name].count_documents({})
                try:
                    st = await db.command("collStats", name)
                except Exception:
                    st = {}
                out[label][name] = {"count": n, "data": st.get("size"), "indexes": st.get("totalIndexSize")}
        # Scan the smaller side in keyset batches. Query indexed sid and _id
        # on the other side; avoid loading either file catalogue in RAM.
        for name in ("series_files", "series_skips", "series_shows", "series_show_aliases"):
            last = None
            while True:
                f = {} if last is None else {"_id": {"$gt": last}}
                batch = await q[name].find(f).sort("_id", 1).limit(250).to_list(length=250)
                if not batch: break
                last = batch[-1]["_id"]
                ids = [d["_id"] for d in batch]
                other = {d["_id"]: d async for d in p[name].find({"_id": {"$in": ids}})}
                if name == "series_files":
                    sids = {d.get("sid") for d in batch if d.get("sid")}
                    sizes = {d.get("size") for d in batch if d.get("size")}
                    candidates = {}
                    if sids and sizes:
                        for d in batch:
                            if d.get("size") and d.get("sid"):
                                candidates.setdefault((d["sid"], d["size"]), []).append(d)
                    matched = set()
                    if candidates:
                        async for old in p[name].find({"sid": {"$in": list(sids)}, "size": {"$in": list(sizes)}}):
                            for d in candidates.get((old.get("sid"), old.get("size")), ()):
                                if d["_id"] != old["_id"] and are_duplicate_candidates(old, d): matched.add(d["_id"])
                    out["duplicate_uids"] += len(other)
                    out["lookalike_records"] += len(matched - set(other))
                elif name == "series_skips":
                    out["duplicate_skips"] += len(other)
                elif name == "series_show_aliases":
                    out["conflicting_aliases"] += sum(d.get("sid") != other[d["_id"]].get("sid") for d in batch if d["_id"] in other)
                else:
                    keys = {d.get("key") for d in batch if d.get("key")}
                    key_matches = {}
                    if keys:
                        async for d in p[name].find({"key": {"$in": list(keys)}}):
                            key_matches.setdefault(d.get("key"), []).append(d)
                    for d in batch:
                        matches = {x["_id"]: x for x in key_matches.get(d.get("key"), []) if x.get("year") == d.get("year")}
                        if d["_id"] in other: matches[d["_id"]] = other[d["_id"]]
                        if matches: out["potential_duplicate_shows"] += 1
                        if d.get("manual_title") and any(x.get("manual_title") and (x.get("title"), x.get("year")) != (d.get("title"), d.get("year")) for x in matches.values()):
                            out["conflicting_manual_titles"] += 1
                if len(batch) < 250: break
        # Count files whose show identity cannot be resolved in either database.
        for db in (p, q):
            cur = await db.series_files.aggregate([{"$group": {"_id": "$sid", "n": {"$sum": 1}}}])
            groups = []
            async def count_unmatched(rows):
                ids = [d["_id"] for d in rows]
                found = {d["_id"] async for d in self.shows.find({"_id": {"$in": ids}}, {"_id": 1})}
                return sum(d["n"] for d in rows if d["_id"] not in found)
            async for d in cur:
                groups.append(d)
                if len(groups) >= 250:
                    out["unmatched_files"] += await count_unmatched(groups); groups = []
            if groups: out["unmatched_files"] += await count_unmatched(groups)
        return out

    async def episode_first_preview(self, promos=(), rules=None):
        """Read-only impact preview for previously unsupported episode-first posts."""
        scanned = affected = 0
        examples = []
        async for batch in self.iter_skips(category=UNRECOGNISED):
            for d in batch:
                if d.get("code") != "unsupported_episode_format": continue
                scanned += 1
                name = str(d.get("name") or "")
                clean = _strip_custom_promos(name, effective_promos(promos))
                if not re.match(r"(?i)^\s*(?:@[\w.]+[\s._-]+)?E\d{1,4}[\s._-]+", clean): continue
                parsed, _ = _resolve_parsed(name, d.get("caption", ""), promos, rules=rules)
                if parsed.kind != "episode" or not parsed.show or not parsed.episodes: continue
                affected += 1
                if len(examples) < 20:
                    examples.append((name, parsed.show, parsed.episodes))
        return {"scanned": scanned, "affected": affected, "examples": examples}

    @_catalogue_mutation
    async def refresh_show_counts(self, sids, dest=None) -> int:
        """Recount just the named shows after files moved between them (no full-catalogue pass).

        ``dest`` = (sid, title, year) is created if missing; any other named show left with no files is
        removed. Returns how many emptied shows were removed."""
        ids={str(x) for x in (sids or ()) if x}
        if dest: ids.add(str(dest[0]))
        removed=0
        for sid in ids:
            n=await self.files.count_documents({"sid":sid})
            existing = await self.shows.find_one({"_id": sid}, {"manual_title": 1}) or {}
            if existing.get("manual_title"):
                await self.shows.update_one({"_id":sid},{"$set":{"count":n}})
            elif n<=0:
                r=await self.shows.delete_one({"_id":sid}); removed+=int(getattr(r,"deleted_count",0) or 0)
            elif dest and sid==str(dest[0]):
                await self.shows.update_one({"_id":sid},{"$set":{"title":dest[1],"year":dest[2],"key":core_key(normalize(dest[1])),"count":n},
                                                          "$setOnInsert":{"added":time.time(),"hits":0}},upsert=True)
            else:
                await self.shows.update_one({"_id":sid},{"$set":{"count":n}})
        self.clear_cache()
        return removed

    @_catalogue_mutation
    async def rehome_files(self, uids, title: str, year: int | None) -> int:
        """Move specific files to the show (title, year), keeping season/episode data untouched.

        The destination show is created if missing; any show left empty is removed; look-alike keys are
        refreshed because they include the show id. Returns files moved. Used only by /fixtitles.
        """
        ids=[str(u) for u in uids]
        if not ids: return 0
        sid=show_id(title,year)
        rows=[d async for d in self.files.find({"_id":{"$in":ids},"manual_title":{"$ne":True},"adopted":{"$ne":True}},{"sid":1})]
        locked={d["_id"] async for d in self.shows.find({"_id":{"$in":list({d.get("sid") for d in rows})},"manual_title":True},{"_id":1})}
        rows=[d for d in rows if d.get("sid") not in locked]
        ids=[d["_id"] for d in rows]
        if not ids: return 0
        sid=await self.resolve_alias(sid)
        dest=await self.shows.find_one({"_id":sid}) or {}
        title,year=dest.get("title") or title,dest.get("year",year)
        old={d.get("sid") for d in rows}
        fields={"sid":sid,"show":title,"year":year}
        if dest.get("manual_title"): fields["manual_title"]=True
        r=await self.files.update_many({"_id":{"$in":ids}},{"$set":fields})
        from pymongo import UpdateOne
        ops=[]
        async for records,_last in self.iter_file_batches(500,query={"_id":{"$in":ids}}):
            for rec in records:
                key=duplicate_key(rec)
                ops.append(UpdateOne({"_id":rec.uid},{"$set":{"dupe_key":key}} if key else {"$unset":{"dupe_key":""}}))
        if ops: await self.files.bulk_write(ops,ordered=False)
        await self.refresh_show_counts([x for x in old if x and x!=sid],dest=(sid,title,year))
        return int(getattr(r,"modified_count",0))

    async def lookalike_extras(self, sid: str) -> int:
        """How many files of one show are same-size look-alikes of another file (read-only)."""
        buckets: dict[str, list] = {}
        extra=0
        async for records,_last in self.iter_file_batches(500,query={"sid":str(sid)}):
            for r in records:
                k=duplicate_key(r)
                if not k: continue
                grp=buckets.setdefault(k,[])
                if any(are_duplicate_candidates(prev,r) for prev in grp): extra+=1
                else: grp.append(r)
        return extra

    @_catalogue_mutation
    async def update_reparsed(self, old: SeriesFile, new: SeriesFile) -> None:
        current = await self.get_file(old.uid)
        if not current or current.adopted: return
        locked = await self.shows.find_one({"_id": current.sid, "manual_title": True})
        if current.manual_title or locked:
            new = replace(new, sid=current.sid, show=(locked or {}).get("title") or current.show, year=(locked or {}).get("year", current.year), manual_title=True)
        else:
            new = await self.canonicalize_record(new)
        old = current
        doc=new.as_doc(); doc.pop("_id",None)
        await self.files.update_one({"_id":old.uid},{"$set":doc})
        if old.sid != new.sid:
            await self.shows.update_one({"_id":old.sid},{"$inc":{"count":-1}})
            await self.shows.delete_one({"_id":old.sid,"count":{"$lte":0},"manual_title":{"$ne":True}})
            await self.shows.update_one({"_id":new.sid},{"$setOnInsert":{"title":new.show,"year":new.year,"key":core_key(normalize(new.show)),"added":time.time(),"hits":0},"$inc":{"count":1}},upsert=True)
        else:
            await self.shows.update_one({"_id":new.sid},{"$set":{"title":new.show,"year":new.year,"key":core_key(normalize(new.show))}})
        self._invalidate_show(old.sid); self._invalidate_show(new.sid)

