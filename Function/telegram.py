"""Telegram sessions and retryable delivery; panel mutations are never retried here."""

import asyncio
import hashlib
import logging
import os
from io import IOBase

from pyrogram import Client
from pyrogram.errors import FloodWait, QueryIdInvalid

from Function.db import bot_token, db_path

log = logging.getLogger(__name__)


def make_client():
    token = bot_token()
    api_id = int(os.environ.get("TG_API_ID", "26410400"))
    api_hash = os.environ.get("TG_API_HASH", "408bf51732560cb81a0e32533b858cbf")
    # Pyrogram ignores bot_token when it finds an already-authorized session.
    # Bind the storage to these credentials so rotation cannot log in as an old bot.
    identity = hashlib.sha256(f"{api_id}:{api_hash}:{token}".encode()).hexdigest()[:20]
    return Client(
        f"pg-toolbox-{identity}",
        api_id=api_id,
        api_hash=api_hash,
        bot_token=token,
        workdir=str(db_path().resolve().parent),
        workers=1,
    )


async def telegram_call(method, *args, **kwargs):
    # Uploads may consume a stream before the final request hits a flood limit.
    streams = [
        (value, value.tell()) for value in (*args, *kwargs.values()) if isinstance(value, IOBase) and value.seekable()
    ]
    while True:
        try:
            for stream, position in streams:
                stream.seek(position)
            return await method(*args, **kwargs)
        except FloodWait as exc:
            log.warning("Telegram delivery paused for %s seconds", exc.value)
            await asyncio.sleep(exc.value)


async def answer_callback(query, *args, **kwargs):
    try:
        await telegram_call(query.answer, *args, **kwargs)
    except QueryIdInvalid:
        # Telegram acknowledgements expire while a previous operation is busy.
        # Persisted selection/confirmation state still validates the actual action.
        pass
