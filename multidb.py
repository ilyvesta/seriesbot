"""Two MongoDB databases behaving as ONE.

When the first database nears the Atlas M0 limit, set DATABASE_URI_2 (and optionally DATABASE_NAME_2). The bot then
treats the second database as an extension of the first:

* READS look in both and return one combined answer (find / find_one / count / distinct / $group aggregates).
* WRITES to an existing document go to whichever database already holds it. A document that exists in neither goes to
  the ACTIVE database (the first one until it is nearly full, then the second). Nothing is ever written to both, so
  new duplicate file identities are prevented; historical duplicates require explicit review.
* The active database is chosen from the cluster's logical size (DATABASE_SWITCH_MB, default 450) and can be forced
  with /dbswitch. A write rejected for quota ("over your space quota") flips to the other database and retries once.

With DATABASE_URI_2 unset none of this is used: the bot talks to the first database exactly as before.

Only the operations the bot uses are implemented; anything else raises NotImplementedError instead of quietly
answering from half the data.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import uuid
import functools
import logging
import time
import os
import socket

log = logging.getLogger("seriesbot")

_NO_AUTO = {"admin", "local", "config"}


def is_quota_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "space quota" in msg or "over your space" in msg or getattr(exc, "code", None) == 8000


def _rank(v):
    if v is None: return (0, 0)
    if isinstance(v, bool): return (3, v)
    if isinstance(v, (int, float)): return (1, v)
    if isinstance(v, str): return (2, v)
    return (4, str(v))


def _cmp(a: dict, b: dict, spec) -> int:
    for key, direction in spec:
        x, y = _rank(a.get(key)), _rank(b.get(key))
        if x != y:
            c = -1 if x < y else 1
            return c if direction >= 0 else -c
    return 0


class _Agg:
    """Merged result of a write that touched both databases."""
    def __init__(self, **kw):
        self.matched_count = self.modified_count = self.upserted_count = self.deleted_count = self.inserted_count = 0
        self.upserted_id = None
        self.bulk_api_result = {}
        self.__dict__.update(kw)

    def add(self, r):
        for k in ("matched_count", "modified_count", "upserted_count", "deleted_count", "inserted_count"):
            setattr(self, k, getattr(self, k) + int(getattr(r, k, 0) or 0))
        if getattr(r, "upserted_id", None) is not None:
            self.upserted_id = r.upserted_id
        return self


class FederatedCursor:
    def __init__(self, col: "FederatedCollection", filt, projection, kw):
        self._c, self._filt, self._proj, self._kw = col, filt, projection, kw
        self._sort = None; self._skip = 0; self._limit = None

    def sort(self, key, direction=None):
        self._sort = [(key, direction or 1)] if isinstance(key, str) else list(key)
        return self

    def skip(self, n): self._skip = int(n); return self
    def limit(self, n): self._limit = int(n) if n else None; return self
    def batch_size(self, n): return self

    async def _stream(self):
        proj, extra = self._proj, set()
        if self._sort and proj and any(v for k, v in proj.items() if k != "_id"):
            proj = dict(proj)                       # the merge needs the sort keys even when they are not projected
            for k, _d in self._sort:
                if k not in proj and not (k == "_id" and proj.get("_id", 1)):
                    proj[k] = 1; extra.add(k)
        need = None if self._limit is None else self._skip + self._limit
        cursors = []
        for col in self._c.parts():
            cur = col.find(self._filt, proj, **self._kw) if proj is not None else col.find(self._filt, **self._kw)
            if self._sort: cur = cur.sort(self._sort)
            if need: cur = cur.limit(need)
            cursors.append(cur)
        its = [c.__aiter__() for c in cursors]

        async def nxt(it):
            try: return await it.__anext__()
            except StopAsyncIteration: return None

        def strip(d):
            for k in extra: d.pop(k, None)
            return d

        if not self._sort:
            for it in its:
                while True:
                    d = await nxt(it)
                    if d is None: break
                    yield strip(d)
            return
        heads = [await nxt(it) for it in its]
        while any(h is not None for h in heads):
            pick = None
            for i, h in enumerate(heads):
                if h is None: continue
                if pick is None or _cmp(h, heads[pick], self._sort) < 0:
                    pick = i
            d = heads[pick]; heads[pick] = await nxt(its[pick])
            yield strip(d)

    async def _limited(self):
        skipped = taken = 0
        async for d in self._stream():
            if skipped < self._skip:
                skipped += 1; continue
            if self._limit is not None and taken >= self._limit: return
            taken += 1
            yield d

    def __aiter__(self): return self._limited().__aiter__()

    async def to_list(self, length=None):
        out = []
        async for d in self._limited():
            out.append(d)
            if length is not None and len(out) >= length: break
        return out


class FederatedCollection:
    def __init__(self, fdb: "FederatedDB", name: str):
        self._fdb, self.name = fdb, name
        self._write_lock = asyncio.Lock()

    @property
    def database(self): return self._fdb

    def parts(self): return [self._fdb.primary[self.name], self._fdb.secondary[self.name]]

    @property
    def _p(self): return self._fdb.primary[self.name]
    @property
    def _s(self): return self._fdb.secondary[self.name]
    @property
    def _active(self): return self._fdb.active_db[self.name]

    # ---------------------------------------------------------------- reads
    def find(self, filt=None, projection=None, **kw):
        return FederatedCursor(self, filt or {}, projection, kw)

    async def find_one(self, filt=None, projection=None, **kw):
        sort = kw.pop("sort", None)
        if sort:
            rows = await self.find(filt, projection, **kw).sort(sort).limit(1).to_list(1)
            return rows[0] if rows else None
        for col in self.parts():
            d = await (col.find_one(filt or {}, projection, **kw) if projection is not None else col.find_one(filt or {}, **kw))
            if d is not None: return d
        return None

    async def count_documents(self, filt=None, **kw):
        return sum([await c.count_documents(filt or {}, **kw) for c in self.parts()])

    async def estimated_document_count(self, **kw):
        return sum([await c.estimated_document_count(**kw) for c in self.parts()])

    async def distinct(self, key, filt=None, **kw):
        seen = set()
        for c in self.parts():
            seen.update(await c.distinct(key, filt or {}, **kw))
        return sorted(seen, key=_rank)

    async def aggregate(self, pipeline, **kw):
        """Supports [$match]* + $group + [$sort|$limit|$skip]*; every other shape raises instead of guessing."""
        gi = next((i for i, st in enumerate(pipeline) if "$group" in st), None)
        if gi is None or any(list(st)[0] not in ("$match",) for st in pipeline[:gi]) \
                or any(list(st)[0] not in ("$sort", "$limit", "$skip") for st in pipeline[gi + 1:]):
            raise NotImplementedError("federated aggregate supports $match + $group + $sort/$limit/$skip only")
        spec = pipeline[gi]["$group"]
        merged: dict = {}
        for col in self.parts():
            cur = await col.aggregate(pipeline[:gi + 1], **kw)
            async for g in cur:
                key = repr(g["_id"])
                if key not in merged:
                    merged[key] = dict(g); continue
                m = merged[key]
                for field, expr in spec.items():
                    if field == "_id": continue
                    op = next(iter(expr))
                    a, b = m.get(field), g.get(field)
                    if op == "$sum": m[field] = (a or 0) + (b or 0)
                    elif op == "$min": m[field] = b if a is None else (a if b is None else min(a, b))
                    elif op == "$max": m[field] = b if a is None else (a if b is None else max(a, b))
                    elif op == "$first": m[field] = a if a is not None else b
                    else: raise NotImplementedError(f"federated $group accumulator {op}")
        rows = list(merged.values())
        for st in pipeline[gi + 1:]:
            (op, arg), = st.items()
            if op == "$sort":
                rows.sort(key=functools.cmp_to_key(lambda x, y, s=list(arg.items()): _cmp(x, y, s)))
            elif op == "$limit": rows = rows[:int(arg)]
            elif op == "$skip": rows = rows[int(arg):]

        class _Cur:
            def __init__(s, r): s.r = r
            def __aiter__(s):
                async def gen():
                    for x in s.r: yield x
                return gen().__aiter__()
            async def to_list(s, length=None): return list(s.r)
        return _Cur(rows)

    # ---------------------------------------------------------------- writes
    async def _active_write(self, fn):
        """Run fn(collection) on the active database; on a quota rejection flip databases and retry once."""
        await self._fdb.maybe_refresh()
        try:
            return await fn(self._active)
        except Exception as exc:
            if is_quota_error(exc) and await self._fdb.flip_after_quota_error():
                return await fn(self._active)
            raise

    async def _exists(self, filt):
        for c in self.parts():
            if await c.find_one(filt, {"_id": 1}) is not None: return True
        return False

    @asynccontextmanager
    async def _identity_write(self):
        # Keep direct catalogue/skip upserts in the same cross-worker domain as
        # the higher-level check-and-insert operation. Acquire in this order to
        # avoid a local collection lock blocking the reservation owner.
        if self.name in ("series_files", "series_shows", "series_show_aliases", "series_skips"):
            async with self._fdb.catalogue_write():
                async with self._write_lock:
                    yield
        else:
            async with self._write_lock:
                yield

    async def insert_one(self, doc):
        async with self._identity_write():
            return await self._insert_one(doc)

    async def _insert_one(self, doc):
        if "_id" in doc and await self._exists({"_id": doc["_id"]}):
            from pymongo.errors import DuplicateKeyError
            raise DuplicateKeyError(f"E11000 duplicate key: _id {doc['_id']!r} already exists in the combined database")
        return await self._active_write(lambda c: c.insert_one(doc))

    async def update_one(self, filt, update, upsert=False, **kw):
        async with self._identity_write():
            return await self._update_one(filt, update, upsert, **kw)

    async def _update_one(self, filt, update, upsert=False, **kw):
        for col in self.parts():
            r = await col.update_one(filt, update, **kw)
            if getattr(r, "matched_count", 0): return r
        if not upsert: return r
        return await self._active_write(lambda c: c.update_one(filt, update, upsert=True, **kw))

    async def update_many(self, filt, update, upsert=False, **kw):
        async with self._identity_write():
            return await self._update_many(filt, update, upsert, **kw)

    async def _update_many(self, filt, update, upsert=False, **kw):
        agg = _Agg()
        for col in self.parts():
            agg.add(await col.update_many(filt, update, **kw))
        if upsert and not agg.matched_count:
            agg.add(await self._active_write(lambda c: c.update_many(filt, update, upsert=True, **kw)))
        return agg

    async def delete_one(self, filt, **kw):
        agg = _Agg()
        for col in self.parts():
            r = await col.delete_one(filt, **kw)
            agg.add(r)
            if getattr(r, "deleted_count", 0): break
        return agg

    async def delete_many(self, filt, **kw):
        agg = _Agg()
        for col in self.parts(): agg.add(await col.delete_many(filt, **kw))
        return agg

    async def find_one_and_delete(self, filt, **kw):
        for col in self.parts():
            d = await col.find_one_and_delete(filt, **kw)
            if d is not None: return d
        return None

    async def bulk_write(self, ops, ordered=True, **kw):
        async with self._identity_write():
            return await self._bulk_write(ops, ordered, **kw)

    async def _bulk_write(self, ops, ordered=True, **kw):
        """Route each operation to the database that already holds its document; new documents go to the active one.

        Operations on a single `_id` are located with ONE lookup per database per chunk. Order across the two
        databases is not preserved (the bot always uses ordered=False).
        """
        ops = list(ops)
        by_id, other = [], []
        for op in ops:
            f = _op_filter(op)
            if type(op).__name__ in ("UpdateOne", "ReplaceOne", "DeleteOne") and isinstance(f, dict) and set(f) == {"_id"} and not isinstance(f["_id"], dict):
                by_id.append(op)
            else:
                other.append(op)
        total = _Agg()
        if by_id:
            ids = [_op_filter(o)["_id"] for o in by_id]
            in_p = await _ids_in(self._p, ids)
            in_s = await _ids_in(self._s, [i for i in ids if i not in in_p])
            to_p = [o for o in by_id if _op_filter(o)["_id"] in in_p]
            to_s = [o for o in by_id if _op_filter(o)["_id"] in in_s]
            new = [o for o in by_id if _op_filter(o)["_id"] not in in_p and _op_filter(o)["_id"] not in in_s]
            new = [o for o in new if _op_upsert(o)]                # an update of a document that exists nowhere does nothing
            for col, batch in ((self._p, to_p), (self._s, to_s)):
                if batch: total.add(await col.bulk_write(batch, ordered=False))
            if new:
                await self._fdb.maybe_refresh()
                target = self._active
                try:
                    total.add(await target.bulk_write(new, ordered=False))
                except Exception as exc:
                    if not is_quota_error(exc) or not await self._fdb.flip_after_quota_error():
                        raise
                    # An unordered bulk can partially succeed before a quota error.
                    # Re-check BOTH databases before retrying only missing upserts.
                    new_ids = [_op_filter(o)["_id"] for o in new]
                    present = await _ids_in(self._p, new_ids) | await _ids_in(self._s, new_ids)
                    remaining = [o for o in new if _op_filter(o)["_id"] not in present]
                    if remaining:
                        total.add(await self._active.bulk_write(remaining, ordered=False))
        for op in other:
            kind, f = type(op).__name__, _op_filter(op)
            if kind == "UpdateOne": total.add(await self._update_one(f, op._doc if hasattr(op, "_doc") else op.update, upsert=_op_upsert(op)))
            elif kind == "UpdateMany": total.add(await self._update_many(f, op._doc if hasattr(op, "_doc") else op.update, upsert=_op_upsert(op)))
            elif kind == "DeleteOne": total.add(await self.delete_one(f))
            elif kind == "DeleteMany": total.add(await self.delete_many(f))
            else: raise NotImplementedError(f"federated bulk_write: {kind} with filter {f!r}")
        return total

    # ---------------------------------------------------------------- maintenance
    async def create_index(self, *a, **k):
        name = None
        for col in (self._s, self._p):                            # the second database must work even if the first is full
            try: name = await col.create_index(*a, **k)
            except Exception:
                if col is self._s: raise
                log.info("index creation on the primary database failed for %s", self.name, exc_info=True)
        return name

    async def drop_index(self, *a, **k):
        for col in self.parts():
            try: await col.drop_index(*a, **k)
            except Exception: pass

    async def drop(self):
        for col in self.parts(): await col.drop()

    def __getattr__(self, item):
        raise AttributeError(f"{item!r} is not supported on the combined database (collection {self.name})")


def _op_filter(op):
    return getattr(op, "_filter", None) if getattr(op, "_filter", None) is not None else getattr(op, "filter", None)


def _op_upsert(op):
    return bool(getattr(op, "_upsert", None) if hasattr(op, "_upsert") else getattr(op, "upsert", False))


async def _ids_in(col, ids):
    found = set()
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        async for d in col.find({"_id": {"$in": chunk}}, {"_id": 1}):
            found.add(d["_id"])
    return found


class CatalogueBusy(RuntimeError):
    """A different (or abandoned) reservation prevented catalogue mutation."""


class FederatedDB:
    RESERVATION_WAIT = 30.0
    RESERVATION_HEARTBEAT = 30.0
    RECOVERY_QUIET = 120.0
    def __init__(self, primary, secondary, switch_bytes: int = 450 * 1024 * 1024, hysteresis_bytes: int = 25 * 1024 * 1024,
                 check_every: float = 300.0):
        self.primary, self.secondary = primary, secondary
        self.switch_bytes, self.hysteresis = int(switch_bytes), int(hysteresis_bytes)
        self.check_every = check_every
        self.active = "primary"; self.manual = False
        self._last = 0.0; self.last_size = None; self._cols = {}
        self._catalog_lock = asyncio.Lock()
        self._catalog_owner = None
        self.worker_id = uuid.uuid4().hex
        self.worker_host = socket.gethostname()[:120]
        self._reservation_owner = None
        self._release_pending = None
        self.reservation_error = None
        self.topology = "unknown"

    @property
    def reservation_col(self):
        return self.secondary["_federation"]

    async def reservation_status(self):
        return await self.reservation_col.find_one({"_id": "catalogue_write"})

    async def recover_reservation(self, owner: str, *, stopped: bool = False):
        """Operator-confirmed recovery only. No automatic expiry or lock stealing.

        A recent heartbeat vetoes recovery; an old heartbeat is NOT proof of death.
        The caller must have independently stopped every other catalogue writer.
        Compare the complete observed ownership/heartbeat before deleting.
        """
        if not stopped:
            raise ValueError("Stop every other catalogue writer first and explicitly confirm stopped.")
        row = await self.reservation_status()
        if row is None:
            return False
        if row.get("owner") != owner:
            raise ValueError("Reservation owner changed. Run /lockstatus again.")
        if row.get("worker") == self.worker_id or owner == self._reservation_owner:
            raise ValueError("This process owns the reservation; let its operation finish or stop this service.")
        last = row.get("heartbeat", row.get("at"))
        if not isinstance(last, (int, float)) or time.time() - last < self.RECOVERY_QUIET:
            raise ValueError("Reservation is recent or its timestamp is invalid. Do not recover a potentially live writer.")
        filt = {"_id": "catalogue_write", "owner": owner, "at": row.get("at")}
        for key in ("worker", "heartbeat"):
            filt[key] = row[key] if key in row else {"$exists": False}
        result = await self.reservation_col.delete_one(filt)
        if not result.deleted_count:
            raise ValueError("Reservation changed while checking. Nothing deleted; run /lockstatus again.")
        self.reservation_error = None
        log.warning("operator recovered catalogue reservation owner=%s", owner)
        return True

    async def _release_reservation(self, owner):
        # A completed operation may safely retry deletion of its OWN reservation.
        # Never release another process's reservation or an active local writer.
        self._release_pending = owner
        try:
            await self.reservation_col.delete_one({"_id": "catalogue_write", "owner": owner})
        except Exception:
            self.reservation_error = "Catalogue reservation release failed; retry pending"
            log.exception("could not release catalogue reservation owner=%s", owner)
            return False
        self._release_pending = None
        self.reservation_error = None
        return True

    async def retry_own_release(self):
        """Retry cleanup only after the owning operation has completed."""
        if self._catalog_lock.locked():
            return False
        async with self._catalog_lock:
            if self._release_pending:
                return await self._release_reservation(self._release_pending)
            return True

    async def _reservation_pulse(self, owner):
        while True:
            await asyncio.sleep(self.RESERVATION_HEARTBEAT)
            try:
                result = await self.reservation_col.update_one(
                    {"_id": "catalogue_write", "owner": owner}, {"$set": {"heartbeat": time.time()}})
                if not result.matched_count:
                    log.critical("catalogue reservation disappeared during active write owner=%s", owner)
                    self.reservation_error = "Active catalogue reservation disappeared"
                    return
            except Exception:
                log.warning("catalogue reservation heartbeat failed owner=%s", owner, exc_info=True)

    @asynccontextmanager
    async def catalogue_write(self):
        """Serialize full identity-check/write batches. Never expire another owner's lock."""
        from pymongo.errors import DuplicateKeyError
        if self._catalog_owner is asyncio.current_task():
            yield
            return
        async with self._catalog_lock:
            if self._release_pending and not await self._release_reservation(self._release_pending):
                raise CatalogueBusy("Catalogue reservation cleanup pending")
            col = self.reservation_col
            owner = uuid.uuid4().hex
            self._reservation_owner = owner
            until = time.monotonic() + self.RESERVATION_WAIT
            acquired = False
            interrupted = False
            pulse = None
            try:
                while True:
                    stamp = time.time()
                    # Shield the insert: on shutdown wait for the in-flight operation
                    # before attempting owner-conditional cleanup.
                    insert = asyncio.create_task(col.insert_one({"_id": "catalogue_write", "owner": owner,
                        "at": stamp, "heartbeat": stamp, "worker": self.worker_id,
                        "host": self.worker_host, "pid": os.getpid()}))
                    try:
                        await asyncio.shield(insert)
                        acquired = True
                        break
                    except asyncio.CancelledError:
                        try:
                            await insert
                        except Exception:
                            pass
                        await self._release_reservation(owner)
                        raise
                    except DuplicateKeyError:
                        # A retried insert can report duplicate after the first attempt
                        # committed. Only our unique owner token proves acquisition.
                        existing = await self.reservation_status()
                        if existing and existing.get("owner") == owner:
                            acquired = True
                            break
                        if time.monotonic() >= until:
                            self.reservation_error = "Catalogue reservation busy; use /lockstatus"
                            raise CatalogueBusy(self.reservation_error)
                        await asyncio.sleep(min(1.0, max(0.0, until - time.monotonic())))
                    except Exception:
                        # A network error can mean the insert was accepted. Clean up
                        # only our unique owner; uncertainty remains fail-closed.
                        await self._release_reservation(owner)
                        raise
                self.reservation_error = None
                self._catalog_owner = asyncio.current_task()
                pulse = asyncio.create_task(self._reservation_pulse(owner))
                try:
                    yield
                except asyncio.CancelledError:
                    interrupted = True
                    self.reservation_error = "Catalogue write interrupted; reservation requires stopped-worker review"
                    log.warning("interrupted catalogue mutation: preserving reservation owner=%s", owner)
                    raise
            finally:
                self._catalog_owner = None
                if pulse is not None:
                    pulse.cancel()
                    await asyncio.gather(pulse, return_exceptions=True)
                if acquired and not interrupted:
                    cleanup = asyncio.create_task(self._release_reservation(owner))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise
                self._reservation_owner = None

    # -- collections
    def __getattr__(self, name):
        if name.startswith("_"): raise AttributeError(name)
        return self[name]

    def __getitem__(self, name):
        return self._cols[name] if name in self._cols else self._cols.setdefault(name, FederatedCollection(self, name))

    @property
    def active_db(self): return self.primary if self.active == "primary" else self.secondary

    async def command(self, *a, **k):
        r = await self.primary.command(*a, **k)
        if a and a[0] == "ping":
            await self.secondary.command(*a, **k)
        return r

    async def list_collection_names(self):
        names = set(await self.primary.list_collection_names()) | set(await self.secondary.list_collection_names())
        return sorted(n for n in names if not n.startswith("_federation"))

    # -- state (kept in the SECOND database, which has room)
    async def load_state(self):
        try:
            d = await self.secondary["_federation"].find_one({"_id": "state"})
        except Exception:
            log.warning("could not read federation state", exc_info=True); return
        if d: self.active = d.get("active", "primary"); self.manual = bool(d.get("manual", False))

    async def _save_state(self):
        try:
            await self.secondary["_federation"].update_one({"_id": "state"}, {"$set": {"active": self.active, "manual": self.manual, "at": time.time()}}, upsert=True)
        except Exception:
            log.warning("could not save federation state", exc_info=True)

    async def set_active(self, which: str, manual: bool):
        self.active, self.manual = which, manual
        await self._save_state()

    async def measure(self) -> int:
        """Preserve v15 routing: primary connection's visible cluster total, falling back to application size."""
        try:
            client = self.primary.client
            total = 0
            for name in await client.list_database_names():
                if name in _NO_AUTO: continue
                st = await client[name].command("dbStats")
                total += int(st.get("dataSize", 0)) + int(st.get("indexSize", 0))
        except Exception:
            st = await self.primary.command("dbStats")
            total = int(st.get("dataSize", 0)) + int(st.get("indexSize", 0))
        if self.topology.startswith("same SRV host"):
            # Each credential may see only its own database. For known shared
            # hosts, both application sizes are a lower bound on cluster usage.
            app_total = 0
            for db in (self.primary, self.secondary):
                st = await db.command("dbStats")
                app_total += int(st.get("dataSize", 0)) + int(st.get("indexSize", 0))
            total = max(total, app_total)
        return total

    async def cluster_sizes(self):
        """Visible logical sizes per connection, not necessarily complete cluster metrics."""
        totals = []
        for db in (self.primary, self.secondary):
            try:
                client = db.client
                total = 0
                for name in await client.list_database_names():
                    if name in _NO_AUTO: continue
                    st = await client[name].command("dbStats")
                    total += int(st.get("dataSize", 0)) + int(st.get("indexSize", 0))
                totals.append(total)
            except Exception:
                totals.append(None)
        return totals

    async def maybe_refresh(self, force=False):
        if self.manual and not force: return
        now = time.monotonic()
        if not force and now - self._last < self.check_every: return
        self._last = now
        try:
            size = await self.measure()
        except Exception:
            log.warning("size check failed; keeping %s", self.active, exc_info=True); return
        self.last_size = size
        if self.active == "primary" and size >= self.switch_bytes:
            log.warning("primary database at %.0f MB: new documents now go to the secondary database", size / 1048576)
            await self.set_active("secondary", False)
        elif self.active == "secondary" and size < self.switch_bytes - self.hysteresis:
            await self.set_active("primary", False)

    async def flip_after_quota_error(self) -> bool:
        if self.active == "primary":
            log.warning("primary database rejected a write (quota): switching to the secondary")
            await self.set_active("secondary", False)
            return True
        return False

    def status(self) -> dict:
        return {"active": self.active, "manual": self.manual, "switch_mb": self.switch_bytes // 1048576,
                "last_cluster_mb": None if self.last_size is None else round(self.last_size / 1048576, 1), "topology": self.topology}
