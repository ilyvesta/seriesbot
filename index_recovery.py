"""Small durable ingestion journal. Only channel/message IDs, never media binaries.

A batch is journalled before catalogue writes. After a restart or error the existing
catch-up worker can replay the exact source messages, without anime announcements.
No journal entry is removed on failed/ambiguous retrieval or catalogue writes.
"""
from __future__ import annotations
import hashlib
import logging
import time
from multidb import FederatedDB, CatalogueBusy

log = logging.getLogger('seriesbot')
KIND = 'index_recovery_v161'
PREFIX = 'index-recovery:'


def collection(ctx):
    db = ctx.series_catalog._db
    # Pin metadata to the same physical database across active-routing changes.
    return db.secondary['series_maintenance'] if isinstance(db, FederatedDB) else db['series_maintenance']


def references(batch):
    return sorted({(int(p['chat_id']), int(p['message_id'])) for kind, p in batch if kind == 'raw'})


async def record(ctx, batch):
    refs = references(batch)
    if not refs:
        return None
    key = PREFIX + hashlib.sha256(repr(refs).encode()).hexdigest()
    await collection(ctx).update_one({'_id': key}, {'$setOnInsert': {
        'record_type': KIND, 'refs': [list(x) for x in refs], 'created': time.time()}}, upsert=True)
    return key


async def complete(ctx, key):
    if key:
        await collection(ctx).delete_one({'_id': key, 'record_type': KIND})


async def refresh(ctx):
    try:
        filt = {'record_type': KIND, '_id': {'$regex': '^index-recovery:'}}
        if ctx.index_batch_id:
            filt['_id']['$ne'] = ctx.index_batch_id
        ctx.index_recovery_pending = await collection(ctx).count_documents(filt)
        ctx.index_recovery_check_error = False
    except Exception:
        ctx.index_recovery_check_error = True
        log.exception('could not inspect indexing recovery journal')


async def rows(ctx, limit=10, *, include_review=True, due_only=False):
    filt = {'record_type': KIND, '_id': {'$regex': '^index-recovery:'}}
    if not include_review:
        filt['review'] = {'$ne': True}
    if due_only:
        filt['$or'] = [{'retry_after': {'$exists': False}}, {'retry_after': {'$lte': time.time()}}]
    return [r async for r in collection(ctx).find(filt).sort('created', 1).limit(limit)]


async def acknowledge(ctx, key):
    if not isinstance(key,str) or not key.startswith(PREFIX):
        return False
    result = await collection(ctx).delete_one({'_id':key,'record_type':KIND,'review':True})
    await refresh(ctx)
    return bool(result.deleted_count)


async def recover(ctx, client, *, limit=3, include_review=False, force=False):
    """Bounded replay using existing parser/catalogue. Missing messages stay reviewable."""
    import handlers
    if ctx.index_recovery_lock.locked() or ctx.index_active or ctx.index_job or ctx.maintenance:
        return 0, 0
    async with ctx.index_recovery_lock:
        count = missing = 0
        try:
            for row in await rows(ctx, limit, include_review=include_review, due_only=not force):
                if ctx.index_active or ctx.index_job or ctx.maintenance:
                    break
                # Missing references are held for manual inspection; avoid polling deleted posts.
                if row.get('review') and not include_review:
                    continue
                try:
                    groups = {}
                    for ch, mid in row['refs']:
                        groups.setdefault(ch, []).append(mid)
                    batch = []
                    absent = []
                    for ch, ids in groups.items():
                        msgs = await handlers._fetch(client, ch, ids)
                        msgs = msgs if isinstance(msgs, list) else [msgs]
                        found = {}
                        for m in msgs:
                            if m and not getattr(m, 'empty', False) and getattr(getattr(m, 'chat', None), 'id', None) == ch:
                                p = handlers._payload_of(m)
                                if p and m.id in ids:
                                    found[m.id] = p
                        absent.extend([ch, mid] for mid in ids if mid not in found)
                        batch.extend(('raw', p) for p in found.values())
                    if batch:
                        await handlers._process_index_batch(ctx, client, batch, announce=False)
                    if absent:
                        await collection(ctx).update_one({'_id': row['_id'], 'record_type': KIND},
                            {'$set': {'refs': absent, 'review': True}})
                        missing += len(absent)
                    else:
                        await complete(ctx, row['_id'])
                    count += len(batch)
                except CatalogueBusy:
                    raise  # no point retrying other catalogue writes under the same lock
                except Exception:
                    # A missing/inaccessible channel must not starve other journal rows.
                    # The row remains durable; retry after five minutes or via /indexretry.
                    await collection(ctx).update_one({'_id': row['_id'], 'record_type': KIND},
                        {'$set': {'retry_after': time.time() + 300}})
                    ctx.index_error = 'One or more saved index batches could not be recovered'
                    log.exception('index recovery failed for %s; retained with backoff',row['_id'])
        finally:
            await refresh(ctx)
        return count, missing
