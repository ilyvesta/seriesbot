"""Series-specific persistent catalog. Uses the bot's own Mongo database only.

Records are bounded and queried by show/season; no collection is loaded wholesale.
"""
from __future__ import annotations
import hashlib, re, time
from collections import OrderedDict
from dataclasses import dataclass, asdict, replace
from normalize import normalize, core_key
from parse import parse_series

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
                  (r.resolution or "").casefold(),(r.codec or "").casefold(),(r.ext or "").casefold()))
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
                   tuple(d.get("languages", ())), d.get("group"), tuple(d.get("tags", ())), d.get("ext"), d.get("caption", ""), int(d.get("indexed_at", 0) or 0))


def _strip_custom_promos(value: str, promos=()) -> str:
    text=(value or "").strip()
    # Keep the extension outside the boundary matching so a final promo token
    # immediately before .mkv/.srt is still recognized as trailing metadata.
    m=re.search(r"(?i)(\.[a-z0-9]{2,5})$",text)
    ext=m.group(1) if m else ""
    if ext: text=text[:-len(ext)]
    for phrase in sorted((str(x).strip() for x in (promos or ()) if str(x).strip()),key=len,reverse=True):
        token=re.escape(phrase)
        bracket=r"(?:[\[({]\s*)?"+token+r"(?:\s*[\])}])?"
        # Promo is removable at the start/end, or at the title boundary immediately
        # before a season/episode/quality marker. Never remove an interior word.
        text=re.sub(r"(?i)^\s*"+bracket+r"\s*[-–—|:]*\s*", "", text)
        text=re.sub(r"(?i)\s*[-–—|:]*\s*"+bracket+r"\s*$", "", text)
        text=re.sub(r"(?i)\s*[-–—|:]*\s*"+bracket+r"\s*(?=(?:s\d{1,2}(?:[._ -]*e|$)|season\b|ep(?:isode)?\b|e\d{1,4}\b|#\d{1,4}\b|\d{1,2}x\d{1,4}\b|(?:2160p|1440p|1080p|720p|480p|x265|x264|hevc|avc)\b))", " ", text)
    return re.sub(r"\s+"," ",text).strip()+ext


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
    generic_name = bool(re.fullmatch(r"(?i)(?:video|document|file|audio|photo)[ _-]?\d+\.(?:mkv|mp4|avi|mov|m4v|webm|ts|wmv|flv|mpg|mpeg|srt|ass|ssa|vtt|sub)", name))
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
    sid = show_id(parsed.show, parsed.year)
    alias=(aliases or {}).get(sid)
    if alias:
        if isinstance(alias,(tuple,list)):
            sid=str(alias[0]); parsed=replace(parsed,show=str(alias[1] or parsed.show),year=alias[2] if len(alias)>2 else parsed.year)
        else: sid=str(alias)
    return SeriesFile(str(uid), sid, parsed.show, parsed.year, parsed.kind, parsed.season, parsed.episodes,
                      parsed.absolute, parsed.ep_title, int(m.chat.id), int(m.id), int(getattr(media, "file_size", 0) or 0),
                      name[:500], parsed.resolution, parsed.source, parsed.codec, tuple(parsed.languages), parsed.group,
                      tuple(parsed.tags), ext, caption[:1000], time.time_ns())

