"""Anime announcements and entry-scoped force join. No catalogue copies or media downloads.

Configuration lives in settings; one small durable record per episode/destination lives in
series_maintenance. A send is claimed before Telegram is called and remains reviewable if
its outcome is uncertain. Only live fresh index events call queue_live.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import string
import time
from datetime import datetime, timezone
from html import escape

from payload import encode_anime, decode_anime
from ui import Btn, join_screen
from pyrogram.enums import ChatMemberStatus, ChatType

log = logging.getLogger("seriesbot.anime")
DEFAULT_TEMPLATE = "{title}\n\n{episode_line}\n\nThe latest episode is now available."
MAX_SHOWS = 30
MAX_CAPTION = 1024
SID_RE = re.compile(r"[0-9a-f]{12}")
KEY_RE = re.compile(r"[0-9a-f]{24}")
FIELDS = {"title", "season", "episode", "episode_line", "cour", "arc", "description"}


def episode_version(f, episode):
    """A playable, unambiguous catalogue version of this episode (including explicit multi-episode files)."""
    return (getattr(f, "kind", None) == "episode" and not getattr(f, "special", False)
            and not getattr(f, "part", None) and episode in getattr(f, "episodes", ())
            and isinstance(f.season, int) and 0 <= f.season <= 99999
            and isinstance(episode, int) and 1 <= episode <= 999999
            and (f.ext or "").lower() not in {"srt", "ass", "ssa", "vtt", "sub", "idx"})


def normal_episode(f):
    # Automatic announcements must identify exactly one complete episode.
    episodes=getattr(f,"episodes",())
    return len(episodes)==1 and episode_version(f,episodes[0])


def key_for(sid, season, episode, channel):
    return hashlib.sha256(f"{sid}:{season}:{episode}:{channel}".encode()).hexdigest()[:24]


def _id(key):
    return "anime:" + key


def _records(ctx):
    return ctx.series_catalog.maintenance_state


async def configs(ctx):
    return dict((await ctx.settings()).get("anime_announcements") or {})


async def get_candidate(ctx, key):
    if not KEY_RE.fullmatch(key):
        return None
    return await _records(ctx).find_one({"_id": _id(key), "record_type": "anime_announcement"})


async def exact_files(ctx, sid, season, episode):
    return [f for f in await ctx.series_catalog.show_files(sid, season)
            if episode_version(f,episode)]


async def open_episode(ctx, show, uid, sid, season, episode):
    import series_handlers
    if not await ctx.series_catalog.get_show(sid):
        await show("That anime show is no longer indexed.")
        return
    text, rows = await series_handlers.episode_screen(ctx, sid, season, episode, exact=True)
    if not rows:
        await show(text)
        return
    sent = await show(text, rows)
    now = time.monotonic()
    # Same bounded message-scoped session store as the ordinary series interface.
    ctx.series_sessions = {k:v for k,v in ctx.series_sessions.items() if v.get("until",0)>now}
    if len(ctx.series_sessions) >= 100:
        ctx.series_sessions.pop(min(ctx.series_sessions, key=lambda k: ctx.series_sessions[k].get("until",0)), None)
    ctx.series_sessions[(sent.chat.id, sent.id)] = {"origin":"anime", "profile":"anime", "owner":uid,
        "sid":sid,"season":season,"episode":episode,"until":now+1800}
    ctx.watch(sent.chat.id, sent.id, (await ctx.settings())["result_ttl_min"])


async def start(ctx, client, m, payload):
    import handlers
    target = decode_anime(payload)
    if not target:
        await m.reply_text("This anime episode link is invalid. Open the announcement again.")
        return
    if m.chat.type != ChatType.PRIVATE:
        await m.reply_text("Open the anime episode link in a private chat with me.")
        return
    if not getattr(ctx,"ready",False) or getattr(ctx,"series_catalog",None) is None:
        await m.reply_text("The catalogue is warming up. Open the announcement again shortly.")
        return
    sid, season, episode = target
    uid = m.from_user.id
    show = handlers.reply_to(m)
    action = {"type":"anime_episode","profile":"anime","sid":sid,"season":season,"episode":episode}
    if not await require_join(ctx, client, uid, show, action):
        await open_episode(ctx, show, uid, sid, season, episode)


def _pending(ctx):
    return getattr(ctx, "anime_pending", {})


async def require_join(ctx, client, uid, show, action):
    import handlers
    missing = await handlers.missing_channels(ctx, client, uid, profile="anime")
    if not missing:
        return False
    text, rows = join_screen(missing)
    rows[-1] = [Btn("I've joined", "afj:ok")]
    sent = await show(text, rows)
    now = time.monotonic()
    pending = _pending(ctx)
    for key in list(pending):
        if pending[key]["until"] <= now: pending.pop(key, None)
    if len(pending) >= 5000: pending.pop(next(iter(pending)))
    pending[(uid, sent.chat.id, sent.id)] = {**action, "missing":{c["chat"] for c in missing}, "until":now+ctx.PENDING_SECONDS}
    ctx.watch(sent.chat.id, sent.id, (await ctx.settings())["result_ttl_min"])
    return True


async def _resume(ctx, client, uid, show, action):
    if action["type"] == "anime_episode":
        await open_episode(ctx, show, uid, action["sid"], action["season"], action["episode"])
    elif action["type"] == "anime_file":
        import series_handlers
        f = await ctx.series_catalog.get_file(action["uid"])
        if not f or not episode_version(f,action["episode"]) or (f.sid,f.season) != (action["sid"],action["season"]):
            await show("That episode version is no longer available. Open the announcement again.")
            return
        await show("Thanks for joining. Sending your file…")
        await series_handlers.deliver_file(ctx, client, uid, f)


async def joined(ctx, client, cb):
    import handlers
    uid, msg = cb.from_user.id, cb.message
    key = (uid,msg.chat.id,msg.id)
    pending = _pending(ctx)
    action = pending.get(key)
    if not action or action["until"] <= time.monotonic():
        pending.pop(key,None)
        await cb.answer("This anime entry expired. Open the announcement again.",show_alert=True)
        return
    missing = await handlers.missing_channels(ctx,client,uid,fresh=True,profile="anime")
    if pending.get(key) is not action:
        await cb.answer("Already resumed."); return
    if missing:
        action["missing"] = {c["chat"] for c in missing}
        text, rows = join_screen(missing,still=True)
        rows[-1]=[Btn("I've joined","afj:ok")]
        await handlers.edit_of(msg)(text,rows)
        await cb.answer("Not all joined yet.")
        return
    pending.pop(key,None)
    await cb.answer()
    async def show(text, rows=None):
        return await handlers.edit_of(msg)(text,rows)
    await _resume(ctx,client,uid,show,action)


async def resume_join(ctx,client,uid,chat):
    """Each pending anime message is independent, including multiple entries by one user."""
    import handlers
    pending = _pending(ctx)
    for key, action in list(pending.items()):
        if action["until"] <= time.monotonic():
            pending.pop(key,None); continue
        if key[0] != uid or chat not in action["missing"]: continue
        missing = await handlers.missing_channels(ctx,client,uid,profile="anime")
        if pending.get(key) is not action: continue
        action["missing"]={c["chat"] for c in missing}
        if missing: continue
        pending.pop(key,None)
        async def show(text,rows=None):
            return await client.send_message(uid,text,parse_mode=handlers.HTML,reply_markup=handlers.kb(rows))
        try: await _resume(ctx,client,uid,show,action)
        except Exception: log.exception("could not resume anime entry")


async def episode_callback(ctx,client,cb,arg):
    import handlers
    uid,msg=cb.from_user.id,cb.message
    key=(msg.chat.id,msg.id)
    st=ctx.series_sessions.get(key)
    if not st or st.get("profile")!="anime" or st.get("owner")!=uid or st["until"]<=time.monotonic() or msg.chat.id!=uid:
        await cb.answer("This anime entry expired. Open the announcement again.",show_alert=True); return
    if arg=="close":
        ctx.series_sessions.pop(key,None); await msg.delete(); await cb.answer(); return
    if arg.startswith("page:") and arg[5:].isascii() and arg[5:].isdigit() and len(arg[5:])<=6:
        action={"type":"anime_episode","profile":"anime","sid":st["sid"],"season":st["season"],"episode":st["episode"]}
        if await require_join(ctx,client,uid,handlers.reply_to(msg),action):
            await cb.answer("Join the required anime channels first."); return
        import series_handlers
        text,rows=await series_handlers.episode_screen(ctx,st["sid"],st["season"],st["episode"],exact=True,exact_page=int(arg[5:]))
        await handlers.edit_of(msg)(text,rows)
        st["until"]=time.monotonic()+1800
        ctx.watch(msg.chat.id,msg.id,(await ctx.settings())["result_ttl_min"])
        await cb.answer(); return
    if not arg.startswith("file:"):
        await cb.answer("Invalid episode action.",show_alert=True); return
    action={"type":"anime_file","profile":"anime","uid":arg[5:],"sid":st["sid"],"season":st["season"],"episode":st["episode"]}
    if await require_join(ctx,client,uid,handlers.reply_to(msg),action):
        await cb.answer("Join the required anime channels first."); return
    await cb.answer("Sending file…")
    async def show(text, rows=None):
        # Do not replace the resolution menu when delivering a file.
        if not text.startswith("Thanks for joining"):
            return await handlers.reply_to(msg)(text,rows)
    await _resume(ctx,client,uid,show,action)
    st["until"]=time.monotonic()+1800
    ctx.watch(msg.chat.id,msg.id,(await ctx.settings())["result_ttl_min"])


# -------- persistent announcement configuration and queue --------
async def validate_destination(client, ref):
    if isinstance(ref,str):
        ref=ref.strip()
        if re.fullmatch(r"-?\d+",ref): ref=int(ref)
    try:
        chat=await client.get_chat(ref)
        if chat.type != ChatType.CHANNEL:
            raise ValueError("Choose a Telegram broadcast channel, not a group or private user.")
        me=await client.get_chat_member(chat.id,"me")
    except ValueError: raise
    except Exception as exc:
        raise ValueError("I cannot access that channel. Add this bot to the channel as an administrator, then use its numeric ID or @username.") from exc
    if me.status not in (ChatMemberStatus.ADMINISTRATOR,ChatMemberStatus.OWNER):
        raise ValueError("Make this bot a channel administrator first.")
    if me.status != ChatMemberStatus.OWNER and not getattr(getattr(me,"privileges",None),"can_post_messages",False):
        raise ValueError("Enable the bot's Post Messages administrator permission in the destination channel.")
    return {"channel":int(chat.id),"channel_title":str(chat.title or chat.id)[:150]}


def image_reference(photo):
    if not photo or not getattr(photo,"file_id",None):
        raise ValueError("Reply with an image sent as a Telegram photo, not as a file/document. Landscape, square and portrait photos are all accepted.")
    return photo.file_id


def _template_check(template):
    if len(template)>MAX_CAPTION: raise ValueError("Caption template is too long (maximum 1024 characters).")
    try:
        for _,field,spec,conv in string.Formatter().parse(template):
            if field is not None and (field not in FIELDS or spec or conv):
                raise ValueError("Use only these placeholders: " + ", ".join(sorted(FIELDS)))
    except ValueError: raise
    return template


def caption_for(title, season, episode, cfg, candidate):
    # Plain user-supplied templates; escape the finished caption, never interpolate HTML.
    ep_line=(f"Season {season} · " if season else "")+f"Episode {episode:02d}"
    values={"title":title,"season":str(season) if season else "","episode":f"{episode:02d}",
            "episode_line":ep_line,"cour":cfg.get("cour", ""),"arc":cfg.get("arc", ""),"description":cfg.get("description", "")}
    if candidate.get("caption_override") is not None:
        text=candidate["caption_override"]
    else:
        template=cfg.get("template") or DEFAULT_TEMPLATE
        text=_template_check(template).format_map(values)
        if not cfg.get("template"):
            extra=[v for v in (values["cour"],values["arc"],values["description"]) if v]
            if extra: text += "\n\n"+"\n".join(extra)
    text=text.strip()
    if not text or len(text.encode("utf-16-le"))//2>MAX_CAPTION:
        raise ValueError("Caption must contain 1–1024 Telegram characters. Shorten the template or edit the caption.")
    return escape(text)


async def configure(ctx,client,sid,changes,existing=False):
    if not SID_RE.fullmatch(sid) or not await ctx.series_catalog.get_show(sid):
        raise ValueError("Show ID not found. Use /showinfo or search for the catalogue show ID.")
    async with ctx.anime_lock:
        all_cfg=await configs(ctx)
        if existing and sid not in all_cfg: raise ValueError("That show configuration was removed. Add it again first.")
        if sid not in all_cfg and len(all_cfg)>=MAX_SHOWS:
            raise ValueError(f"At most {MAX_SHOWS} configured shows; remove a finished show first.")
        c={**all_cfg.get(sid,{}),"enabled":all_cfg.get(sid,{}).get("enabled",False)}
        changes=dict(changes)
        if "destination" in changes:
            changes.update(await validate_destination(client,changes.pop("destination")))
        if "template" in changes: _template_check(changes["template"])
        c.update(changes)
        if c.get("enabled"):
            if not c.get("channel"): raise ValueError("Configure a destination channel before enabling.")
            # Recheck at activation; destination changes were already checked.
            if changes.get("enabled") and "channel" not in changes:
                c.update(await validate_destination(client,c["channel"]))
        all_cfg[sid]=c
        await ctx.set_setting("anime_announcements",all_cfg)
        return c


async def remove_config(ctx,sid):
    async with ctx.anime_lock:
        all_cfg=await configs(ctx); all_cfg.pop(sid,None)
        await ctx.set_setting("anime_announcements",all_cfg)


async def _queue(ctx,sid,season,episode,cfg,resolutions,manual=False):
    channel=cfg["channel"]
    key=key_for(sid,season,episode,channel)
    coll=_records(ctx)
    # Caller serializes configuration/queue edits. Existing terminal records are never reopened here.
    doc=await get_candidate(ctx,key)
    if doc:
        if doc["status"]=="pending":
            values=sorted(set(doc.get("resolutions",[]))|set(resolutions))[:20]
            if values!=doc.get("resolutions",[]):
                await coll.update_one({"_id":_id(key),"status":"pending"},{"$set":{"resolutions":values,"updated":time.time()}})
        return key,False
    now=time.time()
    # Same existing cross-database identity reservation as catalogue additions.
    # Only take it when inserting a new record, never while talking to Telegram.
    async with ctx.series_catalog._catalogue_write():
        result=await coll.update_one({"_id":_id(key)},{"$setOnInsert":{"record_type":"anime_announcement","sid":sid,"season":season,
            "episode":episode,"channel":channel,"status":"pending","resolutions":sorted(set(resolutions))[:20],
            "manual":manual,"created":now,"updated":now,"revision":0}},upsert=True)
    return key,result.upserted_id is not None


async def queue_live(ctx,f):
    if not normal_episode(f): return None
    async with ctx.anime_lock:
        cfg=(await configs(ctx)).get(f.sid)
        if not cfg or not cfg.get("enabled") or not cfg.get("channel"): return None
        return await _queue(ctx,f.sid,f.season,f.episodes[0],cfg,[f.resolution or "unknown"])


async def queue_manual(ctx,sid,season,episode):
    encode_anime(sid,season,episode)
    async with ctx.anime_lock:
        cfg=(await configs(ctx)).get(sid)
        if not cfg or not cfg.get("channel"): raise ValueError("Configure this show's destination first.")
        files=await exact_files(ctx,sid,season,episode)
        if not files: raise ValueError("No exact playable episode with that show ID, stored season and episode.")
        return await _queue(ctx,sid,season,episode,cfg,[f.resolution or "unknown" for f in files],True)


def readiness_issues(ctx,doc,cfg,photo):
    issues=[]
    if not photo: issues.append("No announcement photo is configured. Use Change image.")
    if not cfg.get("enabled"): issues.append("Announcements are disabled for this show. Enable it in Show settings.")
    if cfg.get("channel")!=doc["channel"]: issues.append("The candidate destination does not match the show configuration. Use Change destination or queue the episode for the current channel.")
    if not ctx.bot_username: issues.append("The bot username is unavailable; reopen the preview after startup.")
    return issues


async def proposal(ctx,key):
    doc=await get_candidate(ctx,key)
    if not doc: raise ValueError("Announcement not found.")
    cfg=(await configs(ctx)).get(doc["sid"],{})
    show=await ctx.series_catalog.get_show(doc["sid"])
    if not show: raise ValueError("The show is no longer indexed.")
    if not await exact_files(ctx,doc["sid"],doc["season"],doc["episode"]):
        raise ValueError("The exact episode is no longer indexed.")
    cap=caption_for(show.get("title") or doc["sid"],doc["season"],doc["episode"],cfg,doc)
    username=(ctx.bot_username or "").lstrip("@")
    if not username: raise ValueError("The bot username is unavailable; reopen the preview after startup.")
    url=f"https://t.me/{username}?start="+encode_anime(doc["sid"],doc["season"],doc["episode"])
    photo=doc.get("image_override") or cfg.get("image")
    ready=not readiness_issues(ctx,doc,cfg,photo)
    token=hashlib.sha256(json.dumps([doc["revision"],doc["channel"],photo,cap,url,ready],ensure_ascii=False).encode()).hexdigest()[:12]
    return doc,cfg,cap,url,photo,ready,token


async def publish(ctx,client,key,token):
    import handlers
    async with ctx.anime_lock:
        doc,cfg,cap,url,photo,ready,current=await proposal(ctx,key)
        if doc["status"]!="pending": raise ValueError("This announcement is not pending. It will not be sent again.")
        if not ready: raise ValueError(" ".join(readiness_issues(ctx,doc,cfg,photo)))
        if token!=current: raise ValueError("The preview changed. Open it again before sending.")
    # Channel checks and send RPC do not hold the indexing/configuration lock.
    await validate_destination(client,doc["channel"])
    async with ctx.anime_lock:
        doc,cfg,cap,url,photo,ready,current=await proposal(ctx,key)
        if doc["status"]!="pending" or not ready or token!=current:
            raise ValueError("The announcement changed. Open a new preview before sending.")
        # Durable uncertainty barrier before any network send. If persistence fails, do not send.
        r=await _records(ctx).update_one({"_id":_id(key),"status":"pending","revision":doc["revision"]},
            {"$set":{"status":"review","updated":time.time(),"attempted":time.time(),"review_reason":"Send started; outcome not yet recorded"}})
        if not r.matched_count: raise ValueError("Another action already changed this announcement.")
        ctx.anime_sending.add(key)
    mid=None
    try:
        sent=await asyncio.wait_for(client.send_photo(doc["channel"],photo,caption=cap,parse_mode=handlers.HTML,
            reply_markup=handlers.kb([[Btn(f"Episode {doc['episode']:02d}",url=url)]])), timeout=40)
        if not getattr(sent,"id",None): raise RuntimeError("Telegram returned no message ID")
        mid=int(sent.id); at=time.time()
        r=await _records(ctx).update_one({"_id":_id(key),"status":"review","revision":doc["revision"]},
            {"$set":{"status":"sent","message_id":mid,"sent_at":at,"updated":at,"review_reason":""}})
        if not r.matched_count: raise RuntimeError("Publication record changed")
        return mid
    except Exception as exc:
        log.exception("announcement %s channel=%s returned_message_id=%s may have been sent; review destination before retry",key,doc["channel"],mid)
        detail=f" Telegram returned message ID {mid}; recording it failed." if mid is not None else ""
        raise ValueError("Send outcome requires review."+detail+" Check the destination channel before doing anything else; the bot will not retry automatically.") from exc
    finally:
        ctx.anime_sending.discard(key)


async def change_status(ctx,key,action,revision=None):
    async with ctx.anime_lock:
        doc=await get_candidate(ctx,key)
        if not doc: raise ValueError("Announcement not found.")
        if key in ctx.anime_sending: raise ValueError("Publication is still in progress. Reopen the queue shortly.")
        if revision is not None and doc["revision"]!=revision: raise ValueError("This action expired. Open a new preview.")
        allowed={"cancel":("pending","cancelled"),"reannounce":("sent","pending"),"retry":("review","pending"),"restore":("cancelled","pending"),"mark":("review","sent")}
        before,after=allowed[action]
        if doc["status"]!=before: raise ValueError("That action no longer applies.")
        changes={"status":after,"updated":time.time(),"revision":doc["revision"]+1}
        if action=="mark":
            changes["review_reason"]="Administrator confirmed publication; message ID and original send time may be unknown"
            changes["confirmed_at"]=time.time()
        if after=="pending":
            changes.update({"previous_message_id":doc.get("message_id"),"previous_sent_at":doc.get("sent_at"),"message_id":None,"sent_at":None,"review_reason":""})
        await _records(ctx).update_one({"_id":_id(key),"status":before,"revision":doc["revision"]},{"$set":changes})


async def edit_candidate(ctx,key,field,value):
    async with ctx.anime_lock:
        d=await get_candidate(ctx,key)
        if not d or d["status"]!="pending": raise ValueError("Only pending candidates can be edited.")
        await _records(ctx).update_one({"_id":_id(key),"status":"pending","revision":d["revision"]},
            {"$set":{field:value,"updated":time.time(),"revision":d["revision"]+1}})


async def move_candidate(ctx,client,key,ref):
    """Move one pending candidate and the show's future destination. Never resend history."""
    dest=await validate_destination(client,ref)
    async with ctx.anime_lock:
        d=await get_candidate(ctx,key)
        if not d or d["status"]!="pending": raise ValueError("Only pending candidates can move.")
        if dest["channel"]==d["channel"]: raise ValueError("Choose a different destination channel.")
        all_cfg=await configs(ctx)
        if d["sid"] not in all_cfg: raise ValueError("That show configuration was removed. Add it again first.")
        files=await exact_files(ctx,d["sid"],d["season"],d["episode"])
        if not files: raise ValueError("The episode is no longer indexed.")
        c={**all_cfg[d["sid"]],**dest}
        new,created=await _queue(ctx,d["sid"],d["season"],d["episode"],c,[f.resolution or "unknown" for f in files],True)
        if created:
            overrides={k:d[k] for k in ("caption_override","image_override") if k in d}
            if overrides:
                await _records(ctx).update_one({"_id":_id(new),"status":"pending"},{"$set":overrides})
        # If destination already has a sent/review/cancelled record, keep that state.
        all_cfg[d["sid"]]=c
        await ctx.set_setting("anime_announcements",all_cfg)
        await _records(ctx).update_one({"_id":_id(key),"status":"pending","revision":d["revision"]},{"$set":{
            "status":"cancelled","updated":time.time(),"revision":d["revision"]+1,"moved_to":new}})
        return new


