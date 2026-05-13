"""
Background cleanup of expired sessions.

Sessions and their audio files accumulate forever otherwise. This module runs a
periodic sweep that deletes anything older than SESSION_TTL_DAYS.
"""

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

import store

logger = logging.getLogger(__name__)

SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "7"))
SWEEP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600"))


def _parse_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


async def sweep_once() -> int:
    """Delete every session older than SESSION_TTL_DAYS. Returns count removed."""
    cutoff = datetime.now(UTC) - timedelta(days=SESSION_TTL_DAYS)
    deleted = 0
    for session_id in store.list_session_ids():
        session = store.get_session(session_id)
        if not session:
            continue
        created_at = _parse_created_at(session.get("created_at"))
        if created_at is None or created_at >= cutoff:
            continue
        try:
            if await store.delete_session(session_id):
                deleted += 1
        except Exception as e:
            logger.warning(f"Cleanup: failed to delete session {session_id}: {e}")
    return deleted


async def run_cleanup_loop():
    """Forever-loop that sweeps every SWEEP_INTERVAL_SECONDS."""
    logger.info(
        f"Session cleanup: TTL={SESSION_TTL_DAYS} day(s), "
        f"interval={SWEEP_INTERVAL_SECONDS}s"
    )
    while True:
        try:
            n = await sweep_once()
            if n:
                logger.info(
                    f"Cleanup: removed {n} session(s) older than {SESSION_TTL_DAYS} day(s)"
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Cleanup sweep failed: {e}", exc_info=True)
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
