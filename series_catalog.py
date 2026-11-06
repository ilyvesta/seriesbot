"""Series-specific persistent catalog. Uses the bot's own Mongo database only.

Records are bounded and queried by show/season; no collection is loaded wholesale.
"""
from __future__ import annotations
import hashlib, re, time
from collections import OrderedDict
from dataclasses import dataclass, asdict, replace
from normalize import normalize, core_key
from parse import parse_series, strip_title_groups
from series_parse import is_generic_media_name

VIDEO_EXTS = {"mkv", "mp4", "avi", "mov", "m4v", "webm", "ts", "wmv", "flv", "mpg", "mpeg"}
SUB_EXTS = {"srt", "ass", "ssa", "vtt", "sub", "idx"}


def show_id(title: str, year: int | None = None) -> str:
    key = core_key(normalize(title)) + (f"-{year}" if year else "")
    return hashlib.sha1(key.encode()).hexdigest()[:12]

def duplicate_key(r: "SeriesFile") -> str | None:
    """Stable look-alike key; encoder/group is deliberately not part of identity."""
    if not r.size or r.size <= 0 or (r.ext or "").casefold() in SUB_EXTS:
        return None
    raw="|".join((r.sid,str(r.season),",".join(map(str,r.episodes)),str(int(r.size)),
                  (r.resolution or "").casefold(),(r.codec or "").casefold(),(r.ext or "").casefold(),str(bool(getattr(r,"special",False))), (r.special_label or "").casefold()))
    return hashlib.sha1(raw.encode()).hexdigest()

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
                   tuple(d.get("languages", ())), d.get("group"), tuple(d.get("tags", ())), d.get("ext"), d.get("caption", ""), int(d.get("indexed_at", 0) or 0), bool(d.get("special", False)), d.get("special_label", ""))


def _strip_custom_promos(value: str, promos=()) -> str:
    """Strip configured uploader tokens only as complete, delimited phrases.

    Separators inside a configured phrase are interchangeable (F - D matches
    F_D), but at least one separator is required between its words. This keeps
    a token such as F-D from matching the ordinary word/suffix "FD".
    """
    text=(value or "").strip()
    m=re.search(r"(?i)(\.[a-z0-9]{2,5})$",text)
    ext=m.group(1) if m else ""
    if ext: text=text[:-len(ext)]
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
    """Reject alias destinations that are themselves uploader/promo labels."""
    title=" ".join(str(value or "").split()).strip()
    if not title: return False
    clean=strip_title_groups(_strip_custom_promos(title,promos),promos)
    if clean.casefold()!=title.casefold(): return False
    key=core_key(normalize(title))
    promo_keys={core_key(normalize(str(x))) for x in (promos or ()) if str(x).strip()}
    return key not in promo_keys


def _parse_series_text(value: str, promos=()):
    """Try caption lines independently so multi-line promo text cannot hide the title."""
    for line in (value or "").splitlines():
        line=_strip_custom_promos(line.strip(),promos)
        if not line: continue
        parsed=parse_series(line)
        if parsed.kind in ("episode","pack") and parsed.show:
            return parsed
    return parse_series(_strip_custom_promos((value or "").strip(),promos))


