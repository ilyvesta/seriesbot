"""Series-first search and delivery screens for the series bot."""
from __future__ import annotations
import html, logging, re, asyncio, time, hashlib
from pyrogram.enums import ParseMode
from series_catalog import SUB_EXTS, from_message, show_id
from ui import Btn
from handlers import kb, edit_of, notify_inbox
from normalize import normalize, core_key
from pyrogram.errors import FloodWait

log=logging.getLogger("seriesbot.series")


def _e(s): return html.escape(str(s or ""))

async def _result_ttl(ctx,group=False):
    """Minutes before a search/browse message is deleted (admin settings: separate values for groups and private chats)."""
    try:
        s=await ctx.settings()
        return int((s.get("group_result_ttl_min",7) if group else s.get("result_ttl_min",4)) or (7 if group else 4))
    except Exception: return 7 if group else 4
def _request_key(text):
    raw=core_key(normalize(text)) or str(text).lower()
    return raw[:40].rstrip()+"-"+hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
def _season_label(n): return "Episodes / unnumbered" if n == 0 else (str(n) if n >= 1900 else f"Season {n}")   # date-based shows use the year

def _is_dated(f): return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", getattr(f, "ep_title", "") or ""))

def _epcode(f):
    """E05 / E05-E06, or the air date (2026-09-16) for date-based shows."""
    if _is_dated(f): return f.ep_title
    nums=sorted(set(f.episodes))
    if not nums: return "Episode"
    return f"E{nums[0]:02d}" if len(nums)==1 else f"E{nums[0]:02d}–E{nums[-1]:02d}"
def _ep_label(f):
    if f.kind == "pack": return f"{_season_label(f.season)} · complete pack"
    if not f.episodes: return "Episode"
    nums=f.episodes
    if _is_dated(f): return f.ep_title
    ep=(f"E{nums[0]:02d}" if len(nums)==1 else f"E{nums[0]:02d}–E{nums[-1]:02d}")
    return (f"S{f.season:02d}" if f.season else "")+ep

def _quality(f):
    return " · ".join(x for x in (f.resolution, f.codec, f.source, *f.tags, f.group) if x) or (f.ext or "file").upper()

_RES_ORDER = {"240p":240,"360p":360,"480p":480,"576p":576,"720p":720,"1080p":1080,"1440p":1440,"2160p":2160,"4k":2160}
_PAGE_SIZE = 12


def _file_label(f):
    size = f"{max(1, round(f.size / 1048576))} MB" if f.size else "Size unknown"
    bits = [size]
    bits.extend(x for x in (f.resolution, f.codec, f.source) if x)
    if f.group:
        bits.append(f.group)                      # the encoder matters most when buttons get shortened
    bits.extend(x for x in f.tags if x)
    if getattr(f, "languages", None):
        bits.append("+".join(f.languages[:3]))
    if getattr(f,"ep_title","") and not _is_dated(f):
        bits.append(f.ep_title)
    return " · ".join(bits)


def _index_order(f):
    # New records carry a global ingestion timestamp. Legacy records fall back to
    # their channel message ID until they are re-indexed.
    return (int(getattr(f, "indexed_at", 0) or 0), int(f.message_id), str(f.uid))


