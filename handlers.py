"""Telegram wiring (Kurigram / Pyrogram API). Logic that doesn't need Telegram
lives in index.py, ui.py, parse.py and store.py."""
from __future__ import annotations

import asyncio
import html as htmllib
import io
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from html import escape

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import ChannelInvalid, ChannelPrivate, FloodWait, RPCError, UserNotParticipant
from pyrogram.handlers import (CallbackQueryHandler, ChatJoinRequestHandler, ChatMemberUpdatedHandler, MessageHandler,
                               PreCheckoutQueryHandler)
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice

import ui
from ctx import Ctx, Session, lagos_day, lagos_now
from index import film_id
from normalize import core_key, normalize
from parse import SUB_EXTS, VIDEO_EXTS, detail_lines, is_junk, label_of, looks_like_series, parse_best, parse_filename, quality_key_of
from payload import decode_search
from store import DEDUPE_BATCH, FileRec
from ui import Btn

log = logging.getLogger("seriesbot")
_BG_TASKS: set = set()


def _spawn(coro) -> None:
    """Run a long admin job in the background so it never blocks the bot's other updates."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)

HTML = ParseMode.HTML
GROUPS = (ChatType.GROUP, ChatType.SUPERGROUP)
ADMIN_CMDS = ["lockstatus", "lockrecover", "indexretry", "anime", "setseries", "setseriesgroup", "setfilmbot", "setseriespicks", "curated", "groupsettings", "donorsettings", "delreq", "setsummary", "seriesdigest", "setseriesupdatechannel", "digestshows", "purgeseries", "stats", "forcesub", "settings", "index", "indexstop", "requests", "misses", "alias", "ban",
              "unban", "dedupe", "skipped", "picks", "customize", "setwelcome", "setabout", "sethelp", "setimage",
              "clearimage", "setlinks", "clearlinks", "setfooter", "setpicks", "preview", "reparse", "reparsefind", "mergeshows", "dedupeseries", "dedupewhy", "rebuildshows", "seriesaudit", "catalogueaudit", "episodepreview", "aliascheck", "aliasremove", "shortshows", "allowshort", "identities", "showinfo", "renameshow", "locktitle", "parserrules", "linkmovie", "mergeshow", "addpromo", "delpromo", "promos", "stopreparse", "stopmergeshows",
              "donor", "setdonate", "setcontact", "audioline", "catchup", "adopt", "images", "survey", "audit", "series_skipped", "skipsearch", "skipinfo", "reclassify", "filmreview", "skippedexport", "fixpromoshows", "fixtitles", "dbsize", "skipruns", "skipclear", "dbswitch", "singleshows", "wwemerge", "autoruns"]
AD_CHOICES = [0, 5, 10, 15, 30, 60]
TTL_CHOICES = [2, 3, 4, 5, 6, 7, 10, 15, 20]
REQUEST_PINGS = {5, 10, 25, 50, 100, 250}
REPORT_PINGS = {3, 10, 25}


# ---------------------------------------------------------------- helpers
def kb(rows):
    if not rows:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(b.text, callback_data=b.data, url=b.url)
                                  for b in row] for row in rows])


def _plain(text: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", "", text))


def _markup_error(e: Exception) -> bool:
    return isinstance(e, ValueError) or any(w in str(e).upper() for w in ("ENTIT", "PARSE", "MARKUP"))


def reply_to(m, photo=None):
    async def show(text, rows=None):
        if photo:
            try:
                return await m.reply_photo(photo, caption=text, parse_mode=HTML, reply_markup=kb(rows))
            except Exception as e:
                log.warning("reply_photo failed: %s, fallback to text", e)
        try:
            return await m.reply_text(text, parse_mode=HTML, reply_markup=kb(rows))
        except (RPCError, ValueError) as e:
            if not _markup_error(e):
                raise
            return await m.reply_text(_plain(text), parse_mode=ParseMode.DISABLED, reply_markup=kb(rows))
    return show


def edit_of(msg, new_photo=None):
    """Edit a message in place (text or photo caption, or change media); bad markup falls back to plain text."""
    async def show(text, rows=None):
        text = _preserve_group_badge(msg, text)
        for mode, body in ((HTML, text), (ParseMode.DISABLED, _plain(text))):
            try:
                if getattr(msg, "photo", None):
                    cur_photo = getattr(msg.photo, "file_id", None)
                    if new_photo and new_photo != cur_photo and hasattr(msg, "edit_media"):
                        try:
                            from pyrogram.types import InputMediaPhoto
                            return await msg.edit_media(InputMediaPhoto(new_photo, caption=body, parse_mode=mode), reply_markup=kb(rows))
                        except Exception:
                            pass
                    return await msg.edit_caption(body, parse_mode=mode, reply_markup=kb(rows))
                if new_photo:
                    try:
                        client = getattr(msg, "_client", None)
                        if client is not None and hasattr(client, "send_photo"):
                            sent = await client.send_photo(msg.chat.id, new_photo, caption=body, parse_mode=mode, reply_markup=kb(rows))
                        elif hasattr(msg, "reply_photo"):
                            sent = await msg.reply_photo(new_photo, caption=body, parse_mode=mode, reply_markup=kb(rows))
                        else:
                            sent = None
                        if sent is not None:
                            try:
                                await msg.delete()
                            except Exception:
                                pass
                            return sent
                    except Exception:
                        pass
                return await msg.edit_text(body, parse_mode=mode, reply_markup=kb(rows))
            except (RPCError, ValueError) as e:        # "message not modified" etc.
                if mode == HTML and _markup_error(e):
                    continue
                log.debug("edit skipped: %s", e)
                return msg
        return msg
    return show


def rss_mb() -> int | None:
    try:
        with open("/proc/self/status") as f:
            return round(int(re.search(r"VmRSS:\s+(\d+)", f.read()).group(1)) / 1024)
    except Exception:
        return None


async def _send(client, target: int, text: str, rows=None) -> bool:
    """Send a message; returns True only if Telegram accepted it."""
    try:
        await client.send_message(target, text, parse_mode=HTML, reply_markup=kb(rows))
        return True
    except Exception as e:
        log.warning("couldn't message %s: %s", target, e)
        return False


async def notify_admins(ctx: Ctx, client, text: str, rows=None) -> None:
    """Alerts about the bot itself: always a direct message to every admin."""
    for a in ctx.cfg.admins:
        await _send(client, a, text, rows)


async def notify_inbox(ctx: Ctx, client, text: str, rows=None) -> bool:
    """Requests and file reports: the inbox channel if set, otherwise DM the admins."""
    if ctx.cfg.inbox_channel:
        return await _send(client, ctx.cfg.inbox_channel, text, rows)
    delivered = False
    for admin in ctx.cfg.admins:
        delivered = await _send(client, admin, text, rows) or delivered
    return delivered


async def deliver_donation_claim(ctx: Ctx, client, claim: dict, receipt_message=None, note: str = "") -> bool:
    """Send an identifiable claim card and a receipt labelled with the donor/claim identity."""
    cid = str(claim.get("id", claim.get("_id", "")))
    uid = int(claim["uid"])
    receipt_status = "Receipt attached below." if receipt_message else "Text/reference supplied by donor."
    admin_text = (
        "💰 <b>Exivum Series Bot donation claim</b>\n\n"
        f"• <b>Donor:</b> {escape(claim.get('name') or 'Telegram user')}\n"
        f"• <b>Telegram ID (tap to copy):</b> <code>{uid}</code>\n"
        f"• <b>Payment method:</b> {escape(claim.get('method') or 'Not specified')}\n"
        f"• <b>Claim ID:</b> <code>{escape(cid)}</code>\n"
        f"• <b>Date:</b> {escape(str(claim.get('date', lagos_day())))}\n"
        f"• <b>Submission:</b> {escape(receipt_status)}\n"
        + (f"• <b>Donor's note/reference:</b> {escape(note)}\n" if note else "")
        + "\nReply to this message or the labelled receipt to message the donor. Review the payment before approving perks."
    )
    rows = ui.claim_admin_rows(cid)
    rows.append([Btn("↗ Message donor", url=f"tg://user?id={uid}")])
    try:
        if not await notify_inbox(ctx, client, admin_text, rows):
            return False
    except Exception as e:
        log.warning("couldn't deliver donation claim card: %s", e)
        return False
    if receipt_message is not None and any(getattr(receipt_message, key, None) for key in ("photo", "document", "video", "animation")):
        # copy_message can override the caption so a receipt never arrives in the inbox without its claim identity.
        copied = False
        for target in ([ctx.cfg.inbox_channel] if ctx.cfg.inbox_channel else list(ctx.cfg.admins)):
            try:
                await client.copy_message(
                    chat_id=target, from_chat_id=receipt_message.chat.id, message_id=receipt_message.id,
                    caption=f"🧾 Receipt for claim <code>{escape(cid)}</code>\nTelegram ID: <code>{uid}</code>",
                    parse_mode=HTML,
                )
                copied = True
            except Exception as e:
                log.warning("couldn't copy donation receipt to %s: %s", target, e)
        if not copied:
            return False
    return True


async def deliver_donation_followup(ctx: Ctx, client, claim: dict, message, from_admin: bool = False) -> bool:
    """Relay follow-up messages between the donor and admin inbox with claim identity attached."""
    cid = str(claim.get("id", claim.get("_id", "")))
    uid = int(claim["uid"])
    body = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    if from_admin:
        return await _send(client, uid, f"💬 <b>Message from the bot admin</b>\nClaim: <code>{escape(cid)}</code>\n\n{escape(body)}", [[Btn("↩ Reply about this claim", f"dn:contact:{cid}")]])
    header = (f"💬 <b>Donor follow-up</b>\nTelegram ID: <code>{uid}</code>\n"
              f"Claim ID: <code>{escape(cid)}</code>\nPayment method: {escape(claim.get('method') or 'Not specified')}\n\n")
    try:
        followup_text = header + (escape(body) if body else "The donor sent an attachment; see the attached message.")
        if not await notify_inbox(ctx, client, followup_text, [[Btn("↩ Reply to donor", f"clm:reply:{cid}")]]):
            return False
        if any(getattr(message, k, None) for k in ("media", "photo", "document", "video", "animation")):
            targets = [ctx.cfg.inbox_channel] if ctx.cfg.inbox_channel else list(ctx.cfg.admins)
            copied = False
            for target in targets:
                try:
                    await client.copy_message(chat_id=target, from_chat_id=message.chat.id, message_id=message.id,
                                              caption=f"Follow-up for claim <code>{escape(cid)}</code> | Telegram ID <code>{uid}</code>",
                                              parse_mode=HTML)
                    copied = True
                except Exception as e:
                    log.warning("couldn't copy donation follow-up to %s: %s", target, e)
            return copied
        return True
    except Exception:
        log.exception("couldn't relay donation follow-up")
        return False


# Explicit replies to a bot-authored private prompt only. No persistent chat mode.
# The prompt itself survives a restart; ownership is checked against the claim.
async def _send_reply_prompt(client, uid, text):
    try:
        from pyrogram.types import ForceReply
        markup = ForceReply()
    except ImportError:  # local SDK stubs
        markup = None
    await client.send_message(uid, text, parse_mode=HTML, reply_markup=markup)


async def _claim_prompt(client, uid, cid, receipt):
    label = "Donation receipt" if receipt else "Donation follow-up"
    action = "your receipt, screenshot or payment reference" if receipt else "your message or attachment about this donation"
    await _send_reply_prompt(client, uid, f"<b>{label}</b> · <code>{escape(cid)}</code>\n\nReply to THIS message with {action}. Ordinary searches are not forwarded. Ignore this prompt to cancel.")


def _approval_rows(cid=None):
    rows = [[Btn("Feature my name", "wall:opt:yes"), Btn("Keep off the wall", "wall:opt:no")],
            [Btn("🌟 Supporters Wall", "dn:wall"), Btn("⭐ Custom requests", "customreq:start")]]
    if cid:
        rows.append([Btn("💬 Discuss this claim", f"dn:contact:{cid}")])
    return rows


def _film_request_key(title):
    import hashlib
    key = core_key(normalize(title))
    # Leave room for Telegram's 64-byte callback-data limit.
    return key if len(key.encode('utf-8')) <= 50 else 'rq-' + hashlib.sha1(key.encode('utf-8')).hexdigest()


async def _custom_request_prompt(ctx, client, uid):
    if not await ctx.is_supporter(uid):
        await _send(client, uid, "Custom multi-title requests are an active supporter benefit. You can still request missing titles from normal searches.", [[Btn("Support / Donate", "nav:donate")]])
        return
    await _send_reply_prompt(client, uid, "<b>Supporter custom requests</b>\n\nReply to THIS message with one title per line (up to 20 titles per submission, 80 characters each). Include years or season/episode numbers if needed. You may submit further batches. Only request titles you could not find. Ignore this prompt to cancel.")


async def cmd_supporter(ctx, client, m):
    if not m.from_user or m.from_user.id in ctx.banned or m.chat.type != ChatType.PRIVATE:
        return
    uid = m.from_user.id
    cmd = m.command[0].lower()
    if cmd == 'supporters':
        t, rows = ui.wall_screen(await ctx.store.get_supporters_wall())
        await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
    elif cmd == 'request':
        await _custom_request_prompt(ctx, client, uid)
    elif cmd == 'donationclaim':
        claim = await ctx.store.pending_claim_for_receipt(uid)
        if claim:
            await _claim_prompt(client, uid, str(claim.get('id', claim.get('_id'))), not claim.get('receipt_submitted'))
        else:
            await m.reply_text('No pending donation claim. Open Donate to submit a new claim, or use the Discuss button on a previous claim.')


async def _supporter_reply(ctx, client, m):
    if m.chat.type != ChatType.PRIVATE:
        return False
    rep = getattr(m, 'reply_to_message', None)
    author = getattr(rep, 'from_user', None)
    me = getattr(client, 'me', None)
    bot_id = getattr(ctx, "bot_id", None) or getattr(me, "id", None)
    # Never trust a forwarded/copied prompt or a user-authored look-alike.
    if not rep or not author or not bot_id or author.id != bot_id:
        return False
    prompt = str(getattr(rep, 'text', '') or '')
    match = re.match(r'^(Donation receipt|Donation follow-up|Donation admin reply) · (cl_[a-zA-Z0-9_-]+)\n', prompt)
    custom = prompt.startswith('Supporter custom requests\n\nReply to THIS message')
    if not match and not custom:
        return False
    uid = m.from_user.id
    text = (getattr(m, 'text', None) or getattr(m, 'caption', None) or '').strip()
    if text.lower() in ('cancel', 'stop', '/cancel'):
        await m.reply_text('Cancelled. Nothing was forwarded.')
        return True
    if custom:
        if not await ctx.is_supporter(uid):
            await m.reply_text('Your supporter access is no longer active. Use normal search requests instead.')
            return True
        titles = [line.strip() for line in text.splitlines() if line.strip()]
        if not titles or len(titles) > 20 or any(len(t) > 80 or not core_key(normalize(t)) or t.startswith('/') for t in titles):
            await m.reply_text('Reply to the custom request prompt with 1–20 valid titles, one per line, up to 80 characters each. Nothing was submitted.')
            return True
        seen = set(); submitted = duplicates = notification_failed = failed = 0
        for title in titles:
            if getattr(ctx, 'series_catalog', None) is not None:
                from series_handlers import _request_key
                shorthand = re.fullmatch(r"(.+?)\s+S(\d{1,4})(?:E(\d{1,5}))?", title, re.I)
                if shorthand:
                    title = f"{shorthand[1]} Season {int(shorthand[2])}" + (f" Episode {int(shorthand[3])}" if shorthand[3] else "")
                key = _request_key(title)
            else:
                key = _film_request_key(title)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            try:
                count, new, added = await ctx.store.add_request(key, title, uid)
            except Exception:
                log.exception("custom request save failed")
                failed += 1
                continue
            if not added:
                duplicates += 1
                continue
            submitted += 1
            ctx.stats.add('r')
            notified = await notify_inbox(ctx, client, f'⭐ [SUPPORTER] Custom request: <code>{escape(title)}</code>\nRequester: <code>{uid}</code>\nRequesters: {count}\nKey: <code>{escape(key)}</code>', [[Btn('✅ Done', f'ar:done:{key}'), Btn('💬 Reply', f'ar:rep:{key}'), Btn('🗑️ Delete', f'ar:rm:{key}')]])
            if not notified:
                notification_failed += 1
        note = f"Submitted {submitted} request(s); {duplicates} duplicate(s) skipped."
        if failed:
            note += f" {failed} could not be saved; resend the batch to retry (saved entries are deduplicated)."
        if notification_failed:
            note += f" {notification_failed} saved request(s) could not be sent to the admin inbox; they remain in the request list."
        await m.reply_text(note + " You can reply to the custom request prompt with another batch.")
        return True
    kind, cid = match.groups()
    claim = await ctx.store.get_claim(cid)
    if not claim or (kind == 'Donation admin reply' and not ctx.is_admin(uid)) or (kind != 'Donation admin reply' and int(claim.get('uid', 0)) != uid):
        await m.reply_text('That donation claim is not available for your account.')
        return True
    attachment = any(getattr(m, k, None) for k in ('photo', 'document', 'video', 'animation'))
    if len(text) > 1000:
        await m.reply_text("Please shorten the donation note to 1000 characters or send an attachment.")
        return True
    if not text and not attachment:
        await m.reply_text('Reply with a message or supported receipt attachment.')
        return True
    if kind == 'Donation admin reply':
        ok = await deliver_donation_followup(ctx, client, claim, m, from_admin=True)
        if ok and attachment:
            try:
                await client.copy_message(chat_id=claim['uid'], from_chat_id=m.chat.id, message_id=m.id)
            except Exception:
                log.exception("admin donation attachment failed")
                ok = False
        await m.reply_text('Message sent to the donor.' if ok else 'Delivery failed or was incomplete. Reply to the prompt to retry.')
        return True
    if kind == 'Donation follow-up':
        ok = await deliver_donation_followup(ctx, client, claim, m)
        await m.reply_text('Your follow-up was sent.' if ok else 'Delivery failed. Reply to the prompt to try again.')
        return True
    if claim.get('status') != 'pending' or claim.get('receipt_submitted'):
        await m.reply_text('This receipt is already submitted or the claim is closed. Use /donationclaim or Discuss this claim to contact the admin.')
        return True
    if not await ctx.store.submit_claim_receipt(cid, text):
        await m.reply_text('The receipt could not be attached. Reopen /donationclaim.')
        return True
    try:
        fresh = await ctx.store.get_claim(cid)
        ok = await deliver_donation_claim(ctx, client, fresh or claim, receipt_message=m if attachment else None, note=text)
    except Exception:
        log.exception("donation receipt delivery failed")
        ok = False
    if not ok:
        await ctx.store.reset_claim_receipt(cid)
    await m.reply_text('Receipt sent for review. You can continue searching normally.' if ok else 'Delivery failed. Reply to the receipt prompt to retry.', reply_markup=kb([[Btn('💬 Discuss this claim', f'dn:contact:{cid}')]]) if ok else None)
    return True


def _supporter_show(show, name):
    prefix = f'⭐ Supporter · {escape(" ".join(str(name or "Supporter").split())[:60])}\n\n'
    async def wrapped(text, rows=None):
        return await show(prefix + text, rows)
    return wrapped


def _preserve_group_badge(msg, text):
    if getattr(getattr(msg, 'chat', None), 'type', None) not in GROUPS:
        return text
    old = str(getattr(msg, 'text', None) or getattr(msg, 'caption', None) or '')
    if old.startswith('⭐ Supporter · ') and '\n' in old and not text.startswith('⭐ Supporter · '):
        return escape(old.split('\n', 1)[0][:80]) + '\n\n' + text
    return text


async def load_variants(ctx: Ctx, fid: str) -> list[FileRec]:
    files = await ctx.store.film_files(fid)
    files.sort(key=lambda r: ((r.ext or "").lower() not in SUB_EXTS, quality_key_of(r.res, r.src), r.size),
               reverse=True)
    return files


def rec_lines(rec: FileRec) -> tuple[str, str]:
    return detail_lines(rec.res, rec.src, rec.codec, rec.size, rec.langs, rec.tags, rec.ext, rec.grp)


# ---------------------------------------------------------------- force-join (up to 4 channels)
async def _member(ctx: Ctx, client, uid: int, chan: dict, fresh: bool = False) -> bool:
    """True if this person has joined (or we can't tell, in which case they're let in)."""
    cid = chan["chat"]
    if not fresh and ctx.joined_recently(uid, cid):
        return True
    for attempt in (1, 2):
        try:
            mem = await client.get_chat_member(cid, uid)
            ok = mem.status != ChatMemberStatus.LEFT      # banned there? don't lock them out here
            break
        except UserNotParticipant:
            if await ctx.has_join_request(uid, cid):       # a pending request is enough for approval-only channels
                ctx.mark_joined(uid, cid)
                return True
            return False
        except FloodWait as e:
            if attempt == 2:
                return True                                # can't check right now: let them through
            await asyncio.sleep(min(e.value + 1, 5))
        except Exception as e:                             # glitch, or the bot lost its admin rights
            log.warning("force-join check failed for %s: %s", cid, e)
            if ctx.first_time(f"fs-fail-{cid}"):
                await notify_admins(ctx, client, f"I can't check who joined <b>{escape(chan['title'])}</b>, so I'm "
                                                 "letting people through. Am I still an admin there? (/forcesub list)")
            return True
    if ok:
        ctx.mark_joined(uid, cid)
        return True
    if await ctx.has_join_request(uid, cid):
        ctx.mark_joined(uid, cid)
        return True
    return False


async def missing_channels(ctx: Ctx, client, uid: int, only: set[int] | None = None, fresh: bool = False, profile: str = "general") -> list[dict]:
    """The force-join channels this person still has to join. All checked at the same time."""
    if profile not in ("general", "anime"):
        raise ValueError("Unknown force-join profile")
    if ctx.is_admin(uid):
        return []
    subs = [c for c in (await ctx.subs("anime") if profile == "anime" else await ctx.subs()) if only is None or c["chat"] in only]
    if not subs or await ctx.has_join_bypass(uid):
        return []
    results = await asyncio.gather(*[_member(ctx, client, uid, c, fresh) for c in subs])
    return [c for c, ok in zip(subs, results) if not ok]


async def require_join(ctx: Ctx, client, uid: int, show, action: dict) -> bool:
    """True = blocked. Shows only the missing channels and remembers what the person was trying to do."""
    if action.get("profile") == "anime":
        import anime
        return await anime.require_join(ctx, client, uid, show, action)
    missing = await missing_channels(ctx, client, uid)
    if not missing:
        return False
    ctx.set_pending(uid, action, {c["chat"] for c in missing})
    t, rows = ui.join_screen(missing)
    sent = await show(t, rows)
    ctx.watch(sent.chat.id, sent.id, (await ctx.settings())["result_ttl_min"])
    return True


async def _joined(ctx: Ctx, client, cb) -> None:
    """The "I've joined" button: re-check only what was missing, then carry on with what they wanted."""
    uid, msg = cb.from_user.id, cb.message
    pend = ctx.get_pending(uid)
    if not pend:
        await cb.answer("That expired. Send the series title again.", show_alert=True)
        return
    missing = await missing_channels(ctx, client, uid, only=pend["missing"], fresh=True)
    if missing:
        pend["missing"] = {c["chat"] for c in missing}
        t, rows = ui.join_screen(missing, still=True)
        await edit_of(msg)(t, rows)
        await cb.answer("Not all joined yet.")
        return
    ctx.pending.pop(uid, None)
    await cb.answer()
    if pend["type"] == "search":
        await run_search(ctx, edit_of(msg), uid, pend["text"], pend.get("group", False))
        return
    if pend["type"] == "series_search" and getattr(ctx, "series_catalog", None) is not None:
        import series_handlers
        await series_handlers.search(ctx, client, edit_of(msg), uid, pend["text"], pend.get("group", False))
        return
    if pend["type"] == "series_show" and getattr(ctx, "series_catalog", None) is not None:
        import series_handlers
        await series_handlers.open_show_by_id(ctx, edit_of(msg), uid, pend["sid"])
        return
    if pend["type"] == "series_movie" and getattr(ctx, "series_catalog", None) is not None:
        movie=await ctx.series_catalog.movies.find_one({"_id":pend["uid"]})
        if not movie:
            await edit_of(msg)("That movie is no longer available.")
            return
        await edit_of(msg)("Thanks. Sending the movie…")
        try: await client.copy_message(chat_id=msg.chat.id,from_chat_id=int(movie["channel"]),message_id=int(movie["message_id"]))
        except Exception:
            log.exception("linked movie delivery failed")
            await client.send_message(msg.chat.id,"I couldn't retrieve that movie from its source channel. Please report this item.")
        return
    if pend["type"] == "series_file" and getattr(ctx, "series_catalog", None) is not None:
        import series_handlers
        rec = await ctx.series_catalog.get_file(pend["uid"])
        if not rec:
            await edit_of(msg)("That file isn't available any more.")
            return
        await edit_of(msg)("Thanks. Sending your file…")
        await series_handlers.deliver_file(ctx, client, msg.chat.id, rec)
        return
    rec = await ctx.store.get_file(pend["uid"])
    if not rec:
        await edit_of(msg)("That file isn't available any more.")
        return
    await edit_of(msg)("Thanks. Sending your file…")
    await deliver(ctx, client, msg.chat.id, rec)


async def _resume_after_join(ctx: Ctx, client, uid: int, chat: int) -> None:
    """The person has just joined (or requested to join) a force-join channel: carry on with what they wanted, in their DM."""
    pend = ctx.get_pending(uid)
    if not pend:
        return
    pend["missing"].discard(chat)
    if pend["missing"]:
        return                                              # still more channels to go
    ctx.pending.pop(uid, None)

    async def show(text, rows=None, **kw):
        return await client.send_message(uid, text, reply_markup=kb(rows) if rows else None, parse_mode=HTML,
                                         disable_web_page_preview=True)
    try:
        if pend["type"] == "series_search" and getattr(ctx, "series_catalog", None) is not None:
            import series_handlers
            await series_handlers.search(ctx, client, show, uid, pend["text"], False)
        elif pend["type"] == "series_show" and getattr(ctx, "series_catalog", None) is not None:
            import series_handlers
            await series_handlers.open_show_by_id(ctx, show, uid, pend["sid"])
        elif pend["type"] == "series_file" and getattr(ctx, "series_catalog", None) is not None:
            import series_handlers
            rec = await ctx.series_catalog.get_file(pend["uid"])
            if rec:
                await show("Thanks for joining. Sending your file…")
                await series_handlers.deliver_file(ctx, client, uid, rec)
        elif pend["type"] == "series_movie" and getattr(ctx, "series_catalog", None) is not None:
            movie = await ctx.series_catalog.movies.find_one({"_id": pend["uid"]})
            if movie:
                await show("Thanks for joining. Sending the movie…")
                await client.copy_message(chat_id=uid, from_chat_id=int(movie["channel"]), message_id=int(movie["message_id"]))
        elif pend["type"] == "file":
            rec = await ctx.store.get_file(pend["uid"])
            if rec:
                await show("Thanks for joining. Sending your file…")
                await deliver(ctx, client, uid, rec)
        elif pend["type"] == "search":
            await run_search(ctx, show, uid, pend["text"], False)
    except Exception:
        log.exception("could not resume after join")


async def on_chat_member(ctx: Ctx, client, upd) -> None:
    """Someone joined a force-join channel: notice at once and continue, without waiting for the I've-joined button."""
    try:
        new = upd.new_chat_member
        if not new or not getattr(new, "user", None) or new.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            return
        uid, chat = new.user.id, upd.chat.id
        if not any(c["chat"] == chat for c in await _all_subs(ctx)):
            return
        ctx.mark_joined(uid, chat)
        await _resume_after_join(ctx, client, uid, chat)
        import anime
        await anime.resume_join(ctx, client, uid, chat)
    except Exception:
        log.exception("chat member update failed")


