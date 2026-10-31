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
def _request_key(text):
    raw=core_key(normalize(text)) or str(text).lower()
    return raw[:40].rstrip()+"-"+hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
def _season_label(n): return "Episodes / unnumbered" if n == 0 else f"Season {n}"
def _ep_label(f):
    if f.kind == "pack": return f"{_season_label(f.season)} · complete pack"
    if not f.episodes: return "Episode"
    nums=f.episodes
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
    bits.extend(x for x in f.tags if x)
    if f.group:
        bits.append(f.group)
    if getattr(f,"ep_title",""):
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
    wanted=core_key(normalize(search_query))
    exact=[d for d in candidates if core_key(normalize(d.get("title", "")))==wanted]
    chosen=exact[0] if len(exact)==1 else (candidates[0] if total==1 and candidates else None)
    if chosen and (total==1 or target_season is not None or target_episode is not None or (wanted and wanted==core_key(normalize(chosen.get("title", ""))))):
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
            t,r=await season_screen(ctx,sid,target_season,uid)
        else:
            t,r=await show_screen(ctx,sid,uid=uid)
        sent=await show(t,r)
        if sent:
            now=time.monotonic(); ctx.series_sessions={k:v for k,v in ctx.series_sessions.items() if v.get("until",0)>now}
            if len(ctx.series_sessions)>=100: ctx.series_sessions.clear()
            ctx.series_sessions[(sent.chat.id,sent.id)]={"origin":"direct","until":now+1800}
        return
    text,buttons,total=await results_screen(ctx,search_query,0,target_season,target_episode)
    sent=await show(text,buttons)
    if sent:
        now=time.monotonic()
        ctx.series_sessions={k:v for k,v in ctx.series_sessions.items() if v.get("until",0)>now}
        if len(ctx.series_sessions)>=100: ctx.series_sessions.clear()
        all_rows=await catalog.search_shows(search_query,min(total,300),0)
        snapshot=[{"_id":str(d.get("_id",d.get("sid"))),"title":d.get("title","Series"),"year":d.get("year"),"count":int(d.get("count",0))} for d in all_rows]
        ctx.series_sessions[(sent.chat.id,sent.id)]={"origin":"results","query":search_query[:100],"page":0,"target_season":target_season,"target_episode":target_episode,"rows":snapshot,"total":total,"until":now+1800}
        ctx.watch(sent.chat.id,sent.id,30)

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
    seasons = sorted({f.season for f in files if f.season > 0})
    if not seasons and any(f.season == 0 and f.episodes for f in files):
        return [1]
    return seasons