# -------- mobile admin interface --------
async def _send(ctx,client,uid,text,rows=None,photo=None):
    import handlers
    if photo:
        return await client.send_photo(uid,photo,caption=text,parse_mode=handlers.HTML,reply_markup=handlers.kb(rows))
    return await client.send_message(uid,text,parse_mode=handlers.HTML,reply_markup=handlers.kb(rows),disable_web_page_preview=True)


async def panel(ctx,client,uid):
    cs=await configs(ctx)
    rows=[]
    for sid,c in sorted(cs.items()):
        show=await ctx.series_catalog.get_show(sid)
        title=(show or {}).get("title",sid)
        rows.append([Btn(("🟢 " if c.get("enabled") else "⚪ ")+title[:45],f"an:show:{sid}")])
    rows += [[Btn("Add show","an:add"),Btn("Announcement queue","an:queue:0")],
             [Btn("Publication history","an:history:0"),Btn("Close","an:close")]]
    await _send(ctx,client,uid,"<b>Anime announcements</b>\nConfigure catalogue shows and approve new episodes here. Nothing is posted automatically.\nAnime join channels: <code>/forcesub anime list</code>",rows)


async def show_panel(ctx,client,uid,sid):
    c=(await configs(ctx)).get(sid)
    if c is None: raise ValueError("That show is not configured.")
    show=await ctx.series_catalog.get_show(sid)
    title=(show or {}).get("title",sid)
    text=(f"<b>{escape(title)}</b>\nID: <code>{sid}</code>\nEnabled: {'Yes' if c.get('enabled') else 'No'}\n"
          f"Channel: {escape(c.get('channel_title','Not set'))} ({c.get('channel','—')})\nImage: {'Set' if c.get('image') else 'Missing'}\n"
          f"Template: {escape((c.get('template') or DEFAULT_TEMPLATE)[:350])}\n"
          f"Cour: {escape(c.get('cour') or '—')}\nArc: {escape(c.get('arc') or '—')}\nDescription: {escape(c.get('description') or '—')}")
    rows=[[Btn("Destination",f"an:dest:{sid}"),Btn("Image",f"an:image:{sid}")],
          [Btn("Caption template",f"an:template:{sid}"),Btn("Description",f"an:description:{sid}")],
          [Btn("Cour",f"an:cour:{sid}"),Btn("Arc",f"an:arc:{sid}")],
          [Btn("Disable" if c.get("enabled") else "Enable",f"an:toggle:{sid}"),Btn("Queue episode",f"an:manual:{sid}")],
          [Btn("Remove configuration",f"an:removeask:{sid}"),Btn("Back","an:home")]]
    await _send(ctx,client,uid,text,rows)