def from_message(m, promos=(), aliases=None) -> SeriesFile | None:
    media = getattr(m, "document", None) or getattr(m, "video", None) or getattr(m, "audio", None)
    if not media:
        return None
    name = (getattr(media, "file_name", None) or "").strip()
    caption = (getattr(m, "caption", None) or "").strip()
    generic_name = is_generic_media_name(name)
    if generic_name:
        parsed=_parse_series_text(caption,promos) if caption else parse_series("")
    else:
        parsed=parse_series(_strip_custom_promos(name or caption,promos))
    if parsed.kind not in ("episode", "pack") and caption:
        parsed = _parse_series_text(caption,promos)
    if parsed.kind not in ("episode", "pack") or not parsed.show:
        return None
    ext = (parsed.ext or (name.rsplit(".", 1)[-1] if "." in name else "")).lower() or None
    uid = getattr(media, "file_unique_id", None)
    if not uid:
        return None
    parsed = replace(parsed, show=strip_title_groups(parsed.show, promos))
    sid = show_id(parsed.show, parsed.year)
    alias=(aliases or {}).get(sid)
    if alias:
        if isinstance(alias,(tuple,list)):
            alias_title=str(alias[1] or parsed.show)
            # A stale alias must not reintroduce a configured uploader token
            # that the filename parser has just removed (e.g. Judas Bleach).
            if alias_title_is_safe(alias_title,promos):
                sid=str(alias[0]); parsed=replace(parsed,show=alias_title,year=alias[2] if len(alias)>2 else parsed.year)
        else:
            sid=str(alias)
    return SeriesFile(str(uid), sid, parsed.show, parsed.year, parsed.kind, parsed.season, parsed.episodes,
                      parsed.absolute, parsed.ep_title, int(m.chat.id), int(m.id), int(getattr(media, "file_size", 0) or 0),
                      name[:500], parsed.resolution, parsed.source, parsed.codec, tuple(parsed.languages), parsed.group,
                      tuple(parsed.tags), ext, caption[:1000], time.time_ns(), parsed.special, parsed.special_label)