async def show_screen(ctx, sid, page=0, uid=None, back_results=False):
    files=await _show_files(ctx,sid)
    if not files:return "This series has no indexed files.",[[Btn("Back to search","ss:back")]]
    show=files[0].show; year=files[0].year
    seasons=_display_season_numbers(files)
    if len(seasons)==1:
        return await season_screen(ctx,sid,seasons[0],uid,back_results=back_results)
    total=max(1,(len(seasons)+19)//20); page=max(0,min(page,total-1)); part=seasons[page*20:(page+1)*20]
    rows=[]
    for i in range(0,len(part),2):
        pair=[]
        for n in part[i:i+2]:
            fs=[f for f in files if (f.season==n or (n==1 and f.season==0 and not any(x.season>0 for x in files))) and f.kind=="episode"]
            eps={e for f in fs for e in f.episodes}
            pair.append(Btn(f"Season {n} · {len(eps)} eps",f"ss:season:{sid}:{n}"))
        rows.append(pair)
    if total>1:
        nav=[Btn("« first",f"ss:shows:{sid}:0"),Btn("‹",f"ss:shows:{sid}:{max(0,page-1)}"),Btn(f"{page+1}/{total}","noop"),Btn("›",f"ss:shows:{sid}:{min(total-1,page+1)}"),Btn("» last",f"ss:shows:{sid}:{total-1}")]
        rows.append(nav)
    packs=[f for f in files if f.kind=="pack"]
    if packs: rows.append([Btn(f"Season / series packs · {len(packs)}",f"ss:packs:{sid}")])
    if uid is not None:
        prog=await ctx.series_catalog.get_progress(uid,sid)
        if prog: rows.insert(0,[Btn(f"Continue: E{int(prog['episode']):02d}",f"ss:continue:{sid}")])
    # Only gaps between indexed seasons are requestable.
    if seasons:
        gaps=[n for n in range(seasons[0],seasons[-1]+1) if n not in set(seasons)]
        for n in gaps[:8]: rows.append([Btn(f"Request missing season {n}",f"ss:reqseason:{sid}:{n}")])
    rows.append([Btn("🔔 Follow this show" if not (uid is not None and sid in await ctx.series_catalog.watchlist(uid)) else "✅ Following · tap to unfollow","ss:save:"+sid)])
    if back_results: rows.append([Btn("‹ Back to results","ss:backresults")])
    rows.append([Btn("Close","ss:close")])
    return f"<b>{_e(show)}{f' ({year})' if year else ''}</b>\nChoose a season · {len(seasons)} seasons:",rows

async def season_screen(ctx,sid,season,uid=None,back_results=False):
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
        t,r=await resolution_screen(ctx,sid,season,ordered_res[0] or "unknown",0,back_results=back_results)
        if uid is not None:
            prog=await ctx.series_catalog.get_progress(uid,sid)
            if prog and (int(prog.get("season",-1))==int(season) or (int(season)==1 and int(prog.get("season",-1))==0)):
                r.insert(0,[Btn(f"Continue: E{int(prog['episode']):02d}",f"ss:continue:{sid}")])
            following=sid in await ctx.series_catalog.watchlist(uid)
            r.append([Btn("✅ Following · tap to unfollow" if following else "🔔 Follow this show",f"ss:save:{sid}")])
        for f in sorted(packs,key=_index_order,reverse=True)[:8]: r.append([Btn(f"Pack · {_file_label(f)}"[:60],f"ss:file:{f.uid}")])
        specials=[f for f in files if getattr(f,"special",False)]
        if specials: r.append([Btn(f"Specials / OVA · {len(specials)}",f"ss:specials:{sid}:{season}")])
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
    subs=[f for f in files if (f.ext or "").lower() in SUB_EXTS]
    if subs: rows.append([Btn(f"Subtitles · {len(subs)} files",f"ss:subs:{sid}:{season}")])
    if uid is not None:
        following=sid in await ctx.series_catalog.watchlist(uid)
        rows.append([Btn("✅ Following · tap to unfollow" if following else "🔔 Follow this show",f"ss:save:{sid}")])
    if uid is not None and ctx.is_admin(uid): rows.append([Btn("Admin: inspect/remove a file",f"ss:adminfiles:{sid}:{season}")])
    show_seasons=_display_season_numbers(await _show_files(ctx,sid))
    if back_results: rows.append([Btn("‹ Back to results","ss:backresults")])
    elif len(show_seasons)>1: rows.append([Btn("Back to series",f"ss:backshow:{sid}")])
    rows.append([Btn("Close","ss:close")])
    title=files[0].show if files else "Series"; episode_count=len({e for f in videos for e in f.episodes})
    return f"<b>{_e(title)} · {_e(_season_label(season))}</b>\n{episode_count} episodes indexed. Choose a resolution:",rows

async def _resolution_items(ctx,sid,season,res):
    files=await _show_files(ctx,sid,season); wanted=None if res=="unknown" else res
    videos=[f for f in files if f.kind!="pack" and f.episodes and (f.ext or "").lower() not in SUB_EXTS and not getattr(f,"special",False)]
    by_ep={}
    for f in videos:
        for ep in f.episodes: by_ep.setdefault(ep,[]).append(f)
    all_eps=list(range(min(by_ep),max(by_ep)+1)) if by_ep else []
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

async def resolution_screen(ctx,sid,season,res,page=0,lo=None,hi=None,back_results=False):
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
        nums=sorted(set(f.episodes)); ep_label=(f"E{nums[0]:02d}" if len(nums)==1 else f"E{nums[0]:02d}–E{nums[-1]:02d}")
        label=f"{ep_label} · {_file_label(f)}"
        rows.append([Btn(label[:60],f"ss:file:{f.uid}")])
    unique_eps={e for f,_ in items for e in f.episodes}
    if len(unique_eps)>50 and not ascending:
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
        if len(unique_eps)>50: nav_buttons.append(Btn("Go to episode",f"ss:goto:{sid}:{season}:{res}:0"))
        nav_buttons.append(Btn("‹ Latest episodes",f"ss:res:{sid}:{season}:{res}"))
        rows.append(nav_buttons)
    if gaps and not ascending: rows.append([Btn("Find missing episodes",f"ss:missing:{sid}:{season}:{res}")])
    available_res={f.resolution or "unknown" for f in files if f.kind!="pack" and f.episodes and (f.ext or "").lower() not in SUB_EXTS and not getattr(f,"special",False)}
    all_show_files=await _show_files(ctx,sid); show_seasons=_display_season_numbers(all_show_files)
    if back_results:
        rows.append([Btn("‹ Back to results","ss:backresults")])
    elif len(available_res)>1:
        # Return to the resolution chooser for this season, not the show/season list.
        rows.append([Btn("‹ Back to resolutions",f"ss:season:{sid}:{season}")])
    elif len(show_seasons)>1:
        rows.append([Btn("‹ Back to seasons",f"ss:show:{sid}")])
    else:
        rows.append([Btn("Close","ss:close")])
    title=files[0].show if files else "Series"; label=res if res!="unknown" else "Unknown resolution"
    return f"<b>{_e(title)} · {_e(_season_label(season))} · {_e(label)}</b>\n{total} episode file(s) · showing {start+1}-{min(total,start+len(view))}.",rows

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
    return f"<b>{_e(title)} · Season {season} · {_e(label)}</b>\nGo to episode · Choose a starting episode:",rows


async def missing_screen(ctx,sid,season,res):
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
    rows.append([Btn("Back",f"ss:res:{sid}:{season}:{res}")])
    title=files[0].show if files else "Series"
    return f"<b>{_e(title)} · Season {season} · {_e(res)}</b>\nMissing episodes · {len(missing)} unavailable",rows


async def episode_screen(ctx,sid,season,episode,back_results=False):
    files=await _show_files(ctx,sid,season) if season else await _show_files(ctx,sid)
    versions=sorted([f for f in files if f.kind!="pack" and episode in f.episodes and (f.ext or "").lower() not in SUB_EXTS and not getattr(f,"special",False)],key=_index_order,reverse=True)
    actual_seasons=sorted({f.season for f in versions})
    known_seasons=sorted({f.season for f in files if f.season>0})
    if season==0 and len(actual_seasons)==1: season=actual_seasons[0]
    elif season==0 and not actual_seasons and len(known_seasons)==1: season=known_seasons[0]
    rows=[]
    for f in versions:
        nums=sorted(set(f.episodes)); ep_label=(f"E{nums[0]:02d}" if len(nums)==1 else f"E{nums[0]:02d}–E{nums[-1]:02d}")
        rows.append([Btn(f"{ep_label} · {_file_label(f)}"[:60],f"ss:file:{f.uid}")])
    if not versions: rows.append([Btn("Request this episode",f"ss:reqep:{sid}:{season}:{episode}")])
    if back_results:
        rows.append([Btn("‹ Back to results","ss:backresults")])
    else:
        # Reconstruct the visible parent from the actual season structure. A hidden
        # one-resolution step is skipped, so its parent is the season picker.
        res_values=sorted({f.resolution or "unknown" for f in files if f.kind!="pack" and f.episodes and (f.ext or "").lower() not in SUB_EXTS and not getattr(f,"special",False)})
        show_seasons=_display_season_numbers(await _show_files(ctx,sid))
        if len(res_values)>1:
            rows.append([Btn("‹ Back to resolutions",f"ss:season:{sid}:{season}")])
        elif len(show_seasons)>1:
            rows.append([Btn("‹ Back to seasons",f"ss:show:{sid}")])
        else:
            rows.append([Btn("Close","ss:close")])
    title=files[0].show if files else "Series"
    return f"<b>{_e(title)} · Season {season} · E{episode:02d}</b>",rows

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
    return f"<b>{_e(title)} · Season {season} · Specials / OVA</b>\n{len(specials)} special file(s)",rows

async def subtitles_screen(ctx,sid,season,page=0):
    files=await _show_files(ctx,sid,season)
    subs=[f for f in files if (f.ext or "").lower() in SUB_EXTS]
    subs.sort(key=lambda f:((min(f.episodes) if f.episodes else 0),_index_order(f)),reverse=True)
    total=len(subs); pages=max(1,(total+_PAGE_SIZE-1)//_PAGE_SIZE); start=max(0,min(int(page),max(0,total-1))); page=start//_PAGE_SIZE; last_start=((total-1)//_PAGE_SIZE)*_PAGE_SIZE
    rows=[]
    for f in subs[start:start+_PAGE_SIZE]:
        nums=sorted(set(f.episodes)); ep_label=(f"E{nums[0]:02d}" if len(nums)==1 else f"E{nums[0]:02d}–E{nums[-1]:02d}") if nums else "Subtitle"
        rows.append([Btn(f"{ep_label} · {_file_label(f)}"[:60],f"ss:file:{f.uid}")])
    if pages>1: rows.append([Btn("« first",f"ss:subspage:{sid}:{season}:0"),Btn("‹",f"ss:subspage:{sid}:{season}:{max(0,start-_PAGE_SIZE)}"),Btn(f"{start//_PAGE_SIZE+1}/{pages}","noop"),Btn("›",f"ss:subspage:{sid}:{season}:{start+_PAGE_SIZE}" if start+_PAGE_SIZE<total else "noop"),Btn("» last",f"ss:subspage:{sid}:{season}:{last_start}")])
    rows.append([Btn("‹ Back to season",f"ss:season:{sid}:{season}")])
    title=files[0].show if files else "Series"
    return f"<b>{_e(title)} · Season {season} · Subtitles</b>\n{total} file(s)",rows

async def manual_merge_search(ctx, show, uid, query, step):
    rows=await ctx.series_catalog.search_shows(query,10,0)
    buttons=[]
    for d in rows:
        sid=str(d.get("_id",d.get("sid")))
        buttons.append([Btn(f"{d.get('title','Series')} · {int(d.get('count',0))} files"[:60],f"ss:mergepick:{step}:{sid}")])
    if not buttons:
        await show("No matching series found. Try another search, or /mergeshow to restart.")
        return
    buttons.append([Btn("Cancel","ss:mergecancel")])
    label="wrong show" if step=="old" else "real show"
    await show(f"Choose the {label} to merge:",buttons)


async def discover_screen(ctx,which,uid):
    if which=="new": rows=await ctx.series_catalog.recent_shows(12); title="Recently added series"
    elif which=="picks":
        settings=await ctx.settings(); rows=[]
        for sid in settings.get("series_picks",[])[:30]:
            d=await ctx.series_catalog.get_show(sid)
            if d: rows.append(d)
        title=settings.get("series_picks_name") or "Editor picks"
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
    if not buttons: return f"<b>{title}</b>\nNothing here yet. Follow a show from its page.",[[Btn("‹ Discover","ss:discover:menu")]]
    buttons.append([Btn("‹ Discover","ss:discover:menu"),Btn("Close","ss:close")])
    return f"<b>{title}</b>",buttons

async def deliver_file(ctx, client, chat_id: int, f):
    """Copy an indexed file; preserve its original filename prominently in the caption."""
    settings=await ctx.settings()
    minutes=int(settings.get("autodelete_min",0) or 0)
    raw_name=f.name or f"{f.show} {_ep_label(f)}"
    caption=f"<code>{_e(raw_name)}</code>\n\nJoin <a href=\"https://t.me/exivum\">@exivum</a>"
    if len(caption)>1024:
        caption=caption[:1021]+"…"
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

async def callback(ctx,client,cb,head,arg):
    msg=cb.message; uid=cb.from_user.id
    async def edit(text,rows=None):
        try:
            # Series screens may originate from the photo-based welcome page.
            # edit_of handles both text messages and photo captions; edit_text
            # alone fails on photos, leaving every button on Discover inert.
            await edit_of(msg)(text, rows)
            ttl=int((await ctx.settings()).get("result_ttl_min",4) or 4)
            state=getattr(ctx,"series_sessions",{}).get((msg.chat.id,msg.id))
            if state and state.get("until",0)>time.monotonic(): ttl=30
            ctx.watch(msg.chat.id,msg.id,ttl)
        except Exception:
            log.exception("series screen edit failed")
    try:
        if head=="noop": await cb.answer(); return True
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
        if head=="discover":
            if arg=="menu":
                settings=await ctx.settings()
                rows=[[Btn("Popular series","ss:discover:popular"),Btn("Recently added","ss:discover:new")],[Btn("My List","ss:discover:mylist")]]
                if settings.get("series_picks"): rows.append([Btn(settings.get("series_picks_name") or "Editor picks","ss:discover:picks"),Btn("Surprise me","ss:surprise")])
                rows.append([Btn("Close","ss:close")])
                await edit("<b>Discover series</b>\nBrowse the catalogue.",rows)
            else:
                t,r=await discover_screen(ctx,arg,uid); await edit(t,r)
            await cb.answer(); return True
        if head=="surprise":
            import random
            settings=await ctx.settings(); sids=settings.get("series_picks",[])
            if not sids: await cb.answer("No editor picks have been configured yet.",show_alert=True); return True
            sid=random.choice(sids); t,r=await show_screen(ctx,sid,uid=uid); await edit(t,r); await cb.answer(); return True
        if head=="save":
            sids=await ctx.series_catalog.watchlist(uid)
            if arg in sids: sids.remove(arg); msgtext="Removed from My List."
            else: sids.append(arg); msgtext="Added to My List."
            await ctx.series_catalog.save_watchlist(uid,sids)
            st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await show_screen(ctx,arg,uid=uid,back_results=st.get("origin")=="results")
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
        if head=="mergepick":
            if not ctx.is_admin(uid): await cb.answer("Admins only.",show_alert=True); return True
            step,sid=arg.split(":",1); state=getattr(ctx,"pending_series_merge",{}).get(uid)
            d=await ctx.series_catalog.get_show(sid)
            if not state or not d: await cb.answer("Merge selection expired.",show_alert=True); return True
            if step=="old":
                state["old"]={"sid":sid,"title":d.get("title","Series"),"count":int(d.get("count",0))}; state["step"]="new"
                await edit(f"Wrong show selected: <b>{_e(d.get('title'))}</b> ({int(d.get('count',0))} files).\nNow send a search for the real show.",[[Btn("Cancel","ss:mergecancel")]])
            else:
                state["new"]={"sid":sid,"title":d.get("title","Series"),"year":d.get("year"),"count":int(d.get("count",0))}; state["step"]="confirm"
                old=state.get("old",{})
                await edit(f"<b>Confirm show merge</b>\n\nMove {old.get('count',0)} files from <b>{_e(old.get('title'))}</b> into <b>{_e(d.get('title'))}</b> ({int(d.get('count',0))} existing files)?\nThe real show's title and year will be kept.",[[Btn("Confirm merge",f"ss:mergeconfirm:{old.get('sid')}:{sid}")],[Btn("Cancel","ss:mergecancel")]])
            await cb.answer(); return True
        if head=="mergeconfirm":
            if not ctx.is_admin(uid): await cb.answer("Admins only.",show_alert=True); return True
            oldsid,newsid=arg.split(":",1); state=getattr(ctx,"pending_series_merge",{}).get(uid)
            real=await ctx.series_catalog.get_show(newsid)
            if not state or not real or state.get("old",{}).get("sid")!=oldsid: await cb.answer("Merge confirmation expired.",show_alert=True); return True
            moved=await ctx.series_catalog.merge_show_ids(newsid,[oldsid],real.get("title","Series"),real.get("year"))
            ctx.pending_series_merge.pop(uid,None)
            await edit(f"Merged <b>{_e(state['old']['title'])}</b> into <b>{_e(real.get('title'))}</b>.\nFiles moved: {moved}.",[[Btn("Close","ss:close")]])
            await cb.answer("Shows merged."); return True
        if head=="mergecancel":
            getattr(ctx,"pending_series_merge",{}).pop(uid,None); await edit("Show merge cancelled.",[[Btn("Close","ss:close")]]); await cb.answer(); return True
        if head=="show":
            st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); origin=st.get("origin","direct")
            t,r=await show_screen(ctx,arg,uid=uid,back_results=(origin=="results")); await edit(t,r); await cb.answer(); return True
        if head=="backshow":
            sid=arg; st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); origin=st.get("origin","direct")
            if origin=="results" and st.get("until",0)>=time.monotonic():
                t,r,_=await results_screen(ctx,st["query"],st.get("page",0),st.get("target_season"),st.get("target_episode"),st.get("rows"),st.get("total")); await edit(t,r)
            else:
                t,r=await show_screen(ctx,sid,uid=uid,back_results=False); await edit(t,r)
            await cb.answer(); return True
        if head=="backresults":
            st=ctx.series_sessions.get((msg.chat.id,msg.id))
            if not st or st.get("origin")!="results" or st.get("until",0)<time.monotonic(): await cb.answer("Search results expired. Search again.",show_alert=True); return True
            t,r,_=await results_screen(ctx,st["query"],st.get("page",0),st.get("target_season"),st.get("target_episode"),st.get("rows"),st.get("total")); await edit(t,r); await cb.answer(); return True
        if head=="back": await edit("Search for a series by sending its title."); await cb.answer(); return True
        if head=="shows":
            sid,page=arg.rsplit(":",1); st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await show_screen(ctx,sid,int(page),uid,back_results=st.get("origin")=="results"); await edit(t,r); await cb.answer(); return True
        if head=="goto":
            bits=arg.split(":"); sid,season,res,level=bits[:4]; lo=int(bits[4]) if len(bits)>4 else None; hi=int(bits[5]) if len(bits)>5 else None
            t,r=await goto_screen(ctx,sid,int(season),res,int(level),lo,hi); await edit(t,r); await cb.answer(); return True
        if head=="range":
            sid,season,res,lo,hi=arg.split(":",4)
            _,range_items,_,_=await _resolution_items(ctx,sid,int(season),res)
            if len({e for f,_ in range_items for e in f.episodes})>=500 and int(hi)-int(lo)>=50:
                t,r=await goto_screen(ctx,sid,int(season),res,1,int(lo),int(hi))
            else:
                st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await resolution_screen(ctx,sid,int(season),res,-1,int(lo),int(hi),back_results=st.get("origin")=="results")
            await edit(t,r); await cb.answer(); return True
        if head=="continue":
            sid=arg; prog=await ctx.series_catalog.get_progress(uid,sid)
            if not prog: await cb.answer("No saved progress.",show_alert=True); return True
            season=int(prog["season"]); res=prog.get("resolution") or "unknown"; lo=int(prog["episode"])
            if season==0 and _display_season_numbers(await _show_files(ctx,sid))==[1]: season=1
            _,items,_,by_ep=await _resolution_items(ctx,sid,season,res)
            hi=max(by_ep) if by_ep else lo
            st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await resolution_screen(ctx,sid,season,res,-1,lo,hi,back_results=st.get("origin")=="results"); await edit(t,r); await cb.answer(); return True
        if head=="ep":
            sid,season,ep=arg.split(":",2); st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await episode_screen(ctx,sid,int(season),int(ep),back_results=st.get("origin")=="results"); await edit(t,r); await cb.answer(); return True
        if head=="season":
            sid,season=arg.rsplit(":",1); st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await season_screen(ctx,sid,int(season),uid,back_results=st.get("origin")=="results"); await edit(t,r); await cb.answer(); return True
        if head=="jumpep":
            sid,season,ep=arg.split(":",2); st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await episode_screen(ctx,sid,int(season),int(ep),back_results=st.get("origin")=="results"); await edit(t,r); await cb.answer(); return True
        if head=="jump":
            sid,season=arg.rsplit(":",1); st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await season_screen(ctx,sid,int(season),uid,back_results=st.get("origin")=="results"); await edit(t,r); await cb.answer(); return True
        if head=="res":
            sid,season,res=arg.split(":",2); st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await resolution_screen(ctx,sid,int(season),res,0,back_results=st.get("origin")=="results"); await edit(t,r); await cb.answer(); return True
        if head=="respage":
            bits=arg.split(":"); sid,season,res,offset=bits[:4]; asc=len(bits)>4 and bits[4]=="asc"
            st=ctx.series_sessions.get((msg.chat.id,msg.id),{}); t,r=await resolution_screen(ctx,sid,int(season),res,int(offset),1 if asc else None,None,back_results=st.get("origin")=="results"); await edit(t,r); await cb.answer(); return True
        if head=="missing":
            sid,season,res=arg.split(":",2); t,r=await missing_screen(ctx,sid,int(season),res); await edit(t,r); await cb.answer(); return True
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

