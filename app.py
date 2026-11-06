"""Entry point: python app.py"""
import asyncio
import logging
import time

from aiohttp import web

from config import ConfigError, load
from ctx import Ctx
from index import FilmIndex
from store import MongoStore

log = logging.getLogger("seriesbot")


async def load_index(ctx: Ctx) -> None:
    """Mark the bot ready after Mongo responds; series lookup stays query-driven in Mongo.

    We intentionally do not read every series file into RAM. The catalogue is indexed in Atlas
    and show/season results are fetched in bounded queries to stay inside Render's 512 MB limit.
    """
    try:
        if not await ctx.store.ping():
            raise RuntimeError("MongoDB ping failed")
        counts = await ctx.series_catalog.counts() if getattr(ctx, "series_catalog", None) else {"files": 0, "shows": 0}
        ctx.banned = await ctx.store.banned_ids()
        ctx.start_counts = counts
        t0 = time.monotonic()
        try:                                  # a real write: reads can work while writes are blocked (e.g. a full Atlas quota)
            await ctx.store.db.healthcheck.update_one({"_id": "startup"}, {"$set": {"t": time.time()}}, upsert=True)
            ctx.write_ok = True
        except Exception as e:
            ctx.write_ok = False
            ctx.write_error = str(e)[:300]
            log.exception("MongoDB WRITE test failed - the bot cannot save anything")
        ctx.write_ms = int((time.monotonic() - t0) * 1000)
        ctx.ready = True
        log.info("series catalogue ready: %s files across %s shows", counts.get("files", 0), counts.get("shows", 0))
    except Exception as e:
        ctx.load_error = str(e)
        log.exception("series catalogue startup failed")


def web_app(ctx: Ctx, client) -> web.Application:
    cache = {"t": 0.0, "ok": True, "counts_t": 0.0, "counts": {"files": 0, "shows": 0}}

    async def health(_):
        now = asyncio.get_event_loop().time()
        if now - cache["t"] > 15:
            try: cache["ok"] = await ctx.store.ping()
            except Exception: cache["ok"] = False
            cache["t"] = now
        if ctx.ready and now - cache["counts_t"] > 30 and getattr(ctx, "series_catalog", None):
            try: cache["counts"] = await ctx.series_catalog.counts()
            except Exception: cache["ok"] = False
            cache["counts_t"] = now
        counts = cache["counts"]
        ago = round(time.time() - ctx.last_update) if ctx.last_update else None
        body = {"ready": ctx.ready, "telegram": client.is_connected, "mongo": cache["ok"],
                "mongo_write_ok": ctx.write_ok, "watchdog_ok": getattr(ctx, "watchdog_ok", True), "mongo_write_ms": ctx.write_ms,
                "updates_received": ctx.updates_seen, "index_queue": ctx.index_queue.qsize(), "last_update_seconds_ago": ago,
                "series_files": counts.get("files", 0), "series": counts.get("shows", 0), "error": ctx.load_error}
        # 200 while still loading so Render doesn't restart us; 503 only once something is truly down
        bad = ctx.load_error or getattr(ctx, "watchdog_ok", True) is False or (ctx.ready and not (client.is_connected and cache["ok"]))
        return web.json_response(body, status=503 if bad else 200)

    app = web.Application()
    app.add_routes([web.get("/", lambda _: web.Response(text="ok")), web.get("/health", health),
                    web.head("/", lambda _: web.Response())])
    return app


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load()
    except ConfigError as e:
        raise SystemExit(str(e))
    from pyrogram import Client, idle
    import handlers

    store = MongoStore(cfg.mongo_uri, cfg.db_name)
    await store.setup()
    from series_catalog import MongoSeriesCatalog
    series_catalog = MongoSeriesCatalog(store.db)
    await series_catalog.setup()
    ctx = Ctx(cfg, store, FilmIndex())
    ctx.series_catalog = series_catalog
    client = Client("seriesbot", api_id=cfg.api_id, api_hash=cfg.api_hash, bot_token=cfg.bot_token,
                    in_memory=True, workers=20)
    handlers.register(client, ctx)

    runner = web.AppRunner(web_app(ctx, client))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.port).start()      # open the port first: Render waits for it

    await client.start()
    ctx.bot_username = (await client.get_me()).username
    log.info("started as @%s", ctx.bot_username)
    try:
        from pyrogram.types import BotCommand
        await client.set_bot_commands([
            BotCommand("start", "Home"),
            BotCommand("discover", "Browse lists"),
            BotCommand("mylist", "Your watchlist"),
            BotCommand("donate", "Support the bot"),
            BotCommand("survey", "Community survey"),
            BotCommand("help", "How it works"),
            BotCommand("about", "About this bot"),
        ])
    except Exception:
        log.warning("couldn't set the command menu", exc_info=True)

    async def boot() -> None:
        await load_index(ctx)
        if ctx.ready:
            try:                              # tell the first admin right away whether the bot is healthy
                c = getattr(ctx, "start_counts", {}) or {}
                status = ("OK" if ctx.write_ok else f"FAILED - {getattr(ctx, 'write_error', '')}")
                await client.send_message(sorted(ctx.cfg.admins)[0],
                    f"Series bot started.\nDatabase read: OK\nDatabase write: {status} ({ctx.write_ms} ms)\n"
                    f"Files: {c.get('files', 0):,} · Shows: {c.get('shows', 0):,}")
            except Exception:
                log.warning("could not send the startup message to the admin", exc_info=True)
            await handlers.resume_index(ctx, client)
            await handlers.resume_audit(ctx, client)
    tasks = [asyncio.create_task(boot()), asyncio.create_task(handlers.requests_job(ctx, client)),
             asyncio.create_task(handlers.sweeper(ctx, client)), asyncio.create_task(handlers.stats_job(ctx, client)),
             asyncio.create_task(handlers.index_worker(ctx, client)),
             asyncio.create_task(handlers.watchdog(ctx, client))]
    await idle()
    for t in tasks:
        t.cancel()
    try:
        await ctx.flush_stats()                       # don't lose the last minute of counters
    except Exception:
        pass
    await client.stop()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