async def _all_subs(ctx):
    general = await ctx.subs()
    # Real Ctx supports the new profile; legacy test doubles need only general.
    if hasattr(ctx, "anime_pending"):
        return general + await ctx.subs("anime")
    return general


async def on_join_request(ctx: Ctx, client, req) -> None:
    """Channels that ask people to request to join: a pending request counts, and (optionally) the request is approved."""
    is_sub = any(int(c["chat"]) == int(req.chat.id) for c in await _all_subs(ctx))
    if is_sub:
        await ctx.note_join_request(req.from_user.id, req.chat.id)
        await _resume_after_join(ctx, client, req.from_user.id, req.chat.id)
        import anime
        await anime.resume_join(ctx, client, req.from_user.id, req.chat.id)
    if not (await ctx.settings())["auto_approve"]:
        return
    if is_sub:
        try:
            await client.approve_chat_join_request(req.chat.id, req.from_user.id)
        except Exception as e:
            log.warning("couldn't approve join request: %s", e)
            if ctx.first_time(f"approve-{req.chat.id}"):
                await notify_admins(ctx, client, "I couldn't approve a join request. In that channel I need the "
                                                 "admin right to invite users.")


# ---------------------------------------------------------------- delivering files
async def deliver(ctx: Ctx, client, chat_id: int, rec: FileRec) -> bool:
    film = ctx.index.get(rec.film)
    s = await ctx.settings()
    is_sup = await ctx.is_supporter(chat_id)
    clean_files = await ctx.has_clean_files(chat_id)
    ad = s.get("autodelete_min", 0)
    minutes = max(ad, 45) if (ad and is_sup) else ad
    footer = "" if clean_files else s.get("footer", "")
    cap = ui.caption(film.title if film else "File", film.year if film else None, rec, minutes, footer)
    if len(cap) > 1024:
        cap = cap[:1020] + "…"

    sent = None
    for attempt in (1, 2):
        try:
            src = await client.get_messages(rec.ch, rec.mid)
            if not src or getattr(src, "empty", False) or not (getattr(src, "document", None) or getattr(src, "video", None) or getattr(src, "audio", None)):
                await _drop(ctx, rec)
                await client.send_message(chat_id, "That file is gone from the channel. Try another version.")
                return False
            try:
                sent = await src.copy(chat_id, caption=cap, parse_mode=HTML, protect_content=bool(s.get("protect", False)),
                                      reply_markup=kb([[Btn("Report a problem", f"rep:{rec.uid}")]]))
            except (RPCError, ValueError) as me:
                if _markup_error(me) or isinstance(me, ValueError):
                    plain_cap = _plain(cap)
                    if len(plain_cap) > 1024:
                        plain_cap = plain_cap[:1020] + "…"
                    sent = await src.copy(chat_id, caption=plain_cap, parse_mode=ParseMode.DISABLED,
                                          protect_content=bool(s.get("protect", False)),
                                          reply_markup=kb([[Btn("Report a problem", f"rep:{rec.uid}")]]))
                else:
                    raise
            if sent:
                break
        except FloodWait as e:
            if attempt == 2:
                await client.send_message(chat_id, "Telegram is busy right now. Please try again in a moment.")
                return False
            await asyncio.sleep(min(e.value + 1, 10))
        except (ChannelInvalid, ChannelPrivate):
            if ctx.first_time(f"ch-{rec.ch}"):
                await notify_admins(ctx, client, f"⚠️ <b>Channel access error:</b> I cannot read file channel <code>{rec.ch}</code>. "
                                                 "Please verify the bot is an Administrator in that channel.")
            await client.send_message(chat_id, "I can't access that file storage channel right now. Please notify the admin.")
            return False
        except RPCError as e:
            log.warning("copy RPCError: %s", e)
            await client.send_message(chat_id, "Couldn't send that file version. Try another version.")
            return False
        except Exception as e:
            log.exception("unexpected deliver error: %s", e)
            await client.send_message(chat_id, "Something went wrong while sending this file. Please try another version.")
            return False

    if not sent:
        return False

    await ctx.store.bump_hit(rec.film)
    ctx.index.record_open(rec.film)
    ctx.stats.add("f")
    if minutes and getattr(sent, "id", None):
        await ctx.store.schedule_delete(chat_id, sent.id, time.time() + minutes * 60)
    return True



async def _drop(ctx: Ctx, rec: FileRec) -> None:
    if await ctx.store.remove_file(rec.uid):
        ctx.index.adjust_files(rec.film, -1)
        if await ctx.store.remove_film_if_empty(rec.film):
            await asyncio.to_thread(ctx.index.remove_film, rec.film)


async def sweeper(ctx: Ctx, client, tick: int = 15) -> None:
    """Deletes expired search messages and delivered files."""
    n, last_prune = 0, 0.0
    while True:
        await asyncio.sleep(tick)
        n += 1
        try:
            for chat, msg in ctx.due():
                try:
                    await client.delete_messages(chat, msg)
                except Exception:
                    pass
            if n % 2 == 0:
                due = await ctx.store.due_deletes(time.time())
                for _, chat, msg in due:
                    try:
                        await client.delete_messages(chat, msg)
                    except Exception:
                        pass
                await ctx.store.clear_deletes([k for k, _, _ in due])
            if time.time() - last_prune > 86400:
                last_prune = time.time()
                await ctx.store.prune_daily()
        except Exception:
            log.exception("sweeper failed")


# ---------------------------------------------------------------- search
async def _results_view(ctx: Ctx, s: Session):
    back = Btn("‹ Discover", s.back) if s.back else None
    st = await ctx.settings()
    series_grp = st.get("series_group") if s.group else None
    return ui.results_screen(s.hits, s.page, s.qkey, s.corrected, s.year_relaxed, s.year,
                             suggest=s.kind == "suggest", header=s.header, back=back, requestable=not s.header,
                             series_group=series_grp)


async def _film_view(ctx: Ctx, s: Session):
    fid, title, year = s.film
    st = await ctx.settings()
    note, s.note = s.note, ""
    if s.corrected:
        note += f"Showing results for <b>{escape(s.qkey)}</b>.\n"
    if s.year_relaxed and s.year:
        note += f"No {s.year} version here.\n"
    watch = None if s.group else fid in await ctx.watchlist(s.owner)      # a shared group message has no single owner
    series_grp = st.get("series_group") if s.group else None
    return ui.film_screen(title, year, s.variants, s.vpage,
                          can_back=len(s.hits) > 1 or bool(s.header) or s.kind in ("surprise", "mylist"),
                          is_admin=ctx.is_admin(s.owner) and not s.group, fid=fid, note=note, mode=s.mode,
                          picked=fid in st["picks"], watch=watch, shuffle=s.shuffle,
                          series_group=series_grp)


async def _track(ctx: Ctx, sent, sess: Session) -> None:
    ctx.sessions.put(sent.chat.id, sent.id, sess)
    ctx.watch(sent.chat.id, sent.id, (await ctx.settings())["result_ttl_min"])


async def run_search(ctx: Ctx, show, owner: int, text: str, group: bool = False) -> None:
    res = await asyncio.to_thread(ctx.index.search, text, 30)     # up to 5 pages of films
    if res.kind == "empty":
        if not group:
            await show(ui.EMPTY_QUERY)
        return
    ctx.count_search(owner)
    if res.kind == "none":
        # Search retry (Item 10)
        from parse import clean_query_retry
        for cand in clean_query_retry(text):
            r2 = await asyncio.to_thread(ctx.index.search, cand, 30)
            if r2.kind != "none" and r2.hits:
                res = r2
                res.corrected = True
                break

    if res.kind == "none":
        await ctx.store.add_miss(res.query)
        ctx.stats.add("m")
        st = await ctx.settings()
        if group:
            has_year = bool(re.search(r'\b(1[89]\d\d|20\d\d)\b', text))
            if st.get("group_silent_miss", True) and not has_year:
                return  # Silent miss in groups: do not spam group chats when configured
        t, rows = ui.not_found_screen(text, st.get("series_bot"))
        sent = await show(t, rows)
        await _track(ctx, sent, Session(owner, text, "none", [], qkey=res.query, group=group))
        if group:
            ctx.watch(sent.chat.id, sent.id, 1)
        return
    hits = [(h.film.id, h.film.label, h.film.file_count) for h in res.hits]
    sess = Session(owner, text, res.kind, hits, res.corrected, res.year, res.year_relaxed, qkey=res.query,
                   group=group)
    if res.kind == "match":
        f = res.hits[0].film
        sess.film, sess.variants = (f.id, f.title, f.year), await load_variants(ctx, f.id)
        t, rows = await _film_view(ctx, sess)
    else:
        t, rows = await _results_view(ctx, sess)
    await _track(ctx, await show(t, rows), sess)


async def handle_query(ctx: Ctx, client, show, uid: int, text: str, group: bool = False) -> None:
    """Series-first search. The series bot searches its own catalog rather than redirecting away."""
    if getattr(ctx, "series_catalog", None) is not None:
        if not ctx.ready:
            if not group: await show(ui.WARMING)
            return
        # In a group the results are shown to everyone; the join check happens in the bot's DM when a file is chosen.
        if not group and await require_join(ctx, client, uid, show, {"type": "series_search", "text": text, "group": group}):
            return
        import series_handlers
        await series_handlers.search(ctx, client, show, uid, text, group)
        return
    if looks_like_series(text):
        st = await ctx.settings()
        target = (st.get("series_group") if group else None) or st.get("series_bot")
        t, rows = ui.series_redirect_screen(text, target, is_group=group)
        sent = await show(t, rows)
        ctx.watch(sent.chat.id, sent.id, int(st.get("group_result_ttl_min", 7)) if group else st["result_ttl_min"])
        return
    if not ctx.ready:
        if not group:
            await show(ui.WARMING)
        return
    if await require_join(ctx, client, uid, show, {"type": "search", "text": text, "group": group}):
        return
    await run_search(ctx, show, uid, text, group)


async def on_receipt_media(ctx: Ctx, client, m) -> None:
    if not m.from_user or m.chat.type != ChatType.PRIVATE or m.from_user.id in ctx.banned:
        return
    uid = m.from_user.id
    if ctx.is_admin(uid):
        import anime
        if await anime.admin_reply(ctx, client, m): return
    if ctx.is_admin(uid) and uid in ctx.pending_digest_channel:
        fwd = getattr(m, "forward_from_chat", None)
        if fwd and getattr(fwd, "id", None):
            ctx.pending_digest_channel.discard(uid)
            import series_handlers
            if await series_handlers.configure_series_digest_channel(ctx, client, m, fwd.id):
                t, rows = ui.series_digest_settings_screen(await ctx.settings())
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
            return
    if await _supporter_reply(ctx, client, m):
        return


async def on_text(ctx: Ctx, client, m) -> None:
    if not m.from_user or m.from_user.id in ctx.banned:
        return
    uid, text = m.from_user.id, (m.text or "").strip()
    if ctx.is_admin(uid) and m.chat.type == ChatType.PRIVATE:
        import anime
        if await anime.admin_reply(ctx, client, m): return
    if await _supporter_reply(ctx, client, m):
        return
    if m.chat.type == ChatType.PRIVATE and ctx.is_admin(uid) and uid in ctx.pending_digest_channel:
        fwd = getattr(m, "forward_from_chat", None)
        if fwd and getattr(fwd, "id", None):
            ctx.pending_digest_channel.discard(uid)
            import series_handlers
            if await series_handlers.configure_series_digest_channel(ctx, client, m, fwd.id):
                t, rows = ui.series_digest_settings_screen(await ctx.settings())
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
            return
        if text and not text.startswith("/"):
            if text.lower() in ("cancel", "stop"):
                ctx.pending_digest_channel.discard(uid)
                await m.reply_text("Update channel setup cancelled.")
                return
            ctx.pending_digest_channel.discard(uid)
            import series_handlers
            if await series_handlers.configure_series_digest_channel(ctx, client, m, text):
                t, rows = ui.series_digest_settings_screen(await ctx.settings())
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
            return
    if m.chat.type == ChatType.PRIVATE and ctx.is_admin(uid) and getattr(ctx, "pending_parser_rule", None) and uid in ctx.pending_parser_rule:
        if text and not text.startswith("/"):
            import series_handlers
            await series_handlers.handle_parser_rule_text(ctx, m, text)
            return
    if m.chat.type == ChatType.PRIVATE and ctx.is_admin(uid) and uid in ctx.pending_digest_shows and getattr(ctx, "series_catalog", None) is not None:
        dstate = ctx.pending_digest_shows[uid]
        if text and not text.startswith("/"):
            if time.monotonic() - float(dstate.get("at", 0)) > 900:
                ctx.pending_digest_shows.pop(uid, None)
                await m.reply_text("Digest shows prompt expired. Reopen it from /seriesdigest.")
                return
            if text.lower() in ("cancel", "stop"):
                ctx.pending_digest_shows.pop(uid, None)
                await m.reply_text("Cancelled.")
                return
            import series_handlers
            act = dstate.get("action")
            if act in ("cap", "footer", "glink"):
                note = await series_handlers.digest_apply_text_setting(ctx, act, text)
                if note.startswith("✅"):
                    ctx.pending_digest_shows.pop(uid, None)
                    t, rows = ui.series_digest_settings_screen(await ctx.settings())
                    await m.reply_text(note + "\n\n" + t, parse_mode=HTML, reply_markup=kb(rows))
                else:
                    dstate["at"] = time.monotonic()
                    await m.reply_text(note)
                return
            if act == "search":
                dstate["query"] = text
                dstate["at"] = time.monotonic()
                t, rows = await series_handlers.digest_shows_search_screen(ctx, text)
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
                return
            if act == "preadd":
                from series_catalog import show_id
                ctx.pending_digest_shows.pop(uid, None)
                title, yr = series_handlers.parse_preadd_show_spec(text)
                if not title:
                    await m.reply_text("Please send a valid show title.")
                    return
                sid = show_id(title, yr)
                s = await ctx.settings()
                shows = series_handlers.get_digest_shows(s)
                if not any(x["sid"] == sid for x in shows):
                    shows.append({"sid": sid, "title": title, "year": yr, "added_at": time.time(), "preadd": True})
                    await series_handlers.save_digest_shows(ctx, shows)
                t, rows = await series_handlers.digest_shows_menu_screen(ctx, 0)
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
                return
    if m.chat.type == ChatType.PRIVATE and ctx.is_admin(uid) and getattr(ctx, "pending_series_merge", None) and uid in ctx.pending_series_merge and getattr(ctx, "series_catalog", None) is not None:
        mstate = ctx.pending_series_merge[uid]
        if mstate.get("step") in ("search_absorb", "search_survivor") and text and not text.startswith("/"):
            if time.monotonic() - float(mstate.get("at", 0)) > 600:
                ctx.pending_series_merge.pop(uid, None)
                await m.reply_text("Interactive merge session expired. Send /mergeshow to start again.")
                return
            if text.lower() in ("cancel", "stop"):
                ctx.pending_series_merge.pop(uid, None)
                await m.reply_text("Show merge cancelled.")
                return
            matches = await ctx.series_catalog.search_shows(text, 12, 0)
            def _mlabel(d):
                yr = f" ({d['year']})" if d.get("year") else ""
                return f"{d.get('title', 'Series')}{yr} · {int(d.get('count', 0)):,} files"[:58]
            step = mstate["step"]
            if step == "search_absorb":
                rows = [[Btn(_mlabel(d), f"ss:mergepick_abs:{d['_id']}")] for d in matches]
                if not rows:
                    await m.reply_text("No matching shows found. Send another search query, or cancel:",
                                       reply_markup=kb([[Btn("Cancel", "ss:mergecancel")]]))
                    return
                rows.append([Btn("Cancel", "ss:mergecancel")])
                mstate["at"] = time.monotonic()
                await m.reply_text("<b>Interactive Show Merge (Step 1/3)</b>\nSelect the show to <b>absorb</b> (will be merged away):",
                                   parse_mode=HTML, reply_markup=kb(rows))
                return
            absorb = mstate.get("absorb") or {}
            rows = [[Btn(_mlabel(d), f"ss:mergepick_surv:{d['_id']}")]
                    for d in matches if str(d.get("_id")) != str(absorb.get("sid"))]
            if not rows:
                await m.reply_text("No other matching shows found. Send another search query for the surviving show, or cancel:",
                                   reply_markup=kb([[Btn("Cancel", "ss:mergecancel")]]))
                return
            rows.append([Btn("Cancel", "ss:mergecancel")])
            mstate["at"] = time.monotonic()
            await m.reply_text("<b>Interactive Show Merge (Step 2/3)</b>\nSelect the <b>surviving show</b> (to keep):",
                               parse_mode=HTML, reply_markup=kb(rows))
            return
    if m.chat.type == ChatType.PRIVATE and ctx.is_admin(uid) and getattr(ctx, "pending_curated", None) and uid in ctx.pending_curated and getattr(ctx, "series_catalog", None) is not None:
        cstate = ctx.pending_curated[uid]
        if text and not text.startswith("/"):
            if time.monotonic() - float(cstate.get("at", 0)) > 900:
                ctx.pending_curated.pop(uid, None)
                await m.reply_text("Curated list prompt expired. Reopen it from /settings or /curated.")
                return
            if text.lower() in ("cancel", "stop"):
                ctx.pending_curated.pop(uid, None)
                await m.reply_text("Cancelled.")
                return
            import series_handlers
            act = cstate.get("action")
            settings = await ctx.settings()
            lists = series_handlers.get_curated_lists(settings)
            if act == "create":
                ctx.pending_curated.pop(uid, None)
                name = text.strip()[:40]
                lid = series_handlers._new_list_id(name, {x["id"] for x in lists})
                lists.append({"id": lid, "name": name, "sids": []})
                await series_handlers.save_curated_lists(ctx, lists)
                t, rows = await series_handlers.curated_list_screen(ctx, lid, 0)
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
                return
            if act == "rename":
                ctx.pending_curated.pop(uid, None)
                lid = cstate.get("list_id")
                lst = next((x for x in lists if x["id"] == lid), None)
                if lst:
                    lst["name"] = text.strip()[:40] or lst["name"]
                    await series_handlers.save_curated_lists(ctx, lists)
                t, rows = await series_handlers.curated_list_screen(ctx, lid, 0)
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
                return
            if act == "add_search":
                lid = cstate.get("list_id")
                cstate["query"] = text.strip()
                cstate["at"] = time.monotonic()
                t, rows = await series_handlers.curated_search_screen(ctx, lid, cstate["query"])
                await m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))
                return
            if act == "replace":
                ctx.pending_curated.pop(uid, None)
                lid = cstate.get("list_id")
                lst = next((x for x in lists if x["id"] == lid), None)
                if not lst:
                    await m.reply_text("That curated list no longer exists.")
                    return
                titles = [x.strip() for x in text.split("|") if x.strip()]
                chosen, missed = [], []
                for title in titles[:50]:
                    found = await ctx.series_catalog.search_shows(title, 5, 0)
                    match = next((d for d in found if normalize(d.get("title", "")) == normalize(title)), None) or (found[0] if len(found) == 1 else None)
                    if match:
                        chosen.append(str(match.get("_id", match.get("sid"))))
                    else:
                        missed.append(title)
                lst["sids"] = list(dict.fromkeys(chosen))
                await series_handlers.save_curated_lists(ctx, lists)
                t, rows = await series_handlers.curated_list_screen(ctx, lid, 0)
                note = ("\n\n<i>Not found: " + ", ".join(escape(x) for x in missed) + "</i>") if missed else ""
                await m.reply_text(t + note, parse_mode=HTML, reply_markup=kb(rows))
                return
    pend = ctx.pending_admin_reply.get(uid)
    if pend and m.chat.type == ChatType.PRIVATE and ctx.is_admin(uid):
        ctx.pending_admin_reply.pop(uid, None)
        if time.monotonic() - pend["at"] > 900:
            await m.reply_text("That reply session expired (15 minutes). Press Reply on the request again.")
            return
        if text.lower() in ("cancel", "stop"):
            await m.reply_text("Cancelled. Nothing was sent.")
            return
        req = await ctx.store.get_request(pend["key"])
        if not req or not req.get("users"):
            await m.reply_text("That request no longer exists (it was deleted or completed). Nothing was sent.")
            return
        sent = failed = 0
        for u in req["users"]:
            if await _send(client, u, f"💬 <b>Update on your request for '{escape(req['text'])}':</b>\n\n{escape(text)}"):
                sent += 1
            else:
                failed += 1
        note = f" {failed} could not be reached (they blocked the bot or never started it)." if failed else ""
        await m.reply_text(f"✅ Delivered to {sent} of {sent + failed} requester(s) of '{req['text']}'.{note}")
        return
    if m.chat.type == ChatType.PRIVATE and ctx.is_admin(uid) and uid in ctx.pending_claim_days:
        cid = ctx.pending_claim_days.pop(uid)
        claim = await ctx.store.get_claim(cid)
        if not text.isdigit() or not 1 <= int(text) <= 3650:
            ctx.pending_claim_bypass.discard(uid)
            await m.reply_text("Enter a whole number from 1 to 3650 days. Press the custom-days button again if needed.")
            return
        if not claim or claim.get("status") != "pending":
            await m.reply_text("That claim is no longer pending.")
            return
        days = int(text)
        new_date = await ctx.store.donor_add(claim["uid"], days=days, name=claim.get("name", ""))
        bypass_granted = uid in ctx.pending_claim_bypass
        ctx.pending_claim_bypass.discard(uid)
        if bypass_granted:
            await ctx.store.donor_set_join_bypass(claim["uid"], new_date)
        await ctx.store.resolve_claim(cid, f"approved_{days}d")
        await m.reply_text(f"Approved claim {cid} for {days} days. Access expires {new_date}." + (" Force-join bypass granted." if bypass_granted else ""))
        await _send(client, claim["uid"], f"🎉 <b>Donation verified!</b> Thank you for supporting the series library. Your supporter access is active until <b>{new_date}</b>.\n\nChoose whether to feature your name on the Supporters Wall. Active supporters also receive a badge on group search responses and can use /request for custom requests.",
                    _approval_rows(cid))
        return
    if m.chat.type == ChatType.PRIVATE:
        if uid in ctx.pending_stars_amount and text and not text.startswith("/"):
            ctx.pending_stars_amount.discard(uid)
            if not text.isdigit() or not 1 <= int(text) <= 10000:
                await m.reply_text("Please enter a whole number from 1 to 10,000 Stars. Open Donate → Telegram Stars → Custom amount to try again.")
                return
            amount = int(text)
            try:
                await client.send_invoice(
                    chat_id=uid, title="Exivum Series Bot Support",
                    description=f"Support the series library with {amount} Telegram Stars.",
                    payload=f"stars_{uid}_{amount}", currency="XTR",
                    prices=[LabeledPrice(f"Series library support ({amount} Stars)", amount)]
                )
                await m.reply_text(f"Here's your invoice for {amount} Stars.")
            except Exception as e:
                log.exception("custom Stars invoice failed: %s", e)
                await m.reply_text("I couldn't create that Stars invoice. Please try again.")
            return
    if m.reply_to_message and ctx.is_admin(uid):
        rep_msg = m.reply_to_message
        try:
            rep_text = (rep_msg.text.html if rep_msg.text else (rep_msg.caption.html if rep_msg.caption else "")) or ""
        except Exception:
            rep_text = rep_msg.text or rep_msg.caption or ""
        claim_match = re.search(r"Claim ID:</b>\s*<code>([^<]+)</code>", rep_text, re.IGNORECASE)
        if not claim_match:
            claim_match = re.search(r"Receipt for claim\s*<code>([^<]+)</code>", rep_text, re.IGNORECASE)
        if not claim_match:
            claim_match = re.search(r"Claim ID[^A-Za-z0-9_-]*([A-Za-z0-9_-]+)", rep_text, re.IGNORECASE)
        if claim_match:
            claim = await ctx.store.get_claim(claim_match.group(1))
            if claim and claim.get("status") == "pending":
                ok = await deliver_donation_followup(ctx, client, claim, m, from_admin=True)
                await m.reply_text("Message sent to the donor." if ok else "Delivery to the donor failed.")
                return
        qkey = None
        m_key = re.search(r"Key:\s*<code>([^<]+)</code>", rep_text)
        if m_key:
            qkey = m_key.group(1).strip()
        elif rep_msg.reply_markup and getattr(rep_msg.reply_markup, "inline_keyboard", None):
            for row in rep_msg.reply_markup.inline_keyboard:
                for b in row:
                    if b.callback_data and (b.callback_data.startswith("ar:done:") or b.callback_data.startswith("rq:done:")):
                        qkey = b.callback_data.split(":", 2)[2]
                        break
        if qkey:
            req = await ctx.store.get_request(qkey)
            if req and req.get("users"):
                sent = 0
                for u in req["users"]:
                    if await _send(client, u, f"💬 <b>Update on your request for '{escape(req['text'])}':</b>\n\n{escape(text)}"):
                        sent += 1
                await m.reply_text(f"✅ Delivered your reply to {sent} of {len(req['users'])} requester(s).")
                return
    group = m.chat.type in GROUPS
    if group:
        s = await ctx.settings()
        if not s.get("groups", True):
            return
        if getattr(m, "forward_date", None) or getattr(m, "forward_from", None) or getattr(m, "forward_sender_name", None):
            return
        if getattr(m, "photo", None) or getattr(m, "video", None) or getattr(m, "document", None) or getattr(m, "audio", None) or getattr(m, "animation", None) or getattr(m, "sticker", None) or getattr(m, "voice", None) or getattr(m, "video_note", None):
            return
        if re.search(r"https?://|t\.me/|@\w+", text):
            return
        max_len = s.get("group_max_len", 60)
        if not 3 <= len(text) <= max_len or len(text.split()) > 8:
            return
        if s.get("group_mention_only", False):
            me = getattr(client, "me", None)
            bot_user = (me.username if me else ctx.bot_username) or ""
            if bot_user and f"@{bot_user.lower()}" not in text.lower():
                return
    await ctx.track_user(uid)
    if ctx.throttled(uid):
        return
    show = reply_to(m)
    if group and await ctx.is_supporter(uid):
        show = _supporter_show(show, getattr(m.from_user, "first_name", ""))
    await handle_query(ctx, client, show, uid, text, group)


# ---------------------------------------------------------------- start / help / discover
async def send_home(ctx: Ctx, m) -> None:
    s = await ctx.settings()
    if getattr(ctx, "series_catalog", None) is not None and ctx.ready:
        count=(await ctx.series_catalog.counts()).get("shows",0)
    else:
        count=len(ctx.index) if ctx.ready else 0
    text, rows = ui.home_screen(s, getattr(m.from_user, "first_name", "") or "", count)
    if await ctx.is_supporter(m.from_user.id):
        rows.append([Btn("⭐ Custom requests", "customreq:start")])
    p_imgs = s.get("page_images") or {}
    img = p_imgs.get("welcome") or s.get("image")
    if img:
        try:
            await m.reply_photo(img, caption=text, parse_mode=HTML, reply_markup=kb(rows))
            return
        except RPCError as e:
            log.warning("welcome image failed, sending text: %s", e)
    await m.reply_text(text, parse_mode=HTML, reply_markup=kb(rows))