class MongoSeriesCatalog:
    def __init__(self, db):
        self.files=db.series_files; self.shows=db.series_shows; self.skips=db.series_skips; self.watch=db.series_watchlists; self.requests=db.requests; self.progress=db.series_progress; self.user_stats_col=db.series_user_stats; self.aliases=db.series_show_aliases
        self.maintenance_state=db.series_maintenance; self.movies=db.series_movies
        self._cache=OrderedDict(); self._alias_cache=(0.0,{})
    def _cache_get(self,key):
        now=time.monotonic(); item=self._cache.get(key)
        if not item: return None
        if item[0]<=now:
            self._cache.pop(key,None); return None
        self._cache.move_to_end(key); return item[1]
    def _cache_set(self,key,value):
        self._cache[key]=(time.monotonic()+600,value); self._cache.move_to_end(key)
        while len(self._cache)>300: self._cache.popitem(last=False)
    def _invalidate_show(self,sid):
        self._cache.pop(("show",str(sid)),None)
        # Search pages/counts are query-dependent; clearing them also clears any
        # cached result list that may have contained the changed show.
        for key in list(self._cache):
            if key[0] in ("search","count"): self._cache.pop(key,None)
    async def setup(self):
        await self.files.create_index([("sid",1),("season",1),("episodes",1)])
        await self.files.create_index("dupe_key", sparse=True)
        await self.files.create_index([("channel",1),("message_id",1)])
        await self.shows.create_index([("key",1)])
        await self.shows.create_index([("added",-1)])
        await self.shows.create_index([("hits",-1)])
        await self.skips.create_index([("at",-1)])
        await self.watch.create_index([("uid",1)],unique=True)
        await self.progress.create_index([("uid",1),("sid",1)],unique=True)
        await self.user_stats_col.create_index("uid",unique=True)
        await self.aliases.create_index("sid")
        await self.movies.create_index([("sid",1),("indexed_at",-1)])
        await self.movies.create_index([("channel",1),("message_id",1)],unique=True)
    async def add(self, records: list[SeriesFile], include_added: bool = False):
        if not records: return 0,0,0
        from pymongo import UpdateOne
        unique={r.uid:r for r in records}
        # Alias resolution happens once in from_message(), where configured promo
        # tokens can be checked before a destination title is accepted. Reapplying
        # aliases here used to bypass that validation and could redirect a clean
        # parsed title to a contaminated catalogue entry.
        for r in unique.values(): self._invalidate_show(r.sid)
        known={d["_id"] async for d in self.files.find({"_id":{"$in":list(unique)}},{"_id":1})}
        fresh=[]; skipped_dupes=[]; dupes=0; seen_keys=set()
        candidates=[r for uid,r in unique.items() if uid not in known]
        candidates.sort(key=lambda r:(int(r.indexed_at or 0),int(r.message_id),str(r.uid)))
        dkeys=list({dk for r in candidates if (dk:=duplicate_key(r))})
        existing_keys={d["dupe_key"]:d["_id"] async for d in self.files.find({"dupe_key":{"$in":dkeys}},{"_id":1,"dupe_key":1})} if dkeys else {}
        for r in candidates:
            dk=duplicate_key(r)
            if dk and (dk in existing_keys or dk in seen_keys):
                keep=existing_keys.get(dk,"same-batch")
                skipped_dupes.append(r)
                dupes+=1; continue
            if dk:
                seen_keys.add(dk); existing_keys[dk]=r.uid
            fresh.append(r)
        if skipped_dupes:
            skip_ops=[]
            for r in skipped_dupes:
                skip_ops.append(UpdateOne({"_id":f"{r.channel}:{r.message_id}"},{"$set":{"channel":r.channel,"message_id":r.message_id,"name":r.name or r.show,"reason":"Look-alike (same size)","at":time.time()}},upsert=True))
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
            self._invalidate_show(sid)
            await self.shows.update_one({"_id":sid},{"$setOnInsert":{"title":d["title"],"year":d["year"],"key":d["key"],"added":time.time(),"hits":0},"$inc":{"count":d["n"]}},upsert=True)
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
        filt={"sid":sid}
        if season is not None: filt["season"]=season
        cur=self.files.find(filt,batch_size=500)
        out=[SeriesFile.from_doc(d) async for d in cur]
        return sorted(out,key=lambda f:(f.season,f.episodes[0] if f.episodes else 9999,f.kind,f.name.lower()))
    async def add_skip_record(self,r,reason="Filename did not identify a series"):
        key=f"{r.channel}:{r.message_id}"
        await self.skips.update_one({"_id":key},{"$set":{"channel":r.channel,"message_id":r.message_id,"name":r.name or r.show,"reason":reason,"at":time.time()}},upsert=True)
    async def add_skip_payloads(self, payloads, reason="Filename did not identify a series"):
        if not payloads: return 0
        from pymongo import UpdateOne
        ops=[]
        for p in payloads:
            ch=int(p["chat_id"]); mid=int(p["message_id"])
            name=p.get("file_name") or p.get("caption") or "(no filename)"
            ops.append(UpdateOne({"_id":f"{ch}:{mid}"},{"$set":{"channel":ch,"message_id":mid,"name":str(name)[:500],"reason":reason,"at":time.time()}},upsert=True))
        if ops: await self.skips.bulk_write(ops,ordered=False)
        return len(ops)

    async def add_skip(self,m,reason="Filename did not identify a series"):
        media=getattr(m,"document",None) or getattr(m,"video",None) or getattr(m,"audio",None)
        key=f"{m.chat.id}:{m.id}"
        await self.skips.update_one({"_id":key},{"$set":{"channel":m.chat.id,"message_id":m.id,"name":getattr(media,"file_name",None) or getattr(m,"caption",None) or "(no filename)","reason":reason,"at":time.time()}},upsert=True)
    async def clear_skip(self, channel, message_id):
        await self.skips.delete_one({"_id":f"{channel}:{message_id}"})
    async def skipped_rows(self,limit=12,offset=0):
        return [d async for d in self.skips.find({},batch_size=limit).sort("at",-1).skip(offset).limit(limit)]
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
    async def remove_file(self,uid):
        d=await self.files.find_one_and_delete({"_id":uid})
        if not d: return None
        r=SeriesFile.from_doc(d)
        self._invalidate_show(r.sid)
        await self.shows.update_one({"_id":r.sid},{"$inc":{"count":-1}})
        await self.shows.delete_one({"_id":r.sid,"count":{"$lte":0}})
        return r
    async def canonicalize_record(self, record):
        """Resolve one record's show alias without scanning the entire alias collection."""
        target=await self.resolve_alias(record.sid)
        if target==record.sid: return record
        show=await self.shows.find_one({"_id":target},{"title":1,"year":1})
        if not show: return record
        from dataclasses import replace
        return replace(record,sid=target,show=show.get("title") or record.show,year=show.get("year",record.year))

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
        await self.files.update_many({"sid":str(sid)},{"$set":{"show":new_title,"year":year}})
        await self.shows.update_one({"_id":str(sid)},{"$set":{"title":new_title,"year":year,"key":core_key(normalize(new_title))}})
        # Keep the original stable ID mapped to itself so filenames that still
        # parse under the pre-rename title resolve to the renamed catalogue entry.
        await self.aliases.update_one({"_id":str(sid)},{"$set":{"sid":str(sid),"updated":time.time()}},upsert=True)
        if new_sid!=str(sid):
            await self.aliases.update_one({"_id":new_sid},{"$set":{"sid":str(sid),"updated":time.time()}},upsert=True)
        self.clear_cache()
        return old.get("title","Series"),new_title

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
        shows={str(d["_id"]):d async for d in self.shows.find({"_id":{"$in":list(canonical)}},{"title":1,"year":1})} if canonical else {}
        result={}
        for old,target in mapping.items():
            d=shows.get(target)
            if d: result[old]=(target,d.get("title"),d.get("year"))
        self._alias_cache=(now,result)
        return result

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
    async def merge_show_ids(self, survivor: str, removed: list[str], title: str, year: int | None):
        removed=list(set(removed)-{survivor})
        if not removed: return 0
        moved=await self.files.update_many({"sid":{"$in":removed}},{"$set":{"sid":survivor,"show":title,"year":year}})
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
        self._invalidate_show(survivor)
        for sid in removed:
            self._invalidate_show(sid)
            await self.aliases.update_one({"_id":sid},{"$set":{"sid":survivor,"updated":time.time()}},upsert=True)
        self._alias_cache=(0.0,{})
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
        return int(moved.modified_count)

    async def dedupe_series(self, dry_run=False, progress=None, batch_size=400):
        """Find/remove catalogue look-alikes without changing Telegram posts.

        Dry runs are strictly read-only. Real runs prepare indexed keys in bounded
        batches, then stream key groups and delete only the later catalogue copies.
        """
        import asyncio
        from pymongo import UpdateOne
        scanned=0; last=None
        # Fields required to reproduce duplicate_key; never transfer captions or other
        # unrelated metadata during this full-catalogue operation.
        key_projection={"_id":1,"sid":1,"show":1,"kind":1,"channel":1,"message_id":1,"season":1,"episodes":1,"size":1,
                        "resolution":1,"codec":1,"ext":1,"special":1,
                        "special_label":1,"dupe_key":1,"indexed_at":1,
                        "name":1}
        if dry_run:
            # Do not write to MongoDB. SQLite keeps the seen-key set on temporary
            # disk instead of retaining hundreds of thousands of Python strings
            # in the bot's 512 MB memory budget.
            import sqlite3, tempfile, os
            temp_path=None; db=None; seen_fallback=None
            try:
                fd, temp_path=tempfile.mkstemp(prefix="series-dedupe-",suffix=".sqlite",dir=tempfile.gettempdir())
                os.close(fd)
                db=sqlite3.connect(temp_path)
                db.execute("CREATE TABLE seen (k TEXT PRIMARY KEY)")
            except (OSError, sqlite3.Error):
                if db is not None:
                    try: db.close()
                    except Exception: pass
                db=None
                seen_fallback=set()
            try:
                removed=0; examples=[]
                while True:
                    q={"_id":{"$gt":last}} if last is not None else {}
                    docs=await self.files.find(q,key_projection).sort("_id",1).limit(batch_size).to_list(length=batch_size)
                    if not docs: break
                    for d in docs:
                        scanned+=1
                        key=duplicate_key(SeriesFile.from_doc(d))
                        if not key: continue
                        if db is not None:
                            cur=db.execute("INSERT OR IGNORE INTO seen(k) VALUES (?)",(key,))
                            duplicate=(cur.rowcount==0)
                        else:
                            duplicate=key in seen_fallback
                            seen_fallback.add(key)
                        if duplicate:
                            removed+=1
                            if d.get("show") not in examples and len(examples)<12: examples.append(d.get("show"))
                    if db is not None: db.commit()
                    last=docs[-1]["_id"]
                    if progress and scanned % 4000 < batch_size:
                        try: await progress.edit_text(f"Read-only duplicate check… {scanned:,} files checked; {removed:,} duplicates found.")
                        except Exception: pass
                    await asyncio.sleep(0.05)
                return {"scanned":scanned,"removed":removed,"examples":examples,"dry":True}
            finally:
                if db is not None: db.close()
                if temp_path:
                    try: os.remove(temp_path)
                    except OSError: pass

        # Pass 1: calculate keys and write only changed keys, in bounded bulk writes.
        last=None
        while True:
            q={"_id":{"$gt":last}} if last is not None else {}
            docs=await self.files.find(q,key_projection).sort("_id",1).limit(batch_size).to_list(length=batch_size)
            if not docs: break
            ops=[]
            for d in docs:
                scanned+=1
                key=duplicate_key(SeriesFile.from_doc(d))
                if key != d.get("dupe_key"):
                    ops.append(UpdateOne({"_id":d["_id"]},{"$set":{"dupe_key":key}} if key else {"$unset":{"dupe_key":""}}))
            if ops: await self.files.bulk_write(ops,ordered=False)
            last=docs[-1]["_id"]
            if progress and scanned % 4000 < batch_size:
                try: await progress.edit_text(f"Preparing duplicate index… {scanned:,} files checked.")
                except Exception: pass
            await asyncio.sleep(0.05)

        removed=0; examples=[]; key_after=None; page=1000
        proj={"dupe_key":1,"indexed_at":1,"message_id":1,"channel":1,"name":1,"show":1}
        while True:
            q={"dupe_key":{"$exists":True}} if key_after is None else {"dupe_key":{"$gt":key_after}}
            docs=await self.files.find(q,proj).sort("dupe_key",1).limit(page).to_list(length=page)
            if not docs: break
            groups={}
            for d in docs: groups.setdefault(d["dupe_key"],[]).append(d)
            # A key may cross a page boundary. Re-read only that single key's
            # compact projected records; never fetch all full documents.
            edge=docs[-1]["dupe_key"] if len(docs)==page else None
            if edge is not None:
                # Always defer the final key to a complete lookup; it may continue
                # beyond this page even when only one copy is present in this page.
                groups.pop(edge,None)
                groups[edge]=await self.files.find({"dupe_key":edge},proj).sort("indexed_at",1).to_list(length=None)
            for key,items in groups.items():
                if len(items)<2: continue
                items.sort(key=lambda d:(int(d.get("indexed_at") or 0),int(d.get("message_id") or 0),str(d["_id"])))
                losers=items[1:]
                removed+=len(losers)
                for d in losers:
                    if d.get("show") not in examples and len(examples)<12: examples.append(d.get("show"))
                # Skip records are written in bounded batches below.
                if losers:
                    # Bound both the skip writes and delete selector, even if an
                    # unusually large group contains many identical records.
                    for offset in range(0,len(losers),200):
                        chunk=losers[offset:offset+200]
                        skip_ops=[UpdateOne({"_id":f"{d.get('channel')}:{d.get('message_id')}"},{"$set":{"channel":d.get("channel"),"message_id":d.get("message_id"),"name":d.get("name") or d.get("show"),"reason":"Look-alike (same size)","at":time.time()}},upsert=True) for d in chunk]
                        if skip_ops: await self.skips.bulk_write(skip_ops,ordered=False)
                        await self.files.delete_many({"_id":{"$in":[d["_id"] for d in chunk]}})
            if progress:
                try: await progress.edit_text(f"Removing look-alikes… {removed:,} found so far.")
                except Exception: pass
            if edge is None: break
            key_after=edge
            await asyncio.sleep(0.05)
        self.clear_cache()
        return {"scanned":scanned,"removed":removed,"examples":examples}

    async def add_movie(self, sid: str, channel: int, message_id: int, name: str = "", size: int = 0):
        show=await self.shows.find_one({"_id":str(sid)},{"_id":1})
        if not show: raise ValueError("Parent show not found.")
        raw=f"{int(channel)}:{int(message_id)}"
        key=hashlib.sha1(raw.encode()).hexdigest()[:18]
        await self.movies.update_one({"_id":key},{"$set":{"sid":str(sid),"channel":int(channel),"message_id":int(message_id),"name":str(name or "Movie")[:300],"size":int(size or 0),"indexed_at":time.time()}},upsert=True)
        self._invalidate_show(sid)
        return key

    async def show_movies(self, sid: str):
        return [d async for d in self.movies.find({"sid":str(sid)}).sort("indexed_at",-1).limit(100)]

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

    async def bulk_update_files(self, news: list) -> int:
        """Write several re-parsed records in ONE round trip (shows are rebuilt afterwards)."""
        if not news: return 0
        from pymongo import UpdateOne
        ops=[]
        for n in news:
            doc=n.as_doc(); doc.pop("_id",None)
            ops.append(UpdateOne({"_id":n.uid},{"$set":doc}))
        await self.files.bulk_write(ops,ordered=False)
        return len(ops)

    async def rebuild_shows(self, progress=None) -> dict:
        """Make the shows collection match the files exactly: one doc per show id.

        Fixes shows that vanished (files with no show doc), empty shows and wrong
        counts. Hits/added dates of existing shows are kept."""
        from pymongo import UpdateOne
        cur=await self.files.aggregate([{"$group":{"_id":"$sid","title":{"$first":"$show"},"year":{"$first":"$year"},
                                                   "count":{"$sum":1},"added":{"$min":"$indexed_at"}}}],allowDiskUse=True)
        have={}; created=fixed=0; ops=[]
        existing={d["_id"]:int(d.get("count",0)) async for d in self.shows.find({},{"count":1})}
        async for g in cur:
            sid=g["_id"]; have[sid]=1
            title=g.get("title") or ""; year=g.get("year")
            if not title: continue
            if sid not in existing: created+=1
            elif existing[sid]!=int(g["count"]): fixed+=1
            added=(g.get("added") or 0)/1e9 or time.time()
            ops.append(UpdateOne({"_id":sid},{"$set":{"title":title,"year":year,"key":core_key(normalize(title)),"count":int(g["count"])},
                                              "$setOnInsert":{"added":added,"hits":0}},upsert=True))
            if len(ops)>=500:
                await self.shows.bulk_write(ops,ordered=False); ops=[]
        if ops: await self.shows.bulk_write(ops,ordered=False)
        stale=[sid for sid in existing if sid not in have]
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

    async def update_reparsed(self, old: SeriesFile, new: SeriesFile) -> None:
        doc=new.as_doc(); doc.pop("_id",None)
        self._invalidate_show(old.sid); self._invalidate_show(new.sid)
        await self.files.update_one({"_id":old.uid},{"$set":doc})
        if old.sid != new.sid:
            await self.shows.update_one({"_id":old.sid},{"$inc":{"count":-1}})
            await self.shows.delete_one({"_id":old.sid,"count":{"$lte":0}})
            await self.shows.update_one({"_id":new.sid},{"$setOnInsert":{"title":new.show,"year":new.year,"key":core_key(normalize(new.show)),"added":time.time(),"hits":0},"$inc":{"count":1}},upsert=True)
        else:
            await self.shows.update_one({"_id":new.sid},{"$set":{"title":new.show,"year":new.year,"key":core_key(normalize(new.show))}})