async def queue_panel(ctx,client,uid,page=0,history=False):
    page=max(0,min(10000,page))
    states=["sent","cancelled"] if history else ["pending","review"]
    # Bounded pages, no catalogue scan. Record type namespaces the shared collection.
    cur=_records(ctx).find({"record_type":"anime_announcement","status":{"$in":states}}).sort("updated",-1).skip(page*10).limit(11)
    docs=[d async for d in cur]
    rows=[]
    for d in docs[:10]:
        show=await ctx.series_catalog.get_show(d["sid"])
        title=(show or {}).get("title",d["sid"])
        rows.append([Btn(f"{d['status'].upper()} · {title[:25]} S{d['season']}E{d['episode']:02d}"[:64],f"an:preview:{d['_id'][6:]}")])
    nav=[]; mode="history" if history else "queue"
    if page: nav.append(Btn("‹ Previous",f"an:{mode}:{page-1}"))
    if len(docs)>10: nav.append(Btn("Next ›",f"an:{mode}:{page+1}"))
    if nav: rows.append(nav)
    rows.append([Btn("Back","an:home")])
    await _send(ctx,client,uid,f"<b>{'Publication history' if history else 'Announcement queue'}</b> · Page {page+1}\n"+("Choose an episode." if docs else "Nothing here yet."),rows)


