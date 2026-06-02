"""Bounded notification-delivery trace.

Append-only Redis stream ``taey:notify_trace`` that records every step of the
delivery chain so a future delivery miss is fully traceable:

    enqueue  -> idle_set / stop_blocked -> inject (ok?) | drain -> idle_clear

Retention is bounded two ways so it never grows without limit:
  * count cap  : XADD MAXLEN ~50000 (approximate trim on every append)
  * time cap   : trim_trace() drops entries older than ~24h (call periodically)

HARD RULE: ``trace()`` and ``trim_trace()`` MUST NEVER raise into the caller.
Delivery correctness must not depend on logging succeeding. Every body is
wrapped in a bare try/except that swallows all errors.

Read it with ``scripts/taey-trace`` or directly:
    redis-cli XREVRANGE taey:notify_trace + - COUNT 50
"""
import time

TRACE_STREAM = "taey:notify_trace"
_MAXLEN = 50000                       # approximate count bound
_RETAIN_MS = 24 * 60 * 60 * 1000      # ~24h time bound


def trace(redis_client, event, node="", **fields):
    """Append one trace event. Never raises."""
    try:
        if redis_client is None:
            return
        rec = {"ev": str(event), "node": str(node or ""), "wall": f"{time.time():.3f}"}
        for key, value in fields.items():
            rec[key] = "" if value is None else str(value)
        redis_client.xadd(TRACE_STREAM, rec, maxlen=_MAXLEN, approximate=True)
    except Exception:
        pass


def trim_trace(redis_client):
    """Drop entries older than ~24h. Cheap; safe to call every poll. Never raises."""
    try:
        if redis_client is None:
            return
        minid = int(time.time() * 1000) - _RETAIN_MS
        redis_client.xtrim(TRACE_STREAM, minid=minid, approximate=True)
    except Exception:
        pass
