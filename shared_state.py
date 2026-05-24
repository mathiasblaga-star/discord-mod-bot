"""Mutable state shared between bot.py and dashboard/app.py.

Both modules run in the same process (the dashboard is started as an asyncio
task inside bot.py's event loop). A simple module-level variable is safe
because there is only one thread doing async I/O.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discord.ext.commands import Bot

# Set to the live Bot instance once it is created; cleared on crash/restart.
bot_instance: "Bot | None" = None