def _parse_search_target(query):
    """Parse explicit season/episode syntax before considering a trailing bare number."""
    q=(query or "").strip()
    q=re.sub(r"(?i)\b(?:2160p|1440p|1080p|720p|576p|480p|360p|240p|4k|x265|x264|hevc|avc)\b"," ",q)
    q=re.sub(r"(?i)^\[[^]]+\]\s*","",q)
    season=episode=None
    # Explicit combined forms, allowing punctuation separators.
    pats=[r"(?i)(?:^|[\s._-])s(\d{1,2})[\s._-]*e(?:p)?[\s._-]*(\d{1,4})(?=$|[\s._-])",
          r"(?i)(?:^|[\s._-])(\d{1,2})x(\d{1,4})(?=$|[\s._-])",
          r"(?i)\bseason[\s._-]*(\d{1,2})[\s,._-]*(?:episode|episodes|eps?|e)[\s._-]*(\d{1,4})\b"]
    for pat in pats:
        m=re.search(pat,q)
        if m:
            season,episode=int(m.group(1)),int(m.group(2)); q=q[:m.start()]+' '+q[m.end():]; break
    # Season-only terms must be resolved before bare episode numbers.
    if season is None:
        m=re.search(r"(?i)(?:^|[\s._-])(?:season|szn)[\s._-]*(\d{1,2})\s*$",q)
        if not m: m=re.search(r"(?i)(?:^|[\s._-])s(\d{1,2})\s*$",q)
        if not m: m=re.search(r"(?i)(?:^|[\s._-])season[\s._-]*(one|two|three|four|five|six|seven|eight|nine|ten)\s*$",q)
        if m:
            v=m.group(1).lower(); season=({"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10}.get(v,int(v) if v.isdigit() else 0))
            q=q[:m.start()]
    if episode is None:
        m=re.search(r"(?i)(?:^|[\s._-])(?:episodes?|eps?|ep|e|#)[\s._-]*(\d{1,4})\s*$",q)
        if m: episode=int(m.group(1)); q=q[:m.start()]
    if episode is None:
        m=re.search(r"(?:^|[\s._-])(\d{1,4})\s*$",q)
        if m and int(m.group(1))>0: episode=int(m.group(1)); q=q[:m.start()]
    q=re.sub(r"(?i)\b(?:2160p|1440p|1080p|720p|576p|480p|360p|240p|4k|x265|x264|hevc|avc)\b"," ",q)
    q=re.sub(r"[._]+"," ",q); q=re.sub(r"\s+"," ",q).strip(" -–—[]()")
    return q,season,episode


async def results_screen(ctx,query,page=0,target_season=None,target_episode=None,rows_override=None,total_override=None):
    catalog=ctx.series_catalog; page_size=10
    actual_total=await catalog.count_search_shows(query) if rows_override is None else int(total_override or len(rows_override))
    total=min(actual_total,300)
    if rows_override is None:
        rows=await catalog.search_shows(query,page_size,page*page_size)
    else:
        part=rows_override[page*page_size:(page+1)*page_size]
        if part and isinstance(part[0],dict): rows=[dict(d) for d in part]
        else: rows=[d for sid in part if (d:=await catalog.get_show(sid))]
    buttons=[]
    for d in rows:
        sid=d.get("_id",d.get("sid"))
        label=f"{d['title']}"+(f" ({d['year']})" if d.get('year') else "")+f" · {d.get('count',0)} files"
        if target_episode is not None: action=f"ss:jumpep:{sid}:{target_season or 0}:{target_episode}"
        elif target_season is not None: action=f"ss:jump:{sid}:{target_season}"
        else: action=f"ss:show:{sid}"
        buttons.append([Btn(label[:60],action)])
    nav=[]
    if page>0: nav.append(Btn("‹ Previous", "ss:results:prev"))
    if (page+1)*page_size<total: nav.append(Btn("Next ›", "ss:results:next"))
    if nav: buttons.append(nav)
    buttons.append([Btn("Close","ss:close")])
    pages=max(1,(total+page_size-1)//page_size)
    destination = f" · Season {target_season}" if target_season is not None else ""
    count_label="300+ matches" if actual_total>300 else f"{actual_total} match(es)"
    return f"<b>Series results</b>{destination} · {count_label} · page {page+1}/{pages}\nChoose a series:",buttons,actual_total

async def search(ctx, client, show, uid: int, query: str, group: bool=False):
    catalog=getattr(ctx,"series_catalog",None)
    if not catalog:
        await show("The series catalogue is still starting. Please try again shortly.")
        return
    clean_query, target_season, target_episode = _parse_search_target(query)
    search_query = clean_query or query
    try: await catalog.bump_user_stat(uid,"searches")
    except Exception: pass
    # Exact full-title identity wins before interpreting a trailing number as an episode.
    raw_key=core_key(normalize(query))
    if target_episode is not None and target_season is None:
        exact_full=await catalog.get_show(show_id(query.strip()))
        if exact_full:
            search_query=query.strip(); target_episode=None; target_season=None
    if target_season is None and target_episode is not None and raw_key:
        raw_matches=await catalog.search_shows(query,10,0)
        if any(core_key(normalize(d.get("title", "")))==raw_key for d in raw_matches):
            search_query=query.strip()
            target_episode=None
    total=await catalog.count_search_shows(search_query)
    if not total and target_episode is not None:
        # A trailing bare number is only an episode when the remaining title resolves.
        # Otherwise retry the untouched full text as a title (e.g. a title ending in 100).
        full_total=await catalog.count_search_shows(query.strip())
        if full_total:
            search_query=query.strip(); target_episode=None; target_season=None; total=full_total
    if not total:
        if group and (await ctx.settings()).get("group_silent_miss",True): return
        await ctx.store.add_miss(query)
        ctx.series_pending_requests[uid]=search_query[:100]
        buttons=[[Btn("Request this series","ss:reqshow")],[Btn("Close","ss:close")]]
        await show(f"No series found for <b>{_e(query)}</b>.\nYou can request it, or check the spelling and try again.",buttons)
        return
    # If a season was supplied and the title resolves unambiguously (or exactly),
    # skip the title screen and take the user directly to that season's resolutions.
    candidates=await catalog.search_shows(search_query,10,0)
    # An exact title is a ranking signal, not a reason to hide other catalogue
    # candidates. Only bypass the chooser when the query has one result total.
    chosen=candidates[0] if total==1 and candidates else None
    if chosen:
        sid=chosen.get("_id",chosen.get("sid"))
        if target_episode is not None:
            if target_season is None:
                all_files=await _show_files(ctx,sid)
                ep_seasons=sorted({(1 if f.season==0 else f.season) for f in all_files if target_episode in f.episodes and f.kind!="pack"})
                if len(ep_seasons)>1:
                    rows=[[Btn(f"Season {sn}",f"ss:jumpep:{sid}:{sn}:{target_episode}")] for sn in ep_seasons]
                    rows.append([Btn("Close","ss:close")])
                    t=f"<b>{_e(chosen.get('title','Series'))}</b> · E{target_episode:02d} appears in more than one season. Choose a season:"; r=rows
                else:
                    t,r=await episode_screen(ctx,sid,ep_seasons[0] if ep_seasons else 0,target_episode)
            else:
                t,r=await episode_screen(ctx,sid,target_season,target_episode)
        elif target_season is not None:
            t,r=await season_screen(ctx,sid,target_season,uid,group=group)
        else:
            t,r=await show_screen(ctx,sid,uid=uid,group=group)
        sent=await show(t,r)
        if sent:
            now=time.monotonic(); ctx.series_sessions={k:v for k,v in ctx.series_sessions.items() if v.get("until",0)>now}
            if len(ctx.series_sessions)>=100: ctx.series_sessions.clear()
            ctx.series_sessions[(sent.chat.id,sent.id)]={"origin":"direct","owner":uid,"group":bool(group),"until":now+1800}
            ctx.watch(sent.chat.id,sent.id,await _result_ttl(ctx,group))
        return
    text,buttons,total=await results_screen(ctx,search_query,0,target_season,target_episode)
    sent=await show(text,buttons)
    if sent:
        now=time.monotonic()
        ctx.series_sessions={k:v for k,v in ctx.series_sessions.items() if v.get("until",0)>now}
        if len(ctx.series_sessions)>=100: ctx.series_sessions.clear()
        all_rows=await catalog.search_shows(search_query,min(total,300),0)
        snapshot=[{"_id":str(d.get("_id",d.get("sid"))),"title":d.get("title","Series"),"year":d.get("year"),"count":int(d.get("count",0))} for d in all_rows]
        ctx.series_sessions[(sent.chat.id,sent.id)]={"origin":"results","owner":uid,"group":bool(group),"query":search_query[:100],"page":0,"target_season":target_season,"target_episode":target_episode,"rows":snapshot,"total":total,"until":now+1800}
        ctx.watch(sent.chat.id,sent.id,await _result_ttl(ctx,group))

async def _show_files(ctx,sid,season=None):
    # Some anime uploads use absolute episode numbering and were historically
    # indexed with season=0. Present those as Season 1 without rewriting MongoDB.
    # If the show has real numbered seasons, retain the stored season values.
    if season == 1:
        all_files = await ctx.series_catalog.show_files(sid, None)
        numbered = [f for f in all_files if f.season > 0]
        if not numbered and any(f.season == 0 and f.episodes for f in all_files):
            return [f for f in all_files if f.season == 0]
        return [f for f in all_files if f.season == 1]
    return await ctx.series_catalog.show_files(sid,season)


def _display_season_numbers(files):
    numbered = sorted({f.season for f in files if f.season > 0})
    loose = any(f.season == 0 and f.episodes for f in files)
    if not numbered and loose:
        return [1]                    # only plain-numbered episodes: present them as Season 1
    if numbered and loose:
        return [0] + numbered         # mixed show: keep the plain-numbered files reachable (never hide files)
    return numbered

async def show_screen(ctx, sid, page=0, uid=None, back_results=False, back_discover=None, group=False):
    files=await _show_files(ctx,sid)
    if not files:return "This series has no indexed files.",[[Btn("Back to search","ss:back")]]
    show=files[0].show; year=files[0].year
    seasons=_display_season_numbers(files)
    if len(seasons)==1:
        return await season_screen(ctx,sid,seasons[0],uid,back_results=back_results,back_discover=back_discover,group=group)
    total=max(1,(len(seasons)+19)//20); page=max(0,min(page,total-1)); part=seasons[page*20:(page+1)*20]
    rows=[]
    for i in range(0,len(part),2):
        pair=[]
        for n in part[i:i+2]:
            fs=[f for f in files if (f.season==n or (n==1 and f.season==0 and not any(x.season>0 for x in files))) and f.kind=="episode"]
            eps={e for f in fs for e in f.episodes}
            pair.append(Btn((f"Episodes (no season) · {len(eps)} eps" if n==0 else f"{_season_label(n)} · {len(eps)} eps"),f"ss:season:{sid}:{n}"))
        rows.append(pair)
    if total>1:
        nav=[Btn("« first",f"ss:shows:{sid}:0"),Btn("‹",f"ss:shows:{sid}:{max(0,page-1)}"),Btn(f"{page+1}/{total}","noop"),Btn("›",f"ss:shows:{sid}:{min(total-1,page+1)}"),Btn("» last",f"ss:shows:{sid}:{total-1}")]
        rows.append(nav)
    packs=[f for f in files if f.kind=="pack"]
    if packs: rows.append([Btn(f"Season / series packs · {len(packs)}",f"ss:packs:{sid}")])
    movies=await ctx.series_catalog.show_movies(sid)
    if movies: rows.append([Btn(f"Movies · {len(movies)}",f"ss:movies:{sid}")])
    if uid is not None:
        prog=await ctx.series_catalog.get_progress(uid,sid)
        if prog: rows.insert(0,[Btn(f"Continue: E{int(prog['episode']):02d}",f"ss:continue:{sid}")])
    # Only gaps between indexed seasons are requestable.
    if seasons:
        gaps=[n for n in range(seasons[0],seasons[-1]+1) if n not in set(seasons)] if seasons[0]<1900 else []
        for n in gaps[:8]: rows.append([Btn(f"Request missing season {n}",f"ss:reqseason:{sid}:{n}")])
    rows.append([Btn("🔔 Follow this show" if not (uid is not None and sid in await ctx.series_catalog.watchlist(uid)) else "✅ Following · tap to unfollow","ss:save:"+sid)])
    if uid is not None and ctx.is_admin(uid) and not group:
        picked = sid in _all_curated_sids(await ctx.settings())
        rows.append([Btn("Unpick" if picked else "Hand-pick", f"ss:pick:{sid}"), Btn("Curated lists", "ss:curated:menu")])
    if back_results: rows.append([Btn("‹ Back to results","ss:backresults")])
    elif back_discover: rows.append([Btn("‹ Back to list",f"ss:discover:{back_discover}"), Btn("‹ Discover","ss:discover:menu")])
    rows.append([Btn("Close","ss:close")])
    return f"<b>{_e(show)}{f' ({year})' if year else ''}</b>\nChoose a season · {len(seasons)} seasons:",rows

async def season_screen(ctx,sid,season,uid=None,back_results=False,back_discover=None,group=False):
    files=await _show_files(ctx,sid,season)
    videos=[f for f in files if (f.ext or "").lower() not in SUB_EXTS and f.episodes and f.kind!="pack" and not getattr(f,"special",False)]
    packs=[f for f in files if f.kind=="pack"]
    rows=[]; categories={}
    for f in videos: categories.setdefault(f.resolution or None,[]).append(f)
    def ro(k):
        if not k:return (0,0)
        m=re.search(r"\d+",str(k)); return (1,int(m.group()) if m else 99999)
    ordered_res=sorted(categories,key=ro)
    if len(ordered_res)==1:
        t,r=await resolution_screen(ctx,sid,season,ordered_res[0] or "unknown",0,back_results=back_results,back_discover=back_discover)
        if uid is not None:
            prog=await ctx.series_catalog.get_progress(uid,sid)
            if prog and (int(prog.get("season",-1))==int(season) or (int(season)==1 and int(prog.get("season",-1))==0)):
                r.insert(0,[Btn(f"Continue: E{int(prog['episode']):02d}",f"ss:continue:{sid}")])
            following=sid in await ctx.series_catalog.watchlist(uid)
            r.append([Btn("✅ Following · tap to unfollow" if following else "🔔 Follow this show",f"ss:save:{sid}")])
        if uid is not None and ctx.is_admin(uid) and not group:
            picked = sid in _all_curated_sids(await ctx.settings())
            r.append([Btn("Unpick" if picked else "Hand-pick", f"ss:pick:{sid}"), Btn("Curated lists", "ss:curated:menu")])
        for f in sorted(packs,key=_index_order,reverse=True)[:8]: r.append([Btn(f"Pack · {_file_label(f)}"[:60],f"ss:file:{f.uid}")])
        specials=[f for f in files if getattr(f,"special",False)]
        if specials: r.append([Btn(f"Specials / OVA · {len(specials)}",f"ss:specials:{sid}:{season}")])
        movies=await ctx.series_catalog.show_movies(sid)
        if movies: r.append([Btn(f"Movies · {len(movies)}",f"ss:movies:{sid}")])
        if any((f.ext or "").lower() in SUB_EXTS for f in files): r.append([Btn("Subtitles",f"ss:subs:{sid}:{season}")])
        return t,r
    for res in ordered_res:
        fs=categories[res]; eps={e for f in fs for e in f.episodes}
        rows.append([Btn(f"{res or 'Unknown'} · {len(eps)} episodes",f"ss:res:{sid}:{season}:{res or 'unknown'}")])
    for f in sorted(packs,key=_index_order,reverse=True)[:8]: rows.append([Btn(f"Pack · {_file_label(f)}"[:60],f"ss:file:{f.uid}")])
    if uid is not None:
        prog=await ctx.series_catalog.get_progress(uid,sid)
        if prog and (int(prog.get('season',-1))==int(season) or (int(season)==1 and int(prog.get('season',-1))==0)):
            rows.insert(0,[Btn(f"Continue: E{int(prog['episode']):02d}",f"ss:continue:{sid}")])
    specials=[f for f in files if getattr(f,"special",False)]
    if specials: rows.append([Btn(f"Specials / OVA · {len(specials)}",f"ss:specials:{sid}:{season}")])
    movies=await ctx.series_catalog.show_movies(sid)
    if movies: rows.append([Btn(f"Movies · {len(movies)}",f"ss:movies:{sid}")])
    subs=[f for f in files if (f.ext or "").lower() in SUB_EXTS]
    if subs: rows.append([Btn(f"Subtitles · {len(subs)} files",f"ss:subs:{sid}:{season}")])
    if uid is not None:
        following=sid in await ctx.series_catalog.watchlist(uid)
        rows.append([Btn("✅ Following · tap to unfollow" if following else "🔔 Follow this show",f"ss:save:{sid}")])
    if uid is not None and ctx.is_admin(uid) and not group:
        rows.append([Btn("Admin: episodes missing at every resolution",f"ss:missall:{sid}:{season}")])
        rows.append([Btn("Admin: inspect/remove a file",f"ss:adminfiles:{sid}:{season}")])
        picked = sid in _all_curated_sids(await ctx.settings())
        rows.append([Btn("Unpick" if picked else "Hand-pick", f"ss:pick:{sid}"), Btn("Curated lists", "ss:curated:menu")])
    show_seasons=_display_season_numbers(await _show_files(ctx,sid))
    if len(show_seasons)>1: rows.append([Btn("‹ Back to seasons",f"ss:show:{sid}")])
    elif back_results: rows.append([Btn("‹ Back to results","ss:backresults")])
    elif back_discover: rows.append([Btn("‹ Back to list",f"ss:discover:{back_discover}"), Btn("‹ Discover","ss:discover:menu")])
    rows.append([Btn("Close","ss:close")])
    title=files[0].show if files else "Series"; episode_count=len({e for f in videos for e in f.episodes})
    return f"<b>{_e(title)} · {_e(_season_label(season))}</b>\n{episode_count} episodes indexed. Choose a resolution:",rows

async def _resolution_items(ctx,sid,season,res):
    files=await _show_files(ctx,sid,season); wanted=None if res=="unknown" else res
    videos=[f for f in files if f.kind!="pack" and f.episodes and (f.ext or "").lower() not in SUB_EXTS and not getattr(f,"special",False)]
    by_ep={}
    for f in videos:
        for ep in f.episodes: by_ep.setdefault(ep,[]).append(f)
    dated=any(_is_dated(f) for f in videos)
    all_eps=(sorted(by_ep) if dated else list(range(min(by_ep),max(by_ep)+1))) if by_ep else []
    chosen=[f for f in videos if f.resolution==wanted]
    present={e for f in chosen for e in f.episodes}; gaps=[e for e in all_eps if e not in present]
    def rn(f):
        value=str(f.resolution or "").lower()
        if value in ("4k","uhd"): return 2160
        m=re.search(r"\d+",value); return int(m.group()) if m else 0
    items=[(f,False) for f in chosen]
    # Substitute at file granularity: a multi-episode file is listed once if it covers any gap.
    substituted=set()
    for ep in gaps:
        cand=by_ep.get(ep,[])
        if not cand: continue
        target=rn(type('R',(),{'resolution':wanted})()) if wanted else 0
        best=min(cand,key=lambda f:(abs(rn(f)-target),-rn(f),-_index_order(f)[0],-int(f.message_id)))
        substituted.add(best.uid)
    existing={f.uid for f,_ in items}
    for f in videos:
        if f.uid in substituted and f.uid not in existing: items.append((f,True))
    items.sort(key=lambda x:(min(x[0].episodes or (0,)),_index_order(x[0])),reverse=True)
    return files,items,gaps,by_ep

async def resolution_screen(ctx,sid,season,res,page=0,lo=None,hi=None,back_results=False,back_discover=None):
    files,items,gaps,by_ep=await _resolution_items(ctx,sid,season,res)
    # Default is newest episode first. A range/Continue opens the full ascending list
    # at the first file whose first episode is >= the requested start; paging is unbounded.
    ascending=lo is not None
    if ascending:
        items=sorted(items,key=lambda x:(min(x[0].episodes or (0,)),_index_order(x[0])))
        base=next((i for i,(f,_) in enumerate(items) if max(f.episodes or (0,))>=int(lo)),len(items))
        start=base if int(page)<0 else max(0,min(int(page),max(0,len(items)-1)))
    else:
        items=sorted(items,key=lambda x:(min(x[0].episodes or (0,)),_index_order(x[0])),reverse=True)
        start=max(0,min(int(page),max(0,len(items)-1)))
    total=len(items); pages=max(1,(total+_PAGE_SIZE-1)//_PAGE_SIZE)
    view=items[start:start+_PAGE_SIZE]
    rows=[]
    for f,sub in view:
        ep_label=_epcode(f)
        label=f"{ep_label} · {_file_label(f)}"
        rows.append([Btn(label[:60],f"ss:file:{f.uid}")])
    unique_eps={e for f,_ in items for e in f.episodes}
    dated_view=any(_is_dated(f) for f,_ in items)
    if len(unique_eps)>50 and not ascending and not dated_view:
        rows.insert(0,[Btn("Go to episode",f"ss:goto:{sid}:{season}:{res}:0")])
    if total>_PAGE_SIZE:
        last_start=((total-1)//_PAGE_SIZE)*_PAGE_SIZE
        prev=max(0,start-_PAGE_SIZE); nxt=start+_PAGE_SIZE
        if ascending:
            nav=[Btn("« first",f"ss:respage:{sid}:{season}:{res}:0:asc"),Btn("‹",f"ss:respage:{sid}:{season}:{res}:{prev}:asc"),Btn(f"{start//_PAGE_SIZE+1}/{pages}","noop"),Btn("›",f"ss:respage:{sid}:{season}:{res}:{nxt}:asc" if nxt<total else "noop"),Btn("» last",f"ss:respage:{sid}:{season}:{res}:{last_start}:asc")]
        else:
            nav=[Btn("« first",f"ss:respage:{sid}:{season}:{res}:0"),Btn("‹",f"ss:respage:{sid}:{season}:{res}:{prev}"),Btn(f"{start//_PAGE_SIZE+1}/{pages}","noop"),Btn("›",f"ss:respage:{sid}:{season}:{res}:{nxt}" if nxt<total else "noop"),Btn("» last",f"ss:respage:{sid}:{season}:{res}:{last_start}")]
        rows.append(nav)
    if ascending:
        nav_buttons=[]
        if len(unique_eps)>50 and not dated_view: nav_buttons.append(Btn("Go to episode",f"ss:goto:{sid}:{season}:{res}:0"))
        nav_buttons.append(Btn("‹ Latest episodes",f"ss:res:{sid}:{season}:{res}"))
        rows.append(nav_buttons)
    if gaps and not ascending: rows.append([Btn("Find missing episodes",f"ss:missing:{sid}:{season}:{res}:{start}")])
    available_res={f.resolution or "unknown" for f in files if f.kind!="pack" and f.episodes and (f.ext or "").lower() not in SUB_EXTS and not getattr(f,"special",False)}
    all_show_files=await _show_files(ctx,sid); show_seasons=_display_season_numbers(all_show_files)
    # Back always goes to the screen the user came from; search results are the last resort.
    if len(available_res)>1:
        rows.append([Btn("‹ Back to resolutions",f"ss:season:{sid}:{season}")])
    elif len(show_seasons)>1:
        rows.append([Btn("‹ Back to seasons",f"ss:show:{sid}")])
    elif back_results:
        rows.append([Btn("‹ Back to results","ss:backresults")])
    elif back_discover:
        rows.append([Btn("‹ Back to list",f"ss:discover:{back_discover}"), Btn("‹ Discover","ss:discover:menu")])
    else:
        rows.append([Btn("Close","ss:close")])
    title=files[0].show if files else "Series"; label=res if res!="unknown" else "Unknown resolution"
    return f"<b>{_e(title)} · {_e(_season_label(season))} · {_e(label)}</b>\n{total} episode file(s)",rows

async def goto_screen(ctx,sid,season,res,level=0,lo=None,hi=None):
    _,items,_,by_ep=await _resolution_items(ctx,sid,season,res)
    eps=sorted({ep for f,_ in items for ep in f.episodes})
    if not eps:return "No episodes indexed.",[[Btn("Close","ss:close")]]
    rows=[]
    if len(eps)>=500 and level==0:
        ranges=[(a,min(a+99,max(eps))) for a in range(1,max(eps)+1,100)]
    elif len(eps)>=500 and level==1 and lo is not None:
        ranges=[(a,min(a+11,int(hi))) for a in range(int(lo),int(hi)+1,12)]
    else:
        ranges=[(a,min(a+49,max(eps))) for a in range(1,max(eps)+1,50)]
    for i in range(0,len(ranges),2):
        rows.append([Btn(f"{a}-{b}",f"ss:range:{sid}:{season}:{res}:{a}:{b}") for a,b in ranges[i:i+2]])
    rows.append([Btn("‹ Latest episodes",f"ss:res:{sid}:{season}:{res}")])
    files=await _show_files(ctx,sid,season); title=files[0].show if files else "Series"
    label=res if res!="unknown" else "Unknown resolution"
    return f"<b>{_e(title)} · {_e(_season_label(season))} · {_e(label)}</b>\nGo to episode · Choose a starting episode:",rows


async def missing_screen(ctx,sid,season,res,back_start=0):
    files,items,gaps,by_ep=await _resolution_items(ctx,sid,season,res)
    wanted=None if res=="unknown" else res
    missing=[ep for ep in gaps if not any(f.resolution==wanted and ep in f.episodes for f in files if f.kind!="pack")]
    rows=[]; listed=set()
    for ep in missing:
        versions=sorted(by_ep.get(ep,[]),key=_index_order,reverse=True)
        if not versions: rows.append([Btn(f"E{ep:02d} · Request this episode",f"ss:reqep:{sid}:{season}:{ep}")])
        else:
            for f in versions:
                if f.uid in listed: continue
                listed.add(f.uid); nums=sorted(set(f.episodes)); label=f"E{nums[0]:02d}" if len(nums)==1 else f"E{nums[0]:02d}–E{nums[-1]:02d}"
                rows.append([Btn(f"{label} · {f.resolution or 'Unknown'} · {f.size//1048576} MB",f"ss:file:{f.uid}")])
    if not rows: rows=[[Btn("No unavailable episodes.","noop")]]
    rows.append([Btn("Back",f"ss:respage:{sid}:{season}:{res}:{back_start}" if back_start else f"ss:res:{sid}:{season}:{res}")])
    title=files[0].show if files else "Series"
    return f"<b>{_e(title)} · {_e(_season_label(season))} · {_e(res)}</b>\nMissing episodes · {len(missing)} unavailable",rows


def _ranges(nums):
    out=[]; nums=sorted(nums)
    i=0
    while i<len(nums):
        j=i
        while j+1<len(nums) and nums[j+1]==nums[j]+1: j+=1
        out.append(f"E{nums[i]:02d}" if i==j else f"E{nums[i]:02d}–E{nums[j]:02d}")
        i=j+1
    return out


async def missing_all_screen(ctx,sid,season):
    """Episodes of this season that exist at NO resolution (not just the one being browsed). Specials and subtitles are not counted."""
    files=await _show_files(ctx,sid,season) if season else await _show_files(ctx,sid)
    real=[f for f in files if f.kind!="pack" and not getattr(f,"special",False) and (f.ext or "").lower() not in SUB_EXTS and f.episodes and not _is_dated(f)]
    title=files[0].show if files else "Series"
    if not real:
        return f"<b>{_e(title)} · {_e(_season_label(season))}</b>\nNo numbered episodes to compare.",[[Btn("Back",f"ss:season:{sid}:{season}")]]
    have={e for f in real for e in f.episodes}
    hi=max(have); lo=1
    missing=[e for e in range(lo,hi+1) if e not in have]
    lines=[f"<b>{_e(title)} · {_e(_season_label(season))}</b>",f"Episodes present: {len(have)} (E{min(have):02d}–E{hi:02d})",
           f"Missing at EVERY resolution: {len(missing)}"]
    if missing: lines.append(", ".join(_ranges(missing))[:3000])
    else: lines.append("None: every episode from E01 to E%02d exists at some resolution."%hi)
    rows=[[Btn(f"Request E{e:02d}",f"ss:reqep:{sid}:{season}:{e}")] for e in missing[:8]]
    rows.append([Btn("Back",f"ss:season:{sid}:{season}")])
    return "\n".join(lines),rows


async def episode_screen(ctx,sid,season,episode,back_results=False,back_discover=None):
    files=await _show_files(ctx,sid,season) if season else await _show_files(ctx,sid)
    versions=sorted([f for f in files if f.kind!="pack" and episode in f.episodes and (f.ext or "").lower() not in SUB_EXTS and not getattr(f,"special",False)],key=_index_order,reverse=True)
    actual_seasons=sorted({f.season for f in versions})
    known_seasons=sorted({f.season for f in files if f.season>0})
    if season==0 and len(actual_seasons)==1: season=actual_seasons[0]
    elif season==0 and not actual_seasons and len(known_seasons)==1: season=known_seasons[0]
    rows=[]
    for f in versions:
        ep_label=_epcode(f)
        rows.append([Btn(f"{ep_label} · {_file_label(f)}"[:60],f"ss:file:{f.uid}")])
    if not versions: rows.append([Btn("Request this episode",f"ss:reqep:{sid}:{season}:{episode}")])
    # Parent first (resolutions, then seasons); search results only when nothing else is above.
    res_values=sorted({f.resolution or "unknown" for f in files if f.kind!="pack" and f.episodes and (f.ext or "").lower() not in SUB_EXTS and not getattr(f,"special",False)})
    show_seasons=_display_season_numbers(await _show_files(ctx,sid))
    if len(res_values)>1:
        rows.append([Btn("‹ Back to resolutions",f"ss:season:{sid}:{season}")])
    elif len(show_seasons)>1:
        rows.append([Btn("‹ Back to seasons",f"ss:show:{sid}")])
    elif back_results:
        rows.append([Btn("‹ Back to results","ss:backresults")])
    elif back_discover:
        rows.append([Btn("‹ Back to list",f"ss:discover:{back_discover}"), Btn("‹ Discover","ss:discover:menu")])
    else:
        rows.append([Btn("Close","ss:close")])
    title=files[0].show if files else "Series"
    return f"<b>{_e(title)} · {_e(_season_label(season))} · E{episode:02d}</b>",rows

async def movies_screen(ctx,sid):
    show=await ctx.series_catalog.get_show(sid)
    movies=await ctx.series_catalog.show_movies(sid)
    title=show.get("title","Series") if show else "Series"
    rows=[]
    for movie in movies:
        size=f" · {int(movie.get('size',0))//1048576} MB" if movie.get("size") else ""
        label=(movie.get("name") or "Movie")[:42]
        rows.append([Btn(f"{label}{size}"[:60],f"ss:moviefile:{movie['_id']}")])
    if not rows: rows=[[Btn("No linked movies.","noop")]]
    rows.append([Btn("‹ Back to show",f"ss:show:{sid}")])
    return f"<b>{_e(title)} · Movies</b>\n{len(movies)} linked movie(s).",rows

async def specials_screen(ctx,sid,season):
    files=await _show_files(ctx,sid,season)
    specials=[f for f in files if getattr(f,"special",False)]
    specials.sort(key=_index_order,reverse=True)
    rows=[]
    for f in specials:
        label=f.special_label or _ep_label(f)
        rows.append([Btn(f"{label} · {_file_label(f)}"[:60],f"ss:file:{f.uid}")])
    if not rows: rows=[[Btn("No specials indexed.","noop")]]
    rows.append([Btn("‹ Back to season",f"ss:season:{sid}:{season}")])
    title=files[0].show if files else "Series"
    return f"<b>{_e(title)} · {_e(_season_label(season))} · Specials / OVA</b>\n{len(specials)} special file(s)",rows

async def subtitles_screen(ctx,sid,season,page=0):
    files=await _show_files(ctx,sid,season)
    subs=[f for f in files if (f.ext or "").lower() in SUB_EXTS]
    subs.sort(key=lambda f:((min(f.episodes) if f.episodes else 0),_index_order(f)),reverse=True)
    total=len(subs); pages=max(1,(total+_PAGE_SIZE-1)//_PAGE_SIZE); start=max(0,min(int(page),max(0,total-1))); page=start//_PAGE_SIZE; last_start=((total-1)//_PAGE_SIZE)*_PAGE_SIZE
    rows=[]
    for f in subs[start:start+_PAGE_SIZE]:
        ep_label=_epcode(f) if f.episodes else "Subtitle"
        rows.append([Btn(f"{ep_label} · {_file_label(f)}"[:60],f"ss:file:{f.uid}")])
    if pages>1: rows.append([Btn("« first",f"ss:subspage:{sid}:{season}:0"),Btn("‹",f"ss:subspage:{sid}:{season}:{max(0,start-_PAGE_SIZE)}"),Btn(f"{start//_PAGE_SIZE+1}/{pages}","noop"),Btn("›",f"ss:subspage:{sid}:{season}:{start+_PAGE_SIZE}" if start+_PAGE_SIZE<total else "noop"),Btn("» last",f"ss:subspage:{sid}:{season}:{last_start}")])
    rows.append([Btn("‹ Back to season",f"ss:season:{sid}:{season}")])
    title=files[0].show if files else "Series"
    return f"<b>{_e(title)} · {_e(_season_label(season))} · Subtitles</b>\n{total} file(s)",rows

def get_curated_lists(settings: dict) -> list[dict]:
    """Return normalized curated Discover lists, migrating legacy series_picks if needed."""
    raw = settings.get("curated_lists")
    lists: list[dict] = []
    if isinstance(raw, list) and raw:
        seen_ids: set[str] = set()
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            lid = re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("id") or f"list{idx + 1}"))[:16] or f"list{idx + 1}"
            while lid in seen_ids:
                lid = f"{lid[:12]}_{len(seen_ids)}"
            seen_ids.add(lid)
            name = str(item.get("name") or "Curated").strip()[:40] or "Curated"
            sids = list(dict.fromkeys(str(x) for x in (item.get("sids") or []) if x))
            lists.append({"id": lid, "name": name, "sids": sids})
        return lists
    legacy = list(dict.fromkeys(str(x) for x in (settings.get("series_picks") or []) if x))
    if legacy:
        return [{"id": "picks", "name": str(settings.get("series_picks_name") or "Editor picks")[:40], "sids": legacy}]
    return []


def _all_curated_sids(settings: dict) -> set[str]:
    return {sid for lst in get_curated_lists(settings) for sid in lst.get("sids", ())}


async def save_curated_lists(ctx, lists: list[dict]) -> None:
    clean = [{"id": str(x["id"]), "name": str(x["name"]).strip()[:40] or "Curated",
              "sids": list(dict.fromkeys(str(s) for s in x.get("sids", []) if s))}
             for x in lists]
    await ctx.set_setting("curated_lists", clean)
    all_sids = list(dict.fromkeys(sid for x in clean for sid in x["sids"]))
    await ctx.set_setting("series_picks", all_sids)
    if clean:
        await ctx.set_setting("series_picks_name", clean[0]["name"])


def _new_list_id(name: str, existing_ids: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", name.lower())[:8] or "list"
    cand = slug
    i = 2
    while cand in existing_ids:
        cand = f"{slug[:6]}{i}"
        i += 1
    return cand


async def discover_menu(ctx, settings: dict | None = None) -> tuple[str, list]:
    settings = settings or await ctx.settings()
    rows = [[Btn("Popular series", "ss:discover:popular"), Btn("Recently added", "ss:discover:new")],
            [Btn("My List", "ss:discover:mylist")]]
    curated = [lst for lst in get_curated_lists(settings) if lst.get("sids")]
    for i in range(0, len(curated), 2):
        rows.append([Btn(lst["name"][:28], f"ss:discover:list:{lst['id']}") for lst in curated[i:i + 2]])
    if any(lst.get("sids") for lst in curated):
        rows.append([Btn("Surprise me", "ss:surprise")])
    rows.append([Btn("‹ Home", "nav:home"), Btn("Close", "ss:close")])
    return "<b>Discover series</b>\nBrowse the catalogue.", rows


async def curated_menu_screen(ctx) -> tuple[str, list]:
    settings = await ctx.settings()
    lists = get_curated_lists(settings)
    rows = []
    for lst in lists:
        rows.append([Btn(f"{lst['name']} · {len(lst['sids'])} show(s)"[:58], f"ss:curated:view:{lst['id']}:0")])
    rows.append([Btn("➕ Create curated list", "ss:curated:create")])
    rows.append([Btn("‹ Settings", "st:refresh"), Btn("Close", "ss:close")])
    intro = ("<b>Curated Discover Lists (Hand-picked)</b>\n"
             "Select a list to view, add/remove shows, rename, replace, or clear it.\n"
             "You can also tap <b>Hand-pick</b> on any show page.")
    if not lists:
        intro += "\n\nNo curated lists yet. Create one below or Hand-pick a show from its page."
    return intro, rows


async def curated_list_screen(ctx, list_id: str, page: int = 0) -> tuple[str, list]:
    settings = await ctx.settings()
    lists = get_curated_lists(settings)
    lst = next((x for x in lists if x["id"] == list_id), None)
    if not lst:
        return await curated_menu_screen(ctx)
    sids = lst["sids"]
    per = 10
    pages = max(1, (len(sids) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    chunk = sids[page * per:(page + 1) * per]
    lines = [f"<b>{_e(lst['name'])}</b> · {len(sids)} show(s)"]
    rows = []
    for sid in chunk:
        d = await ctx.series_catalog.get_show(sid)
        title = (d.get("title", "Series") + (f" ({d['year']})" if d and d.get("year") else "")) if d else f"(missing) {sid}"
        lines.append(f"• {_e(title)}")
        rows.append([Btn(title[:58], f"ss:show:{sid}")])
    if not sids:
        lines.append("\n<i>This list is empty. Use Add shows or Hand-pick on any show page.</i>")
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(Btn("‹ Prev", f"ss:curated:view:{list_id}:{page - 1}"))
        nav.append(Btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(Btn("Next ›", f"ss:curated:view:{list_id}:{page + 1}"))
        rows.append(nav)
    rows.append([Btn("➕ Add shows", f"ss:curated:add:{list_id}"), Btn("➖ Remove shows", f"ss:curated:rm:{list_id}:0")])
    rows.append([Btn("✏️ Rename", f"ss:curated:rename:{list_id}"), Btn("🔄 Replace", f"ss:curated:replace:{list_id}")])
    rows.append([Btn("🧹 Clear", f"ss:curated:clear:{list_id}"), Btn("🗑️ Delete", f"ss:curated:delete:{list_id}")])
    rows.append([Btn("‹ All lists", "ss:curated:menu"), Btn("‹ Settings", "st:refresh")])
    return "\n".join(lines), rows


async def curated_remove_screen(ctx, list_id: str, page: int = 0) -> tuple[str, list]:
    settings = await ctx.settings()
    lst = next((x for x in get_curated_lists(settings) if x["id"] == list_id), None)
    if not lst:
        return await curated_menu_screen(ctx)
    sids = lst["sids"]
    per = 10
    pages = max(1, (len(sids) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    rows = []
    for sid in sids[page * per:(page + 1) * per]:
        d = await ctx.series_catalog.get_show(sid)
        title = (d.get("title", "Series") + (f" ({d['year']})" if d and d.get("year") else "")) if d else sid
        rows.append([Btn(f"✖ {title}"[:58], f"ss:curated:delshow:{list_id}:{sid}:{page}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(Btn("‹ Prev", f"ss:curated:rm:{list_id}:{page - 1}"))
        nav.append(Btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(Btn("Next ›", f"ss:curated:rm:{list_id}:{page + 1}"))
        rows.append(nav)
    rows.append([Btn("‹ Back to list", f"ss:curated:view:{list_id}:0")])
    return f"<b>Remove shows from {_e(lst['name'])}</b>\nTap a show to remove it:", rows


async def curated_search_screen(ctx, list_id: str, query: str) -> tuple[str, list]:
    settings = await ctx.settings()
    lst = next((x for x in get_curated_lists(settings) if x["id"] == list_id), None)
    if not lst:
        return await curated_menu_screen(ctx)
    matches = await ctx.series_catalog.search_shows(query, 12, 0)
    current = set(lst["sids"])
    rows = []
    for d in matches:
        sid = str(d.get("_id", d.get("sid")))
        title = d.get("title", "Series") + (f" ({d['year']})" if d.get("year") else "")
        mark = "✅ " if sid in current else "➕ "
        rows.append([Btn(f"{mark}{title}"[:58], f"ss:curated:tog:{list_id}:{sid}")])
    if not rows:
        rows.append([Btn("No matching shows found", "noop")])
    rows.append([Btn("🔍 Search again", f"ss:curated:add:{list_id}"), Btn("‹ Back to list", f"ss:curated:view:{list_id}:0")])
    return f"<b>Add/remove shows in {_e(lst['name'])}</b>\nQuery: <code>{_e(query)}</code>\nTap a show to toggle it:", rows


async def show_pick_screen(ctx, sid: str) -> tuple[str, list]:
    settings = await ctx.settings()
    lists = get_curated_lists(settings)
    d = await ctx.series_catalog.get_show(sid)
    title = d.get("title", "Series") if d else "Series"
    rows = []
    for lst in lists:
        mark = "✅ " if sid in lst["sids"] else "➕ "
        rows.append([Btn(f"{mark}{lst['name']} ({len(lst['sids'])})"[:58], f"ss:pickto:{lst['id']}:{sid}")])
    rows.append([Btn("Manage curated lists", "ss:curated:menu")])
    rows.append([Btn("‹ Back to show", f"ss:show:{sid}")])
    return f"<b>Hand-pick: {_e(title)}</b>\nChoose which curated Discover list(s) include this show:", rows


async def discover_screen(ctx,which,uid):
    settings=await ctx.settings()
    if which=="new": rows=await ctx.series_catalog.recent_shows(12); title="Recently added series"
    elif which.startswith("list:"):
        lid=which.split(":",1)[1]
        lst=next((x for x in get_curated_lists(settings) if x["id"]==lid),None)
        rows=[]
        for sid in (lst.get("sids",[]) if lst else [])[:30]:
            d=await ctx.series_catalog.get_show(sid)
            if d: rows.append(d)
        title=lst["name"] if lst else "Curated list"
    elif which=="picks":
        curated=get_curated_lists(settings); rows=[]
        sids=curated[0]["sids"] if curated else settings.get("series_picks",[])
        for sid in sids[:30]:
            d=await ctx.series_catalog.get_show(sid)
            if d: rows.append(d)
        title=curated[0]["name"] if curated else (settings.get("series_picks_name") or "Editor picks")
    elif which=="mylist":
        sids=await ctx.series_catalog.watchlist(uid); rows=[]
        for sid in sids[:30]:
            d=await ctx.series_catalog.get_show(sid)
            if d: rows.append(d)
        title="My List"
    else: rows=await ctx.series_catalog.popular_shows(12); title="Popular series"
    buttons=[]
    for d in rows:
        sid=d.get("_id",d.get("sid")); name=d.get("title","Series")
        label=f"{name}"+(f" ({d['year']})" if d.get("year") else "")
        buttons.append([Btn(label[:60],f"ss:show:{sid}")])
    if which=="mylist":
        stats=await ctx.series_catalog.user_stats(uid)
        title += f"\nFollowed: {len(sids)} · Searches: {int(stats.get('searches',0))} · Downloads: {int(stats.get('downloads',0))}"
    if not buttons:
        empty_msg = "Nothing here yet. Follow a show from its page." if which == "mylist" else "Nothing in this list yet."
        return f"<b>{_e(title)}</b>\n{empty_msg}",[[Btn("‹ Discover","ss:discover:menu"),Btn("‹ Home","nav:home")],[Btn("Close","ss:close")]]
    buttons.append([Btn("‹ Discover","ss:discover:menu"),Btn("‹ Home","nav:home")])
    buttons.append([Btn("Close","ss:close")])
    return f"<b>{_e(title)}</b>",buttons

_CAPTION_CTA=re.compile(r"(?i)\b(join|subscribe|follow|share|channel|telegram|powered|uploaded|upload by|donate|support us|request|click|download|link|group|admin|credit|encoded|ripped|sub by|subs by)\b")


def clean_source_caption(caption, filename, promos=()):
    """Lines of a source post caption that are safe to pass on: no @handles, links or promotion, nothing that just repeats the filename."""
    from series_catalog import _strip_custom_promos
    from series_parse import _PROMO_HANDLE
    seen=core_key(normalize(filename or "")); out=[]
    name_tokens={w for w in re.findall(r"[a-z0-9]+",str(filename or "").casefold()) if len(w)>1}
    for line in str(caption or "").splitlines():
        line=_strip_custom_promos(line,promos)
        line=_PROMO_HANDLE.sub(" ",line)
        line=re.sub(r"(?i)(?:https?://|www\.|t\.me/|telegram\.me/)\S+"," ",line)
        if _CAPTION_CTA.search(line): continue
        line=" ".join(line.split()).strip(" -|•·*_~`")
        if len(line)<3 or not re.search(r"[^\W\d_]",line): continue
        key=core_key(normalize(line))
        if not key or key in seen or seen in key and seen: continue
        toks={w for w in re.findall(r"[a-z0-9]+",line.casefold()) if len(w)>1}
        if toks and len(toks-name_tokens)<=max(0,len(toks)//5): continue      # restates the filename in other words
        if line not in out: out.append(line[:160])
    return out


async def deliver_file(ctx, client, chat_id: int, f, quiet=False):
    """Copy an indexed file; preserve its original filename prominently in the caption."""
    settings=await ctx.settings()
    minutes=int(settings.get("autodelete_min",0) or 0)
    raw_name=f.name or f"{f.show} {_ep_label(f)}"
    footer='\n\nJoin <a href="https://t.me/exivum">@exivum</a>'
    room=1024-len(footer)-len("<code></code>")-4
    while len(_e(raw_name))>room: raw_name=raw_name[:-10]      # shorten the name BEFORE adding tags
    audio=[]
    if settings.get("show_audio_line",True):
        audio=[x for x in (getattr(f,"tags",()) or ()) if x in ("Dual Audio","Multi Audio")]
        audio+= [l for l in (getattr(f,"languages",()) or ()) if l not in audio]
    audio_line=("🔊 "+_e(" · ".join(audio[:5]))+"\n") if audio else ""
    if audio_line: room-=len(audio_line)
    while len(_e(raw_name))>room: raw_name=raw_name[:-10]
    extra=""
    if settings.get("caption_transfer",True):
        # information from the SOURCE post's caption (episode title, synopsis lines) travels with the file, in monospace,
        # after links, @handles and promo lines are removed so nobody gets free advertising
        lines=clean_source_caption(getattr(f,"caption",""),raw_name,settings.get("series_promos",[]))
        budget=room-len(_e(raw_name))-len("<code></code>")-6
        keep=[]
        for ln in lines[:4]:
            if len(_e("\n".join(keep+[ln])))+len("<code></code>")<=budget: keep.append(ln)
        if keep: extra="\n<code>"+_e("\n".join(keep))+"</code>"
    caption=audio_line+f"<code>{_e(raw_name)}</code>"+extra+footer
    sent=None
    for attempt in (1,2):
        try:
            sent=await client.copy_message(chat_id=chat_id,from_chat_id=f.channel,message_id=f.message_id,
                caption=caption,parse_mode=ParseMode.HTML,protect_content=bool(settings.get("protect",False)),
                reply_markup=kb([[Btn("Report a problem",f"ss:report:{f.uid}")]]))
            break
        except FloodWait as e:
            if attempt==2:
                log.warning("Telegram rate limit prevented series file delivery uid=%s",f.uid)
                return False
            await asyncio.sleep(min(e.value+1,10))
        except Exception as e:
            if quiet:                                    # e.g. the person never started the bot: the caller falls back to a deep link
                log.info("direct delivery not possible uid=%s: %r",f.uid,e); return False
            log.exception("series file delivery failed uid=%s",f.uid)
            try: await client.send_message(chat_id,"I couldn't send that file. Please try another version or report the problem.")
            except Exception: pass
            return False
    if not sent: return False
    if minutes and getattr(sent,"id",None):
        try: await ctx.store.schedule_delete(chat_id,sent.id,time.time()+minutes*60)
        except Exception: log.exception("could not schedule deletion for series file message %s",sent.id)
    await ctx.series_catalog.bump_hit(f.sid)
    if f.episodes:
        try: await ctx.series_catalog.save_progress(chat_id, f.sid, f.season, max(f.episodes), f.resolution)
        except Exception: log.exception("could not save series progress")
    ctx.stats.add("f")
    try: await ctx.series_catalog.bump_user_stat(chat_id,"downloads")
    except Exception: pass
    return True

_GROUP_NAV_HEADS = {
    "results", "show", "shows", "season", "res", "respage", "goto", "range", "continue",
    "ep", "jumpep", "jump", "packs", "specials", "subs", "subspage", "movies",
    "missing", "missall", "backshow", "backresults", "back", "save",
    "reqshow", "reqseason", "reqep", "discover", "surprise",
}


async def callback(ctx,client,cb,head,arg):
    msg=cb.message; uid=cb.from_user.id
    is_group_msg = (
        str(getattr(getattr(msg, "chat", None), "type", "")).lower().endswith("group")
        or getattr(getattr(msg, "chat", None), "id", 0) < 0
    )
    async def edit(text,rows=None,new_photo=None):
        try:
            # Series screens may originate from the photo-based welcome page.
            # edit_of handles both text messages and photo captions; edit_text
            # alone fails on photos, leaving every button on Discover inert.
            res = await edit_of(msg, new_photo=new_photo)(text, rows)
            target_msg = res if getattr(res, "id", None) else msg
            ttl=await _result_ttl(ctx, is_group_msg)
            sessions = getattr(ctx,"series_sessions",{})
            old_state=sessions.get((msg.chat.id,msg.id))
            if old_state:
                old_state["until"]=time.monotonic()+max(1800,ttl*60+60)
                if target_msg.id != msg.id:
                    sessions[(target_msg.chat.id, target_msg.id)] = old_state
            ctx.watch(target_msg.chat.id,target_msg.id,ttl)
        except Exception:
            log.exception("series screen edit failed")
    try:
        if head=="noop": await cb.answer(); return True
        state_cur = getattr(ctx, "series_sessions", {}).get((msg.chat.id, msg.id))
        if is_group_msg or (state_cur and state_cur.get("group")):
            owner = state_cur.get("owner") if state_cur else None
            if head == "close" and owner is not None and uid != owner and not ctx.is_admin(uid):
                await cb.answer("Only the person who opened this can close it.", show_alert=True); return True
            if head in _GROUP_NAV_HEADS and owner is not None and uid != owner:
                await cb.answer("Only the person who searched can navigate these results. Tap any file button to receive it in your DM.", show_alert=True); return True
        if head=="skip":
            # Permission is enforced here, on the server, for every skip-browser action (not just by hiding buttons).
            if not ctx.is_admin(uid):
                await cb.answer("Admins only.",show_alert=True); return True
            if str(arg)=="export":
                await cb.answer("Preparing the export…")
                import handlers
                handlers._spawn(send_skipped_export(ctx,client,msg.chat.id,uid))
                return True
            t,rows=await skip_action(ctx,cb,str(arg).split(":"),uid)
            # Admin browser screens are NOT scheduled for auto-deletion; the records themselves are never expired.
            await edit_of(msg)(t,rows); await cb.answer(); return True
        if head=="adopt":
            if not ctx.is_admin(uid):
                await cb.answer("Admins only.",show_alert=True); return True
            if arg=="go":
                await cb.answer("Writing…")
                await edit_of(msg)(await adopt_confirm(ctx,client,uid),None)
            else:
                await ctx.series_catalog.clear_adopt_plan(uid)
                await edit_of(msg)("Adopt cancelled. Nothing was written.",None); await cb.answer()
            return True
        if head=="ident":
            if not ctx.is_admin(uid):
                await cb.answer("Admins only.",show_alert=True); return True
            if arg=="go":
                toast=await identities_apply_confirm(ctx,uid,edit_of(msg)); await cb.answer(toast)
            else:
                await ctx.series_catalog.maintenance_state.delete_one({"_id":f"identapply:{int(uid)}"})
                await edit_of(msg)("Identity repair cancelled. Nothing was changed.",None); await cb.answer()
            return True
        if head=="close":
            await msg.delete(); await cb.answer(); return True
        if head=="results":
            key=(msg.chat.id,msg.id); state=ctx.series_sessions.get(key)
            if not state or state.get("until",0)<time.monotonic():
                await cb.answer("Those results expired. Search again.",show_alert=True); return True
            page=max(0,int(state.get("page",0))+(-1 if arg=="prev" else 1))
            text,rows,total=await results_screen(ctx,state["query"],page,state.get("target_season"),state.get("target_episode"),state.get("rows"),state.get("total"))
            state["page"]=page; state["until"]=time.monotonic()+1800
            await edit(text,rows); await cb.answer(); return True
        if head=="treparse":
            if not ctx.is_admin(uid):
                await cb.answer("Admins only.",show_alert=True); return True
            if arg=="go":
                if getattr(ctx,"maintenance",None):
                    await cb.answer("Another maintenance job is running.",show_alert=True); return True
                plan=getattr(ctx,"pending_targeted_reparse",{}).get(uid)
                if not plan or time.monotonic()-float(plan.get("at",0))>900:
                    getattr(ctx,"pending_targeted_reparse",{}).pop(uid,None)
                    await cb.answer("Preview expired. Run /reparsefind again.",show_alert=True); return True
                await cb.answer("Reparsing matching records…")
                ctx.maintenance="reparse"
                try:
                    await targeted_reparse_apply(ctx, uid, msg)
                finally:
                    ctx.maintenance=None
            else:
                getattr(ctx,"pending_targeted_reparse",{}).pop(uid,None)
                await edit_of(msg)("Targeted reparse cancelled. Nothing was changed.",None); await cb.answer()
            return True
        if head=="discover":
            settings=await ctx.settings()
            p_imgs=settings.get("page_images") or {}
            if arg=="menu":
                t,rows=await discover_menu(ctx,settings)
                img=p_imgs.get("discover") or p_imgs.get("welcome") or settings.get("image")
                await edit(t,rows,new_photo=img)
            else:
                t,r=await discover_screen(ctx,arg,uid)
                img=p_imgs.get("mylist" if arg=="mylist" else "discover") or p_imgs.get("discover") or p_imgs.get("welcome") or settings.get("image")
                await edit(t,r,new_photo=img)
                sessions=getattr(ctx,"series_sessions",{})
                sessions[(msg.chat.id,msg.id)]={"origin":"discover","discover_which":arg,"owner":uid,"group":is_group_msg,"until":time.monotonic()+1800}
            await cb.answer(); return True
        if head=="surprise":
            import random
            settings=await ctx.settings()
            sids=list(_all_curated_sids(settings)) or list(settings.get("series_picks",[]))
            if not sids: await cb.answer("No editor picks have been configured yet.",show_alert=True); return True
            sid=random.choice(sids); t,r=await show_screen(ctx,sid,uid=uid,back_discover="menu",group=is_group_msg); await edit(t,r); await cb.answer(); return True
        if head=="curated":
            if not ctx.is_admin(uid): await cb.answer("Admins only.",show_alert=True); return True
            parts=str(arg).split(":")
            act=parts[0] if parts else "menu"
            if act=="menu":
                getattr(ctx,"pending_curated",{}).pop(uid,None)
                t,r=await curated_menu_screen(ctx); await edit_of(msg)(t,r)
            elif act=="cancel":
                getattr(ctx,"pending_curated",{}).pop(uid,None)
                t,r=await curated_menu_screen(ctx); await edit_of(msg)(t,r)
            elif act=="create":
                ctx.pending_curated[uid]={"action":"create","at":time.monotonic()}
                await edit_of(msg)("<b>Create curated list</b>\nSend the name for the new curated Discover list (up to 40 characters).",[[Btn("Cancel","ss:curated:cancel")]])
            elif act=="view" and len(parts)>=2:
                lid=parts[1]; pg=int(parts[2]) if len(parts)>=3 and parts[2].isdigit() else 0
                t,r=await curated_list_screen(ctx,lid,pg); await edit_of(msg)(t,r)
            elif act=="add" and len(parts)>=2:
                lid=parts[1]
                ctx.pending_curated[uid]={"action":"add_search","list_id":lid,"at":time.monotonic()}
                await edit_of(msg)("<b>Add shows to curated list</b>\nSend a show title (or part of a title) to search and select shows manually.",[[Btn("Cancel",f"ss:curated:view:{lid}:0")]])
            elif act=="tog" and len(parts)>=3:
                lid,sid=parts[1],parts[2]
                settings=await ctx.settings(); lists=get_curated_lists(settings)
                lst=next((x for x in lists if x["id"]==lid),None)
                if not lst: await cb.answer("List not found.",show_alert=True); return True
                if sid in lst["sids"]:
                    lst["sids"].remove(sid); toast="Removed from list."
                else:
                    lst["sids"].append(sid); toast="Added to list."
                await save_curated_lists(ctx,lists)
                pend=ctx.pending_curated.get(uid)
                if pend and pend.get("query"):
                    t,r=await curated_search_screen(ctx,lid,pend["query"])
                else:
                    t,r=await curated_list_screen(ctx,lid,0)
                await edit_of(msg)(t,r); await cb.answer(toast); return True
            elif act=="rm" and len(parts)>=2:
                lid=parts[1]; pg=int(parts[2]) if len(parts)>=3 and parts[2].isdigit() else 0
                t,r=await curated_remove_screen(ctx,lid,pg); await edit_of(msg)(t,r)
            elif act=="delshow" and len(parts)>=3:
                lid,sid=parts[1],parts[2]; pg=int(parts[3]) if len(parts)>=4 and parts[3].isdigit() else 0
                settings=await ctx.settings(); lists=get_curated_lists(settings)
                lst=next((x for x in lists if x["id"]==lid),None)
                if lst and sid in lst["sids"]:
                    lst["sids"].remove(sid)
                    await save_curated_lists(ctx,lists)
                t,r=await curated_remove_screen(ctx,lid,pg); await edit_of(msg)(t,r); await cb.answer("Show removed."); return True
            elif act=="rename" and len(parts)>=2:
                lid=parts[1]
                ctx.pending_curated[uid]={"action":"rename","list_id":lid,"at":time.monotonic()}
                await edit_of(msg)("<b>Rename curated list</b>\nSend the new name for this curated list.",[[Btn("Cancel",f"ss:curated:view:{lid}:0")]])
            elif act=="replace" and len(parts)>=2:
                lid=parts[1]
                ctx.pending_curated[uid]={"action":"replace","list_id":lid,"at":time.monotonic()}
                await edit_of(msg)("<b>Replace curated list shows</b>\nSend the show titles separated by <code>|</code> (for example: <code>Breaking Bad | Dark | Severance</code>).",[[Btn("Cancel",f"ss:curated:view:{lid}:0")]])
            elif act=="clear" and len(parts)>=2:
                lid=parts[1]
                await edit_of(msg)("Clear all shows from this curated list?",[[Btn("Yes, clear list",f"ss:curated:clearok:{lid}"),Btn("Cancel",f"ss:curated:view:{lid}:0")]])
            elif act=="clearok" and len(parts)>=2:
                lid=parts[1]
                settings=await ctx.settings(); lists=get_curated_lists(settings)
                lst=next((x for x in lists if x["id"]==lid),None)
                if lst:
                    lst["sids"]=[]
                    await save_curated_lists(ctx,lists)
                t,r=await curated_list_screen(ctx,lid,0); await edit_of(msg)(t,r); await cb.answer("List cleared."); return True
            elif act=="delete" and len(parts)>=2:
                lid=parts[1]
                await edit_of(msg)("Delete this curated list permanently?",[[Btn("Yes, delete list",f"ss:curated:delok:{lid}"),Btn("Cancel",f"ss:curated:view:{lid}:0")]])
            elif act=="delok" and len(parts)>=2:
                lid=parts[1]
                settings=await ctx.settings(); lists=[x for x in get_curated_lists(settings) if x["id"]!=lid]
                await save_curated_lists(ctx,lists)
                t,r=await curated_menu_screen(ctx); await edit_of(msg)(t,r); await cb.answer("List deleted."); return True
            await cb.answer(); return True
        if head=="pick":
            if not ctx.is_admin(uid): await cb.answer("Admins only.",show_alert=True); return True
            sid=arg; settings=await ctx.settings(); lists=get_curated_lists(settings)
            if len(lists)==0:
                lists=[{"id":"handpicked","name":"Hand-picked","sids":[sid]}]
                await save_curated_lists(ctx,lists)
                toast="Added to Hand-picked."
                st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await show_screen(ctx,sid,uid=uid,back_results=st.get("origin")=="results",back_discover=st.get("discover_which"),group=is_group_msg)
                await edit(t,r); await cb.answer(toast); return True
            if len(lists)==1:
                lst=lists[0]
                if sid in lst["sids"]:
                    lst["sids"].remove(sid); toast=f"Removed from {lst['name']}."
                else:
                    lst["sids"].append(sid); toast=f"Added to {lst['name']}."
                await save_curated_lists(ctx,lists)
                st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await show_screen(ctx,sid,uid=uid,back_results=st.get("origin")=="results",back_discover=st.get("discover_which"),group=is_group_msg)
                await edit(t,r); await cb.answer(toast); return True
            t,r=await show_pick_screen(ctx,sid)
            await edit(t,r); await cb.answer(); return True
        if head=="pickto":
            if not ctx.is_admin(uid): await cb.answer("Admins only.",show_alert=True); return True
            lid,sid=arg.split(":",1)
            settings=await ctx.settings(); lists=get_curated_lists(settings)
            lst=next((x for x in lists if x["id"]==lid),None)
            if not lst: await cb.answer("List not found.",show_alert=True); return True
            if sid in lst["sids"]:
                lst["sids"].remove(sid); toast=f"Removed from {lst['name']}."
            else:
                lst["sids"].append(sid); toast=f"Added to {lst['name']}."
            await save_curated_lists(ctx,lists)
            t,r=await show_pick_screen(ctx,sid); await edit(t,r); await cb.answer(toast); return True
        if head=="save":
            sids=await ctx.series_catalog.watchlist(uid)
            if arg in sids: sids.remove(arg); msgtext="Removed from My List."
            else: sids.append(arg); msgtext="Added to My List."
            await ctx.series_catalog.save_watchlist(uid,sids)
            st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await show_screen(ctx,arg,uid=uid,back_results=st.get("origin")=="results",back_discover=st.get("discover_which") if st.get("origin")=="discover" else None,group=is_group_msg)
            await edit(t,r); await cb.answer(msgtext,show_alert=False); return True
        if head=="reqshow":
            query=ctx.series_pending_requests.pop(uid,None)
            if not query: await cb.answer("That request expired. Search again.",show_alert=True); return True
            key=_request_key(query)
            people,is_new,added=await ctx.store.add_request(key,query,uid)
            if is_new or (added and people % 5 == 0):
                qkey=key
                await notify_inbox(ctx,client,f"<b>New series request</b>\nTitle: <b>{_e(query)}</b>\nRequester: <code>{uid}</code>\nRequesters: {people}",[[Btn("✅ Done",f"ar:done:{qkey}"),Btn("💬 Reply",f"ar:rep:{qkey}"),Btn("🗑️ Delete",f"ar:rm:{qkey}")]])
            await edit(f"Request submitted for <b>{_e(query)}</b>. I'll notify you if it is added.",[[Btn("Close","ss:close")]])
            await cb.answer("Request submitted."); return True
        if head=="reqseason":
            sid,season=arg.rsplit(":",1); d=await ctx.series_catalog.get_show(sid)
            existing=await ctx.series_catalog.show_files(sid,int(season))
            if existing: await cb.answer("That season already exists.",show_alert=True); return True
            query=f"{d.get('title','Series') if d else 'Series'} Season {season}"
            key=_request_key(query)
            people,is_new,added=await ctx.store.add_request(key,query,uid)
            await ctx.series_catalog.tag_request(key,sid)
            if is_new or (added and people % 5 == 0): await notify_inbox(ctx,client,f"<b>Season request</b>\n{_e(query)}\nRequesters: {people}",[[Btn("✅ Done",f"ar:done:{key}"),Btn("💬 Reply",f"ar:rep:{key}"),Btn("🗑️ Delete",f"ar:rm:{key}")]])
            await cb.answer("Season request sent.",show_alert=True); return True
        if head=="reqep":
            sid,season,ep=arg.split(":",2); d=await ctx.series_catalog.get_show(sid)
            already=await ctx.series_catalog.show_files(sid,int(season))
            if any(int(ep) in f.episodes for f in already if f.kind!="pack"): await cb.answer("That episode already exists.",show_alert=True); return True
            query=f"{d.get('title','Series') if d else 'Series'} Season {season} Episode {ep}"
            key=_request_key(query)
            people,is_new,added=await ctx.store.add_request(key,query,uid)
            await ctx.series_catalog.tag_request(key,sid)
            if is_new or (added and people % 5 == 0): await notify_inbox(ctx,client,f"<b>Episode request</b>\n{_e(query)}\nRequesters: {people}",[[Btn("✅ Done",f"ar:done:{key}"),Btn("💬 Reply",f"ar:rep:{key}"),Btn("🗑️ Delete",f"ar:rm:{key}")]])
            await cb.answer("Episode request sent.",show_alert=True); return True
        if head=="mergepick_abs":
            if not ctx.is_admin(uid): await cb.answer("Admins only.",show_alert=True); return True
            state=getattr(ctx,"pending_series_merge",{}).get(uid)
            if not state or state.get("step")!="search_absorb" or time.monotonic()-float(state.get("at",0))>600:
                ctx.pending_series_merge.pop(uid,None)
                await cb.answer("Merge session expired. Send /mergeshow again.",show_alert=True); return True
            d=await ctx.series_catalog.get_show(arg)
            if not d: await cb.answer("That show was not found.",show_alert=True); return True
            absorb={"sid":str(d["_id"]),"title":d.get("title","Series"),"year":d.get("year"),"count":int(d.get("count",0) or 0)}
            ctx.pending_series_merge[uid]={"step":"search_survivor","absorb":absorb,"at":time.monotonic()}
            yr=f" ({absorb['year']})" if absorb.get("year") else " (no year)"
            await edit(
                f"<b>Interactive Show Merge (Step 2/3)</b>\n\n"
                f"Show to absorb: <b>{_e(absorb['title'])}</b>{yr} · {absorb['count']:,} files\n\n"
                "Now send the title (or part of the title) of the <b>surviving show</b> (the show to keep).",
                [[Btn("Cancel","ss:mergecancel")]]
            )
            await cb.answer(); return True
        if head=="mergepick_surv":
            if not ctx.is_admin(uid): await cb.answer("Admins only.",show_alert=True); return True
            state=getattr(ctx,"pending_series_merge",{}).get(uid)
            if not state or state.get("step")!="search_survivor" or time.monotonic()-float(state.get("at",0))>600:
                ctx.pending_series_merge.pop(uid,None)
                await cb.answer("Merge session expired. Send /mergeshow again.",show_alert=True); return True
            absorb=state.get("absorb") or {}
            if str(arg)==str(absorb.get("sid")):
                await cb.answer("You cannot merge a show into itself.",show_alert=True); return True
            d=await ctx.series_catalog.get_show(arg)
            if not d: await cb.answer("That show was not found.",show_alert=True); return True
            survivor={"sid":str(d["_id"]),"title":d.get("title","Series"),"year":d.get("year"),"count":int(d.get("count",0) or 0)}
            ctx.pending_series_merge[uid]={"step":"batch_confirm","survivor":survivor,"absorbed_sids":[absorb["sid"]],"at":time.monotonic()}
            lab=lambda x: _e(x["title"])+(f" ({x['year']})" if x.get("year") else " (no year)")+f" · {x['count']:,} files"
            await edit(
                "<b>Confirm show merge (Step 3/3)</b>\n\n"
                f"<b>KEEP (surviving):</b> {lab(survivor)}\n"
                f"<b>ABSORB:</b> {lab(absorb)}\n\n"
                "Files, linked movies, follows, progress and requests will be moved, and the surviving show will be deduplicated automatically.\n"
                "This confirmation expires in 10 minutes.",
                [[Btn("Confirm merge","ss:mergebatchconfirm")],[Btn("Cancel","ss:mergecancel")]]
            )
            await cb.answer(); return True
        if head=="mergebatchconfirm":
            if not ctx.is_admin(uid): await cb.answer("Admins only.",show_alert=True); return True
            state=getattr(ctx,"pending_series_merge",{}).get(uid)
            if not state or state.get("step")!="batch_confirm" or time.monotonic()-float(state.get("at",0))>600:
                ctx.pending_series_merge.pop(uid,None)
                await cb.answer("Merge confirmation expired (10 minutes). Send the command again.",show_alert=True); return True
            survivor_sid=str(state.get("survivor",{}).get("sid") or "")
            absorbed_sids=[str(x) for x in state.get("absorbed_sids",[]) if str(x)!=survivor_sid]
            all_sids=[survivor_sid]+absorbed_sids
            if not survivor_sid or len(absorbed_sids)<1 or len(set(all_sids))!=len(all_sids):
                ctx.pending_series_merge.pop(uid,None)
                await cb.answer("The merge selection is invalid. Nothing was changed.",show_alert=True); return True
            live=[]
            for sid in all_sids:
                d=await ctx.series_catalog.get_show(sid)
                if not d:
                    ctx.pending_series_merge.pop(uid,None)
                    await cb.answer("A selected show no longer exists. Nothing was merged.",show_alert=True); return True
                live.append(d)
            survivor=live[0]
            absorbed=live[1:]
            moved_movies=await ctx.series_catalog.movies.count_documents({"sid":{"$in":absorbed_sids}})
            moved=await ctx.series_catalog.merge_show_ids(
                survivor_sid, absorbed_sids, survivor.get("title","Series"), survivor.get("year")
            )
            dedupe_res=await ctx.series_catalog.dedupe_show(survivor_sid)
            ctx.pending_series_merge.pop(uid,None)
            absorbed_names=", ".join(_e(d.get("title","Series")) for d in absorbed)
            await edit(
                f"<b>Show merge complete</b>\n\nKept: <b>{_e(survivor.get('title','Series'))}</b>"+
                (f" ({survivor.get('year')})" if survivor.get("year") else "")+
                f"\nAbsorbed: {absorbed_names}\n\nSeries file records moved: {moved:,}"+
                f"\nLinked movie records moved: {moved_movies:,}"+
                f"\nSurviving show duplicates removed: {dedupe_res['removed']:,} (recorded in Look-alikes skipped)."+
                "\n\nNo Telegram channel posts were deleted.",
                [[Btn("Close","ss:close")]]
            )
            await cb.answer("Shows merged."); return True
        if head=="mergecancel":
            getattr(ctx,"pending_series_merge",{}).pop(uid,None); await edit("Show merge cancelled.",[[Btn("Close","ss:close")]]); await cb.answer(); return True
        if head=="show":
            st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); origin=st.get("origin","direct")
            t,r=await show_screen(ctx,arg,uid=uid,back_results=(origin=="results"),back_discover=st.get("discover_which") if origin=="discover" else None,group=is_group_msg); await edit(t,r); await cb.answer(); return True
        if head=="backshow":
            sid=arg; st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); origin=st.get("origin","direct")
            if origin=="results" and st.get("until",0)>=time.monotonic():
                t,r,_=await results_screen(ctx,st["query"],st.get("page",0),st.get("target_season"),st.get("target_episode"),st.get("rows"),st.get("total")); await edit(t,r)
            else:
                t,r=await show_screen(ctx,sid,uid=uid,back_results=False,back_discover=st.get("discover_which") if origin=="discover" else None,group=is_group_msg); await edit(t,r)
            await cb.answer(); return True
        if head=="backresults":
            st=ctx.series_sessions.get((msg.chat.id,msg.id))
            if not st or st.get("origin")!="results" or st.get("until",0)<time.monotonic(): await cb.answer("Search results expired. Search again.",show_alert=True); return True
            t,r,_=await results_screen(ctx,st["query"],st.get("page",0),st.get("target_season"),st.get("target_episode"),st.get("rows"),st.get("total")); await edit(t,r); await cb.answer(); return True
        if head=="back": await edit("Search for a series by sending its title."); await cb.answer(); return True
        if head=="shows":
            sid,page=arg.rsplit(":",1); st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await show_screen(ctx,sid,int(page),uid,back_results=st.get("origin")=="results",back_discover=st.get("discover_which") if st.get("origin")=="discover" else None,group=is_group_msg); await edit(t,r); await cb.answer(); return True
        if head=="goto":
            bits=arg.split(":"); sid,season,res,level=bits[:4]; lo=int(bits[4]) if len(bits)>4 else None; hi=int(bits[5]) if len(bits)>5 else None
            t,r=await goto_screen(ctx,sid,int(season),res,int(level),lo,hi); await edit(t,r); await cb.answer(); return True
        if head=="range":
            sid,season,res,lo,hi=arg.split(":",4)
            _,range_items,_,_=await _resolution_items(ctx,sid,int(season),res)
            if len({e for f,_ in range_items for e in f.episodes})>=500 and int(hi)-int(lo)>=50:
                t,r=await goto_screen(ctx,sid,int(season),res,1,int(lo),int(hi))
            else:
                st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await resolution_screen(ctx,sid,int(season),res,-1,int(lo),int(hi),back_results=st.get("origin")=="results",back_discover=st.get("discover_which") if st.get("origin")=="discover" else None)
            await edit(t,r); await cb.answer(); return True
        if head=="continue":
            sid=arg; prog=await ctx.series_catalog.get_progress(uid,sid)
            if not prog: await cb.answer("No saved progress.",show_alert=True); return True
            season=int(prog["season"]); res=prog.get("resolution") or "unknown"; lo=int(prog["episode"])
            if season==0 and _display_season_numbers(await _show_files(ctx,sid))==[1]: season=1
            _,items,_,by_ep=await _resolution_items(ctx,sid,season,res)
            hi=max(by_ep) if by_ep else lo
            st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await resolution_screen(ctx,sid,season,res,-1,lo,hi,back_results=st.get("origin")=="results",back_discover=st.get("discover_which") if st.get("origin")=="discover" else None); await edit(t,r); await cb.answer(); return True
        if head=="ep":
            sid,season,ep=arg.split(":",2); st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await episode_screen(ctx,sid,int(season),int(ep),back_results=st.get("origin")=="results",back_discover=st.get("discover_which") if st.get("origin")=="discover" else None); await edit(t,r); await cb.answer(); return True
        if head=="season":
            sid,season=arg.rsplit(":",1); st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await season_screen(ctx,sid,int(season),uid,back_results=st.get("origin")=="results",back_discover=st.get("discover_which") if st.get("origin")=="discover" else None,group=is_group_msg); await edit(t,r); await cb.answer(); return True
        if head=="jumpep":
            sid,season,ep=arg.split(":",2); st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await episode_screen(ctx,sid,int(season),int(ep),back_results=st.get("origin")=="results",back_discover=st.get("discover_which") if st.get("origin")=="discover" else None); await edit(t,r); await cb.answer(); return True
        if head=="jump":
            sid,season=arg.rsplit(":",1); st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await season_screen(ctx,sid,int(season),uid,back_results=st.get("origin")=="results",back_discover=st.get("discover_which") if st.get("origin")=="discover" else None,group=is_group_msg); await edit(t,r); await cb.answer(); return True
        if head=="res":
            sid,season,res=arg.split(":",2); st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await resolution_screen(ctx,sid,int(season),res,0,back_results=st.get("origin")=="results",back_discover=st.get("discover_which") if st.get("origin")=="discover" else None); await edit(t,r); await cb.answer(); return True
        if head=="respage":
            bits=arg.split(":"); sid,season,res,offset=bits[:4]; asc=len(bits)>4 and bits[4]=="asc"
            st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await resolution_screen(ctx,sid,int(season),res,int(offset),1 if asc else None,None,back_results=st.get("origin")=="results",back_discover=st.get("discover_which") if st.get("origin")=="discover" else None); await edit(t,r); await cb.answer(); return True
        if head=="missing":
            bits=arg.split(":"); sid,season,res=bits[:3]; back=int(bits[3]) if len(bits)>3 and bits[3].isdigit() else 0
            t,r=await missing_screen(ctx,sid,int(season),res,back); await edit(t,r); await cb.answer(); return True
        if head=="missall":
            sid,season=arg.split(":",1); t,r=await missing_all_screen(ctx,sid,int(season)); await edit(t,r); await cb.answer(); return True
        if head=="movies":
            t,r=await movies_screen(ctx,arg); await edit(t,r); await cb.answer(); return True
        if head=="moviefile":
            movie=await ctx.series_catalog.movies.find_one({"_id":arg})
            if not movie: await cb.answer("Movie entry not found.",show_alert=True); return True
            if is_group_msg:
                import handlers
                if not await handlers.missing_channels(ctx,client,uid):
                    try:
                        await client.copy_message(chat_id=uid,from_chat_id=int(movie["channel"]),message_id=int(movie["message_id"]))
                        await cb.answer("Movie sent to your DM ✅"); return True
                    except Exception:
                        pass
                username=(getattr(ctx,"bot_username","") or "").lstrip("@")
                await cb.answer(url=f"https://t.me/{username}?start=sm_{arg}"); return True
            await cb.answer("Sending movie…")
            try: await client.copy_message(chat_id=uid,from_chat_id=int(movie["channel"]),message_id=int(movie["message_id"]))
            except Exception:
                log.exception("linked series movie delivery failed")
                await client.send_message(uid,"I couldn't retrieve that movie from its source channel. Please report this item.")
            return True
        if head=="specials":
            sid,season=arg.rsplit(":",1); t,r=await specials_screen(ctx,sid,int(season)); await edit(t,r); await cb.answer(); return True
        if head=="subs":
            sid,season=arg.rsplit(":",1); t,r=await subtitles_screen(ctx,sid,int(season),0); await edit(t,r); await cb.answer(); return True
        if head=="subspage":
            sid,season,page=arg.split(":",2); t,r=await subtitles_screen(ctx,sid,int(season),int(page)); await edit(t,r); await cb.answer(); return True
        if head=="adminfiles":
            if not ctx.is_admin(uid): await cb.answer("Admins only.",show_alert=True); return True
            sid,season=arg.rsplit(":",1); files=await ctx.series_catalog.show_files(sid,int(season))
            rows=[[Btn(f"Remove: {_ep_label(f)} · {_quality(f)}"[:60],f"ss:remove:{f.uid}")] for f in files[:40]]
            rows.append([Btn("Back","ss:season:"+sid+":"+season)])
            await edit("<b>Admin file review</b>\nRemoving a file removes it from the index only; the channel post stays untouched. Choose a bad file:",rows)
            await cb.answer(); return True
        if head=="remove":
            if not ctx.is_admin(uid): await cb.answer("Admins only.",show_alert=True); return True
            f=await ctx.series_catalog.get_file(arg)
            if not f: await cb.answer("File not found.",show_alert=True); return True
            await edit(f"Remove this file from the index?\n<b>{_e(f.show)}</b> · {_e(_ep_label(f))}\n<code>{_e(f.name)}</code>",[[Btn("Confirm remove","ss:confirmremove:"+arg),Btn("Cancel","ss:season:"+f.sid+":"+str(f.season))]])
            await cb.answer(); return True
        if head=="confirmremove":
            if not ctx.is_admin(uid): await cb.answer("Admins only.",show_alert=True); return True
            f=await ctx.series_catalog.remove_file(arg)
            await edit("File removed from the series index. The original channel post was not deleted." if f else "File was already removed.",[[Btn("Close","ss:close")]])
            await cb.answer("Index updated."); return True
        if head=="packs":
            fs=await ctx.series_catalog.show_files(arg); rows=[[Btn(f"{_season_label(f.season)} pack · {_quality(f)} · {f.size//1048576} MB"[:60],f"ss:file:{f.uid}")] for f in fs if f.kind=="pack"]
            rows.append([Btn("Back","ss:show:"+arg)]); await edit("<b>Season and series packs</b>",rows); await cb.answer(); return True
        if head=="file":
            f=await ctx.series_catalog.get_file(arg)
            if not f: await cb.answer("That file is no longer indexed.",show_alert=True); return True
            if getattr(getattr(msg,"chat",None),"type",None) in ("group","supergroup") or getattr(getattr(msg,"chat",None),"id",0)<0:
                # Someone who has already started the bot and joined the required channels gets the file straight in their DM
                # with a small "sent" notice, so they can tap many episodes in a row. Anyone else is sent to the bot once.
                import handlers
                if not await handlers.missing_channels(ctx,client,uid):
                    if await deliver_file(ctx,client,uid,f,quiet=True):
                        await cb.answer("File sent to your DM ✅"); return True
                username=(getattr(ctx,"bot_username","") or "").lstrip("@")
                await cb.answer(url=f"https://t.me/{username}?start=s_{f.uid}")
                return True
            await cb.answer("Sending file…")
            await deliver_file(ctx,client,uid,f)
            return True
        if head=="report":
            f=await ctx.series_catalog.get_file(arg)
            if not f: await cb.answer("That file is no longer indexed.",show_alert=True); return True
            reasons=[("l","Wrong language"),("d","Dubbed, not original"),("a","No sound / out of sync"),
                     ("p","Won't play / corrupted"),("e","Wrong episode / wrong show"),("q","Poor quality"),
                     ("s","Missing subtitles"),("o","Other")]
            rows=[[Btn(label,f"ss:rr:{code}:{f.uid}") for code,label in reasons[i:i+2]] for i in range(0,len(reasons),2)]
            rows.append([Btn("Cancel",f"ss:rr:x:{f.uid}")])
            await msg.edit_reply_markup(kb(rows))
            await cb.answer(); return True
        if head=="rr":
            code,uidarg=arg.split(":",1)
            if code=="x":
                await msg.edit_reply_markup(kb([[Btn("Report a problem",f"ss:report:{uidarg}")]]))
                await cb.answer("Report cancelled."); return True
            f=await ctx.series_catalog.get_file(uidarg)
            if not f: await cb.answer("That file is no longer indexed.",show_alert=True); return True
            labels={"l":"Wrong language","d":"Dubbed, not original","a":"No sound / out of sync","p":"Won't play / corrupted","e":"Wrong episode / wrong show","q":"Poor quality","s":"Missing subtitles","o":"Other"}
            total,reasons,fresh=await ctx.store.add_report(uidarg,code,uid)
            await msg.edit_reply_markup(kb([[Btn("Reported, thanks","noop")]]))
            await cb.answer("Thanks, I've passed it on.",show_alert=True)
            if fresh:
                why=", ".join(f"{labels.get(k,k)} ×{n}" for k,n in reasons.items())
                await notify_inbox(ctx,client,
                    f"<b>Series file report ({total})</b>\nUser: <code>{uid}</code>\nShow: {_e(f.show)}\nSeason: {f.season}\nEpisode: {_e(', '.join(map(str,f.episodes)) or 'Pack')}\nFilename: <code>{_e(f.name)}</code>\nReason: {_e(labels.get(code,code))}\nAll reports: {_e(why)}",
                    [[Btn("Remove this file",f"ss:rmreport:{uidarg}"),Btn("Dismiss",f"ss:dismissreport:{uidarg}")]])
            return True
        if head in ("rmreport","dismissreport"):
            if not ctx.is_admin(uid): await cb.answer("Admins only.",show_alert=True); return True
            if head=="rmreport":
                f=await ctx.series_catalog.remove_file(arg)
                await ctx.store.clear_reports(arg)
                await edit("Series file removed from the index." if f else "File was already removed.",[[Btn("Close","ss:close")]])
            else:
                await ctx.store.clear_reports(arg)
                await edit("Report dismissed.",[[Btn("Close","ss:close")]])
            await cb.answer(); return True
    except Exception:
        log.exception("series callback failed: %s",head)
        try: await cb.answer("Something went wrong. Please try again.",show_alert=True)
        except Exception: pass
        return True
    return False

async def notify_followers(ctx,client,f):
    """Notify users who saved/follow this show; called only for a newly posted channel file."""
    # Coalesce rapid uploads for the same show into one follower notification
    # window. A hard per-show cap keeps a large watchlist from flooding Telegram.
    if not hasattr(ctx,"series_follow_notice_cache"): ctx.series_follow_notice_cache={}
    now=time.monotonic(); last=ctx.series_follow_notice_cache.get(f.sid,0)
    if now-last>=60:
        ctx.series_follow_notice_cache[f.sid]=now
        if len(ctx.series_follow_notice_cache)>500: ctx.series_follow_notice_cache={k:v for k,v in ctx.series_follow_notice_cache.items() if now-v<3600}
        for uid in (await ctx.series_catalog.followers(f.sid))[:20]:
            try:
                username=(getattr(ctx,"bot_username","") or "").lstrip("@")
                url=f"https://t.me/{username}?start=s_{f.uid}"
                text=f"New episode added: <b>{_e(f.show)}</b> · {_e(_ep_label(f))}\n{_e(_quality(f))}"
                await client.send_message(uid,text,parse_mode=ParseMode.HTML,reply_markup=kb([[Btn("Get episode",url=url)]]))
                await asyncio.sleep(0.05)
            except Exception:
                log.info("could not notify series follower %s; they may not have started the bot",uid)
    # Resolve a request only when the newly indexed file satisfies its requested scope.
    norm=core_key(normalize(f.show))
    for req in await ctx.store.open_requests(100):
        query=core_key(normalize(req.get("text","")))
        if not norm or norm not in query:
            continue
        qraw=str(req.get("text",""))
        sm=re.search(r"(?i)\bseason\s+(\d{1,2})\b",qraw)
        em=re.search(r"(?i)\bepisode\s*(\d{1,4})\b",qraw)
        if sm and int(sm.group(1))!=f.season:
            continue
        if em and int(em.group(1)) not in f.episodes:
            continue
        for uid in req.get("users",[]):
            try:
                await client.send_message(uid,f"Your requested series item <b>{_e(req.get('text',''))}</b> has been added to the library.",parse_mode=ParseMode.HTML)
            except Exception: pass
        await ctx.store.close_request(req["key"])

_REPARSE_FIELDS=("kind","season","episodes","absolute","ep_title","special","special_label","part",
                 "resolution","source","codec","languages","group","tags","ext")


def reparse_downgrade(old, parsed):
    """Why a reparse result must NOT overwrite the stored record (None = safe to refresh).

    Reparse may enrich metadata; it may never replace stored evidence with a weaker guess.
    """
    if getattr(old,"adopted",False): return "adopted"                              # administrator decision
    if old.kind=="episode" and parsed.kind=="pack": return "episode_to_pack"
    if parsed.kind=="episode" and old.absolute and not parsed.absolute and parsed.season>0 and old.season==0:
        return "absolute_to_season"                                                 # One Piece 411 must never become S04E11
    if old.special and not parsed.special: return "special_designation_lost"
    if len(old.episodes)>1 and len(parsed.episodes)<len(old.episodes) and set(parsed.episodes)<=set(old.episodes):
        return "multi_episode_lost"
    return None

def _reparse_reading(old, promos):
    """How the current parser reads a stored file: (parsed, candidate title) or (None, None) if unreadable.

    The single reading used by /reparse and by /identities, so they can never disagree about what counts
    as an identity conflict."""
    from parse import parse_series, strip_title_groups, enrich_from_caption
    from series_catalog import _strip_custom_promos, _parse_series_text, is_generic_media_name
    parsed=(parse_series("") if is_generic_media_name(old.name) else parse_series(_strip_custom_promos((old.name or "").strip(),promos)))
    from_name=parsed.kind in ("episode","pack") and bool(parsed.show)
    if not from_name and old.caption:
        parsed=_parse_series_text(old.caption,promos)
    elif from_name and old.caption:
        parsed=enrich_from_caption(parsed,old.name or "",old.caption)
    if parsed.kind not in ("episode","pack") or not parsed.show:
        return None,None
    return parsed,strip_title_groups(parsed.show,promos)


async def reparse_catalog(ctx, client, progress_message, dry=False, recheck=False, resume=False):
    """Conservatively refresh parsed metadata without changing established show identity.

    Reparse is not a rename/merge tool. If the newly parsed title does not normalize
    to the stored title, the entire record is left untouched for explicit review.
    This prevents parser regressions or stale aliases from moving thousands of files.
    """
    from dataclasses import replace
    from parse import parse_series, strip_title_groups, enrich_from_caption
    from series_catalog import _strip_custom_promos, effective_promos, is_generic_media_name
    cat=ctx.series_catalog
    promos=effective_promos((await ctx.settings()).get("series_promos",[]))
    after_id=await cat.get_reparse_checkpoint() if (resume and not dry) else None
    if not dry and not resume: await cat.clear_reparse_checkpoint()
    scanned=changed=unread=identity_review=0; examples=[]; last_edit=[0.0]
    async def say(text,force=False):
        now=time.monotonic()
        if not force and now-last_edit[0]<4: return
        last_edit[0]=now
        try: await progress_message.edit_text(text)
        except Exception: pass
    await say(("Dry run: checking safe metadata updates…" if dry else
               (f"Reparsing safe metadata…\nResuming after {after_id}." if after_id else "Reparsing safe metadata from the beginning…")),True)
    projection={"_id":1,"sid":1,"show":1,"year":1,"kind":1,"season":1,"episodes":1,
                "absolute":1,"ep_title":1,"channel":1,"message_id":1,"size":1,"name":1,
                "resolution":1,"source":1,"codec":1,"languages":1,"group":1,"tags":1,
                "ext":1,"caption":1,"indexed_at":1,"special":1,"special_label":1,"part":1,"adopted":1}
    held_weaker=protected=0
    async for batch,last_id in cat.iter_file_batches(batch_size=400,after_id=after_id,projection=projection):
        pending=[]
        for old in batch:
            scanned+=1
            if getattr(old,"adopted",False):
                protected+=1; continue                  # manual adoption survives every reparse untouched
            parsed,candidate=_reparse_reading(old,promos)
            if parsed is None:
                unread+=1
                continue
            # Identity is immutable during reparse. A disagreement is surfaced for
            # manual review, not resolved through aliases or written to MongoDB.
            if core_key(normalize(candidate)) != core_key(normalize(old.show)):
                identity_review+=1
                if len(examples)<12: examples.append((old.show,candidate,old.name))
                continue
            why=reparse_downgrade(old,parsed)
            if why:
                held_weaker+=1
                if len(examples)<12: examples.append((old.show,f"held: {why}",old.name))
                continue
            new=replace(old,kind=parsed.kind,season=parsed.season,episodes=parsed.episodes,part=parsed.part,
                        absolute=parsed.absolute,ep_title=parsed.ep_title,special=parsed.special,
                        special_label=parsed.special_label,resolution=parsed.resolution or old.resolution,
                        source=parsed.source,codec=parsed.codec,languages=parsed.languages,
                        group=parsed.group,tags=parsed.tags,ext=parsed.ext or old.ext)
            if any(getattr(old,k)!=getattr(new,k) for k in _REPARSE_FIELDS):
                changed+=1; pending.append(new)
        if pending and not dry: await cat.bulk_update_files(pending)
        if not dry: await cat.set_reparse_checkpoint(last_id)
        await say(f"{'Dry run' if dry else 'Reparsing'}… checked {scanned:,} · {'safe metadata updates' if dry else 'updated'} {changed:,} · identity conflicts held for review {identity_review:,} · unreadable {unread:,}")
        if ctx.maintenance_stop.is_set():
            await say(f"Reparse paused safely. Checked {scanned:,}; updated {changed:,}; identity conflicts held {identity_review:,}. Send /reparse resume to continue.",True)
            return {"scanned":scanned,"changed":changed,"identity_review":identity_review,"paused":True}
        await asyncio.sleep(0.3)
    ex="\n".join(f"{o} → {n} | {name[:110]}" for o,n,name in examples) or "none"
    if dry:
        await say(f"Safe reparse dry run complete.\nChecked: {scanned:,}\nSafe metadata updates: {changed:,}\nIdentity conflicts held for review (not changed): {identity_review:,}\nHeld (a weaker reading would replace stored evidence): {held_weaker:,}\nManually adopted (never rewritten): {protected:,}\nUnreadable (left alone): {unread:,}\n\nExamples of conflicts/holds (stored → newly parsed | filename):\n{ex}\n\nNo show IDs, show titles, years or aliases will be changed by /reparse.",True)
        return {"scanned":scanned,"changed":changed,"identity_review":identity_review,"held":held_weaker,"protected":protected,"dry":True}
    if scanned==0:
        await say("Nothing to check: no records were found"+(" after the saved position. Send /reparse (without resume) to start over." if after_id else " in the catalogue."),True)
        return {"scanned":0,"changed":0,"identity_review":identity_review}
    rb=await cat.rebuild_shows()
    await cat.clear_reparse_checkpoint()
    recognized=imported=0
    if recheck:
        await say("Safe metadata refresh complete. Re-checking skipped posts…",True)
        recognized,imported=await recheck_skipped_posts(ctx,client,progress_message)
    await say(f"Safe reparse complete.\nChecked: {scanned:,}\nMetadata records updated: {changed:,}\nIdentity conflicts held for review: {identity_review:,}\nHeld as weaker readings: {held_weaker:,}\nManually adopted (untouched): {protected:,}\nUnreadable (left alone): {unread:,}\nShows now: {rb['shows']:,} (created {rb['created']:,}, empty ones removed {rb['removed_empty']:,})\nSkipped posts recognized: {recognized:,} · newly indexed: {imported:,}\n\nShow titles, show IDs, years and aliases were not changed.",True)
    return {"scanned":scanned,"changed":changed,"identity_review":identity_review,"held":held_weaker,"protected":protected,"imported":imported}


def compile_filename_pattern(pattern: str) -> tuple[str, dict]:
    """Turn a substring, glob (* / ?), or re:<regex> into a MongoDB name filter."""
    raw = str(pattern or "").strip()
    if not raw:
        raise ValueError("Provide a filename pattern, glob (*), or re:<regex>.")
    if raw.startswith("re:"):
        rx = raw[3:].strip()
        label = f"regex: {rx}"
    elif len(raw) >= 2 and raw.startswith("/") and raw.endswith("/"):
        rx = raw[1:-1]
        label = f"regex: {rx}"
    elif "*" in raw or "?" in raw:
        parts = []
        for ch in raw:
            if ch == "*":
                parts.append(".*")
            elif ch == "?":
                parts.append(".")
            else:
                parts.append(re.escape(ch))
        rx = "".join(parts)
        label = f"glob: {raw}"
    else:
        rx = re.escape(raw)
        label = f"match: {raw}"
    if not rx:
        raise ValueError("Pattern cannot be empty.")
    try:
        re.compile(rx, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc
    return label, {"name": {"$regex": rx, "$options": "i"}}


async def targeted_reparse_preview(ctx, uid: int, pattern: str) -> tuple[str, list]:
    """Find files matching a filename pattern, preview how they would reparse, and request confirmation."""
    from dataclasses import replace
    from series_catalog import effective_promos
    label, filt = compile_filename_pattern(pattern)
    cat = ctx.series_catalog
    total = await cat.files.count_documents(filt)
    if total == 0:
        return f"No indexed files matched <code>{_e(pattern)}</code>.", [[Btn("Close", "ss:close")]]

    promos = effective_promos((await ctx.settings()).get("series_promos", []))
    projection = {"_id": 1, "sid": 1, "show": 1, "year": 1, "kind": 1, "season": 1, "episodes": 1,
                  "absolute": 1, "ep_title": 1, "channel": 1, "message_id": 1, "size": 1, "name": 1,
                  "resolution": 1, "source": 1, "codec": 1, "languages": 1, "group": 1, "tags": 1,
                  "ext": 1, "caption": 1, "indexed_at": 1, "special": 1, "special_label": 1, "part": 1, "adopted": 1}

    matched = will_update = title_changes = held = protected = unread = unchanged = 0
    previews: list[str] = []
    async for batch, _last_id in cat.iter_file_batches(batch_size=300, query=filt, projection=projection):
        for old in batch:
            matched += 1
            if getattr(old, "adopted", False):
                protected += 1
                continue
            parsed, candidate = _reparse_reading(old, promos)
            if parsed is None:
                unread += 1
                continue
            why = reparse_downgrade(old, parsed)
            if why:
                held += 1
                continue
            new_title = candidate or old.show
            new_year = parsed.year if parsed.year is not None else old.year
            new_sid = show_id(new_title, new_year)
            new = replace(old, sid=new_sid, show=new_title, year=new_year,
                          kind=parsed.kind, season=parsed.season, episodes=parsed.episodes, part=parsed.part,
                          absolute=parsed.absolute, ep_title=parsed.ep_title, special=parsed.special,
                          special_label=parsed.special_label, resolution=parsed.resolution or old.resolution,
                          source=parsed.source, codec=parsed.codec, languages=parsed.languages,
                          group=parsed.group, tags=parsed.tags, ext=parsed.ext or old.ext)
            diff_title = (core_key(normalize(new.show)) != core_key(normalize(old.show))) or (new.year != old.year)
            diff_meta = any(getattr(old, k) != getattr(new, k) for k in _REPARSE_FIELDS)
            if diff_title or diff_meta:
                will_update += 1
                if diff_title:
                    title_changes += 1
                if len(previews) < 8:
                    previews.append(
                        f"• <code>{_e((old.name or '')[:70])}</code>\n"
                        f"  Was: {_e(old.show)} · {_e(_ep_label(old))} ({old.kind})\n"
                        f"  Now: <b>{_e(new.show)}</b> · {_e(_ep_label(new))} ({new.kind})"
                    )
            else:
                unchanged += 1

    if not hasattr(ctx, "pending_targeted_reparse"):
        ctx.pending_targeted_reparse = {}
    ctx.pending_targeted_reparse[uid] = {
        "pattern": pattern, "label": label, "filt": filt, "total": matched,
        "will_update": will_update, "at": time.monotonic(),
    }
    lines = [
        "<b>Targeted reparse preview (read-only)</b>",
        f"Filter: <code>{_e(label)}</code>",
        f"Matched records: <b>{matched:,}</b>",
        f"Would update: <b>{will_update:,}</b>" + (f" (including {title_changes:,} title change(s))" if title_changes else ""),
        f"Unchanged: {unchanged:,} · Held: {held:,} · Adopted: {protected:,} · Unreadable: {unread:,}",
    ]
    if previews:
        lines += ["", "<b>Preview samples:</b>"] + previews
    if will_update == 0:
        ctx.pending_targeted_reparse.pop(uid, None)
        lines.append("\nNo matching records need updating.")
        return "\n".join(lines), [[Btn("Close", "ss:close")]]
    lines.append("\nOnly these matching records will be reparsed if you confirm.")
    return "\n".join(lines), [[Btn(f"Confirm reparse ({will_update:,})", "ss:treparse:go"), Btn("Cancel", "ss:treparse:cancel")]]


async def targeted_reparse_apply(ctx, uid: int, progress_message) -> dict:
    """Reparse only records matching the confirmed targeted-reparse filter."""
    from dataclasses import replace
    from series_catalog import effective_promos
    plan = getattr(ctx, "pending_targeted_reparse", {}).pop(uid, None)
    if not plan:
        await progress_message.edit_text("No pending targeted reparse plan found.")
        return {"scanned": 0, "updated": 0}
    filt = plan["filt"]
    cat = ctx.series_catalog
    promos = effective_promos((await ctx.settings()).get("series_promos", []))
    projection = {"_id": 1, "sid": 1, "show": 1, "year": 1, "kind": 1, "season": 1, "episodes": 1,
                  "absolute": 1, "ep_title": 1, "channel": 1, "message_id": 1, "size": 1, "name": 1,
                  "resolution": 1, "source": 1, "codec": 1, "languages": 1, "group": 1, "tags": 1,
                  "ext": 1, "caption": 1, "indexed_at": 1, "special": 1, "special_label": 1, "part": 1, "adopted": 1}

    scanned = updated = held = protected = unread = 0
    affected_sids: set[str] = set()
    dest_shows: dict[str, tuple[str, int | None]] = {}
    async for batch, _last_id in cat.iter_file_batches(batch_size=400, query=filt, projection=projection):
        pending = []
        for old in batch:
            scanned += 1
            if getattr(old, "adopted", False):
                protected += 1
                continue
            parsed, candidate = _reparse_reading(old, promos)
            if parsed is None:
                unread += 1
                continue
            why = reparse_downgrade(old, parsed)
            if why:
                held += 1
                continue
            new_title = candidate or old.show
            new_year = parsed.year if parsed.year is not None else old.year
            new_sid = show_id(new_title, new_year)
            new = replace(old, sid=new_sid, show=new_title, year=new_year,
                          kind=parsed.kind, season=parsed.season, episodes=parsed.episodes, part=parsed.part,
                          absolute=parsed.absolute, ep_title=parsed.ep_title, special=parsed.special,
                          special_label=parsed.special_label, resolution=parsed.resolution or old.resolution,
                          source=parsed.source, codec=parsed.codec, languages=parsed.languages,
                          group=parsed.group, tags=parsed.tags, ext=parsed.ext or old.ext)
            diff_title = (old.sid != new.sid) or (old.show != new.show) or (old.year != new.year)
            diff_meta = any(getattr(old, k) != getattr(new, k) for k in _REPARSE_FIELDS)
            if diff_title or diff_meta:
                updated += 1
                pending.append(new)
                affected_sids.add(old.sid)
                affected_sids.add(new.sid)
                dest_shows[new.sid] = (new.show, new.year)
        if pending:
            await cat.bulk_update_files(pending)
    for sid in affected_sids:
        dest = (sid, *dest_shows[sid]) if sid in dest_shows else None
        await cat.refresh_show_counts([sid], dest=dest)
    await progress_message.edit_text(
        f"<b>Targeted reparse complete</b>\n"
        f"Filter: <code>{_e(plan.get('label', plan.get('pattern', '')))}</code>\n"
        f"Matched records scanned: {scanned:,}\n"
        f"Records updated: {updated:,}\n"
        f"Held: {held:,} · Adopted (untouched): {protected:,} · Unreadable: {unread:,}",
        reply_markup=kb([[Btn("Close", "ss:close")]]),
    )
    return {"scanned": scanned, "updated": updated, "held": held, "protected": protected, "unread": unread}


async def recheck_skipped_posts(ctx, client, progress_message):
    """Re-read skipped channel posts with the current parser; index the ones that now parse.

    Walks the skip records with a keyset cursor (the list shrinks as posts are processed, so a
    skip/offset walk would miss records). Posts that still do not parse keep their record and just
    get a refreshed classification label. Look-alikes are left alone.
    """
    from series_catalog import from_message
    cat=ctx.series_catalog
    recognized=imported=0; last=None
    while True:
        page=await cat.skips_after(last,20)
        if not page: break
        last=page[-1]["_id"]
        docs=[d for d in page if d.get("category")!="lookalike"]
        by_channel={}
        for r in docs: by_channel.setdefault(int(r.get("channel",0)),[]).append(r)
        for channel,entries in by_channel.items():
            ids=[int(r.get("message_id",0)) for r in entries if int(r.get("message_id",0))>0]
            if not ids: continue
            try: messages=await client.get_messages(channel,ids)
            except Exception:
                log.info("Could not re-check skipped series posts in channel %s",channel,exc_info=True); continue
            if not isinstance(messages,list): messages=[messages]
            settings=await ctx.settings(); aliases=await cat.alias_map()
            for m in messages:
                if not m: continue
                rec=from_message(m,promos=settings.get("series_promos",[]),aliases=aliases)
                if rec:
                    recognized+=1
                    added,known,dupes=await cat.add([rec]); imported+=added
                    if not dupes and (added or known): await cat.clear_skip(rec.channel,rec.message_id)
                else:
                    await cat.add_skip(m,promos=settings.get("series_promos",[]))     # same record, refreshed label only
        if ctx.maintenance_stop.is_set(): break
        await asyncio.sleep(0.5)
    return recognized,imported


async def rebuild_shows_report(ctx):
    rb=await ctx.series_catalog.rebuild_shows()
    return (f"Shows rebuilt from the files.\nShows now: {rb['shows']:,}\nCreated (files had no show): {rb['created']:,}\n"
            f"Counts corrected: {rb['count_fixed']:,}\nEmpty shows removed: {rb['removed_empty']:,}")


async def audit_report(ctx):
    a=await ctx.series_catalog.audit()
    top="\n".join(f"{i+1}. {t or '?'}{f' ({y})' if y else ''} — {n:,} files" for i,(t,y,n) in enumerate(a["top"]))
    return (f"Series catalogue health\nFiles: {a['files']:,}\nShows listed: {a['shows']:,}\nShows found in files: {a['sids']:,}\n"
            f"Shows with only 1 file: {a['single_file_shows']:,}\n"
            f"Files whose show is missing from the list: {a['orphan_sids']:,}\nListed shows with no files: {a['empty_shows']:,}\n\n"
            f"Biggest shows (an encoder name here means a bad title):\n{top}\n\n"
            "If the missing/empty numbers are above 0, run /rebuildshows.")


_MERGE_NOYEAR=re.compile(r"^(.*?)\s*\(\s*(?:no\s+year|none)\s*\)$",re.I)
_MERGE_YEAR=re.compile(r"^(.*?)\s*\((19\d{2}|20\d{2})\)$")


def _typed_form(d):
    y=d.get("year")
    return f"{d.get('title','Series')} ({y})" if y else f"{d.get('title','Series')} (no year)"


async def resolve_merge_titles(ctx, parts):
    """Turn the titles typed after /mergeshow into catalogue entries. LITERAL MATCHING ONLY.

    A title matches an entry only if it is exactly the entry's stored title (case-sensitive).
    There is no normalising, no fuzzy matching and no fallback. Accepted forms:
      Title             the entry titled exactly Title. If several share that title and exactly one
                        has no year, that one is used; otherwise you must say which
      Title (2005)      the entry titled exactly Title whose year is 2005
      Title (no year)   the entry titled exactly Title that has no year
      sid:<SID>         one specific entry
    A title that itself ends in (2005) is tried as written first.
    Returns (selections, errors); nothing is changed here."""
    selections=[]; errors=[]
    entry=lambda d:{"sid":str(d["_id"]),"title":d.get("title","Series"),"year":d.get("year"),"count":int(d.get("count",0) or 0)}
    for raw in parts:
        text=" ".join(raw.split())
        if text.lower().startswith("sid:"):
            d=await ctx.series_catalog.get_show(text[4:].strip())
            if d: selections.append(entry(d))
            else: errors.append(f"{_e(text)} — no catalogue entry has that SID")
            continue
        # 1) the typed text exactly as written (any year)
        pool=[(d,"as-written") for d in await ctx.series_catalog.shows_with_exact_title(text)]
        # 2) the same text read as "Title (year)" / "Title (no year)"
        no_year=False; want_year=None; base=None
        m=_MERGE_NOYEAR.match(text)
        if m: base=m.group(1).strip(); no_year=True
        else:
            m=_MERGE_YEAR.match(text)
            if m: base=m.group(1).strip(); want_year=int(m.group(2))
        if base:
            for d in await ctx.series_catalog.shows_with_exact_title(base):
                y=d.get("year")
                if (no_year and y is None) or (want_year is not None and y==want_year):
                    pool.append((d,"title+year"))
        seen=set(); pick=[]
        for d,_how in pool:
            if str(d["_id"]) not in seen: seen.add(str(d["_id"])); pick.append(d)
        if len(pick)>1 and base is None:
            blank=[d for d in pick if d.get("year") is None]
            if len(blank)==1: pick=blank          # bare title = the entry that has no year
        if len(pick)==1:
            selections.append(entry(pick[0]))
        elif not pick:
            near=[]
            try:
                from normalize import normalize
                near=[d for d in await ctx.series_catalog.search_shows(base or text,10,0) if normalize(d.get("title",""))==normalize(base or text)][:5]
            except Exception: near=[]
            hint=("\n    Not used (not an exact title), for reference: "+", ".join(f"<code>{_e(_typed_form(d))}</code>" for d in near)) if near else ""
            errors.append(f"{_e(text)} — no entry has exactly this title{hint}")
        else:
            opts="\n".join(f"    – <code>{_e(_typed_form(d))}</code> · {int(d.get('count',0) or 0):,} files · SID <code>{_e(d['_id'])}</code>" for d in pick[:8])
            errors.append(f"{_e(text)} — matches {len(pick)} entries with exactly that title. Type exactly one of these:\n{opts}\n    (or use <code>sid:SID</code>)")
    return selections, errors


async def merge_shows_catalog(ctx, progress_message, dry_run: bool = True):
    """Build a conservative review queue. This function NEVER merges records."""
    from normalize import normalize, core_key
    from series_parse import GENERIC_MEDIA_NAMES

    def generic_title(title):
        norm=normalize(title)
        if norm in GENERIC_MEDIA_NAMES:
            return True
        # Device-generated names, including IMG_2021 and Video 2026-10-28 21-55.
        return bool(re.fullmatch(
            r"(?:"+"|".join(re.escape(x) for x in sorted(GENERIC_MEDIA_NAMES))+r")\s+\d{1,4}(?:\s+\d{1,4}){0,4}", norm
        ))

    async def report(text):
        try:
            await progress_message.edit_text(text)
        except Exception:
            log.exception("Could not update merge-review report")
            try: await progress_message.reply_text(text)
            except Exception: log.exception("Could not send merge-review report")

    # Read the catalogue's show documents, not filenames. Filename parsing and
    # fuzzy similarity must never be allowed to collapse distinct programmes.
    rows=[d async for d in ctx.series_catalog.shows.find({}, {"title":1,"year":1,"count":1})]
    buckets={}
    excluded=0
    for row in rows:
        title=str(row.get("title") or "").strip()
        sid=str(row.get("_id") or "")
        if not sid or not title or generic_title(title):
            excluded+=1
            continue
        key=core_key(normalize(title))
        if not key:
            excluded+=1
            continue
        buckets.setdefault(key, []).append({
            "sid":sid, "title":title, "year":row.get("year"),
            "count":int(row.get("count",0) or 0),
        })

    candidates=[]
    for key, entries in buckets.items():
        if len(entries)<2:
            continue
        # A title with different recorded years is deliberately marked as
        # higher-risk. It remains separate unless the owner explicitly approves.
        years={e.get("year") for e in entries}
        entries.sort(key=lambda e:(str(e.get("year") or ""), e["title"].casefold(), e["sid"]))
        candidates.append({
            "id":len(candidates)+1,
            "type":"year_conflict" if len(years)>1 else "punctuation_or_spelling",
            "key":key,
            "shows":entries,
        })

    candidates.sort(key=lambda c:(c["type"]!="punctuation_or_spelling", c["key"]))
    for i,c in enumerate(candidates,1): c["id"]=i
    await ctx.series_catalog.save_merge_review(candidates, scanned=len(rows))

    lines=[
        "Show-merge review prepared. NO SHOWS HAVE BEEN MERGED.",
        f"Catalogue entries checked: {len(rows):,}",
        f"Generic/empty titles excluded: {excluded:,}",
        f"Candidates awaiting your decision: {len(candidates):,}",
        "",
        "This report is read-only. Nothing is merged automatically.",
        "To merge: tap-copy the command under a candidate and send it. The FIRST entry survives and keeps its title and year; every entry after | is absorbed, after you press Confirm.",
        "Manual form: /mergeshow Survivor | Absorbed | Absorbed (up to 8). Titles must match exactly as stored (no approximate matching). Add (2005) or (no year) to pick one entry; sid:SID also works.",
        "Different years remain separate candidates for your manual decision. No abbreviation or fuzzy-title matches are proposed.",
        "",
    ]
    for c in candidates:
        entries=c["shows"]
        label="YEAR CONFLICT — MANUAL ONLY" if c["type"]=="year_conflict" else "NORMALISED TITLE MATCH"
        detail=" / ".join(
            f'{html.escape(str(e["title"]))}'+(f' ({html.escape(str(e["year"]))})' if e.get("year") else " (no year)")+
            f' — {e["count"]:,} files — SID {html.escape(str(e["sid"]))}' for e in entries
        )
        lines.append(f'#{c["id"]} [{label}] {detail}')
        if c["type"]!="year_conflict":
            ranked=sorted(entries,key=lambda e:-e["count"])
            lines.append("<code>/mergeshow "+" | ".join("sid:"+html.escape(str(e["sid"])) for e in ranked)+"</code>  (keeps the biggest)")
        else:
            lines.append("Different years may be different shows. To merge deliberately, type: "+" | ".join("<code>"+html.escape(str(e["title"]))+(f' ({html.escape(str(e["year"]))})' if e.get("year") else " (no year)")+"</code>" for e in entries)+" in the order you want (first survives).")
    if not candidates:
        lines.append("No safe title-equivalence candidates found.")

    # Keep each Telegram message below its hard limit.
    chunks=[]; current=""
    for line in lines:
        piece=line+"\n"
        if current and len(current)+len(piece)>3400:
            chunks.append(current.rstrip()); current=""
        current+=piece
    if current: chunks.append(current.rstrip())
    for i,chunk in enumerate(chunks):
        if i==0: await report(chunk)
        else:
            try: await progress_message.reply_text(chunk)
            except Exception: log.exception("Could not send merge-review page %s",i+1); break
    return {"groups":len(candidates),"scanned":len(rows),"excluded":excluded,"dry":True}


async def approve_merge_candidate(ctx, progress_message, candidate_id, survivor_sid=None):
    """Apply exactly one owner-approved candidate, preserving the chosen survivor title."""
    review=await ctx.series_catalog.get_merge_review() or {}
    candidate=next((c for c in review.get("candidates",[]) if str(c.get("id"))==str(candidate_id)),None)
    if not candidate:
        await progress_message.edit_text("That review ID does not exist. Run /mergeshows dry to prepare a fresh review.")
        return {"merged":0,"error":"not_found"}
    entries=candidate.get("shows",[])
    ids={str(e.get("sid")) for e in entries}
    if len(ids)<2:
        await ctx.series_catalog.remove_merge_review_candidate(candidate_id)
        await progress_message.edit_text("This candidate no longer contains multiple shows. Nothing was merged.")
        return {"merged":0,"error":"stale"}
    if not survivor_sid:
        await progress_message.edit_text("You must choose which catalogue entry survives. Usage: /mergeshows approve <ID> <SURVIVOR_SID>")
        return {"merged":0,"error":"survivor_required"}
    survivor=next((e for e in entries if str(e.get("sid"))==str(survivor_sid)),None)
    if not survivor:
        await progress_message.edit_text("The survivor SID is not part of this candidate. Nothing was merged.")
        return {"merged":0,"error":"invalid_survivor"}
    # Revalidate that all catalogue entries still exist before changing files.
    live={str(d["_id"]):d async for d in ctx.series_catalog.shows.find({"_id":{"$in":list(ids)}},{"title":1,"year":1,"count":1})}
    if set(live)!=ids:
        await progress_message.edit_text("The catalogue changed since this review was prepared. Run /mergeshows dry again; nothing was merged.")
        return {"merged":0,"error":"stale_catalogue"}
    survivor_doc=live[str(survivor["sid"])]
    removed=sorted(ids-{str(survivor["sid"])})
    await progress_message.edit_text(f'Approving candidate #{candidate_id}: keeping {html.escape(str(survivor_doc.get("title") or ""))} and moving records from {len(removed)} other catalogue entry/entries…')
    moved=await ctx.series_catalog.merge_show_ids(
        str(survivor["sid"]),removed,str(survivor_doc.get("title") or survivor["title"]),survivor_doc.get("year")
    )
    dedupe_res=await ctx.series_catalog.dedupe_show(str(survivor["sid"]))
    await ctx.series_catalog.remove_merge_review_candidate(candidate_id)
    await progress_message.edit_text(
        f'Approved candidate #{candidate_id}.\nSurviving show: {html.escape(str(survivor_doc.get("title") or ""))}'+
        (f' ({html.escape(str(survivor_doc.get("year")))})' if survivor_doc.get("year") else '')+
        f'\nCatalogue entries merged: {len(removed)}\nFiles moved: {moved:,}\nSurviving show duplicates removed: {dedupe_res["removed"]:,} (recorded in Look-alikes skipped).\nSource Telegram posts were preserved. Other review candidates remain pending.'
    )
    return {"merged":len(removed),"moved":moved,"removed_dupes":dedupe_res["removed"],"survivor":str(survivor["sid"])}


async def reject_merge_candidate(ctx, progress_message, candidate_id):
    review=await ctx.series_catalog.get_merge_review() or {}
    if not any(str(c.get("id"))==str(candidate_id) for c in review.get("candidates",[])):
        await progress_message.edit_text("That review ID does not exist. Run /mergeshows dry to prepare a fresh review.")
        return {"rejected":False}
    remaining=await ctx.series_catalog.remove_merge_review_candidate(candidate_id)
    await progress_message.edit_text(f"Rejected candidate #{candidate_id}. No records were changed. {remaining} candidates remain pending.")
    return {"rejected":True,"remaining":remaining}


async def auto_index(ctx,m):
    catalog=getattr(ctx,"series_catalog",None)
    if not catalog or m.chat.id not in ctx.cfg.file_channels: return 0,0,0,"ignored"
    settings=await ctx.settings()
    r=from_message(m,promos=settings.get("series_promos",[]),aliases=await catalog.alias_map())
    if not r:
        await catalog.add_skip(m,promos=settings.get("series_promos",[]))
        return 0,0,0,"unrecognized"
    return (*await catalog.add([r]),"indexed")


async def showinfo_report(ctx, text):
    """Diagnostic: how the bot stores and now reads the files of a show (for odd titles)."""
    from parse import parse_series, strip_title_groups
    from series_catalog import _strip_custom_promos
    promos=(await ctx.settings()).get("series_promos",[])
    files=await ctx.series_catalog.sample_files_by_show(text,5)
    if not files: return f"No stored file has a show title containing '{text}'."
    lines=[f"Sample of {len(files)} stored files for '{text}':"]
    for f in files:
        p=parse_series(_strip_custom_promos((f.name or "").strip(),promos))
        now=strip_title_groups(p.show,promos) if p.show else "(unreadable)"
        lines.append(f"\nFile name: {f.name}\nStored show: {f.show} · S{f.season} E{list(f.episodes)}\nRead now as: {now} · S{p.season} E{list(p.episodes)} · group {p.group}")
    lines.append("\nIf 'Read now' is right and only the stored show NAME carries a promo prefix (e.g. 'Brewfiles Band Of Brothers'), preview the fix with /fixpromoshows dry (reparse never renames shows). If both are wrong, send me these lines.")
    return "\n".join(lines)[:3900]


# ---------------------------------------------------------------------------------------------
# Skipped-media browser and /adopt
# ---------------------------------------------------------------------------------------------
def _post_link(channel, message_id):
    digits = str(abs(int(channel)))
    digits = digits[3:] if digits.startswith("100") else digits
    return f"https://t.me/c/{digits}/{int(message_id)}"


# Short callback codes for skip categories (callback_data must stay under 64 bytes).
SKIP_CODES = {"f": "film", "r": "unrecognised_series", "a": "ambiguous", "u": "unreadable", "l": "lookalike"}
SKIP_CODE_OF = {v: k for k, v in SKIP_CODES.items()}
SKIP_PER_PAGE = 8


def _skip_label(cat):
    from series_classify import CATEGORY_LABELS
    return CATEGORY_LABELS.get(cat, "All skipped posts")


async def skipped_screen(ctx, kind="menu", page=0, uid=None):
    """Admin browser over every post the bot did not index.

    Every list is read with skip/limit on the database cursor, so a very large collection is never
    loaded into memory. Nothing here deletes anything: removal is a separate, confirmed action.
    """
    from series_classify import CATEGORY_BLURBS, CATEGORY_LABELS, REASONS
    cat = ctx.series_catalog
    c = await cat.skip_counts()
    if kind == "menu":
        text = ("<b>Skipped media</b>\nRecords stay here until you process or remove them. Nothing is deleted automatically.\n\n"
                f"<b>Likely films ({c['film']:,})</b> - review list only, no film catalogue\n"
                f"<b>Unrecognised series ({c['unrecognised_series']:,})</b> - episode-like names in unsupported formats\n"
                f"<b>Other / ambiguous ({c['ambiguous']:,})</b> - documentaries, specials, sports, uncertain\n"
                f"<b>Unreadable ({c['unreadable']:,})</b> - not enough information\n"
                f"<b>Look-alikes ({c['lookalike']:,})</b> - suspected duplicate copies\n")
        if c.get("unlabelled"):
            text += f"\n{c['unlabelled']:,} older records are not labelled yet: run /reclassify."
        text += "\n\nSearch: <code>/skipsearch words</code> · Details: <code>/skipinfo CHANNEL:MESSAGE</code> · Re-sort: <code>/reclassify dry</code> · Download all: <code>/skippedexport</code>"
        rows = [[Btn(f"Films ({c['film']:,})", "ss:skip:f:0"), Btn(f"Unrecognised ({c['unrecognised_series']:,})", "ss:skip:r:0")],
                [Btn(f"Ambiguous ({c['ambiguous']:,})", "ss:skip:a:0"), Btn(f"Unreadable ({c['unreadable']:,})", "ss:skip:u:0")],
                [Btn(f"Look-alikes ({c['lookalike']:,})", "ss:skip:l:0"), Btn(f"All ({c['total']:,})", "ss:skip:all:0")],
                [Btn("Export everything (.txt)", "ss:skip:export")],
                [Btn("Close", "ss:close")]]
        return text, rows
    query = None
    if kind == "q":
        view = await cat.get_skip_view(uid or 0)
        query = view.get("query") or ""
        category = view.get("category") or None
        if not query:
            return "No active search. Send <code>/skipsearch words</code>.", [[Btn("‹ Menu", "ss:skip:menu")]]
        title = f"Search: {_e(query)}" + (f" in {_skip_label(category)}" if category else "")
    elif kind == "all":
        category = None; title = "All skipped posts"
    else:
        category = SKIP_CODES.get(kind)
        if category is None:
            return await skipped_screen(ctx, "menu", 0, uid)
        title = CATEGORY_LABELS[category]
    per = SKIP_PER_PAGE
    total = await cat.skip_count(category, query)
    pages = max(1, (total + per - 1) // per)
    if str(page) == "r":
        import random
        page = random.randrange(pages)                     # the Random button
    page = min(max(0, int(page)), pages - 1)
    rows_db = await cat.skip_page(category, per, page * per, query)
    lines = [f"<b>{title} ({total:,})</b> · page {page + 1}/{pages}"]
    if category and not query: lines.append(f"<i>{_e(CATEGORY_BLURBS[category])}</i>")
    lines.append("")
    for i, r in enumerate(rows_db, page * per + 1):
        code = r.get("code") or "legacy_unclassified"
        lines.append(f"{i}. {_e(str(r.get('name') or '(no name)')[:80])}\n"
                     f"   <code>{_e(code)}</code> · <a href=\"{_post_link(r.get('channel'), r.get('message_id'))}\">open post</a> · <code>{r.get('channel')}:{r.get('message_id')}</code>")
        if r.get("category") == "lookalike" and r.get("kept_uid"):
            kept = await cat.file_brief(r["kept_uid"])
            if kept:
                lines.append(f"   kept: {_e(str(kept.get('name') or '')[:60])} · <a href=\"{_post_link(kept.get('channel'), kept.get('message_id'))}\">open</a>")
    if not rows_db:
        lines.append("Nothing here.")
    base = "q" if kind == "q" else kind
    rows = []
    detail = [Btn(str(i), f"ss:skip:d:{r.get('channel')}:{r.get('message_id')}:{base}:{page}") for i, r in enumerate(rows_db, page * per + 1)]
    for k in range(0, len(detail), 4): rows.append(detail[k:k + 4])
    nav = []
    if page > 0: nav.append(Btn("‹ Previous", f"ss:skip:{base}:{page - 1}"))
    if page + 1 < pages: nav.append(Btn("Next ›", f"ss:skip:{base}:{page + 1}"))
    if nav: rows.append(nav)
    if pages > 2:      # a long list must be reachable at both ends and in the middle
        far = [Btn("⏮ First", f"ss:skip:{base}:0"), Btn("🎲 Random", f"ss:skip:{base}:r"), Btn("Last ⏭", f"ss:skip:{base}:{pages - 1}")]
        rows.append(far)
    if pages > 12:
        rows.append([Btn("−10", f"ss:skip:{base}:{max(0, page - 10)}"), Btn(f"{page + 1}/{pages}", "ss:noop"), Btn("+10", f"ss:skip:{base}:{min(pages - 1, page + 10)}")])
    if category and kind != "q" and total:
        rows.append([Btn(f"Remove all {total:,} in this list…", f"ss:skip:bx:{kind}")])
    rows.append([Btn("‹ Menu", "ss:skip:menu")])
    return "\n".join(lines), rows


async def skip_detail_screen(ctx, skip_id, back=None, page=0):
    from series_classify import CATEGORY_LABELS, REASONS
    d = await ctx.series_catalog.get_skip(skip_id)
    if not d:
        return "That skip record no longer exists.", [[Btn("‹ Menu", "ss:skip:menu")]]
    cat = d.get("category") or "unreadable"
    code = d.get("code") or "legacy_unclassified"
    lines = [f"<b>{_e(str(d.get('name') or '(no name)')[:200])}</b>",
             f"Category: <b>{_e(CATEGORY_LABELS.get(cat, cat))}</b>",
             f"Reason code: <code>{_e(code)}</code> - {_e(REASONS.get(code, code))}"]
    if d.get("title_guess"): lines.append(f"Title guess: {_e(d['title_guess'])}")
    lines.append(f"Post: <a href=\"{_post_link(d.get('channel'), d.get('message_id'))}\">open</a> · <code>{_e(d.get('_id'))}</code>")
    if cat != "lookalike":
        lines.append(f"\nTo place it: <code>/adopt dry {_e(d.get('_id'))} | Show title</code>")
    lines.append("\nThis record is only a label. Removing it does not touch the Telegram post or the catalogue.")
    back = back or SKIP_CODE_OF.get(cat, "all")           # return to the SAME list and page the record was opened from
    return "\n".join(lines), [[Btn("Remove this record", f"ss:skip:x:{skip_id}:{back}:{page}")],
                              [Btn("‹ Back to list", f"ss:skip:{back}:{page}"), Btn("‹ Menu", "ss:skip:menu")]]


async def skip_action(ctx, cb, bits, uid):
    """Admin-only dispatch for every skip-browser callback (permission is checked by the caller)."""
    cat = ctx.series_catalog
    act = bits[0] if bits else "menu"
    if act in ("menu",) or not bits:
        return await skipped_screen(ctx, "menu", 0, uid)
    if act in SKIP_CODES or act in ("all", "q"):
        page = bits[1] if len(bits) > 1 and (bits[1].isdigit() or bits[1] == "r") else 0
        return await skipped_screen(ctx, act, page, uid)
    if act in ("d", "x", "xy") and len(bits) >= 3:
        sid = f"{bits[1]}:{bits[2]}"
        back = bits[3] if len(bits) > 3 else None
        pg = int(bits[4]) if len(bits) > 4 and bits[4].isdigit() else 0
        tail = f":{back}:{pg}" if back else ""
        if act == "d": return await skip_detail_screen(ctx, sid, back, pg)
        if act == "x":
            return (f"Remove skip record <code>{_e(sid)}</code>? The post and catalogue are not affected.",
                    [[Btn("Yes, remove it", f"ss:skip:xy:{sid}{tail}"), Btn("Cancel", f"ss:skip:d:{sid}{tail}")]])
        removed = await cat.remove_skip(sid)
        if back:                                           # straight back to the page you were on
            t, rows = await skipped_screen(ctx, back, pg, uid)
        else:
            t, rows = await skipped_screen(ctx, "menu", 0, uid)
        return ("Record removed.\n\n" if removed else "Record was already gone.\n\n") + t, rows
    if act in ("bx", "bxy") and len(bits) >= 2 and bits[1] in SKIP_CODES:
        category = SKIP_CODES[bits[1]]
        n = await cat.skip_count(category)
        if act == "bx":
            return (f"<b>Remove ALL {n:,} records in {_e(_skip_label(category))}?</b>\nThis cannot be undone. The records are the only list of these posts; "
                    "the Telegram posts and the catalogue are not affected.",
                    [[Btn(f"Yes, remove {n:,}", f"ss:skip:bxy:{bits[1]}:{n}")], [Btn("Cancel", f"ss:skip:{bits[1]}:0")]])
        expected = int(bits[2]) if len(bits) > 2 and bits[2].isdigit() else -1
        deleted = await cat.remove_skips_in_category(category, expected)
        t, rows = await skipped_screen(ctx, "menu", 0, uid)
        if deleted is None:
            return "The list changed since you opened the confirmation (or the count was missing). Nothing was removed.\n\n" + t, rows
        return f"Removed {deleted:,} records.\n\n" + t, rows
    return await skipped_screen(ctx, "menu", 0, uid)


# ---------------------------------------------------------------------------------------------
# Full skipped-results export (admin only, read-only, plain text)
# ---------------------------------------------------------------------------------------------
SKIP_EXPORT_BATCH = 200
_EXPORTS_RUNNING: dict = {}          # admin id -> monotonic start time (a stale entry expires)


def _one_line(value, limit=500) -> str:
    """Names can contain line breaks; the export keeps one record per block of fixed lines."""
    return " ".join(str(value or "").split())[:limit]


def _export_link(channel, message_id) -> str:
    try:
        return _post_link(channel, message_id)
    except (TypeError, ValueError):
        return f"chat {channel} message {message_id}"


async def build_skipped_export(ctx):
    """Return (text, total, counts) covering EVERY skip record, whatever its category or label.

    One keyset pass over the whole collection in bounded batches, so nothing is skipped (records
    with no label or an unexpected category included) and memory stays bounded. Read-only.
    The kept-copy line is written only for look-alike records.
    """
    from series_classify import CATEGORY_LABELS, LOOKALIKE
    cat = ctx.series_catalog
    body: list[str] = []
    counts: dict[str, int] = {}
    total = 0
    async for docs in cat.iter_skips(batch=SKIP_EXPORT_BATCH):
        kept = {}
        wanted = [d.get("kept_uid") for d in docs if d.get("category") == LOOKALIKE and d.get("kept_uid")]
        if wanted:
            kept = await cat.files_brief_many(wanted)
        for d in docs:
            total += 1
            category = d.get("category")
            label = CATEGORY_LABELS.get(category, "Unlabelled") if category else "Unlabelled"
            counts[label] = counts.get(label, 0) + 1
            body.append(f"{total}. {_one_line(d.get('name')) or '(no name)'}")
            body.append(f"   category: {label} | code: {d.get('code') or 'legacy_unclassified'}")
            body.append(f"   post: {_export_link(d.get('channel'), d.get('message_id'))} | id: {_one_line(d.get('_id'), 80)}")
            if category == LOOKALIKE:
                if not d.get("kept_uid"):
                    body.append("   kept: (not recorded)")
                else:
                    k = kept.get(str(d["kept_uid"]))
                    if k:
                        body.append(f"   kept: {_one_line(k.get('name')) or '(no name)'} | {_export_link(k.get('channel'), k.get('message_id'))}")
                    else:
                        body.append("   kept: (removed since)")
            body.append("")
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    head = ["Exivum Series Bot - skipped results export", f"Generated: {stamp}", f"Total records: {total:,}"]
    head += [f"  {name}: {n:,}" for name, n in sorted(counts.items())]
    head += ["", "Read-only snapshot. Nothing was removed or changed.", "", ""]
    return "\n".join(head + body), total, counts


def _export_record_lines(d, label, number, kept):
    """The fixed block of lines for one skip record (shared by every export path)."""
    from series_classify import LOOKALIKE
    out = [f"{number}. {_one_line(d.get('name')) or '(no name)'}",
           f"   category: {label} | code: {d.get('code') or 'legacy_unclassified'}",
           f"   post: {_export_link(d.get('channel'), d.get('message_id'))} | id: {_one_line(d.get('_id'), 80)}"]
    if d.get("category") == LOOKALIKE:
        if not d.get("kept_uid"):
            out.append("   kept: (not recorded)")
        else:
            k = kept.get(str(d["kept_uid"]))
            out.append(f"   kept: {_one_line(k.get('name')) or '(no name)'} | {_export_link(k.get('channel'), k.get('message_id'))}"
                       if k else "   kept: (removed since)")
    out.append("")
    return out


EXPORT_PART_RECORDS = 15000        # hard cap on records per part
EXPORT_PART_BYTES = 380_000        # each part stays under one 512 KB Telegram upload chunk: Render's link to Telegram's upload
                                   # servers drops big uploads (SaveFilePart retries / broken pipe), single-chunk files get through
EXPORT_SEND_ATTEMPTS = 3
EXPORT_STALE_SECONDS = 20 * 60     # a lock older than this is treated as dead and replaced
EXPORT_BUILD_TIMEOUT = 15 * 60     # the database pass may not run longer than this
EXPORT_SEND_TIMEOUT = 90           # one upload attempt may not hang longer than this
_EXPORT_CATEGORIES = {"all": None, "a": "ambiguous", "u": "unreadable", "f": "film", "r": "unrecognised_series", "l": "lookalike"}


async def write_skipped_export(ctx, dirpath, category=None, on_progress=None):
    """Stream the export to small .txt part files on disk (one batch in memory, whatever the size).

    A part closes when it reaches EXPORT_PART_BYTES (or EXPORT_PART_RECORDS records). Returns
    [(path, record_count), ...]. Read-only; every record of the chosen category is written once.
    """
    import os
    from series_classify import CATEGORY_LABELS
    cat = ctx.series_catalog
    total_expected = await cat.skip_count(category)
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    counts = await cat.skip_counts()
    header_counts = [(CATEGORY_LABELS[k], counts.get(k, 0)) for k in CATEGORY_LABELS if counts.get(k, 0) and (category in (None, k))]
    if category is None and counts.get("unlabelled"):
        header_counts.append(("Unlabelled", counts["unlabelled"]))
    parts, handle, size = [], None, 0

    def open_part():
        nonlocal handle, size
        idx = len(parts) + 1
        path = os.path.join(dirpath, f"part{idx:04d}.txt")
        handle = open(path, "w", encoding="utf-8", newline="\n")
        head = (f"Exivum Series Bot - skipped results export (part {idx})\nGenerated: {stamp}\nTotal records: {total_expected:,}\n"
                + "".join(f"  {name}: {n:,}\n" for name, n in header_counts)
                + "\nRead-only snapshot. Nothing was removed or changed.\n\n")
        handle.write(head); size = len(head.encode("utf-8"))
        parts.append([path, 0])

    number = 0
    try:
        async for docs in cat.iter_skips(category=category, batch=SKIP_EXPORT_BATCH):
            wanted = [d.get("kept_uid") for d in docs if d.get("category") == "lookalike" and d.get("kept_uid")]
            kept = await cat.files_brief_many(wanted) if wanted else {}
            for d in docs:
                label = CATEGORY_LABELS.get(d.get("category"), "Unlabelled") if d.get("category") else "Unlabelled"
                number += 1
                block = "\n".join(_export_record_lines(d, label, number, kept)) + "\n"
                nbytes = len(block.encode("utf-8"))
                if handle is None or (parts[-1][1] and (size + nbytes > EXPORT_PART_BYTES or parts[-1][1] >= EXPORT_PART_RECORDS)):
                    if handle: handle.close()
                    open_part()
                handle.write(block); size += nbytes; parts[-1][1] += 1
            if on_progress:
                await on_progress(number, total_expected)
    finally:
        if handle: handle.close()
    return [(p, n) for p, n in parts]


async def send_skipped_export(ctx, client, chat_id, uid, category=None) -> bool:
    """Build the export on disk and send it as one or more .txt documents. Admin-only, read-only.

    One export per admin at a time, but a lock can never stay stuck: it expires after 20 minutes, the
    build and every upload have hard timeouts, and the lock is always released. Progress is reported.
    """
    import io, os, tempfile
    if not ctx.is_admin(uid):
        return False
    now = time.monotonic()
    started = _EXPORTS_RUNNING.get(uid)
    if started is not None and now - started < EXPORT_STALE_SECONDS:
        mins = int((now - started) // 60)
        await client.send_message(chat_id, f"An export is already being prepared (running for {mins} min). "
                                           "If it never arrives it will be replaced automatically after 20 minutes, "
                                           "or send /skippedexport reset to cancel it now.")
        return False
    if started is not None:
        log.warning("replacing a stale skipped-export lock for %s", uid)
    _EXPORTS_RUNNING[uid] = now
    status = None
    last = [0.0]
    try:
        async def progress(done, total):
            nonlocal status
            if time.monotonic() - last[0] < 4: return
            last[0] = time.monotonic()
            text = f"Preparing export… {done:,} of about {total:,} records read."
            try:
                if status is None: status = await client.send_message(chat_id, text)
                else: await status.edit_text(text)
            except Exception:
                pass
        with tempfile.TemporaryDirectory(prefix="skipexport-") as tmp:
            try:
                parts = await asyncio.wait_for(write_skipped_export(ctx, tmp, category, progress), EXPORT_BUILD_TIMEOUT)
            except asyncio.TimeoutError:
                await client.send_message(chat_id, "Reading the skipped records from the database took too long and was stopped. Nothing was changed. Try a single list, e.g. /skippedexport u")
                return False
            if not parts:
                await client.send_message(chat_id, "There are no skipped results to export.")
                return True
            total = sum(n for _, n in parts)
            stamp = time.strftime("%Y-%m-%d-%H%M", time.gmtime())
            sent, failed = 0, []
            for idx, (path, n) in enumerate(parts, 1):
                with open(path, "rb") as fh:
                    raw = fh.read()
                cap = f"Skipped results: part {idx} of {len(parts)} · {n:,} of {total:,} records (read-only export)."
                ok_part = False
                for attempt in range(1, EXPORT_SEND_ATTEMPTS + 1):
                    data = io.BytesIO(raw); data.name = f"skipped-results-{stamp}-part{idx}of{len(parts)}.txt"
                    try:
                        await asyncio.wait_for(client.send_document(chat_id, data, caption=cap), EXPORT_SEND_TIMEOUT)
                        ok_part = True; break
                    except Exception as exc:                          # timeouts and Telegram/network errors alike
                        log.warning("export part %s/%s attempt %s failed: %r", idx, len(parts), attempt, exc)
                        await asyncio.sleep(0 if EXPORT_SEND_TIMEOUT < 1 else 3)
                if ok_part: sent += 1
                else: failed.append(idx)
            if failed:
                await client.send_message(chat_id, f"Sent {sent} of {len(parts)} parts. Telegram uploads from the server kept failing for part(s) {', '.join(map(str, failed))}. "
                                                   "Nothing was changed; try again in a few minutes or export one list at a time (/skippedexport u).")
                return False
            await client.send_message(chat_id, f"Export complete: {len(parts)} part(s), {total:,} records.")
        return True
    except asyncio.TimeoutError:
        log.error("skipped export timed out")
        try: await client.send_message(chat_id, "The export took too long and was stopped. Nothing was changed. Try a single list, e.g. /skippedexport u")
        except Exception: pass
        return False
    except Exception:
        log.exception("skipped export failed")
        try: await client.send_message(chat_id, "The export failed. Nothing was changed; please try again.")
        except Exception: pass
        return False
    finally:
        _EXPORTS_RUNNING.pop(uid, None)


async def reclassify_report(ctx, dry=True):
    promos = (await ctx.settings()).get("series_promos", [])
    r = await ctx.series_catalog.reclassify_skips(dry=dry, promos=promos)
    lines = [("Dry run: " if dry else "") + f"checked {r['scanned']:,} skip records; {'would change' if dry else 'changed'} {r['changed']:,}."]
    for (a, b), n in sorted(r["moves"].items(), key=lambda kv: -kv[1])[:8]:
        lines.append(f"{a} → {b}: {n:,}")
    for name, cat_, code in r["examples"][:6]:
        lines.append(f"• {name} → {cat_} ({code})")
    lines.append("Only the labels on skip records are changed. No show, file or alias is touched." + (" Repeat without 'dry' to apply." if dry else ""))
    return "\n".join(lines)[:3900]


# ---------------------------------------------------------------------------------------------
# /adopt: preview first, write only after an explicit confirmation button
# ---------------------------------------------------------------------------------------------
async def build_adopt(ctx, client, spec, title_text, season=None, legacy=False):
    """Compute (never write) the effect of adopting a run of skipped posts into one show."""
    from handlers import _fetch
    from series_catalog import adopt_episode_number, adopted_record
    m = re.fullmatch(r"(-?\d+):(\d+)(?:-(\d+))?", (spec or "").strip())
    if not m:
        return {"error": "Range format: CHANNEL:FROM-TO, for example -1002019316808:943356-943379 (the numbers shown next to each post in /skipped)."}
    ch, lo = int(m.group(1)), int(m.group(2))
    hi = int(m.group(3) or lo)
    if hi < lo or hi - lo > 3000:
        return {"error": "The range must run from a lower to a higher message id, at most 3000 ids wide."}
    if legacy and not season:
        return {"error": "'legacy' needs a season: ... | Season 4 legacy (then 412 is read as season 4 episode 12)."}
    selections, errors = await resolve_merge_titles(ctx, [title_text])
    year = None; new_show = False; similar = []
    cat = ctx.series_catalog
    if selections:
        title, year = selections[0]["title"], selections[0]["year"]
    elif errors and ("no entry has exactly" in errors[0] or "no matching" in errors[0]):
        y = _MERGE_YEAR.match(title_text.strip())
        title, year = (y.group(1).strip(), int(y.group(2))) if y else (title_text.strip(), None)
        new_show = True
        key = core_key(normalize(title))
        similar = [f"{d.get('title')}" + (f" ({d.get('year')})" if d.get('year') else "") async for d in cat.shows.find({"key": key}).limit(5)]
    else:
        return {"error": "Show not chosen:\n" + "\n".join(errors)}
    posts = await cat.skips_in_range(ch, lo, hi)
    if not posts:
        return {"error": "No skipped posts in that range. Check the numbers with /skipped."}
    promos = (await ctx.settings()).get("series_promos", [])
    aliases = await cat.alias_map()
    recs, no_number, samples = [], [], []
    ids = [int(p["message_id"]) for p in posts]
    for i in range(0, len(ids), 200):
        msgs = await _fetch(client, ch, ids[i:i + 200])
        for mm in (msgs if isinstance(msgs, list) else [msgs]):
            if not mm or getattr(mm, "empty", False):
                continue
            media = getattr(mm, "document", None) or getattr(mm, "video", None) or getattr(mm, "audio", None)
            name = (getattr(media, "file_name", None) or getattr(mm, "caption", None) or "").strip()
            got = adopt_episode_number(name, season if legacy else None)
            if not got:
                no_number.append(name or f"{ch}:{mm.id}")
                continue
            use_season = season if season is not None else (got[0] or 0)
            rec = adopted_record(mm, title, year, use_season, got[1], aliases, promos)
            if rec:
                recs.append(rec)
                if len(samples) < 6: samples.append(f"{name[:42]} → {('S%d ' % use_season) if use_season else ''}E{got[1]}")
    already = 0
    if recs:
        already = await cat.files.count_documents({"_id": {"$in": [r.uid for r in recs]}})
    digest = hashlib.sha1("|".join(sorted(f"{r.uid}:{r.sid}:{r.season}:{r.episodes}" for r in recs)).encode()).hexdigest()[:16]
    return {"title": title, "year": year, "season": season, "legacy": bool(legacy), "recs": recs, "posts": len(posts),
            "no_number": no_number, "samples": samples, "new_show": new_show, "similar": similar, "already": already,
            "hash": digest, "channel": ch, "lo": lo, "hi": hi}


def _adopt_preview_text(plan, dry):
    label = f"<b>{_e(plan['title'])}</b>" + (f" ({plan['year']})" if plan["year"] else "") + (f" · season {plan['season']}" if plan["season"] else "") + (" · legacy numbering" if plan["legacy"] else "")
    lines = [f"{'Dry run' if dry else 'Preview'}: {len(plan['recs'])} of {plan['posts']} skipped posts would be added to {label}."]
    lines.append("This is a NEW show (nothing existing is changed)." if plan["new_show"] else "Adds to the existing show with exactly that title.")
    if plan["similar"]:
        lines.append("Similar existing show(s): " + _e("; ".join(plan["similar"])) + ". Nothing is merged; use the exact title (or Title (YEAR)) to add to one of them.")
    if plan["already"]:
        lines.append(f"{plan['already']} of these files are already catalogued and will be left exactly as they are.")
    lines.extend(_e(x) for x in plan["samples"])
    if plan["no_number"]:
        lines.append("No episode number found (stay in the skipped list): " + _e("; ".join(n[:30] for n in plan["no_number"][:5])) + ("…" if len(plan["no_number"]) > 5 else ""))
    return "\n".join(lines)


async def adopt_preview(ctx, client, uid, spec, title_text, season=None, dry=False, legacy=False):
    """Returns (text, rows). Without 'dry' the plan is stored and a Confirm button is offered; no write happens here."""
    plan = await build_adopt(ctx, client, spec, title_text, season, legacy)
    if plan.get("error"):
        return plan["error"], None
    text = _adopt_preview_text(plan, dry)
    if dry or not plan["recs"]:
        return text + ("\n\nPreview only - nothing was written. Send the command without 'dry' to get a confirmation button." if dry else ""), None
    await ctx.series_catalog.save_adopt_plan(uid, {"spec": spec, "title_text": title_text, "season": season, "legacy": bool(legacy), "hash": plan["hash"], "count": len(plan["recs"])})
    return text + "\n\nNothing is written until you confirm.", [[Btn(f"Confirm: add {len(plan['recs'])} file(s)", "ss:adopt:go"), Btn("Cancel", "ss:adopt:no")]]


async def adopt_confirm(ctx, client, uid):
    """Apply a previously previewed plan - but only if recomputing it gives exactly the same result."""
    cat = ctx.series_catalog
    saved = await cat.get_adopt_plan(uid)
    if not saved:
        return "No pending adopt preview (it expires after 15 minutes). Run /adopt again."
    plan = await build_adopt(ctx, client, saved["spec"], saved["title_text"], saved["season"], saved["legacy"])
    await cat.clear_adopt_plan(uid)
    if plan.get("error"):
        return "Nothing written: " + plan["error"]
    if plan["hash"] != saved["hash"]:
        return "Nothing written: the posts or catalogue changed since the preview. Run /adopt dry again."
    added, known, dupes = (await cat.add(plan["recs"]))[:3] if plan["recs"] else (0, 0, 0)
    return (f"Added {added} file(s) to <b>{_e(plan['title'])}</b>. Already known (untouched): {known}. Same-size look-alikes skipped: {dupes}."
            + (f"\nStill skipped (no episode number): {len(plan['no_number'])}." if plan["no_number"] else ""))


# ---------------------------------------------------------------------------
# /identities: reviewed bulk repair for the identity conflicts /reparse holds
# ---------------------------------------------------------------------------
IDENT_FORCE_OVER = 500            # groups larger than this need the word "force"
IDENT_PAGE = 8
IDENT_CACHE_SECONDS = 1800        # a scan stays usable for 30 minutes
IDENT_CONFIRM_SECONDS = 600
_IDENT_PROJECTION = {"_id":1,"sid":1,"show":1,"year":1,"kind":1,"season":1,"episodes":1,
                     "absolute":1,"ep_title":1,"channel":1,"message_id":1,"size":1,"name":1,
                     "resolution":1,"source":1,"codec":1,"languages":1,"group":1,"tags":1,
                     "ext":1,"caption":1,"indexed_at":1,"special":1,"special_label":1,"part":1,"adopted":1}


def _identity_destination(parsed, candidate, aliases, promos):
    """(sid, title, year) a file with this reading would be indexed under today (same rule as from_message)."""
    from series_catalog import alias_title_is_safe
    sid=show_id(candidate,parsed.year); title=candidate; year=parsed.year
    alias=(aliases or {}).get(sid)
    if isinstance(alias,(tuple,list)):
        alias_title=str(alias[1] or candidate)
        if alias_title_is_safe(alias_title,promos):        # a stale alias never reintroduces an uploader token
            sid=str(alias[0]); title=alias_title; year=alias[2] if len(alias)>2 else parsed.year
    elif alias:
        sid=str(alias)
    return sid,title,year


def _identity_blocks(title, count, promos):
    """Reasons a group may not be applied without the word 'force' (empty list = no force needed)."""
    from series_catalog import alias_title_is_safe, suspicious_short_show_title
    out=[]
    if suspicious_short_show_title(title): out.append("suspicious short title")
    elif not alias_title_is_safe(title,promos): out.append("uploader/promo-looking title")
    if count>IDENT_FORCE_OVER: out.append(f"more than {IDENT_FORCE_OVER} records")
    return out


def _group_label(g): return g["title"]+(f" ({g['year']})" if g.get("year") else "")


def _group_hash(g): return hashlib.sha1("\n".join(sorted(g["uids"])).encode()).hexdigest()


async def scan_identity_conflicts(ctx, progress_message=None):
    """Read-only. Finds every record /reparse would hold as an identity conflict and groups them by the
    show they would be indexed under today. Returns None if stopped. Writes nothing."""
    from collections import Counter
    from series_catalog import effective_promos
    cat=ctx.series_catalog
    promos=effective_promos((await ctx.settings()).get("series_promos",[]))
    aliases=await cat.alias_map()
    groups={}; scanned=unread=in_place=adopted=0; last_edit=0.0
    async for batch,_last in cat.iter_file_batches(batch_size=400,projection=_IDENT_PROJECTION):
        for old in batch:
            scanned+=1
            if old.adopted: adopted+=1; continue
            parsed,cand=_reparse_reading(old,promos)
            if parsed is None: unread+=1; continue
            if core_key(normalize(cand))==core_key(normalize(old.show)): continue      # not a conflict
            dest_sid,dest_title,dest_year=_identity_destination(parsed,cand,aliases,promos)
            if dest_sid==old.sid: in_place+=1; continue        # an alias already maps it to where it sits
            g=groups.setdefault(dest_sid,{"sid":dest_sid,"title":dest_title,"year":dest_year,"uids":[],"examples":[],"from":Counter()})
            g["uids"].append(old.uid); g["from"][old.show]+=1
            nm=old.name or "(no filename)"
            if len(g["examples"])<2 and nm not in g["examples"]: g["examples"].append(nm)
        now=time.monotonic()
        if progress_message is not None and now-last_edit>4:
            last_edit=now
            try: await progress_message.edit_text(f"Scanning for identity conflicts (read-only)… checked {scanned:,} · groups so far {len(groups):,}")
            except Exception: pass
        if ctx.maintenance_stop.is_set(): return None
        await asyncio.sleep(0.1)
    # an existing destination show keeps its own display title/year
    sids=list(groups)
    for i in range(0,len(sids),200):
        async for d in cat.shows.find({"_id":{"$in":sids[i:i+200]}},{"title":1,"year":1}):
            g=groups.get(str(d["_id"]))
            if g and d.get("title"): g["title"]=d["title"]; g["year"]=d.get("year")
    out=sorted(groups.values(),key=lambda g:(-len(g["uids"]),g["title"].casefold()))
    for g in out: g["blocks"]=_identity_blocks(g["title"],len(g["uids"]),promos)
    return {"at":time.monotonic(),"scanned":scanned,"unread":unread,"in_place":in_place,"adopted":adopted,"groups":out}


def identities_page_text(scan, page=1):
    groups=scan["groups"]; total=len(groups)
    pages=max(1,-(-total//IDENT_PAGE)); page=min(max(1,int(page)),pages)
    records=sum(len(g["uids"]) for g in groups)
    lines=["<b>Identity conflicts held by /reparse</b> (read-only)",
           f"Checked {scan['scanned']:,} files · {records:,} record(s) in {total:,} group(s)"
           +(f" · {scan['in_place']:,} already resolved by an alias" if scan.get("in_place") else "")]
    if not groups:
        lines.append("\nNothing to repair."); return "\n".join(lines)
    for i,g in enumerate(groups[(page-1)*IDENT_PAGE:page*IDENT_PAGE],(page-1)*IDENT_PAGE+1):
        lines.append(f"\n<b>{i}. {_e(_group_label(g))}</b> — {len(g['uids']):,} record(s)"+(" ⚠ needs force" if g.get("blocks") else ""))
        for ex in g["examples"]: lines.append(f"   • <code>{_e(ex[:90])}</code>")
    lines.append(f"\nPage {page}/{pages}"+(f" · /identities page {page+1}" if page<pages else ""))
    lines.append("Repair one group: /identities apply <Proposed Title>  (you get a confirmation first).\n"
                 "A group shown with ⚠ is refused unless you add the word force at the end.")
    return "\n".join(lines)


def _find_identity_groups(groups, text):
    """Match a typed proposed title (optionally 'Title (2021)' or 'Title (no year)') against the scan."""
    m=re.search(r"\s*\((\d{4}|no year)\)\s*$",text,re.I)
    year=None; no_year=False
    if m:
        text=text[:m.start()]
        if m.group(1).lower()=="no year": no_year=True
        else: year=int(m.group(1))
    key=core_key(normalize(text))
    hits=[g for g in groups if key and core_key(normalize(g["title"]))==key]
    if year is not None: hits=[g for g in hits if g.get("year")==year]
    elif no_year: hits=[g for g in hits if not g.get("year")]
    return hits


async def identities_apply_preview(ctx, uid, text):
    """Returns (text, rows). Validates the request and stores a plan; nothing is written here."""
    from series_catalog import effective_promos
    scan=ctx.identity_scan
    if not scan or time.monotonic()-scan["at"]>IDENT_CACHE_SECONDS:
        return "Run /identities first. The list is kept for 30 minutes and is lost when the bot restarts.",None
    text=" ".join(text.split())
    if not text: return "Usage: /identities apply <Proposed Title> [force]",None
    force=False
    hits=_find_identity_groups(scan["groups"],text)          # a title that really ends in 'Force' wins over the flag
    if not hits and text.split()[-1].casefold()=="force" and len(text.split())>1:
        hits=_find_identity_groups(scan["groups"]," ".join(text.split()[:-1])); force=bool(hits)
    if not hits:
        return f"No conflict group is titled <b>{_e(text)}</b>. Use the title exactly as /identities shows it.",None
    if len(hits)>1:
        return ("Several groups share that title. Add the year as shown:\n"+"\n".join(f"• {_e(_group_label(g))} — {len(g['uids']):,}" for g in hits)),None
    g=hits[0]; n=len(g["uids"])
    promos=effective_promos((await ctx.settings()).get("series_promos",[]))
    blocks=_identity_blocks(g["title"],n,promos)
    if blocks and not force:
        return (f"<b>Refused: {_e(_group_label(g))}</b> — {', '.join(blocks)}.\nNothing was changed. If you have checked it and still want it applied, send:\n"
                f"<code>/identities apply {_e(_group_label(g))} force</code>"),None
    await ctx.series_catalog.maintenance_state.update_one({"_id":f"identapply:{int(uid)}"},
        {"$set":{"sid":g["sid"],"hash":_group_hash(g),"count":n,"force":bool(force),"created":time.time()}},upsert=True)
    lines=["<b>Confirm identity repair</b>",f"Move <b>{n:,}</b> record(s) into <b>{_e(_group_label(g))}</b>."]
    lines.append("From: "+_e(", ".join(f"{t} ({c:,})" for t,c in g["from"].most_common(5))))
    lines+= [f"   • <code>{_e(x[:90])}</code>" for x in g["examples"]]
    if blocks: lines.append("⚠ <b>FORCED</b> past: "+", ".join(blocks))
    lines.append("\nOnly this group's records move. Each is re-read with the current parser first; adopted files are never touched, and a reading that "
                 "would weaken stored data (episode→pack, lost special/multi-episode info) is held back. Nothing is deleted from Telegram. "
                 "Expires in 10 minutes.")
    return "\n".join(lines),[[Btn(f"Confirm: move {n:,}","ss:ident:go"),Btn("Cancel","ss:ident:no")]]


async def identities_apply_group(ctx, g, promos, progress=None):
    """Move one group's records. Every record is re-verified against the live catalogue; returns a counts dict."""
    from dataclasses import replace
    from series_catalog import SeriesFile
    cat=ctx.series_catalog
    aliases=await cat.alias_map()
    sid=g["sid"]
    dest_doc=await cat.shows.find_one({"_id":sid}) or {}
    dest_title=dest_doc.get("title") or g["title"]; dest_year=dest_doc.get("year") if dest_doc else g.get("year")
    before=await cat.lookalike_extras(sid)
    r={"planned":len(g["uids"]),"moved":0,"held":0,"adopted":0,"unreadable":0,"changed":0,"gone":0}
    old_sids=set(); last_edit=0.0
    uids=list(g["uids"])
    for i in range(0,len(uids),200):
        chunk=uids[i:i+200]
        docs=await cat.files.find({"_id":{"$in":chunk}},_IDENT_PROJECTION).to_list(length=len(chunk))
        r["gone"]+=len(chunk)-len(docs); news=[]
        for doc in docs:
            old=SeriesFile.from_doc(doc)
            if old.sid==sid: r["changed"]+=1; continue                    # already there
            if old.adopted: r["adopted"]+=1; continue
            parsed,cand=_reparse_reading(old,promos)
            if parsed is None: r["unreadable"]+=1; continue
            if core_key(normalize(cand))==core_key(normalize(old.show)): r["changed"]+=1; continue
            if _identity_destination(parsed,cand,aliases,promos)[0]!=sid: r["changed"]+=1; continue
            if reparse_downgrade(old,parsed): r["held"]+=1; continue
            news.append(replace(old,sid=sid,show=dest_title,year=dest_year,kind=parsed.kind,season=parsed.season,
                                episodes=parsed.episodes,part=parsed.part,absolute=parsed.absolute,ep_title=parsed.ep_title,
                                special=parsed.special,special_label=parsed.special_label,
                                resolution=parsed.resolution or old.resolution,source=parsed.source,codec=parsed.codec,
                                languages=parsed.languages,group=parsed.group,tags=parsed.tags,ext=parsed.ext or old.ext))
            old_sids.add(old.sid)
        if news: await cat.bulk_update_files(news); r["moved"]+=len(news)
        now=time.monotonic()
        if progress is not None and now-last_edit>4:
            last_edit=now
            try: await progress(f"Moving records… {min(i+200,len(uids)):,}/{len(uids):,} checked · moved {r['moved']:,}")
            except Exception: pass
        await asyncio.sleep(0.1)
    r["emptied"]=await cat.refresh_show_counts(old_sids,(sid,dest_title,dest_year))
    r["total"]=await cat.files.count_documents({"sid":sid})
    r["lookalikes"]=max(0,await cat.lookalike_extras(sid)-before)
    r["title"]=_group_label({"title":dest_title,"year":dest_year})
    return r


def identities_report(r):
    return ("<b>Identity repair complete</b> — "+_e(r["title"])+"\n"
            f"Planned: {r['planned']:,}\nMoved: {r['moved']:,}\n"
            f"Held (would weaken stored data): {r['held']:,}\n"
            f"Skipped: adopted {r['adopted']:,} · unreadable now {r['unreadable']:,} · no longer a conflict/already moved {r['changed']:,} · deleted since scan {r['gone']:,}\n"
            f"Source shows left empty and removed: {r['emptied']:,}\n"
            f"Files now in {_e(r['title'])}: {r['total']:,}\n"
            f"New same-size look-alikes there: {r['lookalikes']:,}"+(" (preview with /dedupeseries dry)" if r["lookalikes"] else "")+
            "\nNo posts were deleted from Telegram.")


async def identities_apply_confirm(ctx, uid, edit):
    """Callback body for the Confirm button. ``edit(text, rows)`` rewrites the confirmation message. Returns the toast."""
    from series_catalog import effective_promos
    cat=ctx.series_catalog
    plan=await cat.maintenance_state.find_one({"_id":f"identapply:{int(uid)}"})
    await cat.maintenance_state.delete_one({"_id":f"identapply:{int(uid)}"})           # a plan is single-use
    if not plan or time.time()-float(plan.get("created",0))>IDENT_CONFIRM_SECONDS:
        await edit("This confirmation expired (10 minutes). Nothing was changed. Run /identities apply again.",None); return "Expired."
    if ctx.maintenance:
        await edit(f"Another maintenance operation is running ({ctx.maintenance}). Nothing was changed. Try again when it finishes.",None); return "Busy."
    scan=ctx.identity_scan
    g=next((x for x in (scan or {}).get("groups",[]) if x["sid"]==plan.get("sid")),None)
    if not g or _group_hash(g)!=plan.get("hash"):
        await edit("The /identities list is gone or has changed since the preview. Nothing was changed. Run /identities again.",None); return "List changed."
    promos=effective_promos((await ctx.settings()).get("series_promos",[]))
    blocks=_identity_blocks(g["title"],len(g["uids"]),promos)
    if blocks and not plan.get("force"):
        await edit("Refused: "+", ".join(blocks)+". Nothing was changed.",None); return "Refused."
    ctx.maintenance="identities"
    async def job():
        try:
            r=await identities_apply_group(ctx,g,promos,progress=lambda t: edit(t,None))
            scan["groups"]=[x for x in scan["groups"] if x["sid"]!=g["sid"]]
            await edit(identities_report(r),[[Btn("Close","ss:close")]])
        except Exception:
            log.exception("identity repair failed")
            await edit("Identity repair stopped because of an error. Records already moved stay moved; run /identities again to see what is left. Check the Render logs.",None)
        finally:
            ctx.maintenance=None
    from handlers import _spawn
    _spawn(job())
    return "Moving records…"


# ---------------------------------------------------------------------------------------------
# /fixpromoshows: shows whose stored TITLE starts with an uploader label ("Brewfiles Band Of Brothers")
# ---------------------------------------------------------------------------------------------
async def promo_prefixed_shows(ctx, sample=25):
    """Find shows whose title = promo label + a clean title, and PROVE it from the files.

    A show is proposed only when (1) its title starts with a built-in/custom promo label followed by more
    words, and (2) the files in it, read with the current parser, ALL give exactly the clean title.
    A real title that merely begins with such a word ("Judas and the Black Messiah") never qualifies:
    its filenames contain the word, so it is left for a manual /mergeshow decision. Read-only.
    """
    from series_catalog import _resolve_parsed, effective_promos, show_id, suspicious_short_show_title
    cat = ctx.series_catalog
    promos = (await ctx.settings()).get("series_promos", [])
    labels = effective_promos(promos)
    patterns = [(lab, re.compile(r"(?i)^\s*" + re.escape(lab).replace(r"\-", r"[-\s]?") + r"[\s._-]+(?P<rest>\S.*)$")) for lab in labels]
    found, scanned, last = [], 0, None
    while True:
        q = {} if last is None else {"_id": {"$gt": last}}
        batch = await cat.shows.find(q).sort("_id", 1).limit(500).to_list(length=500)
        if not batch: break
        last = batch[-1]["_id"]
        for sh in batch:
            scanned += 1
            title = str(sh.get("title") or "")
            for lab, pat in patterns:
                m = pat.match(title)
                if not m: continue
                clean = m.group("rest").strip()
                if len(re.findall(r"[^\W\d_]", clean)) < 3 or suspicious_short_show_title(clean): continue
                want = core_key(normalize(clean))
                files = await cat.files.find({"sid": sh["_id"]}, {"name": 1, "caption": 1}).limit(sample).to_list(length=sample)
                label_in_name = re.compile(r"(?i)(?<![A-Za-z0-9])" + re.escape(lab).replace(r"\-", r"[-\s]?") + r"(?![A-Za-z0-9])")
                if any(label_in_name.search(str(f.get("name") or "")) for f in files):
                    break          # the label really is in the filenames: it may be part of a real title, leave it to /mergeshow
                reads = []
                for f in files:
                    parsed, _g = _resolve_parsed(f.get("name") or "", f.get("caption") or "", promos)
                    if parsed.kind in ("episode", "pack") and parsed.show:
                        reads.append(core_key(normalize(parsed.show)))
                if reads and all(r == want for r in reads):
                    year = sh.get("year")
                    sid = show_id(clean, year)
                    existing = await cat.shows.find_one({"_id": sid}, {"title": 1})
                    found.append({"old_sid": sh["_id"], "old_title": title, "clean": (existing or {}).get("title") or clean,
                                  "year": year, "sid": sid, "exists": bool(existing), "files": int(sh.get("count", 0)), "label": lab})
                break
    return found, scanned


async def fix_promo_shows(ctx, apply=False):
    """Preview (default) or apply the promo-prefix fix. Applying moves files with the existing, alias-keeping merge."""
    found, scanned = await promo_prefixed_shows(ctx)
    lines = [f"Checked {scanned:,} shows. {len(found):,} carry an uploader label in the title and every sampled file reads as the clean title."]
    for f in found[:12]:
        lines.append(f"• {f['old_title']} → {f['clean']}" + (f" ({f['year']})" if f['year'] else "") + (" [merge into existing]" if f["exists"] else " [rename]") + f" · {f['files']:,} files")
    if len(found) > 12: lines.append(f"… and {len(found) - 12:,} more.")
    if not found:
        return "\n".join(lines)[:3900]
    if not apply:
        lines.append("\nPreview only. Nothing changed. Files keep their episode data; the old show id keeps working as an alias. Apply with /fixpromoshows confirm.")
        return "\n".join(lines)[:3900]
    moved = 0
    for f in found:
        moved += await ctx.series_catalog.merge_show_ids(f["sid"], [f["old_sid"]], f["clean"], f["year"])
    ctx.series_catalog.clear_cache()
    lines.append(f"\nDone: {len(found):,} shows fixed, {moved:,} files moved. Old ids redirect through aliases.")
    return "\n".join(lines)[:3900]


# ---------------------------------------------------------------------------------------------
# /fixtitles: stored show titles that disagree with what the files now read as
# ---------------------------------------------------------------------------------------------
TITLE_CLASSES = {
    "label": "Show titled exactly as an uploader label (e.g. 'Brewfiles'): each file moves to the show it reads as",
    "contained": "Stored and newly read titles share their words (one contains the other), e.g. \"'s Beautiful Now\" -> \"It's Beautiful Now\"",
    "other": "Different titles with consistent evidence: REVIEW ONLY, never applied automatically",
}


def _word_subsequence(a, b):
    """True when the word list a appears as a contiguous run inside b."""
    n = len(a)
    return n > 0 and any(b[i:i + n] == a for i in range(len(b) - n + 1))


async def title_proposals(ctx, sample=25):
    """Read-only. Returns ({class: [proposal,...]}, shows_scanned).

    A proposal exists only when the files read with the CURRENT parser (up to `sample` per show) all agree
    on one clean title that differs from the stored one. Bare-label shows are the exception: their files
    legitimately read as many different titles, so every readable file is allowed to move on apply.
    """
    from series_catalog import _resolve_parsed, effective_promos, show_id, suspicious_short_show_title
    from series_parse import GENERIC_MEDIA_NAMES
    cat = ctx.series_catalog
    promos = (await ctx.settings()).get("series_promos", [])
    label_keys = {core_key(normalize(x)) for x in effective_promos(promos)}
    out = {k: [] for k in TITLE_CLASSES}; scanned = 0; last = None
    while True:
        q = {} if last is None else {"_id": {"$gt": last}}
        batch = await cat.shows.find(q).sort("_id", 1).limit(500).to_list(length=500)
        if not batch: break
        last = batch[-1]["_id"]
        for sh in batch:
            scanned += 1
            stored = str(sh.get("title") or ""); skey = core_key(normalize(stored))
            files = await cat.files.find({"sid": sh["_id"]}, {"name": 1, "caption": 1}).limit(sample).to_list(length=sample)
            titles = {}
            for f in files:
                parsed, _g = _resolve_parsed(f.get("name") or "", f.get("caption") or "", promos)
                if parsed.kind in ("episode", "pack") and parsed.show:
                    k = core_key(normalize(parsed.show))
                    if k and k != skey: titles.setdefault(k, []).append(parsed.show)
                    elif k == skey: titles.setdefault(k, []).append(parsed.show)
            if not titles: continue
            is_label = skey in label_keys
            keys = list(titles)
            if not is_label and (len(keys) != 1 or keys[0] == skey):
                continue                                         # disagreeing evidence, or nothing to fix
            def usable(t):
                n = normalize(t)
                return len(re.findall(r"[^\W\d_]", t)) >= 3 and not suspicious_short_show_title(t) and n not in GENERIC_MEDIA_NAMES
            if is_label:
                good = {k: v for k, v in titles.items() if k != skey and usable(v[0])}
                if good:
                    out["label"].append({"old_sid": sh["_id"], "old_title": stored, "year": sh.get("year"), "files": int(sh.get("count", 0)),
                                         "new_titles": sorted({v[0] for v in good.values()})})
                continue
            new = max(titles[keys[0]], key=titles[keys[0]].count)
            if not usable(new): continue
            ca, cb = skey.replace(" ", ""), keys[0].replace(" ", "")
            near = len(min(ca, cb, key=len)) >= 6 and (ca in cb or cb in ca)       # "sbeautifulnow" in "itsbeautifulnow": apostrophe fragments
            kind = "contained" if (near or _word_subsequence(skey.split(), keys[0].split()) or _word_subsequence(keys[0].split(), skey.split())) else "other"
            out[kind].append({"old_sid": sh["_id"], "old_title": stored, "year": sh.get("year"), "files": int(sh.get("count", 0)),
                              "new_titles": [new], "exists": bool(await cat.shows.find_one({"_id": show_id(new, sh.get("year"))}, {"_id": 1}))})
    return out, scanned


def _proposal_line(p):
    return f"• {p['old_title']} → {' | '.join(p['new_titles'][:3])}" + (f" ({p['year']})" if p.get("year") else "") + f" · {p['files']:,} files"


async def fix_titles_report(ctx, mode="dry", klass=None, page=0):
    """dry: counts + examples. list <class> [page]: browse. confirm <label|contained>: apply that class only."""
    from series_catalog import _resolve_parsed, show_id, suspicious_short_show_title
    props, scanned = await title_proposals(ctx)
    if mode == "list":
        rows = props.get(klass or "", [])
        per = 15; pages = max(1, -(-len(rows) // per)); page = max(0, min(int(page), pages - 1))
        head = f"{klass}: {len(rows):,} shows · page {page + 1}/{pages}"
        return (head + "\n" + "\n".join(_proposal_line(p) for p in rows[page * per:(page + 1) * per]) + f"\n\nNext: /fixtitles list {klass} {page + 2}")[:3900]
    if mode == "dry":
        lines = [f"Checked {scanned:,} shows. Proposals (nothing changed yet):"]
        for k, desc in TITLE_CLASSES.items():
            rows = props[k]
            lines.append(f"\n<b>{k}</b> ({len(rows):,} shows, {sum(r['files'] for r in rows):,} files) - {desc}")
            lines.extend(_e(_proposal_line(p)) for p in rows[:5])
        lines.append("\nBrowse a class: /fixtitles list contained 1\nApply a class: /fixtitles confirm label  or  /fixtitles confirm contained\n('other' can only be fixed by hand with /mergeshow.)")
        return "\n".join(lines)[:3900]
    if klass not in ("label", "contained"):
        return "Only 'label' and 'contained' can be applied. Use /fixtitles confirm label  or  /fixtitles confirm contained."
    cat = ctx.series_catalog; promos = (await ctx.settings()).get("series_promos", [])
    done = moved = 0
    for p in props[klass]:
        if klass == "contained":
            moved += await cat.merge_show_ids(show_id(p["new_titles"][0], p["year"]), [p["old_sid"]], p["new_titles"][0], p["year"]); done += 1
            continue
        groups = {}; last = None                              # label: move each readable file to the show it reads as
        while True:
            q = {"sid": p["old_sid"]} if last is None else {"sid": p["old_sid"], "_id": {"$gt": last}}
            docs = await cat.files.find(q, {"name": 1, "caption": 1}).sort("_id", 1).limit(500).to_list(length=500)
            if not docs: break
            last = docs[-1]["_id"]
            for d in docs:
                parsed, _g = _resolve_parsed(d.get("name") or "", d.get("caption") or "", promos)
                if parsed.kind in ("episode", "pack") and parsed.show and core_key(normalize(parsed.show)) != core_key(normalize(p["old_title"])):
                    if len(re.findall(r"[^\W\d_]", parsed.show)) >= 3 and not suspicious_short_show_title(parsed.show):
                        groups.setdefault((parsed.show, p["year"]), []).append(d["_id"])
        for (title, year), ids in groups.items():
            moved += await cat.rehome_files(ids, title, year)
        done += 1
    cat.clear_cache()
    return f"Applied '{klass}': {done:,} shows processed, {moved:,} files moved. Episodes were not changed and old show ids keep working. Run /reparse next."


# ---------------------------------------------------------------------------------------------
# /dbsize: where the MongoDB space goes (read-only)
# ---------------------------------------------------------------------------------------------
ATLAS_M0_LIMIT = 512 * 1024 * 1024


def _mb(n) -> str:
    return f"{(n or 0) / 1048576:,.1f} MB"


async def _db_rows(db):
    rows = []
    for name in await db.list_collection_names():
        if name.startswith("_federation"): continue
        st = await db.command("collStats", name)
        rows.append((name, int(st.get("count", 0)), int(st.get("size", 0)), int(st.get("totalIndexSize", 0)), int(st.get("avgObjSize", 0))))
    rows.sort(key=lambda r: -(r[2] + r[3]))
    return rows


async def db_size_report(ctx) -> str:
    """Per-collection data and index sizes, plus how much of the free-tier limit is used. Read-only."""
    cat = ctx.series_catalog
    from multidb import FederatedDB
    db = cat.files.database
    fed = isinstance(db, FederatedDB)         # NOT hasattr: a pymongo Database invents a collection for any attribute name
    lines = []
    if fed:
        st = db.status()
        lines += [f"<b>Two databases in use</b> - new files go to: {st['active']}{' (manual)' if st['manual'] else ' (automatic)'}; "
                  f"switch at {st['switch_mb']} MB" + (f"; cluster size at last check {st['last_cluster_mb']} MB" if st["last_cluster_mb"] is not None else ""), ""]
        dbs = [("Primary database", db.primary), ("Secondary database", db.secondary)]
    else:
        dbs = [("Database", db)]
    rows = []
    for label, handle in dbs:
        r = await _db_rows(handle)
        data = sum(x[2] for x in r); idx = sum(x[3] for x in r); total = data + idx
        lines.append(f"<b>{label}</b>: {_mb(total)} of 512 MB ({total * 100 // ATLAS_M0_LIMIT}%) - data {_mb(data)}, indexes {_mb(idx)}")
        for name, count, size, isz, avg in r[:8 if fed else 12]:
            lines.append(f"<code>{name}</code>: {count:,} docs · data {_mb(size)} · indexes {_mb(isz)} · avg {avg:,} B/doc")
        lines.append("")
        rows += r
    if not fed:
        total_all = sum(x[2] + x[3] for x in rows)
    if not fed:
        try:   # the Atlas alert counts EVERY database on the cluster, not only this bot's
            client = db.client
            others = []
            for n in await client.list_database_names():
                if n in ("admin", "local", "config") or n == db.name: continue
                stt = await client[n].command("dbStats")
                others.append((n, int(stt.get("dataSize", 0)) + int(stt.get("indexSize", 0))))
            if others:
                lines.append("Other databases on this cluster (they count toward the same 512 MB): " + ", ".join(f"{n} {_mb(sz)}" for n, sz in others))
                lines.append("")
        except Exception:
            log.info("could not list other databases", exc_info=True)
    # what inside a catalogue file document is large?
    try:
        docs = await cat.files.find({}, {"name": 1, "caption": 1}).limit(1000).to_list(length=1000)
        if docs:
            n_files = sum(r[1] for r in rows if r[0] == "series_files") or len(docs)
            cap = sum(len(str(d.get("caption") or "").encode()) for d in docs) / len(docs)
            nam = sum(len(str(d.get("name") or "").encode()) for d in docs) / len(docs)
            lines += ["", f"Sampled {len(docs):,} catalogue files: average caption {cap:,.0f} B, filename {nam:,.0f} B.",
                      f"Captions across all {n_files:,} files are roughly {_mb(cap * n_files)}; filenames {_mb(nam * n_files)}."]
    except Exception:
        log.info("dbsize sampling failed", exc_info=True)
    lines += ["", "Nothing was changed. Each newly indexed file adds roughly its document plus ~3 index entries."]
    return "\n".join(lines)[:3900]


async def db_switch_report(ctx, mode="status") -> str:
    """/dbswitch status | primary | secondary | auto - choose where NEW documents are written (reads always use both)."""
    from multidb import FederatedDB
    db = getattr(ctx.series_catalog.files, "database", None)
    if not isinstance(db, FederatedDB):
        return "The second database is not configured. Set DATABASE_URI_2 (and optionally DATABASE_NAME_2) on Render and redeploy."
    if mode in ("primary", "secondary"):
        await db.set_active(mode, True)
        return f"Manual: new documents are now writing to the {mode}. Existing documents stay where they are; reads always use both."
    if mode == "auto":
        await db.set_active(db.active, False)
        await db.maybe_refresh(force=True)
        return f"Back to automatic. New documents go to the {db.active} until the cluster reaches {db.status()['switch_mb']} MB."
    st = db.status()
    return (f"Writing new documents to: {st['active']} ({'manual' if st['manual'] else 'automatic'}).\n"
            f"Automatic switch at {st['switch_mb']} MB of cluster size; last check: {st['last_cluster_mb'] if st['last_cluster_mb'] is not None else 'not yet'} MB.\n"
            "Commands: /dbswitch primary | secondary | auto")


# ---------------------------------------------------------------------------------------------
# /skipruns: find runs of consecutive unidentified episodes that can be adopted in one go
# ---------------------------------------------------------------------------------------------
_RUN_CODES = ("no_episode_marker", "unsupported_episode_format", "legacy_unclassified")


def _name_shape(name: str) -> str:
    s = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", str(name or ""))
    s = re.sub(r"\[[^\]]*\]", "[]", s)
    s = re.sub(r"[^\W\d_]+", "A", s)
    s = re.sub(r"\d+", "9", s)
    return re.sub(r"A([ _.\-']*A)+", "A", s)


_RUN_STOP = {"series", "season", "episode", "episodes", "ep", "eps", "capitulo", "capítulo", "cap", "chapter", "part", "disc", "disk",
             "ver", "online", "gratis", "hls", "the"}


def run_title(names):
    """The show name shared by a run of files, or "" when the files differ (then the words are episode titles).

    Every file must reduce to the SAME words once numbers, brackets, handles, quality words and generic words such as
    "Episode" are removed. "1-MI ADORABLE MALDICION", "2-MI ADORABLE MALDICION" ... qualify; "01. To You", "02. The Man" do not.
    """
    from series_parse import _strip_promo_tokens, _RES_RE
    keys = []; shown = None
    for n in names:
        s = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", str(n or ""))
        s = _strip_promo_tokens(re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", s))
        words = [w for w in re.findall(r"[^\W\d_]+", s) if w.casefold() not in _RUN_STOP and not _RES_RE.fullmatch(w)]
        if not words: return ""
        keys.append(" ".join(w.casefold() for w in words))
        if shown is None: shown = " ".join(words)
    if not keys or len(set(keys)) != 1 or len(re.findall(r"[^\W\d_]", shown or "")) < 4:
        return ""
    return shown


async def skip_runs(ctx, min_len=5, max_gap=4):
    """Read-only. Groups skipped posts with the same filename shape that sit next to each other in a channel.

    A run like "01. Title / 02. Title / 03. Title ..." is one show whose title is missing from the filenames:
    the administrator supplies the title and adopts the whole run with one /adopt command.
    """
    cat = ctx.series_catalog
    runs = []
    for ch in sorted(await cat.skips.distinct("channel")):
        last = 0; cur = None
        while True:
            docs = await cat.skips.find({"channel": ch, "message_id": {"$gt": last}, "code": {"$in": list(_RUN_CODES)}}).sort("message_id", 1).limit(500).to_list(length=500)
            if not docs: break
            for d in docs:
                shape = _name_shape(d.get("name")); mid = int(d["message_id"])
                if cur and cur["shape"] == shape and mid - cur["hi"] <= max_gap:
                    cur["hi"] = mid; cur["n"] += 1; cur["last"] = d.get("name"); cur["names"].append(d.get("name"))
                else:
                    if cur and cur["n"] >= min_len: runs.append(cur)
                    cur = {"ch": ch, "lo": mid, "hi": mid, "n": 1, "shape": shape, "first": d.get("name"), "last": d.get("name"), "names": [d.get("name")]}
            last = int(docs[-1]["message_id"])
        if cur and cur["n"] >= min_len: runs.append(cur)
    for r in runs:
        r["title"] = run_title(r.pop("names", []))
    runs.sort(key=lambda r: -r["n"])
    return runs


async def skip_runs_report(ctx, page=0):
    runs = await skip_runs(ctx)
    per = 6; pages = max(1, -(-len(runs) // per)); page = max(0, min(int(page), pages - 1))
    total = sum(r["n"] for r in runs)
    lines = [f"<b>{len(runs):,} runs</b> covering {total:,} skipped posts · page {page + 1}/{pages}",
             "Each run is consecutive posts with the same filename shape. If they are all one show, give it a title:", ""]
    for r in runs[page * per:(page + 1) * per]:
        lines.append(f"<b>{r['n']} files</b>: {_e(str(r['first'])[:55])} … {_e(str(r['last'])[:40])}\n"
                     f"<code>/adopt dry {r['ch']}:{r['lo']}-{r['hi']} | {_e(r.get('title') or 'SHOW TITLE')}</code>"
                     + (" (title read from the files)" if r.get("title") else ""))
    if page + 1 < pages: lines.append(f"\nNext: /skipruns {page + 2}")
    lines.append("\nReplace SHOW TITLE with the show's real name (exact title to join an existing show). Runs that mix several shows must be split by hand.")
    return "\n".join(lines)[:3900]


SKIP_CLEAR_CODES = ("non_video_file", "extras_or_featurette", "insufficient_title_information")


async def skip_clear_report(ctx, code, confirm_count=None):
    """Bulk-remove skip RECORDS of one safe code (subtitles/archives, extras, unreadable). Explicit count required."""
    cat = ctx.series_catalog
    if code not in SKIP_CLEAR_CODES:
        return "Only these can be cleared in bulk: " + ", ".join(SKIP_CLEAR_CODES) + "."
    n = await cat.skips.count_documents({"code": code, "category": {"$ne": "lookalike"}})
    if confirm_count is None:
        return (f"{n:,} skip records have code {code}. This removes only the records (not Telegram posts or the catalogue).\n"
                f"To remove them: /skipclear {code} confirm {n}")
    if int(confirm_count) != n:
        return f"The count changed (now {n:,}). Nothing removed. Run /skipclear {code} again."
    r = await cat.skips.delete_many({"code": code, "category": {"$ne": "lookalike"}})
    return f"Removed {int(getattr(r, 'deleted_count', 0)):,} records with code {code}."


# ---------------------------------------------------------------------------------------------
# /reparse skipped (only the skipped posts), /singleshows, /mergewwe
# ---------------------------------------------------------------------------------------------
async def reparse_skipped_only(ctx, client, progress_message):
    """Re-read ONLY the skipped records with the current parser.

    Names are re-judged locally (no Telegram calls, no catalogue pass); only posts that now read as episodes are
    fetched and indexed. Labels of everything else are refreshed. Look-alikes are untouched.
    """
    from pymongo import UpdateOne
    from series_catalog import from_message
    from series_classify import LOOKALIKE, classify_name
    cat=ctx.series_catalog
    promos=(await ctx.settings()).get("series_promos",[])
    scanned=relabelled=0; candidates=[]; last_edit=[0.0]
    async def say(text,force=False):
        now=time.monotonic()
        if not force and now-last_edit[0]<4: return
        last_edit[0]=now
        try: await progress_message.edit_text(text)
        except Exception: pass
    await say("Re-reading the skipped records only (the catalogue is not touched)…",True)
    async for docs in cat.iter_skips(batch=500):
        ops=[]
        for d in docs:
            if d.get("category")==LOOKALIKE: continue
            scanned+=1
            c=classify_name(d.get("name") or "","",promos)
            if c.category=="series":
                candidates.append((int(d["channel"]),int(d["message_id"]))); continue
            if (d.get("category"),d.get("code"))!=(c.category,c.code):
                relabelled+=1
                ops.append(UpdateOne({"_id":d["_id"]},{"$set":{"category":c.category,"code":c.code,"reason":c.reason,"title_guess":(c.title or "")[:120],"classified_at":time.time()}}))
        if ops: await cat.skips.bulk_write(ops,ordered=False)
        await say(f"Checked {scanned:,} skipped records · {len(candidates):,} now readable · {relabelled:,} relabelled")
        if ctx.maintenance_stop.is_set(): break
    recognized=imported=0
    by_channel={}
    for ch,mid in candidates: by_channel.setdefault(ch,[]).append(mid)
    aliases=await cat.alias_map()
    for ch,ids in by_channel.items():
        for i in range(0,len(ids),100):
            if ctx.maintenance_stop.is_set(): break
            try: msgs=await client.get_messages(ch,ids[i:i+100])
            except Exception:
                log.info("could not fetch skipped posts from %s",ch,exc_info=True); continue
            if not isinstance(msgs,list): msgs=[msgs]
            recs=[]
            for m in msgs:
                if not m: continue
                rec=from_message(m,promos=promos,aliases=aliases)
                if rec: recs.append(rec)
            recognized+=len(recs)
            if recs:
                added,known,dupes=await cat.add(recs); imported+=added
            await say(f"Indexing newly readable posts… {recognized:,} of {len(candidates):,}")
            await asyncio.sleep(0.4)
    await say(f"Skipped-only reparse complete.\nSkipped records checked: {scanned:,}\nNow readable: {len(candidates):,} · indexed: {imported:,}\n"
              f"Relabelled (still skipped): {relabelled:,}\nThe catalogue was not re-read. Use /reparse for that.",True)
    return {"scanned":scanned,"recognized":recognized,"imported":imported,"relabelled":relabelled}


async def send_lines_as_txt(client, chat_id, stem, header, lines, part_bytes=380_000):
    """Send lines as small numbered .txt parts (each fits one Telegram upload chunk), retrying each part."""
    import io
    parts=[]; cur=[header]; size=len(header)
    for ln in lines:
        b=len(ln.encode("utf-8"))+1
        if size+b>part_bytes and len(cur)>1: parts.append("\n".join(cur)); cur=[header]; size=len(header)
        cur.append(ln); size+=b
    parts.append("\n".join(cur))
    stamp=time.strftime("%Y-%m-%d-%H%M",time.gmtime()); failed=[]
    for idx,body in enumerate(parts,1):
        ok=False
        for attempt in range(EXPORT_SEND_ATTEMPTS):
            data=io.BytesIO(body.encode("utf-8")); data.name=f"{stem}-{stamp}-part{idx}of{len(parts)}.txt"
            try:
                await asyncio.wait_for(client.send_document(chat_id,data,caption=f"{stem}: part {idx} of {len(parts)}"),EXPORT_SEND_TIMEOUT); ok=True; break
            except Exception as exc:
                log.warning("%s part %s attempt %s failed: %r",stem,idx,attempt+1,exc)
                await asyncio.sleep(0 if EXPORT_SEND_TIMEOUT<1 else 3)
        if not ok: failed.append(idx)
    return len(parts),failed


async def single_shows(ctx, limit=None):
    """Shows that have exactly one file, with that file's name (to spot parser gaps that create stand-alone shows)."""
    cat=ctx.series_catalog; rows=[]; last=None
    while True:
        q={"count":1} if last is None else {"count":1,"_id":{"$gt":last}}
        batch=await cat.shows.find(q).sort("_id",1).limit(500).to_list(length=500)
        if not batch: break
        last=batch[-1]["_id"]
        for sh in batch:
            f=await cat.files.find_one({"sid":sh["_id"]},{"name":1,"season":1,"episodes":1,"adopted":1})
            ep="" if not f else (f"S{f.get('season')}E{','.join(map(str,f.get('episodes') or []))}" if f.get("episodes") else "no-episode")
            rows.append((sh.get("title") or "",sh.get("year"),ep,(f or {}).get("name") or ""))
        if limit and len(rows)>=limit: break
    rows.sort(key=lambda r:r[0].casefold())
    return rows


def _single_line(r):
    return f"{r[0]}" + (f" ({r[1]})" if r[1] else "") + f" | {r[2]} | {r[3]}"


async def single_shows_report(ctx, client, chat_id, export=False, page=0):
    rows=await single_shows(ctx)
    if export:
        n,failed=await send_lines_as_txt(client,chat_id,"single-file-shows",f"Shows with exactly one file: {len(rows):,}\nformat: title (year) | season/episode | filename\n",[_single_line(r) for r in rows])
        return f"Sent {n-len(failed)} of {n} parts ({len(rows):,} shows)."+(f" Failed parts: {failed}; try again." if failed else "")
    per=40; pages=max(1,-(-len(rows)//per)); page=max(0,min(page,pages-1))
    lines=[f"<b>{len(rows):,} shows have exactly one file</b> · page {page+1}/{pages}"]+[_e(_single_line(r)[:110]) for r in rows[page*per:(page+1)*per]]
    lines.append("\nWhole list as a file: /singleshows export" + (f" · next: /singleshows {page+2}" if page+1<pages else ""))
    return "\n".join(lines)[:3900]


async def wwe_merge_report(ctx, apply=False):
    """Fold every 'WWE <programme>' / bare programme show into ONE show titled WWE (reparse then fills the programme in)."""
    from series_catalog import show_id
    from registries import WWE_PROGRAMME, WWE_SHOW
    cat=ctx.series_catalog; found=[]; last=None
    while True:
        q={} if last is None else {"_id":{"$gt":last}}
        batch=await cat.shows.find(q).sort("_id",1).limit(500).to_list(length=500)
        if not batch: break
        last=batch[-1]["_id"]
        for sh in batch:
            t=str(sh.get("title") or "").strip()
            if t.casefold()==WWE_SHOW.casefold(): continue
            if re.match(r"(?i)^WWE\b",t) or WWE_PROGRAMME.match(t):
                found.append((sh["_id"],t,sh.get("year"),int(sh.get("count",0))))
    lines=[f"{len(found):,} WWE-related shows ({sum(f[3] for f in found):,} files) would become episodes of ONE show titled WWE:"]
    lines+= [f"• {t}" + (f" ({y})" if y else "") + f" · {n:,}" for _,t,y,n in sorted(found,key=lambda x:-x[3])[:20]]
    if len(found)>20: lines.append(f"… and {len(found)-20:,} more.")
    if not found: return "\n".join(lines)
    if not apply:
        lines.append("\nPreview only. Apply with /wwemerge confirm, then run /reparse so each file's programme and date are filled in.")
        return "\n".join(lines)[:3900]
    survivor=show_id(WWE_SHOW,None); moved=0
    for sid,t,y,n in found:
        moved+=await cat.merge_show_ids(survivor,[sid],WWE_SHOW,None)
    cat.clear_cache()
    lines.append(f"\nDone: {len(found):,} shows merged into WWE, {moved:,} files moved. Old show ids keep working. Now run /reparse.")
    return "\n".join(lines)[:3900]


async def autoruns_report(ctx, client, apply=False, max_runs=60):
    """Adopt runs whose files all carry the same show name. Preview first; confirm recomputes and applies each run."""
    runs = [r for r in await skip_runs(ctx) if r.get("title")]
    total = sum(r["n"] for r in runs)
    lines = [f"{len(runs):,} runs ({total:,} files) carry a show name in every filename:"]
    for r in runs[:12]:
        lines.append(f"• {r['title']} · {r['n']} files · {r['ch']}:{r['lo']}-{r['hi']}")
    if len(runs) > 12: lines.append(f"… and {len(runs) - 12:,} more.")
    if not runs: return "\n".join(lines)
    if not apply:
        lines.append("\nPreview only. Each run is checked again when you confirm (the show title, an episode number in at least 80% of the files). "
                     f"Titles that match an existing show add to it; others become NEW shows.\nApply: /autoruns confirm (up to {max_runs} runs per command).")
        return "\n".join(lines)[:3900]
    done = added_files = skipped = 0
    for r in runs[:max_runs]:
        plan = await build_adopt(ctx, client, f"{r['ch']}:{r['lo']}-{r['hi']}", r["title"])
        if plan.get("error") or not plan["recs"] or len(plan["recs"]) < 0.8 * plan["posts"]:
            skipped += 1; continue
        added, known, dupes = (await ctx.series_catalog.add(plan["recs"]))[:3]
        added_files += added; done += 1
    lines.append(f"\nAdopted {done} runs ({added_files:,} files). Skipped {skipped} runs that did not pass the checks. Run /autoruns dry again for the rest.")
    return "\n".join(lines)[:3900]


# ------------------------------------------------------------------ daily series digest (10:10 AM Africa/Lagos)
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta, timezone as _timezone

_SERIES_LAGOS = _timezone(_timedelta(hours=1))
_TRAILING_SHOW_YEAR_RE = re.compile(r"(?:\(\s*((?:19|20)\d{2})\s*\)|(?:^|\s)((?:19|20)\d{2}))\s*$")


def series_lagos_cutoff_ts(d: _date, hour: int = 10, minute: int = 10) -> float:
    return _datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=_SERIES_LAGOS).timestamp()


def series_latest_due_cutoff(now: _datetime | None = None, hour: int = 10, minute: int = 10) -> float:
    cur = now or _datetime.now(_SERIES_LAGOS)
    local = cur.astimezone(_SERIES_LAGOS) if cur.tzinfo else cur.replace(tzinfo=_SERIES_LAGOS)
    today_cut = _datetime(local.year, local.month, local.day, hour, minute, 0, tzinfo=_SERIES_LAGOS)
    if local < today_cut:
        today_cut = today_cut - _timedelta(days=1)
    return today_cut.timestamp()


def _parsed_show_year(title: str, explicit_year) -> int | None:
    if explicit_year is not None:
        try:
            y = int(explicit_year)
            if 1900 <= y <= 2100:
                return y
        except (TypeError, ValueError):
            pass
    m = _TRAILING_SHOW_YEAR_RE.search(str(title or "").strip())
    if m:
        yr = m.group(1) or m.group(2)
        if yr:
            return int(yr)
    return None


def parse_preadd_show_spec(raw: str) -> tuple[str, int | None]:
    txt = " ".join(str(raw or "").split()).strip()
    m = re.match(r"^(.*?)\s*\(\s*((?:19|20)\d{2})\s*\)\s*$", txt)
    if m and m.group(1).strip():
        return m.group(1).strip()[:110], int(m.group(2))
    return txt[:110], None


def get_digest_shows(settings: dict) -> list[dict]:
    out = []
    seen = set()
    for x in (settings.get("series_digest_shows") or ()):
        if not isinstance(x, dict):
            continue
        sid = str(x.get("sid") or "").strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append({
            "sid": sid,
            "title": str(x.get("title") or sid),
            "year": x.get("year"),
            "added_at": float(x.get("added_at") or 0),
            "preadd": bool(x.get("preadd", False)),
        })
    return out


async def save_digest_shows(ctx, shows: list[dict]) -> None:
    await ctx.set_setting("series_digest_shows", shows)


def _format_ep_ranges(nums: list[int]) -> str:
    if not nums:
        return ""
    s_nums = sorted(set(int(n) for n in nums))
    parts = []
    start = prev = s_nums[0]
    for n in s_nums[1:]:
        if n == prev + 1:
            prev = n
        else:
            parts.append(f"E{start:02d}" if start == prev else f"E{start:02d}–E{prev:02d}")
            start = prev = n
    parts.append(f"E{start:02d}" if start == prev else f"E{start:02d}–E{prev:02d}")
    return ", ".join(parts)


def summarize_show_updates(records: list) -> str:
    by_season: dict[int, set[int]] = {}
    packs: set[int] = set()
    dated: list[str] = []
    specials = 0
    for r in records:
        if r.kind == "pack":
            packs.add(int(r.season or 0))
            continue
        ep_t = getattr(r, "ep_title", "") or ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ep_t):
            dated.append(ep_t)
            continue
        eps = getattr(r, "episodes", ()) or ()
        if eps:
            by_season.setdefault(int(r.season or 0), set()).update(int(e) for e in eps)
        else:
            specials += 1
    bits = []
    for sn in sorted(by_season):
        ep_txt = _format_ep_ranges(list(by_season[sn]))
        prefix = "Eps" if sn == 0 else (str(sn) if sn >= 1900 else f"S{sn:02d}")
        bits.append(f"{prefix}: {ep_txt}")
    for ps in sorted(packs):
        bits.append("Series Pack" if ps == 0 else f"S{ps:02d} Pack")
    if dated:
        u_dates = sorted(set(dated))
        bits.append(", ".join(u_dates[:3]) + (f" (+{len(u_dates) - 3})" if len(u_dates) > 3 else ""))
    if specials:
        bits.append(f"{specials} special{'s' if specials != 1 else ''}")
    summary = " · ".join(bits) if bits else f"{len(records)} new file{'s' if len(records) != 1 else ''}"
    return summary[:160]


async def open_show_by_id(ctx, show, uid: int, sid: str) -> None:
    if not getattr(ctx, "ready", True) or getattr(ctx, "series_catalog", None) is None:
        await show("The catalogue is warming up. Please try again in a moment.")
        return
    target = await ctx.series_catalog.resolve_alias(sid)
    d = await ctx.series_catalog.get_show(target)
    if not d:
        await show("That show isn't in the catalogue yet.")
        return
    t, r = await show_screen(ctx, target, uid=uid)
    sent = await show(t, r)
    if sent:
        now = time.monotonic()
        ctx.series_sessions = {k: v for k, v in ctx.series_sessions.items() if v.get("until", 0) > now}
        if len(ctx.series_sessions) >= 100:
            ctx.series_sessions.clear()
        ctx.series_sessions[(sent.chat.id, sent.id)] = {
            "origin": "direct",
            "owner": uid,
            "group": False,
            "until": now + 1800,
        }
        ctx.watch(sent.chat.id, sent.id, await _result_ttl(ctx, False))


async def digest_shows_menu_screen(ctx, page: int = 0) -> tuple[str, list]:
    s = await ctx.settings()
    shows = get_digest_shows(s)
    per = 8
    pages = max(1, (len(shows) + per - 1) // per)
    page = max(0, min(int(page), pages - 1))
    chunk = shows[page * per:(page + 1) * per]
    lines = [
        f"<b>Digest Shows ({len(shows)} manual)</b>",
        "Shows listed here are included in the 10:10 AM Lagos digest whenever they receive newly indexed episodes.",
        "Shows with parsed release year <b>2026+</b> are also included automatically.",
        "",
    ]
    rows = []
    for entry in chunk:
        sid = entry["sid"]
        d = await ctx.series_catalog.get_show(sid) if getattr(ctx, "series_catalog", None) is not None else None
        title = (d.get("title") if d else None) or entry["title"]
        year = (d.get("year") if d else None) or entry.get("year")
        yr_eff = _parsed_show_year(title, year)
        badge = "[Manual · Auto 2026+]" if (yr_eff is not None and yr_eff >= 2026) else "[Manual]"
        status = "" if d else " (pre-added)"
        disp = f"{title} ({year})" if year else title
        lines.append(f"• <b>{_e(disp)}</b> <i>{badge}{status}</i>")
        rows.append([Btn(f"✖ Remove: {disp}"[:58], f"sdg:rmshow:{sid}:{page}")])
    if not shows:
        lines.append("<i>No manual Digest Shows added yet. Search existing shows or pre-add a title below.</i>")
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(Btn("‹ Prev", f"sdg:shows:{page - 1}"))
        nav.append(Btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(Btn("Next ›", f"sdg:shows:{page + 1}"))
        rows.append(nav)
    rows.append([Btn("🔍 Search & Add Show", "sdg:addsearch"), Btn("📝 Pre-add by Title", "sdg:preadd")])
    rows.append([Btn("‹ Daily Updates", "sdg:menu"), Btn("‹ Settings", "st:refresh")])
    return "\n".join(lines), rows


async def digest_shows_search_screen(ctx, query: str) -> tuple[str, list]:
    s = await ctx.settings()
    manual = {x["sid"] for x in get_digest_shows(s)}
    matches = await ctx.series_catalog.search_shows(query, 10, 0)
    rows = []
    for d in matches:
        sid = str(d.get("_id", d.get("sid")))
        title = d.get("title", "Series")
        year = d.get("year")
        disp = f"{title} ({year})" if year else title
        yr_eff = _parsed_show_year(title, year)
        auto_tag = " [Auto 2026+]" if (yr_eff is not None and yr_eff >= 2026) else ""
        mark = "✅ " if sid in manual else "➕ "
        rows.append([Btn(f"{mark}{disp}{auto_tag}"[:58], f"sdg:togshow:{sid}")])
    if not rows:
        rows.append([Btn("No matching shows found", "noop")])
    rows.append([Btn("🔍 Search again", "sdg:addsearch"), Btn("‹ Digest Shows", "sdg:shows:0")])
    return (
        f"<b>Add/remove Digest Shows</b>\n"
        f"Query: <code>{_e(query)}</code>\n"
        "Tap a show to toggle it (you can also pre-add any show before its first episode is indexed):",
        rows,
    )


def format_series_digest_messages(entries: list[dict], bot_username: str, day_label: str) -> list[str]:
    if not entries:
        return []
    bot = (bot_username or "").lstrip("@")
    header = f"📺 <b>Daily Series Updates — {html.escape(day_label)}</b>\n\n"
    chunks: list[str] = []
    cur = header
    for idx, item in enumerate(entries, 1):
        title = html.escape(str(item["title"]))
        year = item.get("year")
        label = f"{title} ({year})" if year else title
        url = f"https://t.me/{bot}?start=show_{item['sid']}" if bot else None
        title_line = f"{idx}. <a href=\"{url}\"><b>{label}</b></a>" if url else f"{idx}. <b>{label}</b>"
        summary = html.escape(str(item.get("summary") or ""))
        block = f"{title_line}\n   └ {summary}\n\n"
        if len(cur) + len(block) > 3800 and cur != header:
            chunks.append(cur.rstrip())
            cur = block
        else:
            cur += block
    if cur.strip():
        chunks.append(cur.rstrip())
    return chunks


async def maybe_send_series_digest(ctx, client, now: _datetime | None = None, force: bool = False) -> dict:
    if getattr(ctx, "series_catalog", None) is None:
        return {"published": 0, "reason": "no_catalog"}
    s = await ctx.settings()
    if not force and not s.get("series_digest_auto"):
        return {"published": 0, "reason": "manual_mode"}            # digests are approved and sent from the control panel
    if not s.get("series_digest_enabled") or not s.get("series_digest_channel"):
        return {"published": 0, "reason": "disabled_or_unconfigured"}
    cur_now = now or _datetime.now(_SERIES_LAGOS)
    due_cutoff = series_latest_due_cutoff(cur_now, 10, 10)
    last_cutoff = s.get("series_digest_last_cutoff")
    if last_cutoff is None:
        if not force:
            await ctx.set_setting("series_digest_last_cutoff", due_cutoff)
            return {"published": 0, "reason": "initialized"}
        last_cutoff = 0.0
    if not force and due_cutoff <= float(last_cutoff):
        return {"published": 0, "reason": "not_due"}

    by_sid, shows_map = await ctx.series_catalog.series_digest_candidates(float(last_cutoff), due_cutoff)
    if not by_sid:
        await ctx.set_setting("series_digest_last_cutoff", due_cutoff)
        return {"published": 0, "reason": "no_candidates"}

    manual_list = get_digest_shows(s)
    eligible_manual_sids: set[str] = set()
    eligible_preadd_keys: set[str] = set()
    for entry in manual_list:
        added_at = float(entry.get("added_at") or 0)
        if added_at <= due_cutoff:
            eligible_manual_sids.add(entry["sid"])
            resolved = await ctx.series_catalog.resolve_alias(entry["sid"])
            eligible_manual_sids.add(resolved)
            if entry.get("preadd") and entry.get("year") is None:
                k = core_key(normalize(entry["title"]))
                if k:
                    eligible_preadd_keys.add(k)

    entries: list[dict] = []
    for sid, recs in by_sid.items():
        show_doc = shows_map.get(sid) or {}
        title = str(show_doc.get("title") or (recs[0].show if recs else sid))
        year = show_doc.get("year") if "year" in show_doc else (recs[0].year if recs else None)
        eff_year = _parsed_show_year(title, year)
        title_key = core_key(normalize(title))
        is_manual = (sid in eligible_manual_sids) or (title_key in eligible_preadd_keys)
        is_auto_2026 = eff_year is not None and eff_year >= 2026
        if not (is_manual or is_auto_2026):
            continue
        newest_ts = max(ctx.series_catalog.indexed_at_sec(r.indexed_at) for r in recs)
        entries.append({
            "sid": sid,
            "title": title,
            "year": year,
            "summary": summarize_show_updates(recs),
            "newest_ts": newest_ts,
            "manual": is_manual,
            "auto_2026": is_auto_2026,
        })

    if not entries:
        await ctx.set_setting("series_digest_last_cutoff", due_cutoff)
        return {"published": 0, "reason": "none_eligible"}

    entries.sort(key=lambda x: (-float(x["newest_ts"]), str(x["title"]).lower(), str(x["sid"])))
    bot_user = getattr(ctx, "bot_username", "") or (getattr(getattr(client, "me", None), "username", None) or "")
    day_label = _datetime.fromtimestamp(due_cutoff, tz=_SERIES_LAGOS).strftime("%Y-%m-%d")
    messages = format_series_digest_messages(entries, bot_user, day_label)
    ch = s["series_digest_channel"]
    for text in messages:
        while True:
            try:
                await client.send_message(ch, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                break
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
    await ctx.set_setting("series_digest_last_cutoff", due_cutoff)
    return {"published": len(entries), "reason": "sent", "cutoff": due_cutoff}


# ------------------------------------------------------------------ digest control panel (manual, with preview)
DIGEST_CAP_DEFAULT = 15
DIGEST_CAP_MAX = 30
DIGEST_FOOTER_DEFAULT = "📺 Daily Series Updates"
_DIGEST_ACTS = frozenset({"prev", "edit", "pv", "sel", "top", "up", "down", "drop", "bench", "add", "refresh", "send", "skip", "skipok", "cap", "footer"})


def _digest_drafts(ctx) -> dict:
    if not hasattr(ctx, "digest_drafts"):
        ctx.digest_drafts = {}
    return ctx.digest_drafts


def digest_cap(s: dict) -> int:
    try:
        v = int(s.get("series_digest_cap") or DIGEST_CAP_DEFAULT)
    except (TypeError, ValueError):
        v = DIGEST_CAP_DEFAULT
    return max(1, min(DIGEST_CAP_MAX, v))


def digest_footer(s: dict) -> str:
    f = s.get("series_digest_footer")
    return DIGEST_FOOTER_DEFAULT if f is None else str(f)


def _lagos_str(ts: float) -> str:
    return _datetime.fromtimestamp(float(ts), tz=_SERIES_LAGOS).strftime("%b %d, %H:%M")


def digest_summary(records: list, latest_season: int, prev_max_ep: int | None) -> str | None:
    """What a show contributes to the digest: ONLY its newest season, and within it only episodes newer than anything
    the show already had before this window. Files added later to older seasons (back-filling) contribute nothing.
    Returns None when nothing newsworthy is left, so the show is not listed at all."""
    eps: set[int] = set(); pack = False; dated: list[str] = []
    for r in records:
        if str(getattr(r, "ext", "") or "").casefold() in SUB_EXTS:
            continue
        if r.kind == "pack":
            if int(r.season or 0) == int(latest_season):
                pack = True
            continue
        ep_t = getattr(r, "ep_title", "") or ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ep_t):
            dated.append(ep_t)
            continue
        if getattr(r, "special", False) or not (getattr(r, "episodes", ()) or ()):
            continue
        if int(r.season or 0) != int(latest_season):
            continue
        eps.update(int(e) for e in r.episodes)
    if prev_max_ep is not None:
        eps = {e for e in eps if e > int(prev_max_ep)}
    bits = []
    if eps:
        sn = int(latest_season)
        prefix = "Eps" if sn == 0 else (str(sn) if sn >= 1900 else f"S{sn:02d}")
        bits.append(f"{prefix}: {_format_ep_ranges(sorted(eps))}")
    if pack:
        bits.append("Series Pack" if int(latest_season) == 0 else f"S{int(latest_season):02d} Pack")
    if dated:
        u = sorted(set(dated))
        bits.append(", ".join(u[:3]) + (f" (+{len(u) - 3})" if len(u) > 3 else ""))
    return (" · ".join(bits))[:160] if bits else None


async def build_digest_entry(ctx, sid: str, show_doc: dict | None, since: float, until: float) -> dict | None:
    cat = ctx.series_catalog
    recs = await cat.digest_window_files(sid, since, until)
    if not recs:
        return None
    latest = await cat.latest_season(sid)
    latest = int(latest or 0)
    summary = digest_summary(recs, latest, await cat.max_episode_before(sid, latest, since))
    if not summary:
        return None
    show_doc = show_doc or {}
    title = str(show_doc.get("title") or recs[0].show or sid)
    year = show_doc.get("year") if "year" in show_doc else recs[0].year
    return {"sid": sid, "title": title, "year": year, "summary": summary,
            "newest_ts": max(cat.indexed_at_sec(r.indexed_at) for r in recs)}


async def _digest_eligible(ctx, s: dict, pool: dict, shows_map: dict) -> set[str]:
    manual_sids: set[str] = set(); preadd_keys: set[str] = set()
    for entry in get_digest_shows(s):
        manual_sids.add(entry["sid"])
        manual_sids.add(await ctx.series_catalog.resolve_alias(entry["sid"]))
        if entry.get("preadd") and entry.get("year") is None:
            k = core_key(normalize(entry["title"]))
            if k:
                preadd_keys.add(k)
    out: set[str] = set()
    for sid in pool:
        doc = shows_map.get(sid) or {}
        title = str(doc.get("title") or sid)
        eff = _parsed_show_year(title, doc.get("year"))
        if sid in manual_sids or core_key(normalize(title)) in preadd_keys or (eff is not None and eff >= 2026):
            out.add(sid)
    return out


async def build_digest_draft(ctx, since: float, until: float, mode: str = "new", base_sids: list | None = None) -> dict:
    cat = ctx.series_catalog
    s = await ctx.settings()
    cap = digest_cap(s)
    base = [str(x) for x in (base_sids or [])]
    pool = await cat.digest_pool(since, until)
    ids = sorted(set(pool) | set(base))
    shows_map: dict[str, dict] = {}
    for i in range(0, len(ids), 500):
        async for d in cat.shows.find({"_id": {"$in": ids[i:i + 500]}}, {"title": 1, "year": 1, "key": 1}):
            shows_map[str(d["_id"])] = d
    eligible = await _digest_eligible(ctx, s, pool, shows_map)
    entries: dict[str, dict] = {}
    for sid in sorted(eligible | set(base), key=lambda x: -pool.get(x, {}).get("newest", 0)):
        e = await build_digest_entry(ctx, sid, shows_map.get(sid), since, until)
        if e:
            entries[sid] = e
    newest_first = sorted(entries, key=lambda x: (-float(entries[x]["newest_ts"]), str(entries[x]["title"]).lower(), x))
    if mode == "edit":
        order = [x for x in newest_first if x not in base] + [x for x in base if x in entries]
    else:
        order = newest_first
    others = []
    for sid, info in sorted(pool.items(), key=lambda kv: -kv[1]["newest"]):
        if sid in entries or sid in eligible:
            continue
        doc = shows_map.get(sid) or {}
        others.append({"sid": sid, "title": str(doc.get("title") or sid), "year": doc.get("year"), "n": info["n"]})
        if len(others) >= 300:
            break
    return {"mode": mode, "since": float(since), "until": float(until), "cap": cap, "order": order[:cap],
            "dropped": order[cap:], "entries": entries, "others": others, "sel": 0, "shows_map": {k: {"title": v.get("title"), "year": v.get("year")} for k, v in shows_map.items() if k in {o["sid"] for o in others}}}


def format_digest_post(entries: list[dict], bot_username: str, footer: str) -> str:
    """Newest/most important show FIRST (that is what a notification previews); the caption goes at the bottom;
    no date anywhere."""
    bot = (bot_username or "").lstrip("@")

    def render(max_summary: int) -> str:
        blocks = []
        for idx, item in enumerate(entries, 1):
            year = item.get("year")
            title = html.escape(str(item["title"]))
            label = f"{title} ({year})" if year else title
            url = f"https://t.me/{bot}?start=show_{item['sid']}" if bot else None
            head = f"{idx}. <a href=\"{url}\"><b>{label}</b></a>" if url else f"{idx}. <b>{label}</b>"
            blocks.append(f"{head}\n   └ {html.escape(str(item.get('summary') or '')[:max_summary])}")
        text = "\n\n".join(blocks)
        f = (footer or "").strip()
        return text + (f"\n\n{html.escape(f)}" if f else "")

    text = ""
    for ms in (160, 100, 60, 30):
        text = render(ms)
        if len(text) <= 4000:
            return text
    return text[:4000]


def digest_preview_screen(draft: dict, s: dict) -> tuple[str, list]:
    order, entries, sel = draft["order"], draft["entries"], draft.get("sel", 0)
    edit = draft["mode"] == "edit"
    lines = [f"<b>{'✏️ Edit posted digest' if edit else '📝 Digest preview'}</b> · {len(order)}/{draft['cap']} shows", ""]
    window = f"Window: {_lagos_str(draft['since'])} → now ({_lagos_str(draft['until'])} Lagos)"
    lines.append(window)
    if edit:
        lines.append("New shows are marked 🆕. Updating the post moves the cutoff to now: anything indexed after it goes in the next digest.")
    lines.append("")
    posted = set(draft.get("posted") or ())
    for i, sid in enumerate(order):
        e = entries[sid]
        yr = f" ({e['year']})" if e.get("year") else ""
        mark = "▶ " if i == sel else "   "
        new = " 🆕" if edit and sid not in posted else ""
        if len(order) > 10:                                    # long digests: one compact line per show so the preview fits
            lines.append(f"{mark}<b>{i + 1}.</b> {html.escape(str(e['title']))[:34]}{yr}{new} — {html.escape(str(e['summary'])[:40])}")
        else:
            lines.append(f"{mark}<b>{i + 1}.</b> {html.escape(str(e['title']))}{yr}{new}\n      └ {html.escape(str(e['summary']))}")
    if not order:
        lines.append("<i>Nothing selected. Use ➕ to add shows.</i>")
    footer = digest_footer(s).strip()
    lines += ["", f"Footer (bottom of post): {html.escape(footer) if footer else '<i>none</i>'}"]
    rows: list = []
    nums = [Btn(("•" if i == sel else "") + str(i + 1), f"sdg:sel:{i}") for i in range(len(order))]
    for i in range(0, len(nums), 5):
        rows.append(nums[i:i + 5])
    if order:
        rows.append([Btn("⏫ Top", "sdg:top"), Btn("🔼 Up", "sdg:up"), Btn("🔽 Down", "sdg:down"), Btn("❌ Drop", "sdg:drop")])
    bench_n = len(draft.get("dropped") or ()) + len(draft.get("others") or ())
    rows.append([Btn(f"➕ Add / swap in ({bench_n})", "sdg:bench:0"), Btn("♻️ Refresh", "sdg:refresh")])
    rows.append([Btn("✅ Update the post" if edit else "✅ Send to channel", "sdg:send")])
    rows.append([Btn("⏭ Skip this window", "sdg:skip"), Btn("‹ Digest", "sdg:menu")])
    text = "\n".join(lines)
    while len(text) > 3900 and len(lines) > 6:             # never cut inside a tag: drop whole lines from the middle
        lines.pop(len(lines) // 2)
        text = "\n".join(lines)
    return text, rows


def digest_bench_screen(draft: dict, page: int) -> tuple[str, list]:
    items = []
    for sid in draft.get("dropped") or ():
        e = draft["entries"].get(sid)
        if e:
            items.append((sid, f"{e['title']} · {e['summary']}", "dropped"))
    for o in draft.get("others") or ():
        items.append((o["sid"], f"{o['title']} · {o['n']} new file{'s' if o['n'] != 1 else ''}", "other"))
    per = 8
    pages = max(1, -(-len(items) // per)); page = max(0, min(page, pages - 1))
    full = len(draft["order"]) >= draft["cap"]
    lines = ["<b>➕ Add a show to the digest</b>", "",
             ("The digest is full: the show you pick REPLACES the selected one (▶), which moves back here." if full
              else "The show you pick is added at the TOP. Move it with the arrows afterwards."),
             "Shows flagged ↩ were dropped from this digest; the others are new shows I did not select by default.", ""]
    rows = []
    for sid, label, kind in items[page * per:(page + 1) * per]:
        rows.append([Btn(("↩ " if kind == "dropped" else "") + label[:52], f"sdg:add:{sid}")])
    if not items:
        lines.append("<i>No other shows have new files in this window.</i>")
    nav = []
    if page > 0: nav.append(Btn("‹ Prev", f"sdg:bench:{page - 1}"))
    if page < pages - 1: nav.append(Btn("Next ›", f"sdg:bench:{page + 1}"))
    if nav: rows.append(nav)
    rows.append([Btn("‹ Back to preview", "sdg:pv")])
    lines.append(f"Page {page + 1}/{pages}")
    return "\n".join(lines), rows


async def _digest_bot_username(ctx, client) -> str:
    return getattr(ctx, "bot_username", "") or (getattr(getattr(client, "me", None), "username", None) or "")


async def digest_publish(ctx, client, draft: dict) -> tuple[bool, str]:
    """Send (new digest) or update (edit of the last post). The cutoff moves to the draft's `until` either way, so
    anything indexed after it belongs to the NEXT digest."""
    s = await ctx.settings()
    ch = s.get("series_digest_channel")
    if not ch:
        return False, "Set an update channel first."
    order = [x for x in draft["order"] if x in draft["entries"]]
    if not order:
        return False, "Nothing selected: add at least one show."
    text = format_digest_post([draft["entries"][x] for x in order], await _digest_bot_username(ctx, client), digest_footer(s))
    post = s.get("series_digest_post") or {}
    try:
        if draft["mode"] == "edit":
            if not post.get("message_id"):
                return False, "There is no earlier post to edit."
            try:
                await client.edit_message_text(post.get("channel") or ch, int(post["message_id"]), text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception as e:
                if "MESSAGE_NOT_MODIFIED" not in str(e).upper() and e.__class__.__name__ != "MessageNotModified":
                    raise
            new_post = {**post, "sids": order, "until": draft["until"], "edited_at": time.time()}
        else:
            while True:
                try:
                    msg = await client.send_message(ch, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                    break
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
            new_post = {"channel": ch, "message_id": int(getattr(msg, "id", 0) or getattr(msg, "message_id", 0)),
                        "since": draft["since"], "until": draft["until"], "sids": order, "sent_at": time.time()}
    except Exception as e:
        return False, f"Telegram refused it: {str(e)[:160]}"
    await ctx.set_setting("series_digest_post", new_post)
    await ctx.set_setting("series_digest_last_cutoff", draft["until"])
    return True, f"{len(order)} show{'s' if len(order) != 1 else ''}"


async def digest_apply_text_setting(ctx, action: str, text: str) -> str:
    text = (text or "").strip()
    if action == "cap":
        if not text.isdigit() or not (1 <= int(text) <= DIGEST_CAP_MAX):
            return f"Please send a number from 1 to {DIGEST_CAP_MAX}."
        await ctx.set_setting("series_digest_cap", int(text))
        return f"✅ The digest now holds at most {int(text)} shows."
    if action == "footer":
        if text.lower() in ("none", "off", "-", "clear"):
            await ctx.set_setting("series_digest_footer", "")
            return "✅ No footer will be added to the post."
        await ctx.set_setting("series_digest_footer", text[:120])
        return "✅ Footer saved. It is placed at the bottom of the post."
    return "Unknown setting."


async def digest_panel_cb(ctx, client, cb, show, parts: list[str]) -> None:
    import ui
    uid = cb.from_user.id
    act = parts[0]
    drafts = _digest_drafts(ctx)
    s = await ctx.settings()

    async def render_preview(note: str | None = None):
        d = drafts.get(uid)
        t, rows = digest_preview_screen(d, await ctx.settings())
        await show(t, rows)
        await cb.answer(note) if note else await cb.answer()

    if act in ("prev", "edit", "refresh"):
        if not getattr(ctx, "series_catalog", None):
            await cb.answer("The catalogue is not ready.", show_alert=True); return
        now = time.time()
        old = drafts.get(uid)
        mode = old["mode"] if (act == "refresh" and old) else ("edit" if act == "edit" else "new")
        post = s.get("series_digest_post") or {}
        if mode == "edit":
            if not post.get("message_id"):
                await cb.answer("No digest has been sent yet, so there is nothing to edit.", show_alert=True); return
            since, base = float(post.get("since") or 0), list(post.get("sids") or ())
        else:
            last = s.get("series_digest_last_cutoff")
            since, base = (float(last) if last is not None else series_latest_due_cutoff()), None
        await cb.answer("Building the preview…")
        d = await build_digest_draft(ctx, since, now, mode, base)
        d["posted"] = list(base or ())
        drafts[uid] = d
        if not d["order"]:
            msg = ("No shows have newest-season episodes since the last digest." if mode == "new"
                   else "Every show in the post was dropped.")
            await show(f"<b>📝 Digest preview</b>\n\n{msg}\nWindow: {_lagos_str(since)} → now.\n\nIf these files are only older-season back-fill, you can skip this window.",
                       [[Btn("➕ Add a show", "sdg:bench:0")], [Btn("⏭ Skip this window", "sdg:skip"), Btn("‹ Digest", "sdg:menu")]])
            return
        t, rows = digest_preview_screen(d, s)
        await show(t, rows)
        return

    d = drafts.get(uid)
    if act in ("cap", "footer"):
        ctx.pending_digest_shows[uid] = {"action": act, "at": time.monotonic()}
        prompt = (f"<b>Digest size</b>\nSend the maximum number of shows per digest (1–{DIGEST_CAP_MAX}). Now: <b>{digest_cap(s)}</b>."
                  if act == "cap" else
                  "<b>Digest footer</b>\nSend the line to show at the BOTTOM of every digest post (no date is added).\n"
                  f"Now: <b>{html.escape(digest_footer(s)) or 'none'}</b>\nSend <code>none</code> to remove it.")
        await show(prompt, [[Btn("Cancel", "sdg:menu")]])
        await cb.answer(); return
    if act in ("skip", "skipok"):
        if not d:
            await cb.answer("This preview expired. Open it again.", show_alert=True); return
        if act == "skip":
            await show("<b>Skip this window?</b>\n\nNothing is posted. The cutoff moves to now, so the files indexed so far will NOT appear in any digest.",
                       [[Btn("Yes, skip it", "sdg:skipok")], [Btn("‹ Back to preview", "sdg:pv")]])
            await cb.answer(); return
        await ctx.set_setting("series_digest_last_cutoff", d["until"])
        drafts.pop(uid, None)
        t, rows = ui.series_digest_settings_screen(await ctx.settings())
        await show(t, rows)
        await cb.answer("Window skipped."); return
    if not d:
        await cb.answer("This preview expired. Open it again from the digest menu.", show_alert=True); return
    order = d["order"]
    if act == "pv":
        await render_preview(); return
    if act == "sel":
        i = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        d["sel"] = max(0, min(i, len(order) - 1)) if order else 0
        await render_preview(); return
    if act in ("top", "up", "down", "drop"):
        i = d.get("sel", 0)
        if not order or i >= len(order):
            await cb.answer(); return
        if act == "top" and i > 0:
            order.insert(0, order.pop(i)); d["sel"] = 0
        elif act == "up" and i > 0:
            order[i - 1], order[i] = order[i], order[i - 1]; d["sel"] = i - 1
        elif act == "down" and i < len(order) - 1:
            order[i + 1], order[i] = order[i], order[i + 1]; d["sel"] = i + 1
        elif act == "drop":
            d.setdefault("dropped", []).insert(0, order.pop(i)); d["sel"] = max(0, min(i, len(order) - 1))
        await render_preview(); return
    if act == "bench":
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        t, rows = digest_bench_screen(d, page)
        await show(t, rows); await cb.answer(); return
    if act == "add":
        sid = parts[1] if len(parts) > 1 else ""
        if sid in order:
            await cb.answer("Already in the digest."); return
        e = d["entries"].get(sid)
        if e is None:
            meta = d.get("shows_map", {}).get(sid) or {}
            e = await build_digest_entry(ctx, sid, meta, d["since"], d["until"])
            if e is None:
                await cb.answer("That show only has older-season back-fill in this window, so it has no newest episode to list.", show_alert=True); return
            d["entries"][sid] = e
        if sid in d.get("dropped", []):
            d["dropped"].remove(sid)
        d["others"] = [o for o in d.get("others", []) if o["sid"] != sid]
        if len(order) >= d["cap"] and order:
            i = min(d.get("sel", 0), len(order) - 1)
            d.setdefault("dropped", []).insert(0, order[i]); order[i] = sid; d["sel"] = i
        else:
            order.insert(0, sid); d["sel"] = 0
        await render_preview("Added."); return
    if act == "send":
        ok, info = await digest_publish(ctx, client, d)
        if not ok:
            await cb.answer(info, show_alert=True); return
        drafts.pop(uid, None)
        edited = d["mode"] == "edit"
        await show(f"✅ <b>{'Post updated' if edited else 'Digest sent'}</b> · {info}.\n\nCutoff is now <b>{_lagos_str(d['until'])}</b> (Lagos). Anything indexed after this goes in the next digest.",
                   [[Btn("✏️ Edit this post", "sdg:edit")], [Btn("‹ Digest", "sdg:menu")]])
        await cb.answer("Done."); return
    await cb.answer()


async def configure_series_digest_channel(ctx, client, m, raw_target) -> bool:
    target = raw_target
    if isinstance(target, str):
        val = target.strip()
        m_link = re.search(r"t\.me/([A-Za-z0-9_]{4,})$", val)
        if m_link:
            val = "@" + m_link.group(1)
        elif re.fullmatch(r"-?\d+", val):
            val = int(val)
        elif not val.startswith("@") and re.fullmatch(r"[A-Za-z0-9_]{4,}", val):
            val = "@" + val
        target = val
    try:
        chat = await client.get_chat(target)
        await client.send_message(chat.id, "✅ Daily SeriesBot updates channel verified.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await m.reply_text(
            f"I couldn't post to that channel ({_e(str(e))}). "
            "Make sure I'm added there as an admin with permission to post messages, then try again."
        )
        return False
    title = getattr(chat, "title", None) or (f"@{chat.username}" if getattr(chat, "username", None) else str(chat.id))
    await ctx.set_setting("series_digest_channel", chat.id)
    await ctx.set_setting("series_digest_channel_title", title)
    await ctx.set_setting("series_digest_enabled", True)
    s = await ctx.settings()
    if s.get("series_digest_last_cutoff") is None:
        await ctx.set_setting("series_digest_last_cutoff", series_latest_due_cutoff())
    return True


async def series_digest_cb(ctx, client, cb, show, arg: str) -> None:
    import ui
    uid = cb.from_user.id
    parts = arg.split(":")
    act = parts[0]
    if act in _DIGEST_ACTS:
        await digest_panel_cb(ctx, client, cb, show, parts)
        return
    if act == "menu":
        ctx.pending_digest_channel.discard(uid)
        ctx.pending_digest_shows.pop(uid, None)
        t, rows = ui.series_digest_settings_screen(await ctx.settings())
        await show(t, rows)
        await cb.answer()
        return
    if act == "cancelflow":
        ctx.pending_digest_channel.discard(uid)
        ctx.pending_digest_shows.pop(uid, None)
        t, rows = await digest_shows_menu_screen(ctx, 0)
        await show(t, rows)
        await cb.answer("Cancelled.")
        return
    if act == "tog":
        s = await ctx.settings()
        new_val = not bool(s.get("series_digest_enabled"))
        if new_val and not s.get("series_digest_channel"):
            await cb.answer("Set an update channel first.", show_alert=True)
            return
        await ctx.set_setting("series_digest_enabled", new_val)
        if new_val and s.get("series_digest_last_cutoff") is None:
            await ctx.set_setting("series_digest_last_cutoff", series_latest_due_cutoff())
        t, rows = ui.series_digest_settings_screen(await ctx.settings())
        await show(t, rows)
        await cb.answer(f"Daily series updates {'enabled' if new_val else 'disabled'}.")
        return
    if act == "setch":
        ctx.pending_digest_shows.pop(uid, None)
        ctx.pending_digest_channel.add(uid)
        await show(
            "<b>Set Series Update Channel</b>\n\n"
            "Forward any post from the target channel here, or send its channel ID (<code>-100...</code>) "
            "or <code>@username</code>.\nMake sure I'm an admin with post permission there.",
            [[Btn("Cancel", "sdg:menu")]],
        )
        await cb.answer()
        return
    if act == "test":
        s = await ctx.settings()
        ch = s.get("series_digest_channel")
        if not ch:
            await cb.answer("Set an update channel first.", show_alert=True)
            return
        try:
            await client.send_message(ch, "✅ Test message from SeriesBot Daily Updates (10:10 AM Africa/Lagos).", parse_mode=ParseMode.HTML)
        except Exception as e:
            await cb.answer(f"Test failed: {str(e)[:120]}", show_alert=True)
            return
        await cb.answer("Test post sent to the update channel.", show_alert=True)
        return
    if act == "shows":
        ctx.pending_digest_shows.pop(uid, None)
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        t, rows = await digest_shows_menu_screen(ctx, page)
        await show(t, rows)
        await cb.answer()
        return
    if act == "addsearch":
        ctx.pending_digest_shows[uid] = {"action": "search", "at": time.monotonic()}
        await show(
            "<b>Search & Add Digest Show</b>\nSend a show title to search:",
            [[Btn("Cancel", "sdg:cancelflow")]],
        )
        await cb.answer()
        return
    if act == "preadd":
        ctx.pending_digest_shows[uid] = {"action": "preadd", "at": time.monotonic()}
        await show(
            "<b>Pre-add Digest Show</b>\nSend the exact show title, with optional release year in parentheses.\n"
            "Examples:\n<code>Severance</code>\n<code>Dune Prophecy (2026)</code>",
            [[Btn("Cancel", "sdg:cancelflow")]],
        )
        await cb.answer()
        return
    if act == "togshow" and len(parts) >= 2:
        sid = parts[1]
        s = await ctx.settings()
        shows = get_digest_shows(s)
        existing = next((x for x in shows if x["sid"] == sid), None)
        if existing:
            shows = [x for x in shows if x["sid"] != sid]
            toast = "Removed from Digest Shows."
        else:
            d = await ctx.series_catalog.get_show(sid)
            shows.append({
                "sid": sid,
                "title": d.get("title", "Series") if d else sid,
                "year": d.get("year") if d else None,
                "added_at": time.time(),
                "preadd": False,
            })
            toast = "Added to Digest Shows."
        await save_digest_shows(ctx, shows)
        pend = ctx.pending_digest_shows.get(uid)
        if pend and pend.get("query"):
            t, rows = await digest_shows_search_screen(ctx, pend["query"])
        else:
            t, rows = await digest_shows_menu_screen(ctx, 0)
        await show(t, rows)
        await cb.answer(toast)
        return
    if act == "rmshow" and len(parts) >= 2:
        sid = parts[1]
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        s = await ctx.settings()
        shows = [x for x in get_digest_shows(s) if x["sid"] != sid]
        await save_digest_shows(ctx, shows)
        t, rows = await digest_shows_menu_screen(ctx, page)
        await show(t, rows)
        await cb.answer("Removed from Digest Shows.")
        return
    await cb.answer()