async def cmd_start(ctx: Ctx, client, m) -> None:
    uid = m.from_user.id
    if uid in ctx.banned:
        await m.reply_text(ui.BANNED)
        return
    await ctx.track_user(uid)
    arg = m.command[1] if len(m.command) > 1 else ""
    if arg.startswith("ae_"):
        import anime
        await anime.start(ctx, client, m, arg)
        return
    shared = decode_search(arg)
    if shared:                                           # sent here by the other bot with a search
        await handle_query(ctx, client, reply_to(m), uid, shared)
        return
    if arg.startswith("show_") and getattr(ctx, "series_catalog", None) is not None:
        import series_handlers
        sid = arg[5:]
        if await require_join(ctx, client, uid, reply_to(m), {"type": "series_show", "sid": sid}):
            return
        await series_handlers.open_show_by_id(ctx, reply_to(m), uid, sid)
        return
    if arg.startswith("sm_") and getattr(ctx, "series_catalog", None) is not None:
        movie=await ctx.series_catalog.movies.find_one({"_id":arg[3:]})
        if not movie:
            await m.reply_text("That movie isn't available any more.")
            return
        if await require_join(ctx,client,uid,reply_to(m),{"type":"series_movie","uid":arg[3:]}): return
        try: await client.copy_message(chat_id=m.chat.id,from_chat_id=int(movie["channel"]),message_id=int(movie["message_id"]))
        except Exception:
            log.exception("linked movie start delivery failed")
            await m.reply_text("I couldn't retrieve that movie from its source channel. Please report this item.")
        return
    if arg.startswith("s_") and getattr(ctx, "series_catalog", None) is not None:
        import series_handlers
        if await require_join(ctx, client, uid, reply_to(m), {"type": "series_file", "uid": arg[2:]}):
            return
        rec = await ctx.series_catalog.get_file(arg[2:])
        if rec: await series_handlers.deliver_file(ctx, client, m.chat.id, rec)
        else: await m.reply_text("That file isn't available any more.")
        return
    if arg.startswith("f_"):
        if await require_join(ctx, client, uid, reply_to(m), {"type": "file", "uid": arg[2:]}):
            return
        rec = await ctx.store.get_file(arg[2:])
        if rec:
            await deliver(ctx, client, m.chat.id, rec)
        else:
            await m.reply_text("That file isn't available any more.")
        return
    await send_home(ctx, m)


async def cmd_info(ctx: Ctx, client, m) -> None:
    """/help /about /discover /donate /survey for everyone."""
    if not m.from_user or m.from_user.id in ctx.banned:
        return
    cmd, s = m.command[0].lower(), await ctx.settings()
    p_imgs = s.get("page_images") or {}
    img = p_imgs.get(cmd) or p_imgs.get("welcome") or s.get("image")
    show = reply_to(m, photo=img)
    if cmd == "help":
        t, rows = ui.help_screen(s)
        await show(t, rows)
    elif cmd == "about":
        if getattr(ctx, "series_catalog", None):
            count=(await ctx.series_catalog.counts()).get("shows",0)
            t, rows = ui.about_screen(s, count)
        else:
            t, rows = ui.about_screen(s, len(ctx.index))
        await show(t, rows)
    elif cmd == "discover":
        if getattr(ctx,"series_catalog",None):
            import series_handlers
            t, rows = await series_handlers.discover_menu(ctx, s)
            sent = await show(t, rows)
            if sent:
                ctx.series_sessions[(sent.chat.id, sent.id)] = {
                    "origin": "discover", "discover_which": "menu", "owner": m.from_user.id,
                    "group": m.chat.type in GROUPS, "until": time.monotonic() + 1800,
                }
        else:
            t, rows = ui.discover_screen(s)
            await show(t,rows)
    elif cmd == "mylist":
        if getattr(ctx,"series_catalog",None):
            import series_handlers
            t,rows=await series_handlers.discover_screen(ctx,"mylist",m.from_user.id)
            sent = await show(t,rows)
            if sent:
                ctx.series_sessions[(sent.chat.id, sent.id)] = {
                    "origin": "discover", "discover_which": "mylist", "owner": m.from_user.id,
                    "group": m.chat.type in GROUPS, "until": time.monotonic() + 1800,
                }
        else:
            await show("My List is available in the series bot.")
    elif cmd == "donate":
        methods = s.get("donate_methods", [])
        wall = await ctx.store.get_supporters_wall()
        t, rows = ui.donate_screen(s, methods, wall, ui.contact_link(s, next(iter(sorted(ctx.cfg.admins)), None)))
        await show(t, rows)
    elif cmd == "survey":
        if not s.get("survey_active", True):
            t, rows = ui.survey_closed_screen()
            await show(t, rows)
            return
        u = await ctx.store.get_user(m.from_user.id)
        if u and u.get("sv"):
            t, rows = ui.survey_completed_screen()
            await show(t, rows)
            return
        t, rows = ui.survey_screen(0)
        await show(t, rows)
    else:
        t, rows = ui.discover_screen(s)
        sent = await show(t, rows)
        await _track(ctx, sent, Session(m.from_user.id, "", "discover", [], group=m.chat.type in GROUPS))


async def _discover_hits(ctx: Ctx, which: str, sess: Session) -> tuple[str, list[tuple[str, str, int]]]:
    s = await ctx.settings()
    if which == "pick":
        name = s["picks_name"]
        fids = [f for f in s["picks"] if ctx.index.get(f)]
        if not sess.pick_order:
            random.shuffle(fids)                         # a fresh random order for each viewer...
        else:                                            # ...kept the same while they page through it
            fids = [f for f in sess.pick_order if f in fids] + [f for f in fids if f not in sess.pick_order]
        sess.pick_order = fids
    elif which == "pop":
        name = "Popular this week"
        if ctx.top_cache is None or time.monotonic() - ctx.top_cache[0] > 600:
            ctx.top_cache = (time.monotonic(), [f for f, _ in await ctx.store.top_films(7, 30)])
        fids = ctx.top_cache[1]
    else:
        name, fids = "Recently added", await ctx.store.recent_films(30)
    hits = []
    for fid in fids:
        f = ctx.index.get(fid)
        if f:
            hits.append((f.id, f.label, f.file_count))
    return name, hits


async def _shuffle(ctx: Ctx, sess: Session, uid: int, source: str) -> bool:
    """Surprise me ('picks') or Shuffle ('wl'): a random film that hasn't come up yet in this session."""
    if source == "picks":
        ids = [f for f in (await ctx.settings())["picks"] if ctx.index.get(f)]
    else:
        ids = [f for f in await ctx.watchlist(uid) if ctx.index.get(f)]
    if not ids:
        return False
    pool = [f for f in ids if f not in sess.seen]
    if not pool:                                          # seen them all: start over
        sess.seen.clear()
        pool = ids
        sess.note = "That was all of them. Starting again.\n" if len(ids) > 1 else ""
    fid = random.choice(pool)
    sess.seen.add(fid)
    film = ctx.index.get(fid)
    sess.film, sess.vpage, sess.mode, sess.shuffle = (film.id, film.title, film.year), 0, "", source
    sess.variants = await load_variants(ctx, film.id)
    sess.kind = "surprise" if source == "picks" else "mylist"
    return True


async def _mylist_view(ctx: Ctx, uid: int, sess: Session, page: int = 0):
    wl = await ctx.watchlist(uid)
    films = [f for f in (ctx.index.get(x) for x in wl) if f]
    if len(films) != len(wl):                             # a film left the library: quietly drop it
        await ctx.save_watchlist(uid, [f.id for f in films])
    reqs = await ctx.store.user_requests(uid)
    user = await ctx.store.get_user(uid)
    sess.kind, sess.reqs, sess.page, sess.film, sess.shuffle = "mylist", [r["key"] for r in reqs], page, None, None
    sess.hits = [(f.id, f.label, f.file_count) for f in films]        # so Back from a film returns here
    return ui.mylist_screen([(f.id, f.label) for f in films], page, ctx.cap_for(user), [r["text"] for r in reqs],
                            user.get("sc", 0) + ctx.search_pending.get(uid, 0))


# ---------------------------------------------------------------- callbacks
async def on_cb(ctx: Ctx, client, cb) -> None:
    uid, data = cb.from_user.id, cb.data or ""
    if uid in ctx.banned:
        await cb.answer(ui.BANNED, show_alert=True)
        return
    await ctx.track_user(uid)
    head, _, arg = data.partition(":")
    msg = cb.message
    if head not in ("ae", "afj") and msg is not None and getattr(msg, "chat", None) is not None:
        key = (msg.chat.id, msg.id)
        state = getattr(ctx, "series_sessions", {}).get(key, {})
        if state.get("profile") == "anime" or (uid, *key) in getattr(ctx, "anime_pending", {}):
            await cb.answer("Use the buttons on this anime entry, or start a new search.", show_alert=True)
            return
    if head in ("an", "ae", "afj"):
        import anime
        await anime.callback(ctx, client, cb, head, arg)
        return
    if head == "ss":
        sub, _, subarg = arg.partition(":")
        import series_handlers
        if await series_handlers.callback(ctx, client, cb, sub, subarg):
            return
    if head == "dc" and getattr(ctx, "series_catalog", None) is not None:
        import series_handlers
        s = await ctx.settings()
        p_imgs = s.get("page_images") or {}
        img = p_imgs.get("discover") or p_imgs.get("welcome") or s.get("image")
        t, rows = await series_handlers.discover_menu(ctx, s)
        await edit_of(msg, new_photo=img)(t, rows)
        await cb.answer()
        return
    if head == "ml" and getattr(ctx, "series_catalog", None) is not None:
        import series_handlers
        s = await ctx.settings()
        p_imgs = s.get("page_images") or {}
        img = p_imgs.get("mylist") or p_imgs.get("welcome") or s.get("image")
        t, rows = await series_handlers.discover_screen(ctx, "mylist", uid)
        sent = await edit_of(msg, new_photo=img)(t, rows)
        target_msg = sent if getattr(sent, "id", None) else msg
        ctx.series_sessions[(target_msg.chat.id, target_msg.id)] = {
            "origin": "discover", "discover_which": "mylist", "owner": uid,
            "group": msg.chat.type in GROUPS, "until": time.monotonic() + 1800,
        }
        await cb.answer()
        return
    if head == "noop":
        await cb.answer()
        return
    if head == "fsok":
        await _joined(ctx, client, cb)
        return
    if head == "nav":
        s = await ctx.settings()
        p_imgs = s.get("page_images") or {}
        img = p_imgs.get(arg) or p_imgs.get("welcome") or s.get("image")
        if arg == "help":
            t, rows = ui.help_screen(s)
        elif arg == "about":
            count = ((await ctx.series_catalog.counts()).get("shows", 0)
                     if getattr(ctx, "series_catalog", None) is not None else len(ctx.index))
            t, rows = ui.about_screen(s, count)
        elif arg == "donate":
            methods = s.get("donate_methods", [])
            wall = await ctx.store.get_supporters_wall()
            t, rows = ui.donate_screen(s, methods, wall, ui.contact_link(s, next(iter(sorted(ctx.cfg.admins)), None)))
        elif arg == "discover" and getattr(ctx, "series_catalog", None) is not None:
            import series_handlers
            t, rows = await series_handlers.discover_menu(ctx, s)
        elif arg == "mylist" and getattr(ctx, "series_catalog", None) is not None:
            import series_handlers
            t, rows = await series_handlers.discover_screen(ctx, "mylist", uid)
            img = p_imgs.get("mylist") or p_imgs.get("welcome") or s.get("image")
        elif arg == "discover":
            t, rows = ui.discover_screen(s)
        elif arg == "survey":
            if not s.get("survey_active", True):
                await cb.answer("The community survey is currently closed.", show_alert=True)
                return
            u = await ctx.store.get_user(cb.from_user.id)
            if u and u.get("sv"):
                t, rows = ui.survey_completed_screen()
                img = p_imgs.get("survey") or p_imgs.get("welcome") or s.get("image")
                await edit_of(msg, new_photo=img)(t, rows)
                await cb.answer()
                return
            t, rows = ui.survey_screen(0)
        else:
            count = ((await ctx.series_catalog.counts()).get("shows", 0)
                     if getattr(ctx, "series_catalog", None) is not None else len(ctx.index))
            t, rows = ui.home_screen(s, cb.from_user.first_name or "", count)
            if msg.chat.type == ChatType.PRIVATE and await ctx.is_supporter(uid):
                rows.append([Btn("⭐ Custom requests", "customreq:start")])
            img = p_imgs.get("welcome") or s.get("image")
        await edit_of(msg, new_photo=img)(t, rows)
        await cb.answer()
        return
    if head == "rep":
        await msg.edit_reply_markup(kb(ui.report_rows(arg)))
        await cb.answer()
        return
    if head == "rr":
        await _report(ctx, client, cb, arg)
        return
    if head in ("st", "sdg", "pr", "rmfile", "rdis", "ixstop", "auditstop", "ddy", "ddn", "sk", "skt", "psy", "psn", "gst", "clm", "ar", "rqpg"):
        if not ctx.is_admin(uid):
            await cb.answer("Admins only.", show_alert=True)
            return
        await _admin_cb(ctx, client, cb, head, arg)
        return
    if head == "dn":
        if msg.chat.type != ChatType.PRIVATE or msg.chat.id != uid:
            await cb.answer("Open the bot privately to use Donate.", show_alert=True)
            return
        s = await ctx.settings()
        methods = s.get("donate_methods", [])
        if arg == "myclaim":
            claim = await ctx.store.pending_claim_for_receipt(uid)
            if not claim:
                await cb.answer("No pending donation claim. Choose a payment method to start one.", show_alert=True)
                return
            await _claim_prompt(client, uid, str(claim.get("id", claim.get("_id"))), not claim.get("receipt_submitted"))
            await cb.answer("Reply to the new donation prompt.")
            return
        if arg.startswith("contact:"):
            cid = arg.split(":", 1)[1]
            claim = await ctx.store.get_claim(cid)
            if not claim or int(claim.get("uid", 0)) != uid:
                await cb.answer("I couldn't find that claim for your account.", show_alert=True)
                return
            if msg.chat.type != ChatType.PRIVATE or msg.chat.id != uid:
                await cb.answer("Open the bot privately to discuss your claim.", show_alert=True)
                return
            await _claim_prompt(client, uid, cid, receipt=False)
            await cb.answer("Reply to the new donation prompt.")
            return
        if arg == "stars":
            t, rows = ui.stars_screen(uid)
            await edit_of(msg)(t, rows)
            await cb.answer()
            return
        elif arg == "stcustom":
            ctx.pending_stars_amount.add(uid)
            await _send(client, uid, "Enter the number of Telegram Stars you want to contribute (1–10,000). I'll send an invoice for that exact amount.")
            await cb.answer("Enter your custom Stars amount in the chat.")
            return
        elif arg.startswith("st:"):
            stars_amt = int(arg.split(":")[1])
            try:
                await client.send_invoice(
                    chat_id=msg.chat.id,
                    title="Exivum Series Bot Support",
                    description=f"Support the series library with {stars_amt} Telegram Stars.",
                    payload=f"stars_{uid}_{stars_amt}",
                    currency="XTR",
                    prices=[LabeledPrice(f"Series library support ({stars_amt} Stars)", stars_amt)]
                )
                await cb.answer("Invoice sent above! Tap to pay.")
            except Exception as e:
                log.exception("send_invoice failed: %s", e)
                await cb.answer("Could not send Stars invoice. Try another method.", show_alert=True)
            return
        elif arg == "myid":
            await cb.answer(f"Your User ID is: {uid}\n(Send this to the admin to claim perks)", show_alert=True)
            return
        elif arg.startswith("claimidx:") or arg.startswith("claim:"):
            if arg.startswith("claimidx:"):
                raw_idx = arg.split(":", 1)[1]
                if not raw_idx.isdigit() or int(raw_idx) >= len(methods):
                    await cb.answer("That payment method is no longer available. Please reopen Donate.", show_alert=True)
                    return
                method_name = methods[int(raw_idx)].get("title", "Donation")
            else:
                method_name = arg.split(":", 1)[1]
            user_name = cb.from_user.first_name or f"User {uid}"
            ok, cid = await ctx.store.add_claim(uid, user_name, method_name)
            existing = await ctx.store.get_claim(cid)
            if not existing or int(existing.get("uid", 0)) != uid:
                await cb.answer("Claim not found. Please reopen Donate.", show_alert=True)
                return
            await _claim_prompt(client, uid, cid, receipt=existing.get("status") == "pending" and not existing.get("receipt_submitted"))
            await cb.answer("Reply to the new donation prompt.", show_alert=True)
            return
        elif arg.startswith("m:") and arg[2:].isdigit():
            idx = int(arg[2:])
            if idx < len(methods):
                m_info = methods[idx]
                t, rows = ui.donate_method_screen(m_info, idx, uid)
                qr = m_info.get("qr")
                if qr:
                    try:
                        await msg.reply_photo(qr, caption=t, parse_mode=HTML, reply_markup=kb(rows))
                        await cb.answer()
                        return
                    except Exception:
                        pass
                await edit_of(msg)(t, rows)
                await cb.answer()
                return
        elif arg == "wall" or arg.startswith("wallpage:"):
            wall = await ctx.store.get_supporters_wall()
            page = arg.split(":", 1)[-1]
            t, rows = ui.wall_screen(wall, int(page) if page.isdigit() else 0)
            await edit_of(msg)(t, rows)
            await cb.answer()
            return
        wall = await ctx.store.get_supporters_wall()
        t, rows = ui.donate_screen(s, methods, wall, ui.contact_link(s, next(iter(sorted(ctx.cfg.admins)), None)))
        await edit_of(msg)(t, rows)
        await cb.answer()
        return
    if head == "wall":
        if msg.chat.type != ChatType.PRIVATE or msg.chat.id != uid:
            await cb.answer("Manage your wall preference privately.", show_alert=True)
            return
        if arg not in ("opt:yes", "opt:no", "1", "0", "yes", "no"):
            await cb.answer("Unknown preference.", show_alert=True)
            return
        flag = arg in ("opt:yes", "1", "yes")
        if flag and not await ctx.is_supporter(uid):
            await cb.answer("Only active supporters can join the wall.", show_alert=True)
            return
        if flag:
            # Existing manually granted supporters may not have a display name.
            await ctx.store.set_wall_name(uid, (cb.from_user.first_name or "Supporter")[:60])
        await ctx.store.set_wall_opt_in(uid, flag)
        await cb.answer("Your wall preference has been saved.", show_alert=True)
        return
    if head == "customreq":
        if msg.chat.type != ChatType.PRIVATE or msg.chat.id != uid:
            await cb.answer("Open the bot privately and use /request.", show_alert=True)
            return
        await _custom_request_prompt(ctx, client, uid)
        await cb.answer()
        return
    if head == "sv":
        s = await ctx.settings()
        if not s.get("survey_active", True):
            await cb.answer("The community survey is currently closed.", show_alert=True)
            return
        u = await ctx.store.get_user(uid)
        if u and u.get("sv"):
            t, rows = ui.survey_completed_screen()
            await edit_of(msg)(t, rows)
            await cb.answer("You have already completed the survey!", show_alert=True)
            return
        step_str, _, choice = arg.partition(":")
        step = int(step_str) if step_str.isdigit() else 0
        if choice == "az":
            t, rows = ui.survey_az_buckets_screen()
            await edit_of(msg)(t, rows)
            await cb.answer()
            return
        elif choice.startswith("az:"):
            t, rows = ui.survey_screen(2, extra=choice)
            await edit_of(msg)(t, rows)
            await cb.answer()
            return
        elif choice == "main":
            t, rows = ui.survey_screen(2)
            await edit_of(msg)(t, rows)
            await cb.answer()
            return
        elif choice:
            sess = ctx.sessions.get(msg.chat.id, msg.id)
            if not sess:
                sess = Session(uid, "", "survey", [])
                ctx.sessions.put(msg.chat.id, msg.id, sess)
            if not hasattr(sess, "sv_answers"):
                sess.sv_answers = {}
            q_keys = ["gender", "age", "country", "discovery"]
            if step < len(q_keys):
                sess.sv_answers[q_keys[step]] = choice
            next_step = step + 1
            if next_step >= 4:
                u_fresh = await ctx.store.get_user(uid)
                if not (u_fresh and u_fresh.get("sv")):
                    await ctx.store.record_survey(uid, sess.sv_answers)
                    ctx.stats.add("sv")
                t, rows = ui.survey_screen(4)
            else:
                t, rows = ui.survey_screen(next_step)
            await edit_of(msg)(t, rows)
            await cb.answer()
            return
        t, rows = ui.survey_screen(0)
        await edit_of(msg)(t, rows)
        await cb.answer()
        return
    if head == "dc":
        s = await ctx.settings()
        t, rows = ui.discover_screen(s)
        p_imgs = s.get("page_images") or {}
        img = p_imgs.get("discover") or p_imgs.get("welcome") or s.get("image")
        await edit_of(msg, new_photo=img)(t, rows)
        sess = ctx.sessions.get(msg.chat.id, msg.id)
        if not sess:
            sess = Session(uid, "", "discover", [], group=msg.chat.type in GROUPS)
            ctx.sessions.put(msg.chat.id, msg.id, sess)
        else:
            sess.kind, sess.hits, sess.header, sess.back, sess.film, sess.shuffle = "discover", [], None, None, None, None
        await cb.answer()
        return

    sess = ctx.sessions.get(msg.chat.id, msg.id)
    if head == "ml" and arg == "" and not (sess and sess.kind == "mylist"):      # My list, opened from the home page
        mine = Session(uid, "", "mylist", [], group=msg.chat.type in GROUPS)
        ctx.sessions.put(msg.chat.id, msg.id, mine)
        t, rows = await _mylist_view(ctx, uid, mine)
        p_imgs = (await ctx.settings()).get("page_images") or {}
        img = p_imgs.get("mylist") or p_imgs.get("welcome") or (await ctx.settings()).get("image")
        await edit_of(msg, new_photo=img)(t, rows)
        await cb.answer()
        return
    if not sess:
        await cb.answer(ui.EXPIRED, show_alert=True)
        return
    if sess.owner != uid and not sess.group:               # in groups anyone can tap; files go to their own DM
        await cb.answer(ui.NOT_YOURS, show_alert=True)
        return
    ctx.watch(msg.chat.id, msg.id, (await ctx.settings())["result_ttl_min"])      # activity pushes expiry back
    show = edit_of(msg)

    if head == "dx":
        if sess.group and uid != sess.owner and not ctx.is_admin(uid):
            await cb.answer("Only the person who opened this can close it.", show_alert=True)
            return
        ctx.expiry.pop((msg.chat.id, msg.id), None)
        ctx.sessions.drop(msg.chat.id, msg.id)
        try:
            await msg.delete()
        except RPCError:
            pass
        await cb.answer()
        return
    if head == "dl":
        name, hits = await _discover_hits(ctx, arg, sess)
        sess.kind, sess.hits, sess.header, sess.back, sess.page, sess.film, sess.shuffle = \
            "list", hits, name, "dc", 0, None, None
        if hits:
            t, rows = await _results_view(ctx, sess)
        else:
            t, rows = f"<b>{escape(name)}</b>\nNothing here yet.", [[Btn("‹ Discover", "dc")]]
        await show(t, rows)
    elif head == "r" and arg.isdigit():
        sess.page = int(arg)
        t, rows = await _results_view(ctx, sess)
        await show(t, rows)
    elif head == "b":
        sess.film, sess.mode, sess.shuffle = None, "", None
        if sess.kind == "surprise":                            # Back from Surprise me goes to Discover
            sess.kind, sess.hits, sess.header, sess.back = "discover", [], None, None
            t, rows = ui.discover_screen(await ctx.settings())
        elif sess.kind == "mylist":
            t, rows = await _mylist_view(ctx, uid, sess, sess.page)
        else:
            t, rows = await _results_view(ctx, sess)
        await show(t, rows)
    elif head == "f":
        film = ctx.index.get(arg)
        if not film:
            await cb.answer("That one's gone.", show_alert=True)
            return
        sess.film, sess.vpage, sess.mode, sess.shuffle = (film.id, film.title, film.year), 0, "", None
        sess.variants = await load_variants(ctx, film.id)
        t, rows = await _film_view(ctx, sess)
        await show(t, rows)
    elif head == "sur":
        if not await _shuffle(ctx, sess, uid, "picks"):
            await cb.answer("There's nothing in the list yet.", show_alert=True)
            return
        t, rows = await _film_view(ctx, sess)
        await show(t, rows)
    elif head == "wl" and sess.film and not sess.group:
        wl = list(await ctx.watchlist(uid))
        if arg in wl:
            wl.remove(arg)
            await ctx.save_watchlist(uid, wl)
            await cb.answer("Removed from your watchlist.")
        else:
            cap = await ctx.watchlist_cap(uid)
            if len(wl) >= cap:
                await cb.answer(f"Your watchlist is full ({cap} films). Remove one in My list first.", show_alert=True)
                return
            await ctx.save_watchlist(uid, wl + [arg])
            await cb.answer("Added to your watchlist.")
        t, rows = await _film_view(ctx, sess)
        await show(t, rows)
        return
    elif head == "ml":
        kind, _, rest = arg.partition(":")
        if kind == "s":
            if not await _shuffle(ctx, sess, uid, "wl"):
                await cb.answer("Add a film to your watchlist first.", show_alert=True)
                return
            t, rows = await _film_view(ctx, sess)
            await show(t, rows)
            return
        page = sess.page
        if kind == "p" and rest.isdigit():
            page = int(rest)
        elif kind == "r":
            await ctx.save_watchlist(uid, [f for f in await ctx.watchlist(uid) if f != rest])
        elif kind == "c" and rest.isdigit() and int(rest) < len(sess.reqs):
            await ctx.store.cancel_request(sess.reqs[int(rest)], uid)
        t, rows = await _mylist_view(ctx, uid, sess, page)
        await show(t, rows)
    elif head == "v" and arg.isdigit() and sess.film:
        sess.vpage, sess.mode = int(arg), ""
        t, rows = await _film_view(ctx, sess)
        await show(t, rows)
    elif head == "s" and arg.isdigit() and sess.film and int(arg) < len(sess.variants):
        rec = sess.variants[int(arg)]
        if msg.chat.type in GROUPS:
            await cb.answer(url=ctx.deep_link(rec.uid))      # opens the bot in private, then delivers
            return
        if await require_join(ctx, client, uid, reply_to(msg), {"type": "file", "uid": rec.uid}):
            await cb.answer()
            return
        await cb.answer("Sending…")
        await deliver(ctx, client, msg.chat.id, rec)
        return
    elif head == "rq":
        await _request(ctx, client, cb, sess, msg.chat.type in GROUPS)
        return
    elif head in ("rm", "rd", "rmy", "pk") and sess.film:
        if not ctx.is_admin(uid):
            await cb.answer("Admins only.", show_alert=True)
            return
        await _film_admin(ctx, cb, sess, head, arg, show)
        return
    await cb.answer()


async def _film_admin(ctx: Ctx, cb, sess: Session, head: str, arg: str, show) -> None:
    if head == "rm":
        sess.mode = "remove"
    elif head == "rd" and arg.isdigit() and int(arg) < len(sess.variants):
        t, rows = ui.confirm_remove_screen(sess.variants[int(arg)], rec_lines(sess.variants[int(arg)]), sess.vpage)
        await show(t, rows)
        await cb.answer()
        return
    elif head == "rmy":
        rec = await ctx.store.remove_file(arg)
        if rec:
            ctx.index.adjust_files(rec.film, -1)
            if await ctx.store.remove_film_if_empty(rec.film):
                await asyncio.to_thread(ctx.index.remove_film, rec.film)
        sess.variants = [v for v in sess.variants if v.uid != arg]
        sess.mode = ""
    elif head == "pk":
        picks = list((await ctx.settings())["picks"])
        if arg in picks:
            picks.remove(arg)
            await cb.answer("Removed from the list.")
        else:
            picks.append(arg)
            await cb.answer("Added to the list.")
        await ctx.set_setting("picks", picks)
        t, rows = await _film_view(ctx, sess)
        await show(t, rows)
        return
    t, rows = await _film_view(ctx, sess)
    await show(t, rows)
    await cb.answer()


