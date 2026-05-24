import asyncio
import logging
import os
import time

import discord
from discord.ext import commands

from config import GUILD_ID
from database import init_db
from keep_alive import keep_alive
from token_store import load_token

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

EXTENSIONS = (
    "cogs.spam",
    "cogs.slurs",
    "cogs.nuke_protection",
    "cogs.join_protection",
    "cogs.link_filter",
    "cogs.admin",
    "cogs.settings",
)


def _make_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.guilds = True
    intents.bans = True
    intents.moderation = True

    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
            else:
                synced = await bot.tree.sync()
            log.info("Synced %d slash command(s).", len(synced))
        except Exception:
            log.exception("Slash command sync failed.")

    return bot


async def main():
    await init_db()

    if os.getenv("DASHBOARD_SECRET"):
        import uvicorn
        from dashboard.app import app as dashboard_app
        # Bind to 0.0.0.0 on Railway's $PORT so the dashboard is publicly
        # accessible. Fall back to DASHBOARD_PORT then 8080 for local dev.
        port = int(os.getenv("PORT", os.getenv("DASHBOARD_PORT", "8080")))
        uvicorn_config = uvicorn.Config(
            dashboard_app, host="0.0.0.0", port=port, log_level="warning",
        )
        asyncio.create_task(uvicorn.Server(uvicorn_config).serve())
        log.info("Dashboard available at http://0.0.0.0:%d", port)

    token = load_token()
    bot = _make_bot()
    async with bot:
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        from utils.views import ReviewView, UndoBanButton
        bot.add_view(ReviewView())
        bot.add_dynamic_items(UndoBanButton)
        await bot.start(token, reconnect=True)


if __name__ == "__main__":
    # When the dashboard is enabled it serves as the public web server, so
    # don't also start the plain Flask keep-alive (they'd clash on $PORT).
    if not os.getenv("DASHBOARD_SECRET"):
        keep_alive()
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            log.info("Interrupted — exiting.")
            break
        except Exception:
            log.exception("Bot crashed — restarting in 10s.")
            time.sleep(10)