async def preview(ctx,client,uid,key):
    d=await get_candidate(ctx,key)
    if not d: raise ValueError("Announcement not found.")
    c=(await configs(ctx)).get(d["sid"],{})
    summary=(f"<b>Announcement preview</b>\nStatus: {escape(d['status'])}\nDestination: <code>{d['channel']}</code>"
             f" {escape(c.get('channel_title','') if c.get('channel')==d['channel'] else '(old destination)')}\n"
             f"Indexed qualities: {escape(', '.join(d.get('resolutions',[])))}")
    if d.get("message_id"): summary+=f"\nPublished message ID: <code>{d['message_id']}</code>"
    if d.get("sent_at"): summary+="\nPublished: "+datetime.fromtimestamp(d["sent_at"],timezone.utc).isoformat(timespec="seconds")
    if d["status"]=="review": summary+="\n⚠ Check the destination manually. The prior send may have succeeded."
    rows=[]
    if d["status"]=="pending":
        rows.extend([[Btn("Edit caption",f"an:cap:{key}"),Btn("Change image",f"an:pic:{key}")],
                     [Btn("Change destination",f"an:move:{key}"),Btn("Cancel candidate",f"an:cancel:{key}:{d['revision']}")]])
    elif d["status"]=="review":
        rows.append([Btn("Mark published",f"an:markask:{key}"),Btn("Review and retry",f"an:retryask:{key}")])
    elif d["status"]=="sent": rows.append([Btn("Reannounce",f"an:reannounceask:{key}")])
    elif d["status"]=="cancelled": rows.append([Btn("Restore",f"an:restoreask:{key}")])
    rows.append([Btn("Show settings",f"an:show:{d['sid']}"),Btn("Queue","an:queue:0")])
    try:
        d,c,cap,url,photo,ready,token=await proposal(ctx,key)
        summary+=f"\nExact link: {escape(url)}"
        if d["status"]=="pending":
            if ready: rows.insert(0,[Btn("Send announcement",f"an:send:{key}:{token}")])
            else: summary+="\nNot ready:\n"+"\n".join("• "+escape(issue) for issue in readiness_issues(ctx,d,c,photo))
        if photo:
            try: await _send(ctx,client,uid,cap,[[Btn(f"Episode {d['episode']:02d}",url=url)]],photo=photo)
            except Exception:
                summary+="\nImage preview failed. Replace the image before sending."
                rows=[r for r in rows if not any((b.data or "").startswith("an:send:") for b in r)]
                await _send(ctx,client,uid,cap,[[Btn(f"Episode {d['episode']:02d}",url=url)]])
        else:
            await _send(ctx,client,uid,cap,[[Btn(f"Episode {d['episode']:02d}",url=url)]])
    except ValueError as exc: summary+="\n"+escape(str(exc))
    await _send(ctx,client,uid,summary,rows)