async def _request(ctx: Ctx, client, cb, sess: Session, group: bool) -> None:
    qkey = _film_request_key(sess.query) or sess.qkey
    count, new, added = await ctx.store.add_request(qkey, sess.query[:80], cb.from_user.id)
    if added:
        ctx.stats.add("r")
    is_sup = await ctx.is_supporter(cb.from_user.id)
    sup_tag = "⭐ [SUPPORTER] " if is_sup else ""
    extra = " Open me in private and press Start, or I can't message you." if group else ""
    await cb.answer("Requested. I'll message you when it's added." + extra, show_alert=True)
    if new or (added and (count in REQUEST_PINGS or is_sup)):
        rq_rows = [
            [Btn("✅ Done", f"ar:done:{qkey}"), Btn("💬 Reply", f"ar:rep:{qkey}"), Btn("🗑️ Delete", f"ar:rm:{qkey}")]
        ]
        await notify_inbox(ctx, client, f"{sup_tag}Request: <code>{escape(sess.query)}</code>  ({count} "
                                        f"{'person' if count == 1 else 'people'})\nKey: <code>{qkey}</code>", rq_rows)


async def _report(ctx: Ctx, client, cb, arg: str) -> None:
    code, _, uid_arg = arg.partition(":")
    if code == "x":                                             # cancelled
        await cb.message.edit_reply_markup(kb([[Btn("Report a problem", f"rep:{uid_arg}")]]))
        await cb.answer()
        return
    if code not in ui.REASON_NAMES:
        await cb.answer()
        return
    rec = await ctx.store.get_file(uid_arg)
    await cb.message.edit_reply_markup(kb([[Btn("Reported, thanks", "noop")]]))
    await cb.answer("Thanks, I've passed it on.", show_alert=True)
    if not rec:
        return
    total, reasons, fresh = await ctx.store.add_report(uid_arg, code, cb.from_user.id)
    if fresh:
        ctx.stats.add("p")
    if fresh and (total == 1 or total in REPORT_PINGS):
        film = ctx.index.get(rec.film)
        l1, l2 = rec_lines(rec)
        why = ", ".join(f"{ui.REASON_NAMES.get(c, c)} ×{n}" for c, n in reasons.items())
        await notify_inbox(
            ctx, client,
            f"Problem report ({total}): <b>{escape(film.label if film else rec.film)}</b>\n"
            f"{escape(l1)}\n{escape(l2)}\n\n<b>Reason:</b> {escape(why)}",
            [[Btn("Remove this file", f"rmfile:{rec.uid}"), Btn("Dismiss", f"rdis:{rec.uid}")]])


async def settings_view(ctx: Ctx):
    """The settings screen with a live summary of the catalogue, skipped files and database mode."""
    lines = []
    cat = getattr(ctx, "series_catalog", None)
    try:
        if cat is not None:
            c = await cat.counts(); sk = await cat.skip_counts()
            lines.append(f"{c.get('shows', 0):,} shows · {c.get('files', 0):,} files")
            lines.append(f"Skipped: {sk['total']:,} (films {sk['film']:,} · unrecognised {sk['unrecognised_series']:,} · ambiguous {sk['ambiguous']:,} · unreadable {sk['unreadable']:,} · look-alikes {sk['lookalike']:,})")
            from multidb import FederatedDB
            db = cat.files.database
            if isinstance(db, FederatedDB):
                st = db.status()
                lines.append(f"Two databases: new files go to the {st['active']} ({'manual' if st['manual'] else 'automatic'}, switch at {st['switch_mb']} MB)")
            else:
                lines.append("One database (set DATABASE_URI_2 to add a second)")
            s = await ctx.settings()
            lines.append(f"Promo words: {len(s.get('series_promos') or [])} custom")
    except Exception:
        log.warning("settings summary failed", exc_info=True)
    return ui.settings_screen(await ctx.settings(), {"lines": lines} if lines else None)


async def _admin_cb(ctx: Ctx, client, cb, head: str, arg: str) -> None:
    show = edit_of(cb.message)
    if head == "st":
        s = await ctx.settings()
        if arg == "ad":
            cur = s["autodelete_min"]
            await ctx.set_setting("autodelete_min", AD_CHOICES[(AD_CHOICES.index(cur) + 1) % len(AD_CHOICES)]
                                   if cur in AD_CHOICES else 0)
        elif arg == "rt":
            cur = s["result_ttl_min"]
            await ctx.set_setting("result_ttl_min", TTL_CHOICES[(TTL_CHOICES.index(cur) + 1) % len(TTL_CHOICES)]
                                   if cur in TTL_CHOICES else 4)
        elif arg == "grt":
            cur = s.get("group_result_ttl_min", 7)
            await ctx.set_setting("group_result_ttl_min", TTL_CHOICES[(TTL_CHOICES.index(cur) + 1) % len(TTL_CHOICES)]
                                   if cur in TTL_CHOICES else 7)
        elif arg == "ct":
            await ctx.set_setting("caption_transfer", not s.get("caption_transfer", True))
        elif arg == "pc":
            await ctx.set_setting("protect", not s["protect"])
        elif arg == "gr":
            await ctx.set_setting("groups", not s["groups"])
        elif arg == "se":
            await ctx.set_setting("index_series", not s["index_series"])
        elif arg == "ap":
            await ctx.set_setting("auto_approve", not s["auto_approve"])
        elif arg == "stars":
            await ctx.set_setting("stars_enabled", not s.get("stars_enabled", True))
        elif arg == "sv":
            await ctx.set_setting("survey_active", not s.get("survey_active", True))
        elif arg == "to_groups":
            t, rows = ui.group_settings_screen(s)
            await show(t, rows)
            return
        elif arg == "curated":
            if getattr(ctx, "series_catalog", None) is None:
                await cb.answer("Series catalogue is not connected.", show_alert=True)
                return
            import series_handlers
            t, rows = await series_handlers.curated_menu_screen(ctx)
            await show(t, rows)
            await cb.answer()
            return
        elif arg == "sdigest":
            t, rows = ui.series_digest_settings_screen(s)
            await show(t, rows)
            await cb.answer()
            return
        elif arg == "prules":
            import series_handlers
            t, rows = await series_handlers.parser_rules_menu_screen(ctx)
            await show(t, rows)
            await cb.answer()
            return
        elif arg == "refresh":
            t, rows = await settings_view(ctx)
            await show(t, rows)
            return
        t, rows = await settings_view(ctx)
        await show(t, rows)
    elif head == "sdg":
        import series_handlers
        await series_handlers.series_digest_cb(ctx, client, cb, show, arg)
        return
    elif head == "pr":
        import series_handlers
        await series_handlers.parser_rules_cb(ctx, client, cb, show, arg)
        return
    elif head == "gst":
        s = await ctx.settings()
        if arg == "grp":
            await ctx.set_setting("groups", not s.get("groups", True))
        elif arg == "men":
            await ctx.set_setting("group_mention_only", not s.get("group_mention_only", False))
        elif arg == "sil":
            await ctx.set_setting("group_silent_miss", not s.get("group_silent_miss", True))
        elif arg == "len":
            lens = [40, 60, 80, 100]
            cur = s.get("group_max_len", 60)
            nxt = lens[(lens.index(cur) + 1) % len(lens)] if cur in lens else 60
            await ctx.set_setting("group_max_len", nxt)
        t, rows = ui.group_settings_screen(await ctx.settings())
        await show(t, rows)
    elif head == "clm":
        action, _, cid = arg.partition(":")
        claim = await ctx.store.get_claim(cid)
        if action == "reply":
            if not claim:
                await cb.answer("Claim not found.", show_alert=True)
                return
            await _send_reply_prompt(client, cb.from_user.id, f"<b>Donation admin reply</b> · <code>{escape(cid)}</code>\n\nReply to THIS message with your reply to the donor. Ignore to cancel.")
            await cb.answer("Reply to the new prompt in your private chat.")
            return
        if not claim or claim.get("status") != "pending":
            await cb.answer(f"Claim already {claim.get('status', 'resolved') if claim else 'not found'}.", show_alert=True)
            return
        if action in ("custom", "bypass"):
            ctx.pending_claim_days[cb.from_user.id] = cid
            ctx.pending_claim_bypass.discard(cb.from_user.id)
            if action == "bypass":
                ctx.pending_claim_bypass.add(cb.from_user.id)
            await _send(client, cb.from_user.id,
                        f"How many days of supporter access should I grant for claim <code>{escape(cid)}</code>? Enter a whole number from 1 to 3650."
                        + (" This approval will also grant force-join bypass for the same period." if action == "bypass" else ""))
            await cb.answer("Enter the number of days in your private chat with the bot.")
            return
        if action == "dec":
            await ctx.store.resolve_claim(cid, "declined")
            await show(f"❌ <b>Claim Declined</b> (by {escape(cb.from_user.first_name)})\nUser: <code>{claim['uid']}</code>")
            await cb.answer("Claim declined.")
            await _send(client, claim["uid"], "⚠️ Your donation claim could not be verified. If you think this is an error or need to clarify the transfer, you can message the admin below.",
                        [[Btn("💬 Discuss this claim", f"dn:contact:{cid}")]])
        elif action in ("30", "60", "90"):
            days = int(action)
            new_date = await ctx.store.donor_add(claim["uid"], days=days, name=claim["name"])
            await ctx.store.resolve_claim(cid, f"approved_{days}d")
            await show(f"✅ <b>Claim Approved ({days} days)</b> by {escape(cb.from_user.first_name)}\nUser: <code>{claim['uid']}</code>\nExpires: {new_date}")
            await cb.answer(f"Approved {days} days!")
            await _send(client, claim["uid"], f"🎉 <b>Donation verified!</b> Thank you for supporting the series library. Your supporter access is active for <b>{days} days</b>, until <b>{new_date}</b>.\n\nChoose whether to feature your name on the Supporters Wall. Active supporters also receive a badge on group search responses and can use /request for custom requests.",
                        _approval_rows(cid))
        return
    elif head == "ar":
        action, _, qkey = arg.partition(":")
        req = await ctx.store.get_request(qkey)
        if not req or not req.get("open"):
            await cb.answer("Request not found or already closed.", show_alert=True)
            return
        if action == "done":
            await ctx.store.close_request(qkey)
            film_name = req["text"]
            for u in req.get("users", []):
                await _send(client, u, f"🎬 <b>Good news!</b> Your requested title <b>{escape(film_name)}</b> has been added to the library! You can search and stream/download it now.")
            admin_name = cb.from_user.first_name or "Admin"
            orig = cb.message.text or cb.message.caption or ""
            await show(f"{orig}\n\n✅ <b>Completed by {escape(admin_name)}</b>", rows=[[Btn("✅ Completed", "noop")]])
            await cb.answer(f"Marked done! Notified {len(req.get('users', []))} user(s).")
        elif action == "rm":
            await ctx.store.delete_request(qkey)
            await show(f"🗑️ <b>Request deleted by {escape(cb.from_user.first_name)}</b>")
            await cb.answer("Request deleted.")
        elif action == "rep":
            admin_id = cb.from_user.id
            if not ctx.is_admin(admin_id):
                await cb.answer("Admins only.", show_alert=True)
                return
            ctx.pending_admin_reply[admin_id] = {"key": qkey, "at": time.monotonic()}
            ok = await _send(client, admin_id,
                             f"💬 <b>Replying to {len(req.get('users', []))} requester(s)</b> of <b>{escape(req['text'])}</b>.\n\n"
                             "Type your message now and send it here. It will be delivered to them as an update on their request.\n"
                             "Send the word <b>cancel</b> to cancel (this expires in 15 minutes).")
            if ok:
                await cb.answer("I sent you a private message. Type your reply there.", show_alert=True)
            else:
                ctx.pending_admin_reply.pop(admin_id, None)
                await cb.answer("I can't message you privately yet. Open the bot, press Start, then press Reply again.", show_alert=True)
        return
    elif head == "rqpg":
        page = int(arg) if arg.isdigit() else 0
        all_reqs = await ctx.store.open_requests(500)
        total = len(all_reqs)
        per_page = 10
        pages = max(1, (total + per_page - 1) // per_page)
        page = max(0, min(page, pages - 1))
        slice_reqs = all_reqs[page * per_page:(page + 1) * per_page]
        lines = [f"<b>Open Requests ({total} total, page {page + 1}/{pages}):</b>\n"]
        for r in slice_reqs:
            lines.append(f"• <code>{escape(r['text'])}</code> ({len(r['users'])} users) — <code>/delreq {escape(r['key'])}</code>")
        rows = []
        nav = []
        if page > 0:
            nav.append(Btn("‹ Prev", f"rqpg:{page - 1}"))
        if page < pages - 1:
            nav.append(Btn("Next ›", f"rqpg:{page + 1}"))
        if nav:
            rows.append(nav)
        await show("\n".join(lines), rows=rows)
        await cb.answer()
        return
    elif head == "rmfile":
        rec = await ctx.store.remove_file(arg)
        if rec:
            ctx.index.adjust_files(rec.film, -1)
            if await ctx.store.remove_film_if_empty(rec.film):
                await asyncio.to_thread(ctx.index.remove_film, rec.film)
        await ctx.store.clear_reports(arg)
        await show("File removed from the database.")
    elif head == "rdis":
        await ctx.store.clear_reports(arg)
        await show("Dismissed.")
    elif head == "auditstop":
        if getattr(ctx, "audit_job", None):
            ctx.audit_job["stop"] = True
        await cb.answer("Stopping after this batch.")
        return
    elif head == "ixstop":
        if ctx.index_job:
            ctx.index_job["stop"] = True
        await cb.answer("Stopping after this batch.")
        return
    elif head == "ddy":
        items, ctx.pending_dedupe = ctx.pending_dedupe or [], None
        if not items:
            await show("That list expired. Run /dedupe again.")
        else:
            await show(f"Removing {len(items):,} copies…")
            n = await ctx.store.delete_files(items)
            per: dict[str, int] = {}
            for i in items:
                per[i["film"]] = per.get(i["film"], 0) + 1
            for f, c in per.items():
                ctx.index.adjust_files(f, -c)
            await show(f"Removed {n:,} look-alike copies and kept one of each. "
                       "They're listed under /skipped, Look-alikes, with the copy that was kept.")
    elif head == "ddn":
        ctx.pending_dedupe = None
        await show("Okay, nothing removed.")
    elif head == "psy":
        items, ctx.pending_purge = ctx.pending_purge or [], None
        if not items:
            await show("That list expired. Run /purgeseries again.")
        elif ctx.maintenance:
            await show("Another big job is running. Try again when it's done.")
        else:
            await show(f"Deleting {len(items):,} files in small batches…")
            asyncio.create_task(_purge_delete(ctx, cb.message, items))
    elif head == "psn":
        ctx.pending_purge = None
        await show("Okay, nothing deleted.")
    elif head == "sk":
        await _skipped_view(ctx, show, arg)
    elif head == "skt":
        await _skipped_file(ctx, client, cb.message.chat.id, arg)
    await cb.answer()


# ---------------------------------------------------------------- indexing
@dataclass
class IndexSummary:
    seen: int = 0
    added: int = 0
    known: int = 0
    lookalike: int = 0
    series: int = 0
    unreadable: int = 0
    other: int = 0

    def add(self, o: "IndexSummary") -> None:
        for k in self.__dataclass_fields__:
            setattr(self, k, getattr(self, k) + getattr(o, k))


def _media_of(m):
    """(media object, extension) for files worth indexing, else (None, None)."""
    media = m.document or m.video
    if not media:
        return None, None
    name = getattr(media, "file_name", None) or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if m.video or ext in VIDEO_EXTS or ext in SUB_EXTS or str(getattr(media, "mime_type", "") or "").startswith("video/"):
        return media, ext
    return None, None


async def index_messages(ctx: Ctx, msgs) -> IndexSummary:
    s = IndexSummary()
    cfg = await ctx.store.settings()
    recs: list[FileRec] = []
    films: dict[str, tuple] = {}
    notes: list[dict] = []
    for m in msgs:
        if not (m.document or m.video or m.audio):
            continue
        s.seen += 1
        media, _ = _media_of(m)
        if media is None:
            s.other += 1
            continue
        fname = getattr(media, "file_name", None)
        size = media.file_size or 0
        p = parse_best(fname, m.caption)
        if is_junk(p.title) or not normalize(p.title):
            s.unreadable += 1
            notes.append({"kind": "unreadable", "uid": media.file_unique_id, "name": fname or m.caption or "(no name)",
                          "ch": m.chat.id, "mid": m.id, "size": size})
            continue
        if p.kind == "series" and not cfg["index_series"]:
            s.series += 1
            notes.append({"kind": "series", "uid": media.file_unique_id, "name": fname or p.raw,
                          "ch": m.chat.id, "mid": m.id, "size": size})
            continue
        fid = film_id(normalize(p.title), p.year)
        films[fid] = (p.title, p.year)
        recs.append(FileRec(media.file_unique_id, fid, m.chat.id, m.id, size, p.resolution,
                            p.source, p.languages, (fname or "")[:120], p.codec, p.group, p.tags, p.ext))
    new, known, look = await ctx.store.add_files(recs, films)
    s.added, s.known, s.lookalike = len(new), known, len(look)
    for r, kept in look:
        notes.append({"kind": "lookalike", "uid": r.uid, "name": r.name, "ch": r.ch, "mid": r.mid,
                      "size": r.size, "keep": kept})
    await ctx.store.note_skips(notes)
    skipped_now = {r.uid for r, _ in look}
    await ctx.store.clear_skips([r.uid for r in recs if r.uid not in skipped_now])   # readable now: off the list
    per: dict[str, int] = {}
    for r in new:
        per[r.film] = per.get(r.film, 0) + 1
    if per:
        await asyncio.to_thread(ctx.index.add_films, [(films[f][0], films[f][1], n, 0, ()) for f, n in per.items()])
    return s


async def on_channel_file(ctx: Ctx, client, m) -> None:
    """Copy only the small media metadata into the queue; do no DB or parsing work here."""
    if not m or not getattr(m, "chat", None) or m.chat.id not in ctx.cfg.file_channels:
        return
    if getattr(ctx, "series_catalog", None) is None:
        # The legacy film-only path remains unchanged.
        try:
            summary = await index_messages(ctx, [m])
            if summary.added: log.info("auto-indexed channel=%s message=%s files=%s", m.chat.id, m.id, summary.added)
        except Exception: log.exception("automatic film indexing failed")
        return
    try:
        from types import SimpleNamespace
        media=getattr(m,"document",None) or getattr(m,"video",None) or getattr(m,"audio",None)
        if not media: return
        kind="document" if getattr(m,"document",None) else "video" if getattr(m,"video",None) else "audio"
        payload={"chat_id":int(m.chat.id),"message_id":int(m.id),"caption":getattr(m,"caption","") or "",
                 "media_kind":kind,"file_name":getattr(media,"file_name",None),
                 "file_unique_id":getattr(media,"file_unique_id",None),"file_size":int(getattr(media,"file_size",0) or 0)}
        _note_autoindex(ctx,int(m.chat.id),int(m.id))
        ctx.index_queue.put_nowait(("raw",payload))
    except asyncio.QueueFull:
        # Exceptional path only: persist the overflow reference instead of silently losing it.
        import index_recovery as recovery
        try:
            await recovery.record(ctx, [("raw", payload)])
            await recovery.refresh(ctx)
        except Exception:
            _remember_unrecorded(ctx, payload["chat_id"], payload["message_id"])
            log.exception("could not persist overflow reference")
        ctx.index_error = "Index queue overflow; recovery pending"
        log.error("series auto-index queue full; channel=%s message=%s queued for recovery",m.chat.id,m.id)
    except Exception:
        log.exception("could not queue channel media channel=%s message=%s",m.chat.id,getattr(m,"id","?"))


def _rss_mb() -> int:
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return -1


async def watchdog(ctx: Ctx, client) -> None:
    """Once a minute: log one health line (memory, event-loop lag, Telegram round trip, queues) and
    restart the process if Telegram stops answering, so the bot can never stay deaf for hours."""
    import os
    fails, last = 0, time.monotonic()
    ctx.watchdog_ok = True
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        lag, last = max(0.0, now - last - 60), now
        t0, rtt = time.monotonic(), -1.0
        try:
            await asyncio.wait_for(client.get_me(), 25)
            rtt = time.monotonic() - t0
            fails, ctx.watchdog_ok = 0, True
        except Exception as e:
            fails += 1
            ctx.watchdog_ok = False
            log.warning("watchdog: Telegram did not answer (%s of 3): %r", fails, e)
        ago = round(time.time() - ctx.last_update) if ctx.last_update else None
        log.info("heartbeat: mem=%sMB loop_lag=%.1fs telegram_rtt=%.1fs updates=%s last_update_ago=%ss index_queue=%s indexing=%s maintenance=%s",
                 _rss_mb(), lag, rtt, ctx.updates_seen, ago, ctx.index_queue.qsize(), bool(ctx.index_job or ctx.index_active), ctx.maintenance)
        if ctx.index_error or ctx.index_recovery_pending or ctx.index_unrecorded:
            log.warning("indexing degraded: error=%s recovery_batches=%s unrecorded_channels=%s", ctx.index_error, ctx.index_recovery_pending, len(ctx.index_unrecorded))
        if lag > 5:
            log.warning("the event loop was blocked for about %.0f seconds", lag)
        if fails >= 3:
            log.error("watchdog: Telegram unreachable for 3 checks - restarting the process")
            await asyncio.sleep(1)
            os._exit(3)


async def _process_index_batch(ctx: Ctx, client, batch, *, announce: bool):
    """Existing ingestion path, shared by live and historical recovery."""
    import series_handlers
    from types import SimpleNamespace
    from series_catalog import from_message
    cat = ctx.series_catalog
    settings = await ctx.settings()
    records, skips = [], []
    aliases = await cat.alias_map()
    for kind, payload in batch:
        if kind != "raw":
            log.warning("discarding unsupported legacy index queue item kind=%s",kind)
            continue
        media = SimpleNamespace(file_name=payload.get("file_name"),file_unique_id=payload.get("file_unique_id"),file_size=payload.get("file_size",0))
        msg = SimpleNamespace(chat=SimpleNamespace(id=payload["chat_id"]),id=payload["message_id"],caption=payload.get("caption",""),
            document=media if payload.get("media_kind")=="document" else None,
            video=media if payload.get("media_kind")=="video" else None,
            audio=media if payload.get("media_kind")=="audio" else None)
        rec = from_message(msg,promos=settings.get("series_promos",[]),aliases=aliases,rules=settings.get("parser_rules"))
        (skips if rec is None else records).append(payload if rec is None else rec)
    if skips:
        await cat.add_skip_payloads(skips,promos=settings.get("series_promos",[]))
    if records:
        added,known,dupes,fresh = await cat.add(records,include_added=True)
        log.info("auto-indexed series batch=%s added=%s known=%s duplicates=%s queue=%s",len(records),added,known,dupes,ctx.index_queue.qsize())
        for rec in fresh:
            if announce and settings.get("anime_announcements"):
                try:
                    import anime
                    await anime.queue_live(ctx, rec)
                except Exception:
                    log.exception("anime candidate creation failed; use /anime queue for %s",rec.sid)
            try: await series_handlers.notify_followers(ctx,client,rec)
            except Exception: log.exception("series follower notification failed for %s",rec.show)
    # Never advance the watermark for an unsuccessful batch.
    for kind, payload in batch:
        if kind == "raw":
            ch, mid = payload["chat_id"], payload["message_id"]
            ctx.seen_ids[ch] = max(ctx.seen_ids.get(ch,0),mid)


async def _index_alert(ctx, client):
    now = time.monotonic()
    if ctx.index_alert_at is not None and now - ctx.index_alert_at < 900:
        return
    ctx.index_alert_at = now
    if ctx.cfg.admins:
        try:
            await asyncio.wait_for(_send(client, sorted(ctx.cfg.admins)[0],
                "⚠️ Series indexing is degraded. Check /lockstatus and /indexretry. Failed batches are retained when MongoDB is writable."), 15)
        except Exception:
            log.warning("could not notify admin of indexing degradation",exc_info=True)


def _remember_unrecorded(ctx, ch, mid):
    lo, hi = ctx.index_unrecorded.get(ch, (mid, mid))
    ctx.index_unrecorded[ch] = (min(lo, mid), max(hi, mid))


async def index_worker(ctx: Ctx, client) -> None:
    """Durably journal each batch before writing; never discard on retry exhaustion."""
    import index_recovery as recovery
    from multidb import CatalogueBusy
    while True:
        first = await ctx.index_queue.get()
        batch = [first]
        for _ in range(99):
            try: batch.append(ctx.index_queue.get_nowait())
            except asyncio.QueueEmpty: break
        key = None
        ctx.index_active = True
        try:
            while not ctx.ready or getattr(ctx,"series_catalog",None) is None:
                await asyncio.sleep(2)
            # If Mongo cannot even record recovery metadata, keep this bounded batch
            # in memory and stop draining the queue until durable recording works.
            while True:
                try:
                    key = await recovery.record(ctx, batch)
                    ctx.index_batch_id = key
                    break
                except Exception:
                    ctx.index_error = "Cannot persist indexing recovery journal"
                    log.exception("index journal unavailable; holding batch in memory")
                    await _index_alert(ctx, client)
                    await asyncio.sleep(30)
            for attempt in range(4):
                try:
                    await _process_index_batch(ctx,client,batch,announce=True)
                    await recovery.complete(ctx,key)
                    ctx.index_error = None
                    break
                except CatalogueBusy:
                    ctx.index_error = "Catalogue reservation busy; use /lockstatus"
                    log.warning("catalogue reservation busy; batch retained in recovery journal %s",key)
                    ctx.index_batch_id = None
                    await recovery.refresh(ctx)
                    await _index_alert(ctx,client)
                    # Don't consume another 3 x 30s on a known blocked reservation.
                    await asyncio.sleep(30)
                    break
                except Exception:
                    ctx.index_error = "Index batch failed; retained for recovery"
                    log.exception("background series auto-index batch failed (attempt %s/4)",attempt+1)
                    if attempt == 3:
                        await _index_alert(ctx,client)
                    else:
                        await asyncio.sleep(2 ** attempt)
        except asyncio.CancelledError:
            if key is None and getattr(ctx,"series_catalog",None) is not None:
                try: await recovery.record(ctx,batch)
                except Exception: log.exception("shutdown could not persist in-flight index batch")
            raise
        finally:
            ctx.index_batch_id = None
            ctx.index_active = False
            for _ in batch: ctx.index_queue.task_done()
        if ctx.index_error or ctx.index_recovery_pending:
            await recovery.refresh(ctx)
        await asyncio.sleep(0)


CATCHUP_QUIET_SECONDS = 30 * 60      # scan this long after the last auto-indexed post


def _note_autoindex(ctx: Ctx, ch: int, mid: int) -> None:
    """A channel post was queued: remember its id and schedule a catch-up scan 30 minutes after the burst ends."""
    lo, hi = ctx.catchup_range.get(ch, [mid, mid])
    ctx.catchup_range[ch] = [min(lo, mid), None if hi is None else max(hi, mid)]
    ctx.catchup_due[ch] = time.monotonic() + CATCHUP_QUIET_SECONDS


def _payload_of(m) -> dict | None:
    media = getattr(m, "document", None) or getattr(m, "video", None) or getattr(m, "audio", None)
    if not media:
        return None
    kind = "document" if getattr(m, "document", None) else "video" if getattr(m, "video", None) else "audio"
    return {"chat_id": int(m.chat.id), "message_id": int(m.id), "caption": getattr(m, "caption", "") or "",
            "media_kind": kind, "file_name": getattr(media, "file_name", None),
            "file_unique_id": getattr(media, "file_unique_id", None), "file_size": int(getattr(media, "file_size", 0) or 0)}


async def catchup_channel(ctx: Ctx, client, ch: int, lo: int, hi: int | None = None, max_ids: int = 3000) -> dict:
    """Re-read channel messages from `lo` and index any media the catalogue does not have yet.
    Known files are ignored (indexing is idempotent), so this is safe to repeat. With hi=None it scans
    forward until two consecutive empty blocks; otherwise it stops once it has passed `hi`."""
    import series_handlers
    from series_catalog import from_message
    cat = ctx.series_catalog
    settings = await ctx.settings()
    aliases = await cat.alias_map()
    pos, empty, scanned, recovered, seen_max = max(1, lo), 0, 0, 0, 0
    while scanned < max_ids and (hi is None or pos <= hi):
        end = min(pos + 200, pos + max_ids - scanned, hi + 1 if hi is not None else pos + 200)
        ids = list(range(pos, end))
        msgs = await _fetch(client, ch, ids)
        msgs = msgs if isinstance(msgs, list) else [msgs]
        real = [m for m in msgs if m and not getattr(m, "empty", False)]
        scanned += len(ids)
        pos += len(ids)
        if real:
            empty = 0
            seen_max = max(seen_max, max(int(m.id) for m in real))
            recs, skips = [], []
            for m in real:
                payload = _payload_of(m)
                if payload is None:
                    continue
                rec = from_message(m, promos=settings.get("series_promos", []), aliases=aliases, rules=settings.get("parser_rules"))
                (skips if rec is None else recs).append(payload if rec is None else rec)
            if recs:
                added, _known, _dupes, fresh = await cat.add(recs, include_added=True)
                recovered += added
                for rec in fresh[:20]:
                    try: await series_handlers.notify_followers(ctx, client, rec)
                    except Exception: log.exception("catch-up follower notification failed for %s", rec.show)
            if skips:
                await cat.add_skip_payloads(skips,promos=(await ctx.settings()).get("series_promos",[]))
        else:
            empty += 1
        if hi is not None and pos > hi:
            break
        if hi is None and empty >= 2:
            break
        await asyncio.sleep(1.0)                 # stay gentle with Telegram and the database
    if seen_max:
        ctx.seen_ids[ch] = max(ctx.seen_ids.get(ch, 0), seen_max)
    return {"channel": ch, "recovered": recovered, "ids_scanned": scanned, "next_id": pos}


async def _load_seen(ctx: Ctx, ch: int) -> int:
    saved = int((await ctx.settings()).get(f"seen_{ch}") or 0)
    if saved:
        return saved
    try:                                           # first run: use the newest message already in the catalogue
        doc = await ctx.series_catalog.files.find_one({"channel": ch}, sort=[("message_id", -1)], projection={"message_id": 1})
        return int((doc or {}).get("message_id") or 0)
    except Exception:
        log.exception("could not read the newest indexed message of channel %s", ch)
        return 0


async def catchup_all(ctx: Ctx, client, window: int = 300) -> str:
    lines = []
    for ch in sorted(ctx.cfg.file_channels):
        seen = await _load_seen(ctx, ch)
        res = await catchup_channel(ctx, client, ch, max(1, seen - window), None)
        lines.append(f"Channel {ch}: recovered {res['recovered']} missed file(s) (scanned {res['ids_scanned']} message ids).")
    return "\n".join(lines) or "No database channels are configured."


async def catchup_worker(ctx: Ctx, client) -> None:
    """Background safety net. (1) Once at startup: scan from just before the newest message the bot last handled,
    which covers any time it was offline. (2) 30 minutes after a burst of auto-indexed posts: re-check that
    burst's id range for posts that slipped through. Quiet channels cost nothing."""
    while not ctx.ready or getattr(ctx, "series_catalog", None) is None:
        await asyncio.sleep(5)
    await asyncio.sleep(120)
    now = time.monotonic()
    for ch in sorted(ctx.cfg.file_channels):
        try:
            seen = await _load_seen(ctx, ch)
        except Exception:
            ctx.index_catchup_errors.add(ch)
            log.exception("could not load startup catch-up watermark channel=%s",ch)
            continue
        if seen:
            ctx.seen_ids[ch] = max(ctx.seen_ids.get(ch, 0), seen)
            ctx.catchup_range.setdefault(ch, [max(1, seen - 100), None])
            ctx.catchup_due.setdefault(ch, now)
    last_saved = {}
    last_flush = time.monotonic()
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        import index_recovery as recovery
        from multidb import FederatedDB
        await recovery.refresh(ctx)
        db = ctx.series_catalog._db
        # Try bounded journal recovery only when no live indexing is underway and
        # no foreign reservation is presently visible. No lock is ever stolen.
        if not ctx.index_active and not ctx.index_queue.qsize() and not ctx.index_job and not ctx.maintenance:
            try:
                if isinstance(db,FederatedDB):
                    await db.retry_own_release()
                if not isinstance(db, FederatedDB) or not await db.reservation_status():
                    if ctx.index_recovery_pending:
                        await recovery.recover(ctx, client)
                    if not ctx.index_recovery_pending and not ctx.index_unrecorded and not ctx.index_catchup_errors:
                        ctx.index_error = None
            except Exception:
                ctx.index_error = "Index recovery failed; retained for retry"
                log.exception("durable index recovery failed")
                await _index_alert(ctx,client)
        if now - last_flush > 300:                  # remember how far we got, so a restart knows where to resume
            last_flush = now
            for ch, val in list(ctx.seen_ids.items()):
                if val and ch not in ctx.index_unrecorded and ch not in ctx.catchup_range and not ctx.index_active and not ctx.index_queue.qsize() and last_saved.get(ch) != val:
                    try:
                        await ctx.store.set_setting(f"seen_{ch}", val)
                        last_saved[ch] = val
                    except Exception:
                        log.exception("could not save seen id for channel %s", ch)
        for ch in ctx.index_catchup_errors - set(ctx.catchup_range):
            try:
                seen = await _load_seen(ctx,ch)
                if seen:
                    ctx.catchup_range.setdefault(ch,[max(1,seen-100),None])
                    ctx.catchup_due.setdefault(ch,now)
                else:
                    ctx.index_catchup_errors.discard(ch)
            except Exception:
                log.exception("could not retry catch-up watermark channel=%s",ch)
        for ch, due in list(ctx.catchup_due.items()):
            if due > now:
                continue
            if ctx.index_job or ctx.maintenance or ctx.index_active or ctx.index_recovery_lock.locked() or ctx.index_queue.qsize() > 0:
                ctx.catchup_due[ch] = now + 300     # busy: try again in five minutes
                continue
            lo, hi = ctx.catchup_range.pop(ch, [1, None])
            ctx.catchup_due.pop(ch, None)
            try:
                overflow = ctx.index_unrecorded.get(ch)
                if isinstance(db,FederatedDB) and await db.reservation_status():
                    _reschedule_catchup(ctx,ch,lo,hi,now)
                    continue
                was_open = hi is None
                if overflow:
                    lo = min(lo, overflow[0]); hi = max(hi or overflow[1], overflow[1])
                res = await catchup_channel(ctx, client, ch, max(1, int(lo) - 50), hi)
                if overflow and ctx.index_unrecorded.get(ch) == overflow:
                    if res["next_id"] > overflow[1]:
                        ctx.index_unrecorded.pop(ch,None)
                    else:
                        ctx.index_unrecorded[ch] = (max(overflow[0],res["next_id"]),overflow[1])
                if (hi is not None and res["next_id"] <= hi) or (hi is None and res["ids_scanned"] >= 3000):
                    _reschedule_catchup(ctx,ch,res["next_id"],hi,now)
                elif overflow and was_open:
                    _reschedule_catchup(ctx,ch,res["next_id"],None,now)
                ctx.index_catchup_errors.discard(ch)
                log.info("catch-up scan channel=%s recovered=%s ids_scanned=%s", ch, res["recovered"], res["ids_scanned"])
                if res["recovered"] and ctx.cfg.admins:
                    await _send(client, sorted(ctx.cfg.admins)[0],
                                f"🔎 Catch-up scan recovered {res['recovered']} file(s) in channel {ch} that automatic indexing had missed.")
            except Exception:
                ctx.index_catchup_errors.add(ch)
                ctx.index_error = "Catch-up failed; retry scheduled"
                _reschedule_catchup(ctx,ch,lo,hi,now)
                log.exception("catch-up scan failed for channel %s", ch)


def _reschedule_catchup(ctx,ch,lo,hi,now):
    other = ctx.catchup_range.get(ch)
    if other:
        lo = min(lo,other[0]); hi = None if hi is None or other[1] is None else max(hi,other[1])
    ctx.catchup_range[ch] = [lo,hi]
    ctx.catchup_due[ch] = now + 300


EMPTY_BLOCKS_TO_STOP = 25        # 25 x 200 empty IDs confirm the end after sparse/deleted messages


async def _fetch(client, ch: int, ids: list[int]):
    while True:
        try:
            return await client.get_messages(ch, ids)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)


