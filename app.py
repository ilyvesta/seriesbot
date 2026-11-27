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
        # A normal test write does not prove the federated catalogue is writable.
        from multidb import FederatedDB
        import index_recovery
        if getattr(ctx,"series_catalog",None):
            await index_recovery.refresh(ctx)
            db = ctx.series_catalog._db
            if isinstance(db,FederatedDB):
                try:
                    row = await db.reservation_status()
                    if row and row.get("worker") != db.worker_id:
                        ctx.index_error = "Existing catalogue reservation; inspect /lockstatus"
                        log.warning("startup found catalogue reservation owner=%s; no automatic removal",row.get("owner"))
                except Exception:
                    ctx.index_error = "Could not inspect catalogue reservation"
                    log.exception("startup reservation inspection failed")
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
        db = getattr(getattr(ctx,"series_catalog",None),"_db",None)
        from multidb import FederatedDB
        reservation_error = db.reservation_error if isinstance(db,FederatedDB) else None
        index_bad = bool(ctx.index_error or ctx.index_recovery_pending or ctx.index_unrecorded or
                         ctx.index_recovery_check_error or ctx.index_catchup_errors or reservation_error)
        body = {"indexing_ok": not index_bad, "indexing_error": ctx.index_error or reservation_error,
                "index_recovery_batches": ctx.index_recovery_pending, "index_unrecorded_channels": len(ctx.index_unrecorded),
                "index_journal_check_ok": not ctx.index_recovery_check_error, "index_batch_active": ctx.index_active,
                "index_catchup_failed_channels": len(ctx.index_catchup_errors),
                "ready": ctx.ready, "telegram": client.is_connected, "mongo": cache["ok"],
                "mongo_write_ok": ctx.write_ok, "watchdog_ok": getattr(ctx, "watchdog_ok", True), "mongo_write_ms": ctx.write_ms,
                "updates_received": ctx.updates_seen, "index_queue": ctx.index_queue.qsize(), "last_update_seconds_ago": ago,
                "series_files": counts.get("files", 0), "series": counts.get("shows", 0), "error": ctx.load_error}
        # 200 while still loading so Render doesn't restart us; 503 only once something is truly down
        bad = ctx.load_error or getattr(ctx, "watchdog_ok", True) is False or (ctx.ready and (index_bad or ctx.write_ok is False or not (client.is_connected and cache["ok"])))
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
    if cfg.mongo_uri2:
        # Second database configured: both are used as ONE (reads combine, writes go to the active one, no duplicates).
        from pymongo import AsyncMongoClient
        from multidb import FederatedDB
        second = AsyncMongoClient(cfg.mongo_uri2, serverSelectionTimeoutMS=20000, connectTimeoutMS=15000,
                                  socketTimeoutMS=60000, waitQueueTimeoutMS=15000, maxPoolSize=30)[cfg.db_name2]
        store.db = FederatedDB(store.db, second, switch_bytes=cfg.switch_mb * 1024 * 1024)
        from urllib.parse import urlsplit
        host1, host2 = urlsplit(cfg.mongo_uri).hostname, urlsplit(cfg.mongo_uri2).hostname
        if host1 and host2 and cfg.mongo_uri.startswith("mongodb+srv://") and cfg.mongo_uri2.startswith("mongodb+srv://"):
            if host1.lower() == host2.lower():
                if cfg.db_name == cfg.db_name2:
                    raise RuntimeError("Both database connections select the same database; federation would count it twice")
                store.db.topology = "same SRV host (shared cluster capacity)"
            else:
                store.db.topology = "different SRV hosts (confirm cluster identity in Atlas)"
        await store.db.load_state()
        await store.db.maybe_refresh(force=True)
        log.info("second database enabled: active=%s", store.db.active)
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
    bot = await client.get_me()
    ctx.bot_username = bot.username
    ctx.bot_id = bot.id
    log.info("started as @%s", ctx.bot_username)
    try:
        from pyrogram.types import BotCommand
        await client.set_bot_commands([
            BotCommand("start", "Home"),
            BotCommand("discover", "Browse lists"),
            BotCommand("mylist", "Your watchlist"),
            BotCommand("donate", "Support the bot"),
            BotCommand("supporters", "Supporters Wall"),
            BotCommand("request", "Supporter custom requests"),
            BotCommand("donationclaim", "My donation claim"),
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
                    f"Files: {c.get('files', 0):,} · Shows: {c.get('shows', 0):,}\n"
                    f"Indexing: {ctx.index_error or 'OK'} · Recovery batches: {ctx.index_recovery_pending}")
            except Exception:
                log.warning("could not send the startup message to the admin", exc_info=True)
            await handlers.resume_index(ctx, client)
            await handlers.resume_audit(ctx, client)
    tasks = [asyncio.create_task(boot()), asyncio.create_task(handlers.requests_job(ctx, client)),
             asyncio.create_task(handlers.sweeper(ctx, client)), asyncio.create_task(handlers.stats_job(ctx, client)),
             asyncio.create_task(handlers.index_worker(ctx, client)),
             asyncio.create_task(handlers.watchdog(ctx, client)),
             asyncio.create_task(handlers.catchup_worker(ctx, client))]
    try:
        await idle()
    finally:
        # Give a running batch time to complete normally. A forced cancellation
        # inside a catalogue write deliberately preserves its reservation for review.
        try:
            await asyncio.wait_for(ctx.index_queue.join(), 10)
        except asyncio.TimeoutError:
            log.warning("shutdown: index queue not drained; preserving remaining references")
        try:
            await client.stop()
        except Exception:
            log.exception("Telegram shutdown failed")
        all_tasks = set(tasks) | set(handlers._BG_TASKS)
        for t in all_tasks:
            t.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        # No new Telegram updates can arrive after client.stop(). Journal any
        # queued but not yet started batches in bounded chunks.
        import index_recovery
        while not ctx.index_queue.empty():
            batch = []
            for _ in range(100):
                try: batch.append(ctx.index_queue.get_nowait())
                except asyncio.QueueEmpty: break
            try:
                await index_recovery.record(ctx,batch)
            except Exception:
                log.exception("shutdown could not journal queued refs %s",index_recovery.references(batch))
            finally:
                for _ in batch: ctx.index_queue.task_done()
    try:
        await ctx.flush_stats()                       # don't lose the last minute of counters
    except Exception:
        pass
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