PROMPTS={"add":"Reply with the show title or its catalogue show ID (12 hexadecimal characters). You can choose the matching show from the catalogue.",
         "dest":"Reply with the channel ID or @username. A private channel needs its numeric ID.",
         "image":"Reply with a Telegram photo (any orientation). Send it as a photo, not a file.","template":"Reply with a plain caption template. Available placeholders: {title}, {season}, {episode}, {episode_line}, {cour}, {arc}, {description}. Send 'reset' for the default.",
         "cour":"Reply with the cour text, or 'reset' to clear.","arc":"Reply with the arc text, or 'reset' to clear.",
         "description":"Reply with a short description, or 'reset' to clear.","manual":"Reply with stored season and episode separated by a space, e.g. 2 6. Use 0 for an unnumbered/absolute season.",
         "cap":"Reply with the complete plain-text caption (1024 characters maximum), or 'reset' for the show template.",
         "pic":"Reply with a Telegram photo (any orientation) for this candidate only. Send it as a photo, not a file.",
         "move":"Reply with a different destination channel ID or @username. This changes the show's destination and queues this episode there; the old candidate is cancelled."}


async def prompt(ctx,client,uid,action,target=""):
    from pyrogram import types
    import handlers
    kw={"parse_mode":handlers.HTML}
    if hasattr(types,"ForceReply"): kw["reply_markup"]=types.ForceReply(selective=True)
    sent=await client.send_message(uid,escape(PROMPTS[action])+"\nReply to this message (or reply 'cancel').",**kw)
    now=time.monotonic()
    ctx.anime_prompts={k:v for k,v in ctx.anime_prompts.items() if v["until"]>now}
    if len(ctx.anime_prompts)>=100: ctx.anime_prompts.pop(next(iter(ctx.anime_prompts)))
    ctx.anime_prompts[(uid,sent.id)]={"action":action,"target":target,"until":now+900}