async def find_last_id(client, ch: int, floor: int = 1) -> int:
    """Find a candidate by probing, then scan forward across sparse/deleted-ID gaps."""
    async def any_in(start: int, n: int = 100) -> bool:
        return any(x and not x.empty for x in await _fetch(client, ch, list(range(start, start + n))))
    lo, hi = max(floor, 1), max(floor, 1) + 1000
    while await any_in(hi):
        lo, hi = hi, hi * 2
    while hi - lo > 100:
        mid = (lo + hi) // 2
        lo, hi = (mid, hi) if await any_in(mid) else (lo, mid)
    best = max(floor - 1, 0)
    for start in range(lo, hi + 100, 200):
        for x in await _fetch(client, ch, list(range(start, min(start + 200, hi + 100)))):
            if x and not x.empty:
                best = max(best, x.id)
    # Sparse channels can have thousands of deleted IDs between surviving posts. The
    # exponential probes above find a candidate cheaply; scan forward through the gap
    # and require 5,000 consecutive empty IDs before accepting it as the true end.
    pos, empty_blocks = best + 1, 0
    while empty_blocks < EMPTY_BLOCKS_TO_STOP:
        ids=list(range(pos,pos+200))
        batch=await _fetch(client,ch,ids)
        found=[x for x in batch if x and not getattr(x,"empty",False)]
        if found:
            best=max(best,max(int(x.id) for x in found))
            empty_blocks=0
        else:
            empty_blocks+=1
        pos+=len(ids)
    return best


def _progress_text(ch: int, pos: int, first: int, last: int, tot: IndexSummary, started: float, state: str) -> str:
    done = max(pos - first, 0)
    span = max(last - first + 1, 1)
    pct = min(100, round(100 * done / span))
    rate = done / max(time.monotonic() - started, 1)
    eta = f" · about {round((last - pos) / rate / 60)} min left" if rate > 0 and state == "running" and pos < last else ""
    return (f"<b>Indexing</b> <code>{ch}</code>\n"
            f"Message {pos:,} of {last:,}  ({pct}%){eta}\n\n"
            f"Added <b>{tot.added:,}</b>\n"
            f"Already had {tot.known:,} · look-alikes skipped {tot.lookalike:,}\n"
            f"Skipped: {tot.series:,} unrecognized/non-series · {tot.unreadable:,} unreadable\n"
            + (f"\n<b>{state}</b>" if state != "running" else ""))


async def run_index(ctx: Ctx, client, status, targets: list[tuple[int, int, int | None]]) -> None:
    """targets: (channel, first message id, last id or None to detect). Updates `status` live."""
    if ctx.index_job:
        await _edit(status, "An indexing run is already active.", None)
        return
    job = ctx.index_job = {"stop": False}
    stop_rows = [[Btn("Stop", "ixstop")]]
    try:
        await ctx.store.set_setting("index_job", {"targets": [[c, f, l] for c, f, l in targets]})
        for ch, first, last in targets:
            if job["stop"]:
                break
            await _edit(status, f"Looking for the newest post in <code>{ch}</code>…", stop_rows)
            last = last or await find_last_id(client, ch, first)
            tot, started, tick, pos, empty_run = IndexSummary(), time.monotonic(), time.monotonic(), first, 0
            while not job["stop"] and pos <= last:
                ids = list(range(pos, min(pos + 200, last + 1)))
                live = [x for x in await _fetch(client, ch, ids) if x and not getattr(x, "empty", False)]
                empty_run = 0 if live else empty_run + 1
                if live:
                    if getattr(ctx, "series_catalog", None) is not None:
                        import series_handlers
                        batch = IndexSummary()
                        records = []
                        from series_catalog import from_message
                        settings = await ctx.settings()
                        aliases = await ctx.series_catalog.alias_map()
                        for item in live:
                            batch.seen += 1
                            rec = from_message(item, promos=settings.get("series_promos", []), aliases=aliases, rules=settings.get("parser_rules"))
                            if rec is None:
                                batch.series += 1
                                await ctx.series_catalog.add_skip(item,promos=settings.get("series_promos", []))
                            else:
                                records.append(rec)
                        batch.added, batch.known, batch.lookalike = await ctx.series_catalog.add(records)
                        tot.add(batch)
                    else:
                        tot.add(await index_messages(ctx, live))
                pos = ids[-1] + 1
                await asyncio.sleep(0.4)                  # leave room for users' messages and the database
                if time.monotonic() - tick >= 5:
                    tick = time.monotonic()
                    await ctx.store.set_setting(f"cp_{ch}", pos)
                    await _edit(status, _progress_text(ch, min(pos, last), first, last, tot, started, "running"), stop_rows)
            pos = min(pos, last + 1)
            await ctx.store.set_setting(f"cp_{ch}", pos)
            state = f"Stopped. Run /index {ch} to continue from here." if job["stop"] else "Done."
            await _edit(status, _progress_text(ch, min(pos, last), first, last, tot, started, state), None)
        # A stopped or finished job must NOT resume by itself after the next restart.
        # (Checkpoints are kept, so /index continues from where it stopped.)
        await ctx.store.set_setting("index_job", None)
    except Exception as e:
        log.exception("indexing failed")
        await _edit(status, f"Indexing stopped with an error: {escape(str(e))}\nRun /index to resume.", None)
    finally:
        ctx.index_job = None


async def _edit(status, text: str, rows) -> None:
    try:
        await status.edit_text(text, parse_mode=HTML, reply_markup=kb(rows))
    except FloodWait as e:
        await asyncio.sleep(min(e.value, 10))
    except RPCError:
        pass


async def resume_index(ctx: Ctx, client) -> None:
    """Resume an indexing run interrupted by a restart."""
    job = (await ctx.store.settings()).get("index_job")
    if not job or not job.get("targets") or not ctx.cfg.admins:
        return
    cps = await ctx.store.settings()
    targets = [(c, int(cps.get(f"cp_{c}") or f), l) for c, f, l in job["targets"]]
    await asyncio.sleep(90)                       # let the bot settle and answer users first
    status = await client.send_message(sorted(ctx.cfg.admins)[0], "I restarted while indexing. Resuming where I left off…")
    asyncio.create_task(run_index(ctx, client, status, targets))


# ---------------------------------------------------------------- read-only channel audit (Phase 0)
AUDIT_BATCH = 200

def _audit_counts():
    return {"existing": 0, "documents": 0, "videos": 0, "audio": 0, "subtitles": 0,
            "other_documents": 0, "empty": 0, "not_media": 0, "highest": 0, "ids": 0}

def _audit_classify(m, out):
    if not m or getattr(m, "empty", False):
        out["empty"] += 1
        return
    out["existing"] += 1
    out["highest"] = max(out["highest"], int(getattr(m, "id", 0) or 0))
    doc = getattr(m, "document", None)
    video = getattr(m, "video", None)
    audio = getattr(m, "audio", None)
    if doc:
        out["documents"] += 1
        name = (getattr(doc, "file_name", None) or "").lower()
        ext = name.rsplit(".", 1)[-1] if "." in name else ""
        if ext in {"srt", "ass", "ssa", "vtt", "sub", "idx"}:
            out["subtitles"] += 1
        else:
            out["other_documents"] += 1
    elif video:
        out["videos"] += 1
    elif audio:
        out["audio"] += 1
    elif any(getattr(m, x, None) for x in ("photo", "animation", "voice", "video_note", "sticker")):
        out["not_media"] += 1
    else:
        out["not_media"] += 1

def _audit_text(ch, c, pos, last, started, state="running", run_start=1):
    done = max(0, pos - run_start)
    rate = done / max(time.monotonic() - started, 1)
    eta = f"\nEstimated remaining: {int((last-pos+1)/rate/60)} min" if rate and pos <= last else ""
    empty_share = 100*c["empty"]/max(c["ids"], 1)
    return (f"<b>Read-only audit</b> <code>{ch}</code>\nIDs scanned total (including earlier runs): {c['ids']:,} (channel highest ID: {last:,}){eta}\n"
            f"Existing messages: {c['existing']:,}\nDocuments: {c['documents']:,} (subtitles: {c['subtitles']:,}; other documents: {c['other_documents']:,})\n"
            f"Videos: {c['videos']:,} · Audio: {c['audio']:,} · Not media: {c['not_media']:,}\n"
            f"Empty/deleted IDs: {c['empty']:,} ({empty_share:.1f}%)\nHighest message ID: {c['highest']:,}\n<b>{state}</b>")

async def _run_audit(ctx, client, status, ch, start=1, last=None):
    job = ctx.audit_job = {"stop": False, "channel": ch}
    await ctx.store.set_setting("audit_job", {"channel": ch, "start": start, "last": last})
    try:
        last = last or await find_last_id(client, ch, start)
        await ctx.store.set_setting(f"audit_last_{ch}", last)
        c = _audit_counts()
        saved_counts = (await ctx.store.settings()).get(f"audit_counts_{ch}") or {}
        c.update({k: int(v) for k, v in saved_counts.items() if k in c})
        pos, tick, started = start, time.monotonic(), time.monotonic()
        while pos <= last and not job["stop"]:
            ids = list(range(pos, min(pos + AUDIT_BATCH, last + 1)))
            messages = await _fetch(client, ch, ids)
            by_id = {getattr(x, "id", None): x for x in messages if x is not None}
            for mid in ids:
                _audit_classify(by_id.get(mid), c)
            c["ids"] += len(ids); pos += len(ids)
            if time.monotonic() - tick >= 5:
                tick = time.monotonic()
                await ctx.store.set_setting(f"audit_cp_{ch}", pos)
                await ctx.store.set_setting(f"audit_counts_{ch}", c)
                await _edit(status, _audit_text(ch, c, pos, last, started, run_start=start), [[Btn("Stop", "auditstop")]])
        await ctx.store.set_setting(f"audit_cp_{ch}", pos)
        await ctx.store.set_setting(f"audit_counts_{ch}", c)
        state = "Stopped. Run /audit CHANNEL to resume." if job["stop"] else "Audit complete. No database files were changed."
        await _edit(status, _audit_text(ch, c, min(pos, last+1), last, started, state, run_start=start), None)
        if job["stop"]:
            await ctx.store.set_setting("audit_job", {"channel": ch, "start": pos, "last": last})
            await ctx.store.set_setting(f"audit_complete_{ch}", False)
        else:
            await ctx.store.set_setting("audit_job", None)
            await ctx.store.set_setting(f"audit_complete_{ch}", True)
    except Exception as e:
        log.exception("audit failed")
        await _edit(status, f"Audit failed: {escape(str(e))}. Run /audit {ch} to resume.", None)
    finally:
        ctx.audit_job = None

async def _audit_cmd(ctx, client, m, args, say):
    if ctx.maintenance or ctx.index_job or getattr(ctx, "audit_job", None):
        await say("Another maintenance job is running. Try again when it finishes.")
        return
    if len(args) != 1 or not re.fullmatch(r"-?\d+", args[0]):
        await say("Usage: <code>/audit CHANNEL_ID</code>\nRun once for each configured series channel. This scan writes no file records.")
        return
    ch = int(args[0])
    if ch not in ctx.cfg.file_channels:
        await say("That channel is not in FILE_CHANNELS.")
        return
    settings = await ctx.store.settings()
    if settings.get(f"audit_complete_{ch}"):
        start = 1
        last = None
        await ctx.store.set_setting(f"audit_cp_{ch}", 1)
        await ctx.store.set_setting(f"audit_counts_{ch}", _audit_counts())
        await ctx.store.set_setting(f"audit_complete_{ch}", False)
    else:
        start = int(settings.get(f"audit_cp_{ch}") or 1)
        last = settings.get(f"audit_last_{ch}")
    status = await say(f"Starting read-only audit of <code>{ch}</code> from message {start:,}…")
    asyncio.create_task(_run_audit(ctx, client, status, ch, start, last))

async def resume_audit(ctx, client):
    job = (await ctx.store.settings()).get("audit_job")
    if not job or not ctx.cfg.admins:
        return
    ch = int(job["channel"]); start = int((await ctx.store.settings()).get(f"audit_cp_{ch}") or job.get("start", 1))
    status = await client.send_message(sorted(ctx.cfg.admins)[0], f"Resuming read-only audit of {ch} from {start:,}…")
    asyncio.create_task(_run_audit(ctx, client, status, ch, start, job.get("last")))


# ---------------------------------------------------------------- admin commands
def _reply_html(m) -> str | None:
    src = m.reply_to_message
    if not src:
        return None
    for field in (src.text, src.caption):
        if field:
            return getattr(field, "html", None) or str(field)
    return None