async def reparse_catalog(ctx, client, progress_message):
    """Reparse in short keyset-paginated batches; resume safely after a failure or stop."""
    from dataclasses import replace
    from parse import parse_series
    scanned=changed=recognized_skips=imported=0
    sid_moves={}; example_names=[]
    after_id=await ctx.series_catalog.get_reparse_checkpoint()
    try: await progress_message.edit_text(f"Reparsing series records…\\nResuming after {after_id or 'the beginning'}.")
    except Exception: pass
    async for batch,last_id in ctx.series_catalog.iter_file_batches(batch_size=100,after_id=after_id):
        for old in batch:
            raw=(old.name or "").strip()
            parsed=parse_series(raw)
            if (parsed.kind not in ("episode","pack") or not parsed.show) and old.caption:
                parsed=parse_series(old.caption.splitlines()[0])
            scanned+=1
            if parsed.kind in ("episode","pack") and parsed.show:
                sid=show_id(parsed.show,parsed.year)
                new=replace(old,sid=sid,show=parsed.show,year=parsed.year,kind=parsed.kind,
                            season=parsed.season,episodes=parsed.episodes,absolute=parsed.absolute,
                            ep_title=parsed.ep_title,special=parsed.special,special_label=parsed.special_label,resolution=parsed.resolution or old.resolution,
                            source=parsed.source or old.source,codec=parsed.codec or old.codec,
                            languages=parsed.languages or old.languages,group=parsed.group or old.group,
                            tags=parsed.tags or old.tags,ext=parsed.ext or old.ext)
                if any(getattr(old,k)!=getattr(new,k) for k in ("sid","show","year","kind","season","episodes","absolute","ep_title","special","special_label","resolution","source","codec","languages","group","tags","ext")):
                    if old.sid != new.sid: sid_moves[old.sid]=(new.sid,new.show,new.year)
                    if new.show and new.show not in example_names and len(example_names)<10: example_names.append(new.show)
                    await ctx.series_catalog.update_reparsed(old,new); changed+=1
        # Persist only after the entire page has been processed.
        await ctx.series_catalog.set_reparse_checkpoint(last_id)
        try:
            await progress_message.edit_text(
                f"Reparsing series records…\\nChecked {scanned:,} in this run · changed {changed:,} · "
                f"last record {str(last_id)[:18]}."
            )
        except Exception: pass
        if ctx.maintenance_stop.is_set():
            try: await progress_message.edit_text(
                f"Reparse paused safely.\\nChecked this run: {scanned:,} · changed: {changed:,}. "
                "Run /reparse to resume from the saved checkpoint."
            )
            except Exception: pass
            return {"scanned":scanned,"changed":changed,"paused":True}
    # Once all records have their corrected identities, migrate references from old show IDs.
    for old_sid,(new_sid,new_title,new_year) in sid_moves.items():
        await ctx.series_catalog.merge_show_ids(new_sid,[old_sid],new_title,new_year)
    # Re-check skipped posts in bounded pages. Offset is deliberately small and no full collection is loaded.
    offset=0
    while True:
        rows=await ctx.series_catalog.skipped_rows(20,offset)
        if not rows: break
        offset+=len(rows)
        by_channel={}
        for r in rows: by_channel.setdefault(int(r.get("channel",0)),[]).append(r)
        for channel, entries in by_channel.items():
            ids=[int(r.get("message_id",0)) for r in entries if int(r.get("message_id",0))>0]
            if not ids: continue
            try: messages=await client.get_messages(channel,ids)
            except Exception:
                log.info("Could not re-check skipped series posts in channel %s",channel,exc_info=True); continue
            if not isinstance(messages,list): messages=[messages]
            for m in messages:
                if not m: continue
                settings=await ctx.settings()
                rec=from_message(m,promos=settings.get("series_promos",[]),aliases=await ctx.series_catalog.alias_map())
                if rec:
                    recognized_skips+=1
                    a,known,dupes=await ctx.series_catalog.add([rec])
                    imported+=a
                    if not dupes and (a or known):
                        await ctx.series_catalog.clear_skip(rec.channel,rec.message_id)
        try: await progress_message.edit_text(
            f"Rechecking skipped posts…\\nStored records checked: {scanned:,} · changed: {changed:,} · "
            f"skipped recognized: {recognized_skips:,} · newly indexed: {imported:,}"
        )
        except Exception: pass
        if ctx.maintenance_stop.is_set():
            try: await progress_message.edit_text(
                f"Reparse paused during skipped-post recovery.\\nStored records checked: {scanned:,} · "
                f"changed: {changed:,} · recognized: {recognized_skips:,} · imported: {imported:,}. "
                "Run /reparse again to finish."
            )
            except Exception: pass
            return {"scanned":scanned,"changed":changed,"recognized_skips":recognized_skips,"imported":imported,"paused":True}
        if len(rows)<20: break
    merge_report=await merge_shows_catalog(ctx,progress_message,dry_run=False)
    if merge_report.get("paused"):
        return {"scanned":scanned,"changed":changed,"recognized_skips":recognized_skips,"imported":imported,"paused":True}
    await ctx.series_catalog.clear_reparse_checkpoint()
    summary=(f"Reparse complete.\\nStored records checked: {scanned:,}\\nRecords changed: {changed:,}\\n"
             f"Shows merged: {merge_report.get('merged',0):,}\\nSkipped posts recognized: {recognized_skips:,}\\n"
             f"Files newly indexed: {imported:,}\\nExample shows: {', '.join((merge_report.get('affected',[]) or example_names)[:10]) or 'none'}")
    try: await progress_message.edit_text(summary)
    except Exception: pass
    return {"scanned":scanned,"changed":changed,"recognized_skips":recognized_skips,"imported":imported,"merged":merge_report.get("merged",0)}