async def admin_reply(ctx,client,m):
    if not ctx.is_admin(m.from_user.id) or m.chat.type!=ChatType.PRIVATE: return False
    reply=getattr(m,"reply_to_message",None)
    key=(m.from_user.id,getattr(reply,"id",None) or getattr(m,"reply_to_message_id",None))
    p=getattr(ctx,"anime_prompts",{}).get(key)
    if not p: return False
    uid=m.from_user.id
    if p["until"]<=time.monotonic():
        ctx.anime_prompts.pop(key,None); await m.reply_text("Anime setup prompt expired. Reopen /anime."); return True
    text=(getattr(m,"text",None) or "").strip(); act,target=p["action"],p["target"]
    if text.lower()=="cancel":
        ctx.anime_prompts.pop(key,None); await m.reply_text("Cancelled."); return True
    try:
        if act=="add":
            target=text.lower()
            if not SID_RE.fullmatch(target):
                if not text: raise ValueError("Reply with a title or show ID.")
                hits=await ctx.series_catalog.search_shows(text[:100],limit=10)
                if not hits: raise ValueError("No matching show. Try another title or its show ID.")
                rows=[[Btn((str(h.get("title",h["_id"]))+ (f" ({h['year']})" if h.get("year") else ""))[:45]+" · "+h["_id"],f"an:pick:{h['_id']}")] for h in hits]
                rows.append([Btn("Back","an:home")])
                ctx.anime_prompts.pop(key,None)
                await _send(ctx,client,uid,"Select the exact catalogue identity:",rows)
                return True
            await configure(ctx,client,target,{})
        elif act=="dest": await configure(ctx,client,target,{"destination":text},existing=True)
        elif act=="image": await configure(ctx,client,target,{"image":image_reference(getattr(m,"photo",None))},existing=True)
        elif act in ("template","description","cour","arc"):
            if not text: raise ValueError("Send text as a reply.")
            if len(text)>(1024 if act=="template" else 300): raise ValueError("Text is too long.")
            await configure(ctx,client,target,{act:"" if text.lower()=="reset" else text},existing=True)
        elif act=="manual":
            bits=text.split()
            if len(bits)!=2 or not all(x.isascii() and x.isdigit() for x in bits): raise ValueError("Send season and episode, e.g. 2 6.")
            target,_=await queue_manual(ctx,target,int(bits[0]),int(bits[1]))
        elif act=="cap":
            if not text or len(text.encode("utf-16-le"))//2>MAX_CAPTION: raise ValueError("Send 1–1024 caption characters.")
            await edit_candidate(ctx,target,"caption_override",None if text.lower()=="reset" else text)
        elif act=="pic": await edit_candidate(ctx,target,"image_override",image_reference(getattr(m,"photo",None)))
        elif act=="move": target=await move_candidate(ctx,client,target,text)
        ctx.anime_prompts.pop(key,None)
        if act in ("manual","cap","pic","move"): await preview(ctx,client,uid,target)
        else: await show_panel(ctx,client,uid,target)
    except ValueError as exc: await m.reply_text(str(exc))
    return True