def _inline_arg(m) -> str:
    parts = (m.text or m.caption or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


PURGE_ROUND = 50000
PURGE_PAUSE = 0.3          # seconds between delete batches, so Render and Mongo get a breather


def _series_only(rows: list[dict]) -> list[dict]:
    return [r for r in rows if parse_filename(r["name"]).kind == "series"]


async def _purge_scan(ctx: Ctx, status) -> None:
    """Dry run: read every saved file name, count the ones that are really series episodes. Deletes nothing."""
    ctx.maintenance = "purge"
    found, scanned, chunk, tick = [], 0, [], time.monotonic()
    try:
        async for r in ctx.store.iter_file_names():
            chunk.append(r)
            if len(chunk) >= 500:
                found += await asyncio.to_thread(_series_only, chunk)       # off the main loop; keeps the bot responsive
                scanned, chunk = scanned + len(chunk), []
                if len(found) >= PURGE_ROUND:
                    break
                if time.monotonic() - tick >= 5:
                    tick = time.monotonic()
                    await _edit(status, f"Scanning… {scanned:,} files checked, {len(found):,} look like series so far.", None)
        if chunk and len(found) < PURGE_ROUND:
            found += await asyncio.to_thread(_series_only, chunk)
            scanned += len(chunk)
        found = found[:PURGE_ROUND]
        if not found:
            await _edit(status, f"Checked {scanned:,} files. None look like series episodes.", None)
            return
        ctx.pending_purge = found
        sample = "\n".join(f"<code>{escape(r['name'][:70])}</code>" for r in found[:10])
        more = f"\nThat's one round of {PURGE_ROUND:,}. Run it again afterwards for the rest." if len(found) >= PURGE_ROUND else ""
        await _edit(status, f"Checked {scanned:,} files. <b>{len(found):,}</b> look like series episodes "
                            f"(S01E01, 1x01, Season 2 or S01 anywhere in the name).{more}\n\nFirst 10:\n{sample}\n\n"
                            "Delete them from the database? Films left with no files are removed too. "
                            "Your channel posts aren't touched.",
                    [[Btn("Yes, delete them", "psy"), Btn("Cancel", "psn")]])
    except Exception as e:
        log.exception("purge scan failed")
        await _edit(status, f"The scan stopped with an error: {escape(str(e))}", None)
    finally:
        ctx.maintenance = None


async def _purge_delete(ctx: Ctx, status, items: list[dict]) -> None:
    """Delete in small batches with short pauses, so Render's memory and Mongo don't spike."""
    ctx.maintenance = "purge"
    removed, touched, tick = 0, {}, time.monotonic()
    try:
        for i in range(0, len(items), 500):
            batch = items[i:i + 500]
            removed += await ctx.store.delete_files(batch, kind="series")
            for it in batch:
                touched[it["film"]] = touched.get(it["film"], 0) + 1
            if time.monotonic() - tick >= 5:
                tick = time.monotonic()
                await _edit(status, f"Deleting… {removed:,} of {len(items):,} files removed.", None)
            await asyncio.sleep(PURGE_PAUSE)
        films_gone = 0
        for n, (fid, cnt) in enumerate(touched.items(), 1):
            if await ctx.store.remove_film_if_empty(fid):
                await asyncio.to_thread(ctx.index.remove_film, fid)
                films_gone += 1
            else:
                await asyncio.to_thread(ctx.index.adjust_files, fid, -cnt)
            if n % 200 == 0:
                await asyncio.sleep(0.1)
        await _edit(status, f"Done. Removed <b>{removed:,}</b> series files and <b>{films_gone:,}</b> films that were left "
                            f"empty. {len(touched) - films_gone:,} films lost some files but still have others. "
                            "The names are listed under /skipped, Series.", None)
    except Exception as e:
        log.exception("purge failed")
        await _edit(status, f"Deleting stopped with an error after {removed:,} files: {escape(str(e))}\n"
                            "Run /purgeseries again to continue.", None)
    finally:
        ctx.maintenance = None


async def _skip_entries(ctx: Ctx, kind: str, rows: list[dict]) -> list[dict]:
    kept = await ctx.store.files_by_uid([r["keep"] for r in rows if r.get("keep")]) if kind == "lookalike" else {}
    out = []
    for r in rows:
        e = {"name": r["n"], "size": r["z"], "ch": r["c"], "mid": r["m"]}
        if kind == "lookalike":
            k = kept.get(r.get("keep"))
            e.update(kept_gone=k is None, kept_name=k.name if k else "", kept_ch=k.ch if k else 0, kept_mid=k.mid if k else 0)
        out.append(e)
    return out


async def _skipped_view(ctx: Ctx, show, arg: str) -> None:
    if arg == "m" or arg.partition(":")[0] not in ui.SKIP_KINDS:
        t, rows = ui.skipped_menu(await ctx.store.skip_counts())
        await show(t, rows)
        return
    code, _, pg = arg.partition(":")
    page = int(pg) if pg.isdigit() else 0
    kind = ui.SKIP_KINDS[code][0]
    total, rows = await ctx.store.skips_page(kind, page, ui.SKIP_PAGE)
    t, btns = ui.skip_list_screen(code, await _skip_entries(ctx, kind, rows), page, total)
    await show(t, btns)


async def _skipped_file(ctx: Ctx, client, chat_id: int, code: str) -> None:
    if code not in ui.SKIP_KINDS:
        return
    kind, label, blurb = ui.SKIP_KINDS[code]

    def where(ch: int, mid: int) -> str:
        return ui.post_link(ch, mid) or f"chat {ch} message {mid}"
    lines: list[str] = []
    total = 0
    async for page in ctx.store.skips_iter(kind):          # bounded pages: never every record at once
        kept = await ctx.store.files_by_uid([r["keep"] for r in page if r.get("keep")]) if kind == "lookalike" else {}
        for r in page:
            total += 1
            lines.append(f"{total}. {r['n'] or '(no name)'} | {r['z']} bytes | {where(r['c'], r['m'])}")
            if kind == "lookalike":
                k = kept.get(r.get("keep"))
                lines.append("   kept: " + (f"{k.name or k.uid} | {where(k.ch, k.mid)}" if k else "(removed since)"))
    data = io.BytesIO("\n".join([f"{label} ({total} records)", blurb, ""] + lines).encode("utf-8"))
    data.name = f"skipped-{kind}.txt"
    await client.send_document(chat_id, data, caption=f"{label}: {total:,} records.")


async def on_admin_cmd(ctx: Ctx, client, m) -> None:
    if not m.from_user or not ctx.is_admin(m.from_user.id):
        return
    cmd, args = m.command[0].lower(), m.command[1:]
    uid = m.from_user.id
    say = lambda t, rows=None: m.reply_text(t, parse_mode=HTML, reply_markup=kb(rows))   # noqa: E731

    if cmd in ("lockstatus", "lockrecover", "indexretry"):
        import index_recovery as recovery
        from multidb import FederatedDB
        if not getattr(ctx,"series_catalog",None):
            await say("Series catalogue is not connected."); return
        db = ctx.series_catalog._db
        try:
            if cmd == "lockstatus":
                row = await db.reservation_status() if isinstance(db,FederatedDB) else None
                await recovery.refresh(ctx)
                lines = ["<b>Catalogue reservation</b>"]
                if not isinstance(db,FederatedDB): lines.append("Single database: no federated reservation.")
                elif not row: lines.append("No reservation is held.")
                else:
                    owner = str(row.get("owner",""))
                    last = row.get("heartbeat",row.get("at"))
                    age = f"{max(0,int(time.time()-last))} seconds" if isinstance(last,(int,float)) else "unknown"
                    lines.extend([f"Owner: <code>{escape(owner)}</code>", f"Worker: {escape(str(row.get('worker','legacy/unknown')))}", f"Host/PID: {escape(str(row.get('host','unknown')))} / {escape(str(row.get('pid','unknown')))}", f"Last activity: {age}",
                        "Age is NOT proof the writer stopped. Stop all other writers before recovery.", f"<code>/lockrecover {escape(owner)} stopped</code>"])
                lines.append(f"Recovery batches: {ctx.index_recovery_pending}; unrecorded channels: {len(ctx.index_unrecorded)}")
                if ctx.index_error: lines.append(escape(ctx.index_error))
                if isinstance(db,FederatedDB) and db.reservation_error: lines.append(escape(db.reservation_error))
                await say("\n".join(lines))
            elif cmd == "lockrecover":
                if not isinstance(db,FederatedDB): await say("No federated reservation."); return
                if len(args)!=2 or args[1].lower()!="stopped":
                    await say("Run /lockstatus first. Stop ALL other catalogue writers. Then /lockrecover OWNER stopped. Never recover a live writer."); return
                changed = await db.recover_reservation(args[0],stopped=True)
                await say("Reservation recovered. Pending batches will be retried; use /indexretry now." if changed else "No reservation is held.")
            else:
                if args and args[0].lower()=="list":
                    rows=await recovery.rows(ctx,10)
                    lines=["<b>Index recovery journal (first 10)</b>"]
                    for row in rows:
                        groups={}
                        for ch,mid in row['refs']: groups.setdefault(ch,[]).append(mid)
                        for ch,ids in list(groups.items())[:5]: lines.append(f"<code>{ch} {min(ids)} {max(ids)}</code> — {len(ids)} refs"+(" · missing, review" if row.get('review') else ""))
                        if len(groups)>5: lines.append(f"… and {len(groups)-5} more channels")
                        if row.get('review'): lines.append(f"Review ID: <code>{row['_id']}</code>")
                    await say("\n".join(lines) if rows else "No saved recovery batches.")
                    return
                if args and args[0].lower()=="acknowledge":
                    if len(args)!=3 or args[2].lower()!="confirm":
                        await say("Usage: /indexretry acknowledge REVIEW_ID confirm (only for permanently unavailable posts)"); return
                    if ctx.index_recovery_lock.locked() or ctx.index_active:
                        await say("Indexing or recovery is busy; retry later."); return
                    async with ctx.index_recovery_lock:
                        deleted=await recovery.acknowledge(ctx,args[1])
                    await say("Unavailable references acknowledged. No catalogue files deleted." if deleted else "No matching review batch. Nothing deleted.")
                    return
                if args and (len(args)!=1 or args[0].lower()!="review"):
                    await say("Usage: /indexretry | /indexretry list | /indexretry review | /indexretry acknowledge REVIEW_ID confirm"); return
                if ctx.index_active or ctx.index_queue.qsize() or ctx.index_job or ctx.maintenance or ctx.index_recovery_lock.locked():
                    await say("Indexing is busy; retry when the current batch finishes."); return
                if isinstance(db,FederatedDB): await db.retry_own_release()
                if isinstance(db,FederatedDB) and await db.reservation_status():
                    await say("Catalogue reservation held. Check /lockstatus first."); return
                progress=await say("Retrying up to three saved batches…")
                async def job():
                    try:
                        n,missing=await recovery.recover(ctx,client,include_review=bool(args),force=True)
                        await _edit(progress,f"Recovery checked {n} media reference(s); {missing} unavailable. Remaining batches: {ctx.index_recovery_pending}. Use /indexretry list to inspect.",None)
                    except Exception:
                        log.exception("manual index recovery failed")
                        await _edit(progress,"Recovery failed; journal retained. Check /lockstatus and logs.",None)
                _spawn(job())
        except ValueError as e: await say(escape(str(e)))
        except Exception:
            log.exception("index reliability admin command failed")
            await say("Could not complete operation. Check Render logs; no lock is automatically stolen.")
        return
    if cmd == "anime":
        import anime
        await anime.admin_command(ctx, client, m, args)
        return
    if cmd in ("stopreparse", "stopmergeshows"):
        expected = "reparse" if cmd == "stopreparse" else "mergeshows"
        if ctx.maintenance == expected:
            ctx.maintenance_stop.set()
            await say(f"Stop requested. {expected} will stop after its current batch.")
        elif cmd == "stopreparse" and ctx.maintenance == "reparse":
            ctx.maintenance_stop.set()
            await say("Stop requested. Reparse will stop after its current batch.")
        else:
            await say("That maintenance operation is not running.")
    elif cmd == "reparsefind":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        elif ctx.maintenance:
            await say(f"Another maintenance operation is running ({ctx.maintenance}). Wait for it to finish or stop it first.")
        elif not args:
            await say("Usage: <code>/reparsefind &lt;pattern&gt;</code>\nExamples:\n"
                      "<code>/reparsefind S4.</code>\n<code>/reparsefind *Animax_Industry*</code>\n<code>/reparsefind re:^S\\d+\\.\\d+\\.</code>")
        else:
            import series_handlers
            try:
                t, rows = await series_handlers.targeted_reparse_preview(ctx, uid, " ".join(args))
                await say(t, rows)
            except ValueError as e:
                await say(escape(str(e)))
    elif cmd == "reparse":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        elif ctx.maintenance:
            await say(f"Another maintenance operation is running ({ctx.maintenance}). Wait for it to finish or stop it first.")
        elif args and args[0].lower() in ("match", "find"):
            import series_handlers
            pattern = " ".join(args[1:]).strip()
            if not pattern:
                await say("Usage: <code>/reparse match &lt;pattern&gt;</code> (or <code>/reparsefind &lt;pattern&gt;</code>)")
            else:
                try:
                    t, rows = await series_handlers.targeted_reparse_preview(ctx, uid, pattern)
                    await say(t, rows)
                except ValueError as e:
                    await say(escape(str(e)))
        else:
            dry = "dry" in [a.lower() for a in args]
            recheck = "skipped" in [a.lower() for a in args]
            only_skipped = recheck and "all" not in [a.lower() for a in args]      # /reparse skipped = ONLY the skipped records
            resume = "resume" in [a.lower() for a in args]
            ctx.maintenance = "reparse"
            ctx.maintenance_stop.clear()
            progress = await say("Starting reparse (dry run)…" if dry else "Starting reparse in small batches…")
            import series_handlers

            async def _job():
                try:
                    if only_skipped:
                        await series_handlers.reparse_skipped_only(ctx, client, progress)
                    else:
                        await series_handlers.reparse_catalog(ctx, client, progress, dry=dry, recheck=recheck, resume=resume)
                except Exception:
                    log.exception("series reparse failed")
                    try: await progress.edit_text("Reparse stopped because of an error. Its checkpoint was kept; send /reparse resume to continue. Check the Render logs.")
                    except Exception: pass
                finally:
                    ctx.maintenance = None
                    ctx.maintenance_stop.clear()
            _spawn(_job())
    elif cmd == "rebuildshows":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            progress = await say("Rebuilding the shows list from the files…")

            async def _job():
                try:
                    await progress.edit_text(await series_handlers.rebuild_shows_report(ctx))
                except Exception:
                    log.exception("rebuild shows failed")
                    try: await progress.edit_text("Rebuild failed. Check the Render logs.")
                    except Exception: pass
            _spawn(_job())
    elif cmd == "linkmovie":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        elif not args or not getattr(m,"reply_to_message",None):
            await say("Reply to a forwarded movie post from one of the bot's database channels with /linkmovie <exact show title>.")
        else:
            source=m.reply_to_message
            origin=getattr(source,"forward_from_chat",None)
            source_id=getattr(origin,"id",None)
            source_mid=getattr(source,"forward_from_message_id",None)
            forward_origin=getattr(source,"forward_origin",None)
            if not source_id and forward_origin:
                chat=getattr(forward_origin,"chat",None)
                source_id=getattr(chat,"id",None)
                source_mid=source_mid or getattr(forward_origin,"message_id",None)
            media=getattr(source,"document",None) or getattr(source,"video",None)
            if not source_id or not source_mid or source_id not in ctx.cfg.file_channels or not media:
                await say("I couldn't verify that as a forwarded video/document from a configured series channel. The original channel post has not been changed.")
            else:
                title=" ".join(args).strip()
                matches=await ctx.series_catalog.search_shows(title,limit=20)
                exact=[d for d in matches if normalize(d.get("title",""))==normalize(title)]
                if len(exact)!=1:
                    await say("No unique exact show title matched. Copy the title from the bot's show page and try again.")
                else:
                    try:
                        key=await ctx.series_catalog.add_movie(exact[0]["_id"],source_id,source_mid,getattr(media,"file_name",None) or getattr(source,"caption",None) or "Movie",getattr(media,"file_size",0))
                        await say(f"Linked movie to <b>{escape(exact[0]['title'])}</b>. It will appear in that show's separate Movies section. The original channel post remains untouched.")
                    except Exception as e: await say(escape(str(e)))
    elif cmd == "renameshow":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            value=" ".join(args).strip()
            if "|" not in value:
                await say("Usage: /renameshow Current show title | New show title")
            else:
                old_title,new_title=[x.strip() for x in value.split("|",1)]
                matches=await ctx.series_catalog.search_shows(old_title,limit=20)
                exact=[d for d in matches if normalize(d.get("title",""))==normalize(old_title)]
                if len(exact)!=1:
                    await say("I couldn't identify one exact existing show title. Use its exact stored title before the |.")
                else:
                    try:
                        sid = exact[0]["_id"]
                        old,new=await ctx.series_catalog.rename_show(sid,new_title)
                        s = await ctx.settings()
                        ov = dict(s.get("manual_title_overrides") or {})
                        ov[str(sid)] = {"title": new, "year": exact[0].get("year"), "updated": time.time()}
                        await ctx.set_setting("manual_title_overrides", ov)
                        await say(
                            f"Renamed <b>{escape(old)}</b> to <b>{escape(new)}</b> and saved a durable manual title override. "
                            f"Existing files, follows and progress retain their show ID, and future /reparse runs will not revert this title."
                        )
                    except ValueError as e: await say(escape(str(e)))
    elif cmd == "locktitle":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            target = " ".join(args).strip()
            if not target:
                await say("Usage: <code>/locktitle Exact show title</code> (locks an existing show's current title as a durable manual override so /reparse never reverts it).")
            else:
                d = await ctx.series_catalog.get_show(target)
                if d is None:
                    matches = await ctx.series_catalog.search_shows(target, limit=20)
                    exact = [x for x in matches if normalize(x.get("title", "")) == normalize(target)]
                    d = exact[0] if len(exact) == 1 else None
                if d is None:
                    await say("I couldn't identify one exact stored show. Pass its exact current title (or show ID).")
                else:
                    try:
                        sid = d["_id"]
                        locked = await ctx.series_catalog.lock_show_title(sid)
                        locked_title = locked["title"] if isinstance(locked, dict) else str(locked)
                        s = await ctx.settings()
                        ov = dict(s.get("manual_title_overrides") or {})
                        ov[str(sid)] = {"title": locked_title, "year": d.get("year"), "updated": time.time()}
                        await ctx.set_setting("manual_title_overrides", ov)
                        await say(
                            f"🔒 Locked manual title override for <b>{escape(locked_title)}</b> (<code>{escape(str(sid))}</code>). "
                            f"Future /reparse and /reparse dry runs will protect this title and will not flag it as an identity conflict."
                        )
                    except ValueError as e:
                        await say(escape(str(e)))
    elif cmd == "parserrules":
        import series_handlers
        t, rows = await series_handlers.parser_rules_menu_screen(ctx)
        await say(t, rows)
    elif cmd == "showinfo":
        if not getattr(ctx, "series_catalog", None) or not args:
            await say("Usage: /showinfo <part of a show title>, for example /showinfo Judas Bleach")
        else:
            import series_handlers
            progress = await say("Looking…")

            async def _job():
                try: await progress.edit_text(escape(await series_handlers.showinfo_report(ctx, " ".join(args))))
                except Exception:
                    log.exception("showinfo failed")
                    try: await progress.edit_text("Lookup failed. Check the Render logs.")
                    except Exception: pass
            _spawn(_job())
    elif cmd in ("catalogueaudit", "episodepreview"):
        if not ctx.is_admin(uid):
            await say("Admins only.")
        elif not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            progress = await say("Scanning catalogue (read only)…")
            async def _preview_job():
                try:
                    cat = ctx.series_catalog
                    if cmd == "catalogueaudit":
                        report = await cat.cross_database_audit()
                        if not report["configured"]:
                            text = "Second database not configured."
                        else:
                            lines = ["Cross-database audit — READ ONLY"]
                            for side in ("primary", "secondary"):
                                lines.append(side.title())
                                for name, row in report[side].items():
                                    lines.append(f"{name}: {row['count']:,} docs; data {row['data']} B; indexes {row['indexes']} B")
                            lines.extend(f"{key.replace('_', ' ')}: {value:,}" for key, value in report.items() if key not in ("configured", "primary", "secondary"))
                            lines.append("Counts are observational; no records were changed.")
                            text = "\n".join(lines)
                    else:
                        settings = await ctx.settings()
                        report = await cat.episode_first_preview(settings.get("series_promos", []), settings.get("parser_rules"))
                        lines = [f"Episode-first impact preview — READ ONLY\nUnsupported posts checked: {report['scanned']:,}\nProposed matches: {report['affected']:,}"]
                        lines.extend(f"{name[:100]} → {title} E{','.join(map(str, eps))}" for name, title, eps in report["examples"])
                        lines.append("No adoption or reclassification performed. Review /adopt or /reparse skipped separately.")
                        text = "\n".join(lines)
                    await progress.edit_text(escape(text)[:3900])
                except Exception:
                    log.exception("read-only catalogue preview failed")
                    await progress.edit_text("Preview failed. Check Render logs; no catalogue records were modified.")
            _spawn(_preview_job())
    elif cmd == "seriesaudit":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            progress = await say("Checking the series catalogue…")

            async def _job():
                try:
                    await progress.edit_text(await series_handlers.audit_report(ctx))
                except Exception:
                    log.exception("series audit failed")
                    try: await progress.edit_text("Audit failed. Check the Render logs.")
                    except Exception: pass
            _spawn(_job())
    elif cmd == "aliascheck":
        if not ctx.is_admin(uid):
            await say("Admins only.")
        elif not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        elif not args:
            await say("Read-only diagnostic. Usage: /aliascheck Title One;Title Two;Title Three")
        else:
            titles=[x.strip() for x in " ".join(args).split(";") if x.strip()][:12]
            from series_catalog import show_id
            cat=ctx.series_catalog
            lines=["<b>Alias diagnostic — READ ONLY</b>","This checks whether a parsed title is being redirected by a saved alias. No database records are changed.",""]
            for title in titles:
                sids=[show_id(title,None)]
                async for d in cat.shows.find({"title":title},{"_id":1,"year":1}).limit(5):
                    if d.get("year") is not None:
                        sids.append(show_id(title,d.get("year")))
                lines.append(f"<b>{escape(title)}</b>")
                seen=set(); found=False
                for sid in sids:
                    if sid in seen: continue
                    seen.add(sid)
                    row=await cat.aliases.find_one({"_id":sid},{"sid":1})
                    if not row: continue
                    found=True
                    target=await cat.resolve_alias(sid)
                    dest=await cat.shows.find_one({"_id":target},{"title":1,"year":1})
                    dest_title=(dest or {}).get("title","(target show missing)")
                    dest_year=(dest or {}).get("year")
                    lines.append(f"• SID <code>{sid}</code> → <code>{target}</code> → <b>{escape(str(dest_title))}</b>"+(f" ({dest_year})" if dest_year else ""))
                if not found:
                    lines.append("• No direct alias found for the title-derived SID.")
            await say("\n".join(lines))
    elif cmd == "allowshort":
        await say("Short titles no longer require an allow-list." if ctx.is_admin(uid) else "Admins only.")
    elif cmd == "identities":
        if not ctx.is_admin(uid):
            await say("Admins only.")
        elif not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            low = [a.lower() for a in args]
            if low and low[0] == "apply":
                text, rows = await series_handlers.identities_apply_preview(ctx, uid, " ".join(args[1:]))
                await say(text, rows)
            elif low and (low[0].isdigit() or (low[0] == "page" and len(low) > 1 and low[1].isdigit())):
                scan = ctx.identity_scan
                if not scan or time.monotonic() - scan["at"] > series_handlers.IDENT_CACHE_SECONDS:
                    await say("No recent /identities list. Send /identities to scan again.")
                else:
                    await say(series_handlers.identities_page_text(scan, int(low[-1])))
            elif ctx.maintenance:
                await say(f"Another maintenance operation is running ({ctx.maintenance}). Wait for it to finish first.")
            else:
                ctx.maintenance = "identities"
                ctx.maintenance_stop.clear()
                progress = await say("Scanning for identity conflicts (read-only)…")

                async def _job():
                    try:
                        scan = await series_handlers.scan_identity_conflicts(ctx, progress)
                        if scan is None:
                            await progress.edit_text("Scan stopped. Nothing was changed.")
                        else:
                            ctx.identity_scan = scan
                            await progress.edit_text(series_handlers.identities_page_text(scan, 1))
                    except Exception:
                        log.exception("identities scan failed")
                        try: await progress.edit_text("The identity scan stopped because of an error. Nothing was changed. Check the Render logs.")
                        except Exception: pass
                    finally:
                        ctx.maintenance = None
                        ctx.maintenance_stop.clear()
                _spawn(_job())
    elif cmd == "shortshows":
        if not ctx.is_admin(uid):
            await say("Admins only.")
        elif not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            cat=ctx.series_catalog
            rows=[d async for d in cat.shows.find({"title":{"$regex":r"^[A-Za-z]{3}$"}}, {"title":1,"year":1,"count":1}).sort("count",-1).limit(100)]
            suspicious=rows[:40]
            if not suspicious:
                await say("No three-letter show identities found. No records changed.")
            else:
                lines=["<b>Three-letter show identities — READ ONLY</b>",
                       "These are ordinary catalogue titles; length is not a reason to reject them. This report does not delete, merge, or rename anything.",""]
                for d in suspicious:
                    title=str(d.get("title") or "")
                    year=d.get("year")
                    count=int(d.get("count",0) or 0)
                    lines.append(f"• <b>{escape(title)}</b>"+(f" ({year})" if year else "")+f" — {count:,} files")
                lines.append("\nReview each title before using /mergeshow or /aliasremove. No catalogue data was modified.")
                await say("\n".join(lines))
    elif cmd == "aliasremove":
        if not ctx.is_admin(uid):
            await say("Admins only.")
        elif not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            raw=" ".join(args).strip()
            confirm=raw.casefold().startswith("confirm ")
            if confirm: raw=raw[8:].strip()
            force=raw.casefold().startswith("force ")
            if force: raw=raw[6:].strip()
            kept_self=[]
            titles=[x.strip() for x in raw.split(";") if x.strip()][:12]
            if not titles:
                await say("Usage: /aliasremove Title One;Title Two\nThen repeat with /aliasremove confirm followed by the same titles.")
            else:
                from series_catalog import show_id
                cat=ctx.series_catalog; rows=[]; ids=[]
                for title in titles:
                    candidates={show_id(title,None)}
                    async for d in cat.shows.find({"title":title},{"_id":1,"year":1}).limit(10):
                        candidates.add(show_id(title,d.get("year")))
                    matches=[]
                    for sid in sorted(candidates):
                        row=await cat.aliases.find_one({"_id":sid},{"sid":1})
                        if not row: continue
                        if str(row.get("sid"))==str(sid) and not force:
                            kept_self.append(title); continue        # a rename's own alias: removing it undoes the rename
                        target=await cat.resolve_alias(sid)
                        dest=await cat.shows.find_one({"_id":target},{"title":1,"year":1})
                        dest_title=(dest or {}).get("title","(target show missing)")
                        matches.append((sid,target,dest_title))
                        ids.append(sid)
                    rows.append((title,matches))
                ids=list(dict.fromkeys(ids))
                if not confirm:
                    lines=["<b>Alias removal preview — no changes made</b>","Only direct alias records for the exact titles below will be removed. Catalogue files and show records are not touched.",""]
                    for title,matches in rows:
                        lines.append(f"<b>{escape(title)}</b>")
                        if matches:
                            for sid,target,dest in matches:
                                lines.append(f"• <code>{sid}</code> → {escape(str(dest))} (<code>{target}</code>)")
                        else: lines.append("• No matching alias.")
                    if kept_self:
                        lines.append("\nKept (these are /renameshow aliases; removing them would undo the rename): " + escape(", ".join(dict.fromkeys(kept_self))) + ". Add the word force right after /aliasremove to remove them anyway.")
                    if ids:
                        lines.append("\nTo remove exactly these aliases, repeat the command with <code>confirm</code> after /aliasremove.")
                    else:
                        lines.append("\nNo aliases found; nothing to remove.")
                    await say("\n".join(lines))
                else:
                    removed=await cat.remove_alias_ids(ids)
                    lines=[f"<b>Alias cleanup complete</b>",f"Direct alias records removed: {removed}","No files or show records were changed.","\nNext run <code>/reparse dry</code> and review the proposed changes before applying them."]
                    await say("\n".join(lines))
    elif cmd == "mergeshow":
        if not ctx.is_admin(uid):
            await say("Admins only.")
        elif not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            raw = " ".join(args).strip()
            if not raw:
                ctx.pending_series_merge[uid] = {"step": "search_absorb", "at": time.monotonic()}
                await say(
                    "<b>Interactive Show Merge (Step 1/3)</b>\n\n"
                    "Send the title (or part of the title) of the <b>show to absorb</b> (the duplicate/wrong entry to merge away).\n\n"
                    "Tip: You can still merge directly with <code>/mergeshow Survivor | Absorbed</code>.",
                    [[Btn("Cancel", "ss:mergecancel")]]
                )
            else:
                parts = [x.strip() for x in raw.split("|") if x.strip()]
                if len(parts) < 2:
                    await say("Usage: /mergeshow Survivor | Show to absorb | Another to absorb\n"
                              "Or send /mergeshow with no arguments to search and select interactively.\n\n"
                              "The FIRST title survives and keeps its own title and year; every title after | is absorbed (up to 8). "
                              "To pick one entry when several share a title, add the year as (2005) or (no year). "
                              "You can also use sid:SID. /mergeshows dry prints ready-to-copy commands.")
                elif len(parts) > 8:
                    await say("You can merge at most 8 catalogue entries in one operation. The first survives; the rest are absorbed.")
                else:
                    selections, errors = await series_handlers.resolve_merge_titles(ctx, parts)
                    if errors:
                        await say("<b>Merge not prepared — no changes made</b>\n\n" + "\n".join("• " + x for x in errors))
                    elif len({x["sid"] for x in selections}) != len(selections):
                        await say("The same catalogue entry was selected more than once. Nothing was changed.")
                    else:
                        survivor, absorbed = selections[0], selections[1:]
                        ctx.pending_series_merge[uid] = {"step": "batch_confirm", "survivor": survivor,
                                                         "absorbed_sids": [x["sid"] for x in absorbed], "at": time.monotonic()}
                        lab = lambda x: escape(x["title"]) + (f" ({x['year']})" if x.get("year") else " (no year)") + f" · {x['count']:,} files"
                        lines = ["<b>Confirm show merge</b>", "", "<b>KEEP (first title):</b> " + lab(survivor)]
                        lines += ["<b>ABSORB:</b> " + lab(x) for x in absorbed]
                        lines += ["", f"This merges {len(absorbed)} show(s) into the first entry, which keeps its title and year.",
                                  "Files, linked movies, follows, progress and requests are reassigned and the surviving show is deduplicated automatically.",
                                  "This confirmation expires in 10 minutes."]
                        await say("\n".join(lines), [[Btn("Confirm merge", "ss:mergebatchconfirm")], [Btn("Cancel", "ss:mergecancel")]])
    elif cmd in ("addpromo", "delpromo", "promos"):
        settings = await ctx.settings()
        promos = list(settings.get("series_promos", []))
        value = " ".join(args).strip()
        if cmd == "promos":
            await say("<b>Custom series promo tokens</b>\n" + ("\n".join(f"• {escape(x)}" for x in promos) if promos else "No custom promo tokens configured."))
        elif not value:
            await say(f"Usage: /{cmd} <text>")
        elif cmd == "addpromo":
            if value.casefold() not in {x.casefold() for x in promos}: promos.append(value)
            await ctx.set_setting("series_promos", promos)
            await say(f"Added promo token: <b>{escape(value)}</b>. You can review possible duplicate identities with /mergeshows dry and merge entries manually with /mergeshow.")
        else:
            promos = [x for x in promos if x.casefold() != value.casefold()]
            await ctx.set_setting("series_promos", promos)
            await say(f"Removed promo token: <b>{escape(value)}</b>.")
    elif cmd == "dedupewhy":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        elif len(args) < 3 or not args[-1].isdigit() or not args[-2].isdigit():
            await say("Usage: <code>/dedupewhy Show Title SEASON EPISODE</code>\nExample: <code>/dedupewhy Friends 10 16</code>\nRead-only: lists every stored copy of that episode and says, pair by pair, why the dedupe did or did not treat them as one file.")
        else:
            title = " ".join(args[:-2]); season, episode = int(args[-2]), int(args[-1])
            info = await ctx.series_catalog.explain_duplicates(title, season, episode)
            recs, pairs, shows = info["records"], info["pairs"], info["shows"]
            if not recs:
                await say(f"No stored files for <b>{escape(title)}</b> S{season:02d}E{episode:02d} in {len(shows)} matching show(s).")
            else:
                lines = [f"<b>{escape(title)}</b> S{season:02d}E{episode:02d}: {len(recs)} stored copies in {len({d['sid'] for d in recs})} catalogue entr{'y' if len({d['sid'] for d in recs}) == 1 else 'ies'}"]
                for n, d in enumerate(recs, 1):
                    bits = [x for x in (d.get("resolution"), d.get("source"), d.get("codec"), ", ".join(d.get("languages") or ()), ", ".join(d.get("tags") or ()), d.get("group")) if x]
                    lines.append(f"\n<b>#{n}</b> {int(d.get('size') or 0):,} bytes · show <code>{escape(str(d['sid'])[:6])}</code> ({escape(str(d.get('_show_title') or ''))}{' ' + str(d['_show_year']) if d.get('_show_year') else ''})\n<code>{escape((d.get('name') or '')[:90])}</code>\n{escape(' · '.join(bits) or 'no quality tags')}")
                lines.append("")
                for i, j, why in pairs[:20]:
                    lines.append(f"#{i + 1} vs #{j + 1}: " + ("✅ same file, dedupe removes one" if why is None else f"❌ kept apart: {escape(why)}"))
                if len({d['sid'] for d in recs}) > 1:
                    lines.append("\n⚠️ These copies sit under different catalogue entries. Look-alikes are only compared inside one show, so merge the entries first (<code>/mergeshow</code>).")
                await say("\n".join(lines)[:3900])
    elif cmd == "dedupeseries":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        elif ctx.maintenance or ctx.index_job or getattr(ctx, "audit_job", None):
            await say("Another maintenance or indexing job is running. Wait for it to finish first.")
        else:
            dry = bool(args and args[0].lower() == "dry")
            ctx.maintenance = "dedupeseries"
            progress = await say("Checking existing series for look-alike duplicates (read-only dry run)…" if dry else "Preparing duplicate keys and removing catalogue duplicates in batches…")

            async def _job():
                try:
                    result = await ctx.series_catalog.dedupe_series(dry_run=dry, progress=progress)
                    if not dry: await ctx.series_catalog.rebuild_shows()
                    verb = "would be removed" if dry else "removed from the catalogue"
                    examples = ", ".join(result["examples"]) or "None"
                    await progress.edit_text(f"Series dedupe {'dry run' if dry else 'complete'}.\nScanned: {result['scanned']:,}\nDuplicates {verb}: {result['removed']:,}\nExample shows: {escape(examples)}" + (("\nSame size and episode but kept as separate files: " + ", ".join(f"{k} ×{v:,}" for k, v in sorted(result.get("kept_apart", {}).items(), key=lambda kv: -kv[1])[:6])) if result.get("kept_apart") else "") + ("\nUse /dedupewhy Show SEASON EPISODE to see why for one episode." if not result["removed"] else ""))
                except Exception:
                    log.exception("series dedupe failed")
                    try: await progress.edit_text("Series dedupe stopped because of an error. Check the Render logs.")
                    except Exception: pass
                finally:
                    ctx.maintenance = None
            _spawn(_job())
    elif cmd == "mergeshows":
        if not ctx.is_admin(uid):
            await say("Admins only.")
        elif not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        elif ctx.maintenance:
            await say(f"Another maintenance operation is running ({ctx.maintenance}). Wait for it to finish or stop it first.")
        elif not args or args[0].lower() in ("dry", "review"):
            import series_handlers
            ctx.maintenance = "mergeshows"
            ctx.maintenance_stop.clear()
            progress = await say("Preparing a read-only show-identity review…")
            async def _job():
                try:
                    await series_handlers.merge_shows_catalog(ctx, progress, dry_run=True)
                except Exception:
                    log.exception("series show-identity review failed")
                    try: await progress.edit_text("Show review stopped because of an error. Check the Koyeb logs.")
                    except Exception: pass
                finally:
                    ctx.maintenance = None
                    ctx.maintenance_stop.clear()
            _spawn(_job())
        else:
            await say("Usage: /mergeshows dry\n\nThis command only lists possible duplicate show identities. To merge a pair you spotted yourself, send /mergeshow and select the incorrect show first, then the correct show.")
    elif cmd == "audit":
        await _audit_cmd(ctx, client, m, args, say)
    elif cmd == "series_skipped":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            page=int(args[0]) if args and args[0].isdigit() else 1
            page=max(1,page); rows = await ctx.series_catalog.skipped_rows(12,(page-1)*12)
            if not rows:
                await say("No unrecognized media posts on that page. Use <code>/series_skipped</code> to return to page 1.")
            else:
                lines=[f"<b>Media not indexed as a series</b> · page {page}","These channel posts had no recognizable show/season/episode pattern or were identified as look-alike duplicates.",""]
                for r in rows:
                    lines.append(f"• <code>{escape(str(r.get('channel')))}:{escape(str(r.get('message_id')))}</code> — {escape(str(r.get('name','(no name)'))[:90])}\n  <i>{escape(str(r.get('reason','Unrecognized series filename'))[:90])}</i>")
                lines.append("\nRename the file or add a useful caption, then repost it or run /index CHANNEL FROM_MESSAGE_ID.")
                lines.append(f"Next page: <code>/series_skipped {page+1}</code> · Previous: <code>/series_skipped {max(1,page-1)}</code>")
                await say("\n".join(lines))
    elif cmd == "stats":
        c = await ctx.store.counts()
        sc = await ctx.series_catalog.counts() if getattr(ctx, "series_catalog", None) else {"files": 0, "shows": 0}
        mem, subs = rss_mb(), await ctx.subs()
        day = lagos_day()
        saved, waiting = await ctx.store.stats_get(day), ctx.stats.peek(day)
        t = {k: saved.get(k, 0) + waiting.get(k, 0) for k in set(saved) | set(waiting)}
        if getattr(ctx, "series_catalog", None):
            library_lines=[f"Series files: {sc['files']:,}", f"Series: {sc['shows']:,}"]
        else:
            library_lines=[f"Files: {c['files']:,}", f"Films: {c['films']:,}  (search index: {len(ctx.index):,})"]
        lines = library_lines + [f"Users: {c['users']:,}", f"Supporters: {c.get('supporters', 0):,}", f"Open requests: {c['requests']}",
                 f"Surveys taken: {((await ctx.store.get_survey_stats()).get('_meta') or {}).get('count', 0):,}"]
        if c["db_mb"] is not None:
            lines.append(f"Database: {c['db_mb']} MB of 512")
        if mem:
            lines.append(f"Bot memory: {mem} MB of 512" + ("  (getting tight)" if mem > 420 else ""))
        lines.append("Force-join: " + (f"{len(subs)} channel{'s' if len(subs) != 1 else ''} ("
                                       + ", ".join(escape(x['title']) for x in subs) + ")" if subs else "off"))
        lines += ["", f"<b>Today</b>  ({day}, Lagos time)", f"Users active: {t.get('u', 0):,}",
                  f"New users: {t.get('n', 0):,}", f"Searches: {t.get('s', 0):,}", f"Files sent: {t.get('f', 0):,}",
                  f"Requests: {t.get('r', 0):,}"]
        top = []
        if getattr(ctx, "series_catalog", None):
            for d in await ctx.series_catalog.popular_shows(5):
                top.append(f"• {escape(d.get('title','Series'))}  ({d.get('hits',0)} file sends)")
        else:
            for fid,n in await ctx.store.top_films(7,5):
                f=ctx.index.get(fid)
                if f: top.append(f"• {escape(f.label)}  ({n})")
        if top:
            lines += ["", "Most received this week:"] + top
        miss = await ctx.store.top_misses(5)
        if miss:
            lines += ["", "Most searched but not found:"] + [f"• {escape(k)}  ({n})" for k, n in miss]
        await say("\n".join(lines))
    elif cmd == "setseries":
        name = (args[0] if args else "").lstrip("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", name):
            cur = (await ctx.settings())["series_bot"]
            await say(f"The series bot is @{escape(cur)}. To change it: <code>/setseries @username</code>")
            return
        await ctx.set_setting("series_bot", name)
        await say(f"Series bot is now @{escape(name)}. Series searches and 'not found' messages point there.")
    elif cmd == "setseriespicks":
        raw = m.text.split(None,1)[1] if len(m.text.split(None,1))>1 else ""
        titles=[x.strip() for x in raw.split("|") if x.strip()]
        if not titles:
            current=(await ctx.settings()).get("series_picks",[])
            lines=[]
            for sid in current:
                d=await ctx.series_catalog.get_show(sid) if getattr(ctx,"series_catalog",None) else None
                if d: lines.append("• "+escape(d.get("title","")))
            await say("Current editor picks:\n"+("\n".join(lines) if lines else "None")+"\n\nUse <code>/setseriespicks Show One | Show Two</code> to replace them, or manage lists with /curated.")
            return
        if not getattr(ctx,"series_catalog",None): await say("Series catalogue is not connected."); return
        chosen=[]; missed=[]
        for title in titles[:30]:
            found=await ctx.series_catalog.search_shows(title,5)
            match=next((d for d in found if normalize(d.get("title",""))==normalize(title)),None)
            if match: chosen.append(match.get("_id",match.get("sid")))
            else: missed.append(title)
        import series_handlers
        unique_chosen=list(dict.fromkeys(chosen))
        lists=series_handlers.get_curated_lists(await ctx.settings())
        if lists:
            lists[0]["sids"]=unique_chosen
        else:
            lists=[{"id":"picks","name":(await ctx.settings()).get("series_picks_name") or "Editor picks","sids":unique_chosen}]
        await series_handlers.save_curated_lists(ctx,lists)
        msg=f"Saved {len(unique_chosen)} editor pick(s)."
        if missed: msg+="\nNot found: "+", ".join(escape(x) for x in missed)
        await say(msg)
    elif cmd == "setfilmbot":
        name = (args[0] if args else "").lstrip("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", name):
            cur = (await ctx.settings()).get("film_bot", "EricRohmerBot")
            await say(f"Film bot link: @{escape(cur)}. Change it with <code>/setfilmbot @username</code>.")
            return
        await ctx.set_setting("film_bot", name)
        await say(f"The ‘Looking for a film?’ button now points to @{escape(name)}.")
    elif cmd == "setseriesgroup":
        url = args[0].strip() if args else ""
        if not (url.startswith("https://t.me/") or url.startswith("http://t.me/") or url.lower() == "off"):
            cur = (await ctx.settings()).get("series_group")
            await say(f"Current series group: {escape(cur or 'Not set')}.\n"
                      f"To change: <code>/setseriesgroup https://t.me/...</code> or <code>/setseriesgroup off</code>")
            return
        val = None if url.lower() == "off" else url
        await ctx.set_setting("series_group", val)
        await say("Series group link removed." if not val else f"Series group set to {escape(val)}.")
    elif cmd == "groupsettings":
        t, rows = ui.group_settings_screen(await ctx.settings())
        await say(t, rows)
    elif cmd == "setsummary":
        arg = (args[0] if args else "").lower()
        if arg == "off" or re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", arg):
            await ctx.set_setting("summary_time", None if arg == "off" else arg)
            await say("Daily summary is off." if arg == "off" else f"Daily summary will arrive around {arg} Lagos time.")
        else:
            cur = (await ctx.settings())["summary_time"]
            await say(f"Daily summary: {cur + ' Lagos time' if cur else 'off'}. Change it with "
                      "<code>/setsummary 08:00</code> or <code>/setsummary off</code>.")
    elif cmd == "seriesdigest":
        t, rows = ui.series_digest_settings_screen(await ctx.settings())
        await say(t, rows)
    elif cmd == "digestshows":
        if getattr(ctx, "series_catalog", None) is None:
            await say("Series catalogue is not connected.")
            return
        import series_handlers
        t, rows = await series_handlers.digest_shows_menu_screen(ctx, 0)
        await say(t, rows)
    elif cmd == "setseriesupdatechannel":
        raw = _inline_arg(m)
        if not raw:
            await say("Usage: <code>/setseriesupdatechannel @channel</code> or <code>/setseriesupdatechannel -100...</code>\n"
                      "Or use /seriesdigest and tap <b>Set Update Channel</b> to forward a post.")
            return
        import series_handlers
        if await series_handlers.configure_series_digest_channel(ctx, client, m, raw):
            t, rows = ui.series_digest_settings_screen(await ctx.settings())
            await say(t, rows)
    elif cmd == "purgeseries":
        await say("The /purgeseries command is disabled; remove individual series files only after reviewing them.")
    elif cmd == "settings":
        t, rows = await settings_view(ctx)
        await say(t, rows)
    elif cmd == "customize":
        await say(ui.CUSTOMIZE)
    elif cmd == "preview":
        page = args[0].lower() if args else "welcome"
        if page in ("help", "about", "discover", "donate", "survey", "mylist"):
            m.command = [page]                              # render that page exactly as users see it, with ITS image
            await cmd_info(ctx, client, m)
        elif page == "welcome":
            await send_home(ctx, m)
        else:
            await say("Usage: /preview [welcome|discover|about|help|donate|survey|mylist]")
    elif cmd == "forcesub":
        await _forcesub(ctx, client, m, args, say)
    elif cmd == "index":
        await _index_cmd(ctx, client, m, args, say)
    elif cmd == "indexstop":
        if ctx.index_job:
            ctx.index_job["stop"] = True
            await say("Stopping after the current batch.")
        else:
            await say("Nothing is running.")
    elif cmd == "dedupe":
        await say("Scanning for look-alike files. This can take a few minutes…")
        items = await ctx.store.lookalikes()
        if not items:
            await say("No look-alikes found.")
        else:
            ctx.pending_dedupe = items
            more = (f"\nThat's one round of {DEDUPE_BATCH:,}. If there are more, run /dedupe again afterwards."
                    if len(items) >= DEDUPE_BATCH else "")
            await say(f"Found <b>{len(items):,}</b> extra copies: same film, exactly the same size, uploaded separately. "
                      f"One of each stays. Remove the extras? You can review them afterwards in /skipped.{more}",
                      [[Btn("Yes, remove them", "ddy"), Btn("No", "ddn")]])
    elif cmd == "skipped":
        if getattr(ctx, "series_catalog", None):
            import series_handlers
            t, rows = await series_handlers.skipped_screen(ctx)
        else:
            t, rows = ui.skipped_menu(await ctx.store.skip_counts())
        await say(t, rows)
    elif cmd == "singleshows":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            progress = await say("Listing shows with exactly one file…")
            export = bool(args) and args[0].lower() == "export"
            pg = int(args[0]) - 1 if args and args[0].isdigit() else 0

            async def _ss_job():
                try: await progress.edit_text(await series_handlers.single_shows_report(ctx, client, m.chat.id, export, pg), parse_mode=HTML)
                except Exception:
                    log.exception("singleshows failed")
                    try: await progress.edit_text("Failed. Check the Render logs.")
                    except Exception: pass
            _spawn(_ss_job())
    elif cmd == "autoruns":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            arg = (args[0].lower() if args else "dry")
            if arg not in ("dry", "confirm"):
                await say("Usage: /autoruns dry  or  /autoruns confirm")
            else:
                progress = await say("Looking for runs that name their show…")

                async def _ar_job():
                    try: await progress.edit_text(await series_handlers.autoruns_report(ctx, client, apply=(arg == "confirm")), parse_mode=HTML)
                    except Exception:
                        log.exception("autoruns failed")
                        try: await progress.edit_text("Failed. Check the Render logs.")
                        except Exception: pass
                _spawn(_ar_job())
    elif cmd == "wwemerge":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            arg = (args[0].lower() if args else "dry")
            if arg not in ("dry", "confirm"):
                await say("Usage: /wwemerge dry  or  /wwemerge confirm")
            else:
                progress = await say("Looking for WWE shows…")

                async def _ww_job():
                    try: await progress.edit_text(await series_handlers.wwe_merge_report(ctx, apply=(arg == "confirm")))
                    except Exception:
                        log.exception("wwemerge failed")
                        try: await progress.edit_text("Failed. Check the Render logs.")
                        except Exception: pass
                _spawn(_ww_job())
    elif cmd == "skipruns":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            progress = await say("Looking for runs…")
            pg = int(args[0]) - 1 if args and args[0].isdigit() else 0

            async def _sr_job():
                try: await progress.edit_text(await series_handlers.skip_runs_report(ctx, pg), parse_mode=HTML, disable_web_page_preview=True)
                except Exception:
                    log.exception("skipruns failed")
                    try: await progress.edit_text("Failed. Check the Render logs.")
                    except Exception: pass
            _spawn(_sr_job())
    elif cmd == "skipclear":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            if not args:
                await say("Usage: /skipclear CODE  (codes: " + ", ".join(series_handlers.SKIP_CLEAR_CODES) + ")")
            else:
                cnt = int(args[2]) if len(args) >= 3 and args[1].lower() == "confirm" and args[2].isdigit() else None
                await say(await series_handlers.skip_clear_report(ctx, args[0].lower(), cnt))
    elif cmd == "dbswitch":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            mode = (args[0].lower() if args else "status")
            if mode not in ("status", "primary", "secondary", "auto"):
                await say("Usage: /dbswitch status | primary | secondary | auto")
            else:
                await say(await series_handlers.db_switch_report(ctx, mode))
    elif cmd == "dbsize":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            try: await say(await series_handlers.db_size_report(ctx))
            except Exception:
                log.exception("dbsize failed"); await say("Could not read the database sizes. Check the Render logs.")
    elif cmd == "fixtitles":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            a = [x.lower() for x in args]
            mode = a[0] if a else "dry"
            if mode not in ("dry", "list", "confirm") or (mode in ("list", "confirm") and len(a) < 2):
                await say("Usage:\n/fixtitles dry\n/fixtitles list contained 1\n/fixtitles confirm label\n/fixtitles confirm contained")
            else:
                progress = await say("Reading every show against its files… this takes a few minutes.")
                klass = a[1] if len(a) > 1 else None
                pg = int(a[2]) - 1 if len(a) > 2 and a[2].isdigit() else 0

                async def _ft_job():
                    try: await progress.edit_text(await series_handlers.fix_titles_report(ctx, mode, klass, pg), parse_mode=HTML)
                    except Exception:
                        log.exception("fixtitles failed")
                        try: await progress.edit_text("Failed. Check the Render logs, then run /fixtitles dry again.")
                        except Exception: pass
                _spawn(_ft_job())
    elif cmd == "fixpromoshows":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            arg = (args[0].lower() if args else "dry")
            if arg not in ("dry", "confirm"):
                await say("Usage: /fixpromoshows dry  (preview)  or  /fixpromoshows confirm  (apply)")
            else:
                progress = await say("Working…")

                async def _fp_job():
                    try: await progress.edit_text(await series_handlers.fix_promo_shows(ctx, apply=(arg == "confirm")))
                    except Exception:
                        log.exception("fixpromoshows failed")
                        try: await progress.edit_text("Failed. Nothing was half-applied that matters; check the Render logs and run /fixpromoshows dry again.")
                        except Exception: pass
                _spawn(_fp_job())
    elif cmd == "skippedexport":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            arg = (args[0].lower() if args else "all")
            if arg == "reset":
                series_handlers._EXPORTS_RUNNING.pop(uid, None)
                await say("Export lock cleared. Send /skippedexport again.")
            elif arg not in series_handlers._EXPORT_CATEGORIES:
                await say("Usage: /skippedexport [u|a|f|r|l|reset]\nNo argument exports everything. u unreadable, a ambiguous, f films, r unrecognised series, l look-alikes. Large lists arrive as several numbered .txt parts.")
            else:
                await say("Preparing the export… large lists arrive as several numbered parts.")
                _spawn(series_handlers.send_skipped_export(ctx, client, m.chat.id, uid, series_handlers._EXPORT_CATEGORIES[arg]))
    elif cmd == "skipsearch" or cmd == "skipinfo" or cmd == "filmreview" or cmd == "reclassify":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
            return
        import series_handlers
        if cmd == "filmreview":
            t, rows = await series_handlers.skipped_screen(ctx, "f", 0, uid)
            await say(t, rows)
        elif cmd == "skipsearch":
            words = list(args)
            category = None
            if len(words) > 1 and words[0] in series_handlers.SKIP_CODES:
                category = series_handlers.SKIP_CODES[words[0]]; words = words[1:]
            query = " ".join(words).strip()
            if not query:
                await say("Usage: /skipsearch words  (or /skipsearch f words to search only likely films; codes: f films, r unrecognised, a ambiguous, u unreadable, l look-alikes)")
            else:
                await ctx.series_catalog.save_skip_view(uid, category, query)
                t, rows = await series_handlers.skipped_screen(ctx, "q", 0, uid)
                await say(t, rows)
        elif cmd == "skipinfo":
            if not args:
                await say("Usage: /skipinfo CHANNEL:MESSAGE_ID")
            else:
                t, rows = await series_handlers.skip_detail_screen(ctx, args[0].strip())
                await say(t, rows)
        else:
            dry = bool(args) and args[0].lower() == "dry"
            if not dry and not (args and args[0].lower() == "confirm"):
                await say("Re-sorting only changes the labels on skip records. Preview first: <code>/reclassify dry</code>, then <code>/reclassify confirm</code>.")
            else:
                progress = await say("Working…")

                async def _rc_job():
                    try: await progress.edit_text(await series_handlers.reclassify_report(ctx, dry=dry), parse_mode=HTML)
                    except Exception:
                        log.exception("reclassify failed")
                        try: await progress.edit_text("Reclassify failed. Check the Render logs.")
                        except Exception: pass
                _spawn(_rc_job())
    elif cmd == "adopt":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        else:
            raw = " ".join(args).strip()
            dry = raw.lower().startswith("dry ")
            if dry: raw = raw[4:].strip()
            parts = [x.strip() for x in raw.split("|") if x.strip()]
            if len(parts) < 2 or len(parts) > 3:
                await say("Usage: /adopt [dry] CHANNEL:FROM-TO | Show title | Season 1 (optional)\n\n"
                          "Gives a run of skipped posts to one show. Example:\n"
                          "/adopt dry -1002019316808:943356-943379 | Fullmetal Alchemist: Brotherhood\n\n"
                          "Episode numbers are read from each filename (57. Eternal Leave = episode 57). The title must match an existing show exactly, "
                          "or a NEW show is created. Without 'dry' you get a preview and a Confirm button: nothing is written until you press it.\n\n"
                          "Legacy numbering (only for files you have checked yourself): ... | Sabrina the Teenage Witch | Season 4 legacy  (412 = S04E12).")
            else:
                season = None; legacy = False
                if len(parts) == 3:
                    sm = re.fullmatch(r"(?i)season\s*(\d{1,2})(\s+legacy)?", parts[2])
                    if not sm:
                        await say("The third part must look like: Season 2   (or: Season 4 legacy)")
                        return
                    season = int(sm.group(1)); legacy = bool(sm.group(2))
                import series_handlers
                progress = await say("Working…")

                async def _job():
                    try:
                        text, rows = await series_handlers.adopt_preview(ctx, client, uid, parts[0], parts[1], season, dry, legacy)
                        await progress.edit_text(text, parse_mode=HTML, reply_markup=kb(rows))
                    except Exception:
                        log.exception("adopt failed")
                        try: await progress.edit_text("Adopt failed. Check the Render logs.")
                        except Exception: pass
                _spawn(_job())
    elif cmd == "curated" or (cmd == "picks" and getattr(ctx, "series_catalog", None) is not None):
        if getattr(ctx, "series_catalog", None) is None:
            await say("Series catalogue is not connected.")
        else:
            import series_handlers
            t, rows = await series_handlers.curated_menu_screen(ctx)
            await say(t, rows)
    elif cmd == "picks":
        s = await ctx.settings()
        lines = []
        for fid in s["picks"]:
            f = ctx.index.get(fid)
            lines.append(f"• {escape(f.label)}" if f else f"• (missing) {fid}")
        await say(f"<b>{escape(s['picks_name'])}</b>\n" + ("\n".join(lines) or "Empty. Use the Hand-pick button on a film page."))
    elif cmd in ("setwelcome", "setabout", "sethelp"):
        key = cmd[3:]
        if args and args[0].lower() == "reset":
            await ctx.set_setting(key, None)
            await say(f"Back to the default {key} text.")
            return
        html = _reply_html(m)
        if not html:
            await say(f"Reply to a message containing the text with <code>/{cmd}</code>.")
            return
        await ctx.set_setting(key, ui.safe_html(html))
        await say(f"Saved. Use <code>/preview {key}</code> to see it.")
    elif cmd == "images":
        s = await ctx.settings()
        p_imgs = s.get("page_images") or {}
        img = s.get("image")
        pages = ["welcome", "about", "discover", "mylist", "donate", "help", "survey"]
        lines = ["<b>Configured Images:</b>"]
        for p in pages:
            val = p_imgs.get(p) or (img if p == "welcome" else None)
            lines.append(f"• {p.title()}: {'Set' if val else 'None'}")
        lines.append("\nSet page image: reply to a photo with <code>/setimage [page]</code> (welcome, about, discover, mylist, donate, help, survey).")
        await say("\n".join(lines))
    elif cmd == "setimage":
        photo = m.photo or (m.reply_to_message.photo if m.reply_to_message else None)
        if not photo:
            await say("Send a photo with <code>/setimage [page]</code> as its caption, or reply to a photo with it.\nPages: welcome, about, discover, mylist, donate, help, survey")
            return
        raw_page = args[0].lower() if args else "welcome"
        page = {"my_list": "mylist", "my-list": "mylist"}.get(raw_page, raw_page)
        s = await ctx.settings()
        p_imgs = dict(s.get("page_images") or {})
        p_imgs[page] = photo.file_id
        if page == "welcome":
            await ctx.set_setting("image", photo.file_id)
        await ctx.set_setting("page_images", p_imgs)
        await say(f"Image for '<b>{escape(page)}</b>' saved. See it with <code>/preview {escape(page)}</code>.")
    elif cmd == "clearimage":
        raw_page = args[0].lower() if args else "welcome"
        page = {"my_list": "mylist", "my-list": "mylist"}.get(raw_page, raw_page)
        s = await ctx.settings()
        p_imgs = dict(s.get("page_images") or {})
        p_imgs.pop(page, None)
        if page == "welcome":
            await ctx.set_setting("image", None)
        await ctx.set_setting("page_images", p_imgs)
        await say(f"Image for '<b>{escape(page)}</b>' removed.")
    elif cmd == "donorsettings":
        s = await ctx.settings()
        stars_on = s.get("stars_enabled", True)
        donors = await ctx.store.donor_list()
        lines = [
            "<b>Donor Settings & Tiers</b>\n",
            "• <b>Basic Supporter:</b> 30 days perks",
            "• <b>Standard Supporter:</b> 60 days perks",
            "• <b>Full / Star Supporter:</b> 90 days perks",
            f"• <b>Telegram Stars:</b> {'Enabled' if stars_on else 'Disabled'} (toggle via <code>/settings</code>)",
            f"• <b>Active Supporters:</b> {len(donors)} users",
            "\nManual perk command:\n<code>/donor add UID DAYS NOTE</code>"
        ]
        await say("\n".join(lines))
    elif cmd == "donor":
        sub = args[0].lower() if args else ""
        if sub == "claims":
            claims = await ctx.store.list_pending_claims(20)
            lines = ["<b>Pending donation claims (up to 20)</b>"]
            for claim in claims:
                cid = escape(str(claim.get("id", "")))
                state = "receipt marked submitted" if claim.get("receipt_submitted") else "awaiting receipt"
                lines.append(f"<code>/donor claim {cid}</code> — {state}")
            if not claims:
                lines.append("No pending claims.")
            await say("\n".join(lines))
        elif sub == "claim" and len(args) == 2:
            claim = await ctx.store.get_claim(args[1])
            if not claim:
                await say("Claim not found.")
                return
            cid = claim["id"]
            status = claim.get("status", "unknown")
            state = "marked submitted" if claim.get("receipt_submitted") else "not submitted"
            lines = ["<b>Donation claim recovery</b>", f"Claim: <code>{escape(cid)}</code>",
                     f"Telegram ID: <code>{int(claim['uid'])}</code>",
                     f"Method: {escape(str(claim.get('method', ''))[:100])}",
                     f"Status: {escape(status)} · Receipt: {state}",
                     f"Saved note: {escape(str(claim.get('note', ''))[:500])}",
                     "Verify payment independently. A submitted flag does not prove that a receipt was delivered. Ask the donor to resend proof using /donationclaim if necessary."]
            await say("\n".join(lines), ui.claim_admin_rows(cid) if status == "pending" else [[Btn("Reply to donor", f"clm:reply:{cid}")]])
        elif sub == "add" and len(args) >= 2 and args[1].isdigit():
            duid = int(args[1])
            days = int(args[2]) if len(args) >= 3 and args[2].isdigit() else 30
            note = " ".join(args[3:]) if len(args) >= 4 else "manual"
            await ctx.store.donor_add(duid, days=days, name=note)
            await _send(client, duid, "⭐ Your supporter access is active. Choose whether to appear on the Supporters Wall, or use /request for custom requests.", _approval_rows())
            await say(f"Granted {days} days supporter perks to <code>{duid}</code> ({escape(note)}).")
        elif sub == "remove" and len(args) >= 2 and args[1].isdigit():
            duid = int(args[1])
            await ctx.store.donor_remove(duid)
            await say(f"Removed supporter perks from <code>{duid}</code>.")
        elif sub == "list":
            donors = await ctx.store.donor_list()
            if not donors:
                await say("No active supporters.")
            else:
                lines = [f"<b>Active Supporters ({len(donors)}):</b>"]
                for d in donors[:30]:
                    lines.append(f"• <code>{d.get('uid')}</code> until {d.get('su')} ({escape(d.get('name', ''))})")
                await say("\n".join(lines))
        else:
            await say("Usage:\n<code>/donor add UID [DAYS] [NOTE]</code>\n<code>/donor remove UID</code>\n<code>/donor list</code>\n<code>/donor claims</code>\n<code>/donor claim CLAIM_ID</code>")
    elif cmd == "catchup":
        if not getattr(ctx, "series_catalog", None):
            await say("Series catalogue is not connected.")
        elif ctx.index_job or ctx.maintenance:
            await say("Indexing or maintenance is running. Try again when it finishes.")
        else:
            window = int(args[0]) if args and args[0].isdigit() else 300
            window = max(50, min(window, 5000))
            progress = await say(f"Catch-up scan started (checking the latest ~{window} messages of each database channel and anything newer)…")

            async def _job():
                try: await progress.edit_text(await catchup_all(ctx, client, window))
                except Exception:
                    log.exception("manual catch-up failed")
                    try: await progress.edit_text("Catch-up scan failed. Check the Render logs.")
                    except Exception: pass
            _spawn(_job())
    elif cmd == "audioline":
        v = (args[0].lower() if args else "")
        if v not in ("on", "off"):
            cur = (await ctx.settings()).get("show_audio_line", True)
            await say(f"Usage: /audioline on|off. Shows a line like 🔊 Hindi · English above the filename in delivered files when the filename or caption states the audio. Currently: {'on' if cur else 'off'}.")
        else:
            await ctx.set_setting("show_audio_line", v == "on")
            await say(f"Audio line is now {v}.")
    elif cmd == "setcontact":
        value = " ".join(args).strip()
        if not value:
            current = (await ctx.settings()).get("donate_contact")
            await say("Usage: /setcontact @yourusername (or a t.me link). /setcontact clear removes it, and the Donate page then links to the first admin's account.\n"
                      f"Current: {escape(str(current)) if current else 'not set'}")
        elif value.lower() == "clear":
            await ctx.set_setting("donate_contact", None)
            await say("Contact button reset. It now links to the first admin's account.")
        else:
            await ctx.set_setting("donate_contact", value)
            link = ui.contact_link({"donate_contact": value})
            await say(f"Donate page contact button now opens: {escape(str(link))}")
    elif cmd == "setdonate":
        sub = args[0].lower() if args else ""
        s = await ctx.settings()
        methods = list(s.get("donate_methods", []))
        if sub == "reset":
            await ctx.set_setting("donate_methods", [])
            await say("Donation methods cleared.")
        elif sub == "list":
            if not methods:
                await say("No donation methods configured.")
            else:
                lines = ["<b>Configured Donation Methods:</b>"]
                for i, mth in enumerate(methods, 1):
                    lines.append(f"\n<b>{i}. {escape(mth.get('title',''))}</b> ({escape(mth.get('type',''))})\n{escape(mth.get('details',''))}")
                    lines.append(f"<code>/setdonate edit {i} | TYPE | TITLE | DETAILS</code>")
                    lines.append(f"<code>/setdonate remove {i}</code>")
                await say("\n".join(lines))
        elif sub == "remove":
            if len(args) < 2 or not args[1].isdigit():
                await say("Usage: <code>/setdonate remove NUMBER</code>. Get numbers with <code>/setdonate list</code>.")
            else:
                idx = int(args[1]) - 1
                if not 0 <= idx < len(methods):
                    await say("That payment method number doesn't exist. Check <code>/setdonate list</code>.")
                else:
                    removed = methods.pop(idx)
                    await ctx.set_setting("donate_methods", methods)
                    await say(f"Removed donation method <b>{escape(removed.get('title', ''))}</b>.")
        elif sub in ("add", "edit"):
            body = " ".join(args[1:])
            parts = [p.strip() for p in body.split("|")]
            edit_idx = None
            if sub == "edit":
                if len(args) < 3 or not args[1].isdigit():
                    await say("Usage: <code>/setdonate edit NUMBER | TYPE | TITLE | DETAILS</code>")
                    return
                edit_idx = int(args[1]) - 1
                body = " ".join(args[2:])
                parts = [p.strip() for p in body.split("|")]
                if not 0 <= edit_idx < len(methods):
                    await say("That payment method number doesn't exist. Check <code>/setdonate list</code>.")
                    return
            if len(parts) >= 3 and parts[0] and parts[1]:
                m_type, m_title = parts[0], parts[1]
                fields = parts[2:]
                # Accept the short bank format the owner already used, while retaining labelled/custom fields.
                if m_type.lower() == "bank" and len(fields) >= 3 and not any(":" in x for x in fields[:3]):
                    details = f"Bank: {fields[0]}\nAccount number: {fields[1]}\nAccount name: {' | '.join(fields[2:])}"
                else:
                    details = "\n".join(x for x in fields if x)
                old = methods[edit_idx] if edit_idx is not None else {}
                entry = {"type": m_type, "title": m_title, "details": details,
                         "qr": old.get("qr")}
                if edit_idx is None:
                    methods.append(entry)
                    action = "Added"
                else:
                    methods[edit_idx] = entry
                    action = "Updated"
                await ctx.set_setting("donate_methods", methods)
                await say(f"{action} donation method: <b>{escape(m_title)}</b>. View with /setdonate list.")
            else:
                await say("<b>Usage:</b> <code>/setdonate add TYPE | TITLE | DETAILS | MORE DETAILS</code>\n\n"
                          "Example bank: <code>/setdonate add bank | Bank Transfer | Opay | 8031656616 | Martin Akinola</code>\n"
                          "Example USDT: <code>/setdonate add crypto | USDT (BEP20) | Network: BSC (BEP20) | Address: 0xYourWalletAddress</code>\n"
                          "For labelled details, separate each field with <code>|</code>. All fields after TITLE are saved.")
        else:
            msg_lines = [
                "<b>Configure Payment Methods (/setdonate):</b>",
                "<code>/setdonate add TYPE | TITLE | DETAILS | MORE DETAILS</code>",
                "<code>/setdonate list</code> — view methods and their numbers",
                "<code>/setdonate remove NUMBER</code> — remove one method",
                "<code>/setdonate edit NUMBER | TYPE | TITLE | DETAILS</code> — replace one method",
                "<code>/setdonate reset</code> — clear all methods",
                "\nAll fields after TITLE are saved. Use <code>|</code> between fields."
            ]
            await say("\n".join(msg_lines))
    elif cmd == "survey":
        sub = args[0].lower() if args else "stats"
        if sub == "on":
            await ctx.set_setting("survey_active", True)
            await say("Survey activated.")
        elif sub == "off":
            await ctx.set_setting("survey_active", False)
            await say("Survey deactivated.")
        elif sub == "stats":
            stats = await ctx.store.get_survey_stats()
            total = stats.get("_meta", {}).get("count", 0)
            if not total:
                await say("No survey responses yet.")
            else:
                lines = [f"<b>Survey Responses ({total:,} total users):</b>\n"]
                if "gender" in stats:
                    lines.append("<b>Gender:</b>")
                    for k, v in stats.get("gender", {}).items():
                        lines.append(f"  • {escape(k)}: {v:,} users")
                if "age" in stats:
                    lines.append("\n<b>Age:</b>")
                    for k, v in stats.get("age", {}).items():
                        lines.append(f"  • {escape(k)}: {v:,} users")
                if "country" in stats:
                    lines.append("\n<b>Top Countries:</b>")
                    countries = sorted(stats.get("country", {}).items(), key=lambda x: x[1], reverse=True)[:5]
                    for k, v in countries:
                        lines.append(f"  • {escape(k)}: {v:,} users")
                if "discovery" in stats:
                    lines.append("\n<b>Discovery Source:</b>")
                    for k, v in stats.get("discovery", {}).items():
                        lines.append(f"  • {escape(k)}: {v:,} users")
                cohorts = stats.get("_cohorts", {})
                if cohorts:
                    lines.append("\n<b>Top Demographic Cohorts:</b>")
                    top_c = sorted(cohorts.items(), key=lambda x: x[1], reverse=True)[:8]
                    for cohort, cnt in top_c:
                        clean_cohort = cohort.replace("_", ".")
                        lines.append(f"  • {escape(clean_cohort)}: {cnt:,} users")
                await say("\n".join(lines))
        else:
            await say("Usage: <code>/survey [stats|on|off]</code>")
    elif cmd == "setlinks":
        raw = _reply_html(m) or _inline_arg(m)
        links = []
        for line in re.sub(r"<[^>]+>", "", raw or "").splitlines():
            if "|" in line:
                label, url = (x.strip() for x in line.split("|", 1))
                url = url if url.startswith(("http://", "https://", "tg://")) else "https://" + url.lstrip("@/")
                if label and "." in url:
                    links.append([label[:30], url])
        if not links:
            await say("Reply to a message with one button per line, like:\n<code>Join our channel | https://t.me/yourchannel</code>")
            return
        await ctx.set_setting("links", links[:8])
        await say(f"Saved {len(links[:8])} button(s). /preview to see them.")
    elif cmd == "clearlinks":
        await ctx.set_setting("links", [])
        await say("Buttons removed.")
    elif cmd == "setfooter":
        if args and args[0].lower() == "off":
            await ctx.set_setting("footer", "")
            await say("Footer removed.")
            return
        html = _reply_html(m) or _inline_arg(m)
        if not html:
            await say("Reply to a message with <code>/setfooter</code>, or <code>/setfooter off</code>.")
            return
        footer = ui.safe_html(html)
        if len(footer) > 600:
            await say("Footer is too long. Please use a shorter footer (maximum 600 characters including formatting).")
            return
        await ctx.set_setting("footer", footer)
        await say("Footer saved. It will appear on newly delivered files (except clean-file supporters).")
    elif cmd == "setpicks":
        name = _inline_arg(m)[:30]
        if not name:
            await say("Usage: <code>/setpicks Name</code>")
            return
        await ctx.set_setting("picks_name", name)
        await say(f"The list is now called <b>{escape(name)}</b>.")
    elif cmd == "requests":
        page = int(args[0]) - 1 if args and args[0].isdigit() else 0
        all_reqs = await ctx.store.open_requests(500)
        total = len(all_reqs)
        per_page = 10
        pages = max(1, (total + per_page - 1) // per_page)
        page = max(0, min(page, pages - 1))
        slice_reqs = all_reqs[page * per_page:(page + 1) * per_page]
        if not slice_reqs:
            await say("No open requests.")
            return
        lines = [f"<b>Open Requests ({total} total, page {page + 1}/{pages}):</b>\n"]
        for r in slice_reqs:
            lines.append(f"• <code>{escape(r['text'])}</code> ({len(r['users'])} users) — <code>/delreq {escape(r['key'])}</code>")
        rows = []
        nav = []
        if page > 0:
            nav.append(Btn("‹ Prev", f"rqpg:{page - 1}"))
        if page < pages - 1:
            nav.append(Btn("Next ›", f"rqpg:{page + 1}"))
        if nav:
            rows.append(nav)
        await say("\n".join(lines), rows=rows)
    elif cmd == "delreq":
        if not args:
            await say("Usage: <code>/delreq &lt;key&gt;</code>")
            return
        key = args[0]
        ok = await ctx.store.delete_request(key)
        await say(f"Request <code>{escape(key)}</code> {'deleted.' if ok else 'not found.'}")
    elif cmd == "misses":
        rows = await ctx.store.top_misses(15)
        await say("Searches that found nothing:\n" + "\n".join(f"• {escape(k)}  ({n})" for k, n in rows)
                  if rows else "Nothing missed yet.")
    elif cmd == "alias":
        await _alias(ctx, m.text, say)
    elif cmd in ("ban", "unban") and args and re.fullmatch(r"\d+", args[0]):
        flag = cmd == "ban"
        await ctx.store.set_banned(int(args[0]), flag)
        (ctx.banned.add if flag else ctx.banned.discard)(int(args[0]))
        await say(f"{'Banned' if flag else 'Unbanned'} {args[0]}.")
    else:
        await say("Check the command and try again.")


async def _index_cmd(ctx: Ctx, client, m, args, say) -> None:
    if not getattr(m, "from_user", None) or not ctx.is_admin(m.from_user.id):
        await say("Admins only.")
        return
    if ctx.index_job:
        await say("An indexing run is already going. /indexstop to stop it.")
        return
    cps = await ctx.store.settings()
    if not args:
        targets = [(c, int(cps.get(f"cp_{c}") or 1), None) for c in sorted(ctx.cfg.file_channels)]
        status = await say("Starting…")
        asyncio.create_task(run_index(ctx, client, status, targets))
        return

    raw_ch = args[0].strip()
    range_args = args[1:]
    if len(range_args) > 2 or any(not re.fullmatch(r"\d+", a) for a in range_args):
        await say(
            "Usage:\n"
            "<code>/index</code> (all configured FILE_CHANNELS, resume from checkpoints)\n"
            "<code>/index CHANNEL_ID_or_@username</code> (resume or manually index that channel)\n"
            "<code>/index CHANNEL START_MESSAGE_ID</code> (start at that message)\n"
            "<code>/index CHANNEL START_MESSAGE_ID END_MESSAGE_ID</code> (index a range)"
        )
        return

    is_num = bool(re.fullmatch(r"-?\d+", raw_ch))
    if not is_num and not raw_ch.startswith("@"):
        await say(
            "Channel must be a numeric ID (e.g. <code>-1001234567890</code>) or <code>@username</code>."
        )
        return

    ref = int(raw_ch) if is_num else raw_ch
    manual_external = False
    ch_title = str(ref)

    if isinstance(ref, int) and ref in ctx.cfg.file_channels:
        ch = ref
    else:
        # Manual one-off indexing of a channel outside FILE_CHANNELS.
        # Verify the bot can see the channel, is an administrator there, and can
        # read historical messages. Never add it to FILE_CHANNELS or auto-monitoring.
        try:
            chat = await client.get_chat(ref)
        except RPCError as e:
            await say(
                f"I cannot access <code>{escape(str(ref))}</code> ({escape(str(e))}). "
                "Add me to that channel as an administrator first."
            )
            return
        ch = int(chat.id)
        ch_title = getattr(chat, "title", None) or str(ch)
        if ch not in ctx.cfg.file_channels:
            manual_external = True
            try:
                me = await client.get_chat_member(ch, "me")
            except RPCError as e:
                await say(
                    f"I cannot verify my permissions in <b>{escape(ch_title)}</b> (<code>{ch}</code>): "
                    f"{escape(str(e))}. Make sure I am an administrator there."
                )
                return
            if getattr(me, "status", None) not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                await say(
                    f"I am not an administrator in <b>{escape(ch_title)}</b> (<code>{ch}</code>). "
                    "Add me as an admin there before running manual /index."
                )
                return
            # Probe historical message access before starting the background job.
            probe_start = int(range_args[0]) if range_args else max(1, int(cps.get(f"cp_{ch}") or 1))
            try:
                await _fetch(client, ch, [probe_start, probe_start + 1])
            except RPCError as e:
                await say(
                    f"Cannot read historical messages from <b>{escape(ch_title)}</b> (<code>{ch}</code>): "
                    f"{escape(str(e))}.\nTelegram bots can only read channel history where they have admin access."
                )
                return

    first = int(range_args[0]) if len(range_args) > 0 else int(cps.get(f"cp_{ch}") or 1)
    last = int(range_args[1]) if len(range_args) > 1 else None
    if last is not None and last < first:
        await say("END_MESSAGE_ID must be greater than or equal to START_MESSAGE_ID.")
        return

    targets = [(ch, first, last)]
    note = (
        f"Starting manual one-off index of <b>{escape(ch_title)}</b> (<code>{ch}</code>)…\n"
        "This channel will <b>not</b> be added to FILE_CHANNELS or automatic background monitoring."
        if manual_external
        else "Starting…"
    )
    status = await say(note)
    asyncio.create_task(run_index(ctx, client, status, targets))


def _subs_list(subs: list[dict]) -> str:
    if not subs:
        return "Force-join is off."
    return "\n".join(f"{i}. <b>{escape(c['title'])}</b>  <code>{c['chat']}</code>\n    {escape(c['link'])}"
                     for i, c in enumerate(subs, 1))


async def _forcesub(ctx: Ctx, client, m, args, say) -> None:
    profile = "anime" if args and args[0].lower() == "anime" else "general"
    if profile == "anime": args = args[1:]
    elif args and args[0].lower() == "general": args = args[1:]
    lock = getattr(ctx, "force_sub_lock", None)
    if lock is None:
        return await _forcesub_profile(ctx, client, args, say, profile)
    async with lock:
        return await _forcesub_profile(ctx, client, args, say, profile)


async def _forcesub_profile(ctx, client, args, say, profile):
    subs = await ctx.subs("anime") if profile == "anime" else await ctx.subs()
    key = "anime_force_subs" if profile == "anime" else "force_subs"
    cmd = "/forcesub anime" if profile == "anime" else "/forcesub"
    verb = args[0].lower() if args else "list"
    if verb not in ("add", "remove", "replace", "list", "off"):
        verb, args = "add", ["add", *args]                     # old style: /forcesub CHANNEL
    if verb == "list":
        await say(f"<b>{profile.title()} profile</b>\n" + _subs_list(subs) + f"\n\n<code>{cmd} add CHANNEL [invite link]</code>\n"
                  f"<code>{cmd} remove CHANNEL</code> (id, @username or its number above)\n"
                  f"<code>{cmd} replace OLD NEW [invite link]</code>\n"
                  f"<code>{cmd} off</code>\nUp to 4 channels.")
    elif verb == "off":
        await ctx.set_setting(key, [])
        await say(f"{profile.title()} force-join is off.")
    elif verb in ("remove", "replace"):
        ref = args[1] if len(args) > 1 else ""
        hit = None
        for i, c in enumerate(subs, 1):
            tail = c["link"].rstrip("/").rsplit("/", 1)[-1].lower()
            if ref == str(i) or ref == str(c["chat"]) or ref.lower().lstrip("@") == tail:
                hit = c
                break
        if not hit:
            await say(f"I couldn't find that one. See <code>{cmd} list</code>.")
            return
        remaining = [c for c in subs if c is not hit]
        if verb == "replace":
            if len(args) < 3:
                await say(f"Usage: <code>{cmd} replace OLD NEW [invite link]</code>")
                return
            # _forcesub_add only persists after new-channel and invite validation.
            await _forcesub_add(ctx, client, args[2:], remaining, say, key=key, cmd=cmd)
        else:
            await ctx.set_setting(key, remaining)
            await say(f"Removed <b>{escape(hit['title'])}</b>.")
    else:
        await _forcesub_add(ctx, client, args[1:], subs, say, key=key, cmd=cmd)


async def _forcesub_add(ctx: Ctx, client, args, subs: list[dict], say, key="force_subs", cmd="/forcesub") -> None:
    if not args:
        await say(f"Usage: <code>{cmd} add CHANNEL_ID_or_@username [invite link]</code>")
        return
    ref = int(args[0]) if re.fullmatch(r"-?\d+", args[0]) else args[0]
    try:
        chat = await client.get_chat(ref)
        me = await client.get_chat_member(chat.id, "me")
    except RPCError:
        await say("I can't see that channel. Add me as an admin there first.")
        return
    title = chat.title or str(chat.id)
    if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await say(f"I'm not an admin in <b>{escape(title)}</b>. Make me an admin there first. "
                  "Telegram only lets me check who has joined if I am.")
        return
    already = any(c["chat"] == chat.id for c in subs)
    if len(subs) >= ctx.MAX_FORCE_SUB and not already:
        await say(f"You can have up to {ctx.MAX_FORCE_SUB} force-join channels, and you already have that many. "
                  "More than that makes joining feel like a chore for people, and every extra channel is one more "
                  "check on every film request, which the free Render plan handles badly. "
                  f"Remove one first: <code>{cmd} remove 1</code>")
        return
    link = args[1] if len(args) > 1 else (f"https://t.me/{chat.username}" if chat.username else chat.invite_link)
    if not link:
        try:
            link = await client.export_chat_invite_link(chat.id)
        except RPCError:
            await say("I couldn't make an invite link. Send it yourself: "
                      f"<code>{cmd} add CHANNEL_ID https://t.me/+…</code>")
            return
    entry = {"chat": chat.id, "link": link, "title": title}
    await ctx.set_setting(key, [entry if c["chat"] == chat.id else c for c in subs] if already else [*subs, entry])
    note = ""
    if not chat.username:
        note = ("\n\nPrivate channel: if its invite link asks you to approve new people, I approve them "
                "automatically while Auto-approve is on (/settings). That needs the right to invite users.")
    await say(f"Added <b>{escape(title)}</b>.{note}")


async def _alias(ctx: Ctx, text: str, say) -> None:
    body = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
    if "=" not in body:
        await say("Usage: <code>/alias parasite 2019 = Gisaengchung</code>")
        return
    left, alias = (x.strip() for x in body.split("=", 1))
    film = ctx.index.get(left) if re.fullmatch(r"[0-9a-f]{10}", left) else None
    if film is None:
        res = await asyncio.to_thread(ctx.index.search, left)
        if res.kind != "match":
            names = "\n".join(f"{h.film.label}  <code>{h.film.id}</code>" for h in res.hits[:5])
            await say("I need exactly one film. Repeat with its id:\n" + (names or "(no matches)"))
            return
        film = res.hits[0].film
    await ctx.store.add_alias(film.id, alias)
    await asyncio.to_thread(ctx.index.add_alias, film.id, alias)
    await say(f"“{escape(alias)}” now finds <b>{escape(film.label)}</b>.")


# ---------------------------------------------------------------- background job
async def requests_job(ctx: Ctx, client, every: int = 600) -> None:
    """Tell requesters when their film shows up (exact title matches only)."""
    while True:
        await asyncio.sleep(every)
        if not ctx.ready:
            continue
        try:
            for req in await ctx.store.open_requests(200):
                res = await asyncio.to_thread(ctx.index.search, req["text"])
                if res.kind == "match" and res.hits[0].match == "exact" and not res.corrected \
                        and not res.year_relaxed:
                    label = res.hits[0].film.label
                    for uid in req["users"]:
                        try:
                            await client.send_message(uid, f"<b>{escape(label)}</b> is now available. "
                                                           "Send me the name to get it.", parse_mode=HTML)
                        except FloodWait as e:
                            await asyncio.sleep(e.value + 1)
                        except Exception:
                            pass                              # blocked the bot, etc.
                    await ctx.store.close_request(req["key"])
        except Exception:
            log.exception("requests job failed")


# ---------------------------------------------------------------- counters and daily summary
async def maybe_send_summary(ctx: Ctx, client) -> None:
    s = await ctx.settings()
    at = s["summary_time"]
    if not at:
        return
    now = lagos_now()
    today = now.date().isoformat()
    if s["summary_last"] is None:                        # first time ever: start tomorrow, don't send an empty report
        await ctx.set_setting("summary_last", today)
        return
    if s["summary_last"] == today or now.strftime("%H:%M") < at:
        return
    await ctx.flush_stats()
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    text = ui.daily_summary(yesterday, await ctx.store.stats_get(yesterday), await ctx.store.open_requests(5))
    await notify_inbox(ctx, client, text)
    await ctx.set_setting("summary_last", today)         # remembered in the database, so a restart never repeats or skips it


async def stats_job(ctx: Ctx, client, tick: int = 60) -> None:
    """Counters live in memory and are written once a minute, as one small update per day."""
    while True:
        await asyncio.sleep(tick)
        try:
            await ctx.flush_stats()
            await maybe_send_summary(ctx, client)
            if getattr(ctx, "series_catalog", None) is not None and getattr(ctx, "ready", False):
                import series_handlers
                await series_handlers.maybe_send_series_digest(ctx, client)
        except Exception:
            log.exception("stats job failed")


# ---------------------------------------------------------------- registration
async def on_pre_checkout(ctx: Ctx, client, query) -> None:
    try:
        await query.answer(ok=True)
    except Exception as e:
        log.warning("failed to answer pre-checkout: %s", e)


async def on_successful_payment(ctx: Ctx, client, m) -> None:
    uid = m.from_user.id
    payment = getattr(m, "successful_payment", None)
    stars = int(getattr(payment, "total_amount", 50) if payment else 50)
    name = getattr(m.from_user, "first_name", "") or str(uid)

    if stars < 100:
        days = 30
        tier_benefits = ("• Supporter badge, priority and custom multi-title requests\n"
                         "• Larger watchlist capacity\n"
                         "• Extended file availability where configured\n")
    elif stars <= 250:
        days = 60
        tier_benefits = ("• Supporter badge, priority and custom multi-title requests\n"
                         "• Larger watchlist capacity\n"
                         "• Extended file availability where configured\n")
    else:
        days = 90
        tier_benefits = ("• Supporter badge, priority and custom multi-title requests\n"
                         "• Larger watchlist capacity\n"
                         "• Extended file availability where configured\n"
                         "• File messages without the standard footer\n"
                         "• Force-join bypass while your qualifying supporter period is active\n")

    new_date = await ctx.store.donor_add(uid, days=days, name=name)
    if stars > 250:
        await ctx.store.donor_set_join_bypass(uid, new_date)
        await ctx.store.donor_set_clean_files(uid, new_date)

    text = (f"⭐ <b>Thank you for supporting Exivum!</b>\n\n"
            f"Your contribution helps cover hosting and database costs and gives me more room to expand and maintain the series library.\n\n"
            f"Your supporter access is active until <b>{new_date}</b>. Your current benefits include:\n{tier_benefits}\n"
            "Thank you for helping keep the service running. Would you like your name "
            f"(<b>{escape(name)}</b>) featured on the Supporters Wall?")
    rows = _approval_rows()
    await m.reply_text(text, parse_mode=HTML, reply_markup=kb(rows))


# ---------------------------------------------------------------- registration
def _note_update_bound(ctx: Ctx):
    async def callback(client, obj):
        try: await _note_update(ctx, client, obj)
        except Exception: pass
    return callback


def _bind(ctx: Ctx, fn):
    async def callback(client, obj):
        try:
            await fn(ctx, client, obj)
        except Exception:
            log.exception("handler %s failed", fn.__name__)
    return callback


async def _note_update(ctx: Ctx, client, obj) -> None:
    """Diagnostics only: record that an update arrived (never blocks, never stops other handlers)."""
    ctx.updates_seen += 1
    ctx.last_update = time.time()
    who = getattr(getattr(obj, "from_user", None), "id", "?")
    kind = "button" if hasattr(obj, "data") else "message"
    log.info("update #%s: %s from user %s", ctx.updates_seen, kind, who)


def register(client, ctx: Ctx) -> None:
    admins = filters.user(list(ctx.cfg.admins))
    add = client.add_handler
    add(MessageHandler(_note_update_bound(ctx), filters.private | filters.group), group=-1)
    add(CallbackQueryHandler(_note_update_bound(ctx)), group=-1)
    add(MessageHandler(_bind(ctx, cmd_supporter), filters.command(["supporters", "request", "donationclaim"]) & filters.private))
    add(MessageHandler(_bind(ctx, cmd_start), filters.command("start") & filters.private))
    add(MessageHandler(_bind(ctx, on_admin_cmd), filters.command(ADMIN_CMDS) & filters.private & admins))
    add(MessageHandler(_bind(ctx, cmd_info), filters.command(["help", "about", "discover", "mylist", "donate", "survey"])))
    add(MessageHandler(_bind(ctx, on_successful_payment), filters.successful_payment & filters.private))
    add(PreCheckoutQueryHandler(_bind(ctx, on_pre_checkout)))
    add(MessageHandler(_bind(ctx, on_receipt_media), filters.private & (filters.photo | filters.document | filters.video | filters.animation)))
    add(MessageHandler(_bind(ctx, on_text), filters.text & ~filters.regex(r"^/") & ~filters.bot
                       & (filters.private | filters.group)))
    add(MessageHandler(_bind(ctx, on_channel_file),
                       filters.channel & (filters.document | filters.video | filters.audio)))
    add(CallbackQueryHandler(_bind(ctx, on_cb)))
    add(ChatJoinRequestHandler(_bind(ctx, on_join_request)))
    add(ChatMemberUpdatedHandler(_bind(ctx, on_chat_member)))