class MongoSeriesCatalog:
    def __init__(self, db):
        self.files=db.series_files; self.shows=db.series_shows; self.skips=db.series_skips; self.watch=db.series_watchlists; self.requests=db.requests; self.progress=db.series_progress; self.user_stats_col=db.series_user_stats; self.aliases=db.series_show_aliases
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
    async def add(self, records: list[SeriesFile]) -> tuple[int,int,int]:
        if not records: return 0,0,0
        from pymongo import UpdateOne
        unique={r.uid:r for r in records}
        from dataclasses import replace
        for uid,r in list(unique.items()):
            target=await self.resolve_alias(r.sid)
            if target!=r.sid:
                show=await self.shows.find_one({"_id":target})
                if show: unique[uid]=replace(r,sid=target,show=show.get("title",r.show),year=show.get("year",r.year))
        for r in unique.values(): self._invalidate_show(r.sid)
        known={d["_id"] async for d in self.files.find({"_id":{"$in":list(unique)}},{"_id":1})}
        fresh=[]; dupes=0; seen_keys=set()
        candidates=[r for uid,r in unique.items() if uid not in known]
        candidates.sort(key=lambda r:(int(r.indexed_at or 0),int(r.message_id),str(r.uid)))
        dkeys=list({dk for r in candidates if (dk:=duplicate_key(r))})
        existing_keys={d["dupe_key"]:d["_id"] async for d in self.files.find({"dupe_key":{"$in":dkeys}},{"_id":1,"dupe_key":1})} if dkeys else {}
        for r in candidates:
            dk=duplicate_key(r)
            if dk and (dk in existing_keys or dk in seen_keys):
                keep=existing_keys.get(dk,"same-batch")
                await self.add_skip_record(r,"Look-alike (same size)")
                dupes+=1; continue
            if dk:
                seen_keys.add(dk); existing_keys[dk]=r.uid
            fresh.append(r)
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
        return len(fresh),len(known),dupes
    def _search_filter(self,query):
        q=core_key(normalize(query)); tokens=[re.escape(x) for x in q.split() if x]
        if not q: return {},q
        return {"$or":[{"key":{"$regex":"^"+re.escape(q)}},{"key":{"$regex":".*"+".*".join(tokens)+".*"}}]},q
    async def search_shows(self, query: str, limit: int = 10, offset: int = 0) -> list[dict]:
        filt,q=self._search_filter(query)
        if not q: return []
        key=("search",q,int(limit),int(offset)); cached=self._cache_get(key)
        if cached is not None: return [dict(x) for x in cached]
        cur=self.shows.find(filt, {"title":1,"year":1,"count":1,"added":1,"key":1}).skip(offset).limit(limit)
        rows=[d async for d in cur]; self._cache_set(key,rows); return rows
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
    async def alias_map(self):
        now=time.monotonic()
        if now-self._alias_cache[0]<60: return self._alias_cache[1]
        mapping={}
        async for row in self.aliases.find({},batch_size=200):
            old=str(row.get("_id","")); target=await self.resolve_alias(str(row.get("sid","")))
            show=await self.shows.find_one({"_id":target},{"title":1,"year":1})
            if old and target and show: mapping[old]=(target,show.get("title"),show.get("year"))
        self._alias_cache=(now,mapping)
        return mapping
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
        # The duplicate identity includes the show ID; refresh keys after moving files.
        from pymongo import UpdateOne
        key_ops=[]
        async for doc in self.files.find({"sid":survivor},batch_size=500):
            rec=SeriesFile.from_doc(doc); key=duplicate_key(rec)
            if key: key_ops.append(UpdateOne({"_id":rec.uid},{"$set":{"dupe_key":key}}))
            else: key_ops.append(UpdateOne({"_id":rec.uid},{"$unset":{"dupe_key":""}}))
            if len(key_ops)>=500:
                await self.files.bulk_write(key_ops,ordered=False); key_ops.clear()
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

    async def dedupe_series(self, dry_run=False, progress=None, batch_size=250):
        """Recompute keys for legacy records and remove later catalog duplicates only."""
        removed=0; examples=[]; scanned=0; ops=[]
        from pymongo import UpdateOne
        async for d in self.files.find({},batch_size=batch_size).sort([("indexed_at",1),("message_id",1),("_id",1)]):
            r=SeriesFile.from_doc(d); scanned+=1; key=duplicate_key(r)
            if key:
                earlier={"sid":r.sid,"season":r.season,"episodes":list(r.episodes),"size":r.size,
                         "resolution":r.resolution,"codec":r.codec,"ext":r.ext,
                         "$or":[{"indexed_at":{"$lt":r.indexed_at}},
                                {"indexed_at":r.indexed_at,"message_id":{"$lt":r.message_id}},
                                {"indexed_at":None,"message_id":{"$lt":r.message_id}},
                                {"indexed_at":r.indexed_at,"message_id":r.message_id,"_id":{"$lt":r.uid}}]}
                keep=await self.files.find_one(earlier,{"_id":1})
                if keep:
                    if not dry_run:
                        await self.add_skip_record(r,"Look-alike (same size)")
                        await self.files.delete_one({"_id":r.uid})
                        await self.shows.update_one({"_id":r.sid},{"$inc":{"count":-1}})
                        await self.shows.delete_one({"_id":r.sid,"count":{"$lte":0}})
                        self._invalidate_show(r.sid)
                    removed+=1
                    if r.show not in examples and len(examples)<12: examples.append(r.show)
                elif not dry_run:
                    ops.append(UpdateOne({"_id":r.uid},{"$set":{"dupe_key":key}}))
            if len(ops)>=batch_size and not dry_run:
                await self.files.bulk_write(ops,ordered=False); ops.clear()
            if progress and scanned%1000==0:
                try: await progress.edit_text(f"Scanning indexed series files… {scanned:,} checked · {removed:,} look-alikes {'found' if dry_run else 'removed'}.")
                except Exception: pass
        if ops and not dry_run: await self.files.bulk_write(ops,ordered=False)
        return {"scanned":scanned,"removed":removed,"examples":examples}

    async def iter_files(self,batch_size:int=500):
        async for d in self.files.find({},batch_size=batch_size): yield SeriesFile.from_doc(d)
    async def counts(self): return {"files":await self.files.count_documents({}),"shows":await self.shows.count_documents({})}
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