async def merge_shows_catalog(ctx, progress_message, dry_run: bool = False):
    """Stream stored records and group only compact show-id counts in memory."""
    from parse import parse_series
    from normalize import normalize, core_key
    from series_catalog import _parse_series_text, _strip_custom_promos
    groups={}; scanned=0
    settings=await ctx.settings(); promos=settings.get("series_promos",[])
    async for batch,_last_id in ctx.series_catalog.iter_file_batches(batch_size=250):
        for old in batch:
            raw=_strip_custom_promos(old.name or "",promos)
            parsed=parse_series(raw)
            if parsed.kind not in ("episode","pack") or not parsed.show:
                parsed=_parse_series_text(old.caption or "",promos)
            if parsed.kind not in ("episode","pack") or not parsed.show: continue
            identity=(core_key(normalize(parsed.show)),parsed.year)
            if not identity[0]: continue
            bucket=groups.setdefault(identity,{"title":parsed.show,"year":parsed.year,"counts":{},"variants":[]})
            bucket["counts"][old.sid]=bucket["counts"].get(old.sid,0)+1
            if old.show and old.show not in bucket["variants"] and len(bucket["variants"])<12: bucket["variants"].append(old.show)
            scanned+=1
        if scanned and scanned%1000<250:
            try: await progress_message.edit_text(f"Checking stored filenames and captions… {scanned:,} records checked.")
            except Exception: pass
        if ctx.maintenance_stop.is_set():
            try: await progress_message.edit_text(f"Show merge paused safely during scan. {scanned:,} records checked; run /mergeshows again.")
            except Exception: pass
            return {"groups":0,"scanned":scanned,"paused":True}
    plans=[]
    for bucket in groups.values():
        counts=bucket["counts"]
        if len(counts)>1:
            stored={}
            for sid,n in counts.items():
                show=await ctx.series_catalog.get_show(sid)
                stored[sid]=int((show or {}).get("count",n))
            survivor=max(counts,key=lambda sid:(stored[sid],counts[sid],sid))
            plans.append((bucket,survivor,[sid for sid in counts if sid!=survivor],stored))
    if dry_run:
        lines=[f"Dry run complete. Records checked: {scanned:,}",f"Merge groups: {len(plans)}"]
        for bucket,survivor,removed,stored in plans:
            lines.append(f"• {bucket['title']} ({bucket['year'] or 'no year'}): " + " / ".join(bucket["variants"]) + f" → survivor has {stored[survivor]:,} files")
        if not plans: lines.append("No duplicate show identities found.")
        try: await progress_message.edit_text("\n".join(lines))
        except Exception: pass
        return {"groups":len(plans),"scanned":scanned}
    merged=moved=0; affected=[]
    for bucket,survivor,removed,stored in plans:
        if ctx.maintenance_stop.is_set():
            try: await progress_message.edit_text(f"Show merge paused safely. {merged:,} show records merged; {moved:,} files moved. Run /mergeshows again to finish.")
            except Exception: pass
            return {"groups":len(plans),"merged":merged,"moved":moved,"affected":affected,"scanned":scanned,"paused":True}
        title,year=bucket["title"],bucket["year"]
        moved+=await ctx.series_catalog.merge_show_ids(survivor,removed,title,year)
        merged+=len(removed); affected.append(title+(f" ({year})" if year else ""))
        try: await progress_message.edit_text(f"Merging shows… {merged:,} show records merged; {moved:,} files moved.")
        except Exception: pass
    lines=["Show merge complete.",f"Records checked: {scanned:,}",f"Shows merged: {merged:,}",f"Files moved to surviving shows: {moved:,}","Affected shows: "+(", ".join(dict.fromkeys(affected)) if affected else "none")]
    try: await progress_message.edit_text("\n".join(lines))
    except Exception: pass
    return {"groups":len(plans),"merged":merged,"moved":moved,"affected":affected,"scanned":scanned}


async def auto_index(ctx,m):
    catalog=getattr(ctx,"series_catalog",None)
    if not catalog or m.chat.id not in ctx.cfg.file_channels: return 0,0,0,"ignored"
    settings=await ctx.settings()
    r=from_message(m,promos=settings.get("series_promos",[]),aliases=await catalog.alias_map())
    if not r:
        await catalog.add_skip(m)
        return 0,0,0,"unrecognized"
    return (*await catalog.add([r]),"indexed")