async def admin_command(ctx,client,m,args):
    if not ctx.is_admin(m.from_user.id) or m.chat.type!=ChatType.PRIVATE:
        await m.reply_text("Open the bot privately as an administrator to manage anime announcements."); return
    uid=m.from_user.id
    try:
        if not args: await panel(ctx,client,uid)
        elif args[0]=="queue" and len(args)==4:
            key,_=await queue_manual(ctx,args[1],int(args[2]),int(args[3])); await preview(ctx,client,uid,key)
        elif args[0]=="queue" and len(args)==1: await queue_panel(ctx,client,uid)
        elif args[0]=="add" and len(args)==2:
            await configure(ctx,client,args[1],{}); await show_panel(ctx,client,uid,args[1])
        elif args[0]=="history": await queue_panel(ctx,client,uid,history=True)
        else: await m.reply_text("/anime — management\n/anime add SID\n/anime queue SID SEASON EPISODE\n/anime queue\n/anime history")
    except ValueError as exc: await m.reply_text(str(exc))


async def callback(ctx,client,cb,head,arg):
    if head=="ae": return await episode_callback(ctx,client,cb,arg)
    if head=="afj": return await joined(ctx,client,cb)
    uid=cb.from_user.id
    if not ctx.is_admin(uid) or cb.message.chat.type!=ChatType.PRIVATE or cb.message.chat.id!=uid:
        await cb.answer("Admins only, in private chat.",show_alert=True); return
    act,_,target=arg.partition(":")
    await cb.answer()
    try:
        if act=="close": await cb.message.delete()
        elif act=="home": await panel(ctx,client,uid)
        elif act=="show": await show_panel(ctx,client,uid,target)
        elif act=="pick":
            await configure(ctx,client,target,{})
            await show_panel(ctx,client,uid,target)
        elif act in ("queue","history"): await queue_panel(ctx,client,uid,int(target),act=="history")
        elif act=="preview": await preview(ctx,client,uid,target)
        elif act in PROMPTS: await prompt(ctx,client,uid,act,target)
        elif act=="toggle":
            c=(await configs(ctx)).get(target)
            if c is None: raise ValueError("Configuration no longer exists.")
            await configure(ctx,client,target,{"enabled":not c.get("enabled")},existing=True); await show_panel(ctx,client,uid,target)
        elif act=="removeask":
            await _send(ctx,client,uid,"Remove this announcement configuration? Catalogue records stay untouched.",[[Btn("Confirm remove",f"an:remove:{target}"),Btn("Back","an:home")]])
        elif act=="remove": await remove_config(ctx,target); await panel(ctx,client,uid)
        elif act=="send":
            key,_,token=target.partition(":")
            mid=await publish(ctx,client,key,token)
            await _send(ctx,client,uid,f"Announcement published. Message ID: <code>{mid}</code>",[[Btn("Queue","an:queue:0")]])
        elif act.endswith("ask") and act[:-3] in ("mark","retry","reannounce","restore"):
            verb=act[:-3]
            d=await get_candidate(ctx,target)
            if not d: raise ValueError("Announcement not found.")
            warning={"mark":"Confirm you found the published post in its channel. This records it as sent without resending.","retry":"Check the channel first. Retry may duplicate a post if Telegram accepted the previous send. This only reopens the candidate; you must approve a new preview.","reannounce":"Deliberately allow another publication of this episode? A fresh preview and send approval are still required.","restore":"Restore this cancelled candidate to the queue?"}[verb]
            await _send(ctx,client,uid,warning,[[Btn("Confirm",f"an:{verb}:{target}:{d['revision']}"),Btn("Back",f"an:preview:{target}")]])
        elif act in ("cancel","mark","retry","reannounce","restore"):
            key,_,rev=target.partition(":")
            if not rev.isdigit(): raise ValueError("This action expired. Open a new preview.")
            await change_status(ctx,key,act,int(rev)); await preview(ctx,client,uid,key)
        else: raise ValueError("Unknown anime action. Reopen /anime.")
    except ValueError as exc: await _send(ctx,client,uid,escape(str(exc)),[[Btn("Back","an:home")]])
    except Exception:
        log.exception("anime admin action failed")
        await _send(ctx,client,uid,"Anime action failed. Check the queue before retrying.",[[Btn("Queue","an:queue:0")]])
