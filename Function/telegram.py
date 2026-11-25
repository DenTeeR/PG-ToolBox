"""Reuse Holderbot's Telegram app defaults; deployments may override them."""

import os

from pyrogram import Client

from Function.db import bot_token


def make_client():
    return Client(
        "holder",
        api_id=int(os.environ.get("TG_API_ID", "26410400")),
        api_hash=os.environ.get("TG_API_HASH", "408bf51732560cb81a0e32533b858cbf"),
        bot_token=bot_token(),
    )
