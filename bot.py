import asyncio
import logging
import os
import time
import threading
import webbrowser

import discord
from discord.ext import commands

import local_config
import shared_state
from database import init_db, set_guild_config
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


def _get_token() -> str:
    """Get the Discord token: local config (exe) → encrypted file (Railway)."""
    token = local_config.discord_token()
    if token:
        return token
    return load_token()


def _get_guild_id() -> int:
    return local_config.guild_id()


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
        guild_id = _get_guild_id()
        log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
        try:
            if guild_id:
                guild = discord.Object(id=guild_id)
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
            else:
                synced = await bot.tree.sync()
            log.info("Synced %d slash command(s).", len(synced))
        except Exception:
            log.exception("Slash command sync failed.")

    return bot


async def _seed_guild_config_from_local() -> None:
    """On first run, seed the guild_config row from local_config values."""
    cfg = local_config.load()
    guild_id = cfg.get("guild_id", 0)
    if not guild_id:
        return
    updates = {}
    for key in ("mod_log_channel_id", "mod_alerts_channel_id", "admin_role_id", "muted_role_id"):
        val = cfg.get(key, 0)
        if val:
            updates[key] = int(val)
    if updates:
        try:
            await set_guild_config(int(guild_id), **updates)
        except Exception:
            log.exception("Failed to seed guild config from local_config.")


async def _start_dashboard() -> None:
    """Start the FastAPI dashboard as an asyncio task."""
    import uvicorn
    from dashboard.app import app as dashboard_app

    port = local_config.dashboard_port()
    uvicorn_config = uvicorn.Config(
        dashboard_app, host="0.0.0.0", port=port, log_level="warning",
    )
    asyncio.create_task(uvicorn.Server(uvicorn_config).serve())
    log.info("Dashboard available at http://localhost:%d", port)


def _open_browser(port: int) -> None:
    """Open the dashboard in the default browser after a short delay."""
    def _open():
        time.sleep(1.2)
        webbrowser.open(f"http://localhost:{port}")
    threading.Thread(target=_open, daemon=True).start()


async def _run_bot(token: str) -> None:
    """Create, configure, and run the Discord bot."""
    await _seed_guild_config_from_local()
    bot = _make_bot()
    shared_state.bot_instance = bot
    try:
        async with bot:
            for ext in EXTENSIONS:
                await bot.load_extension(ext)
            from utils.views import ReviewView, UndoBanButton
            bot.add_view(ReviewView())
            bot.add_dynamic_items(UndoBanButton)
            await bot.start(token, reconnect=True)
    finally:
        shared_state.bot_instance = None


async def main():
    await init_db()

    # ── Dashboard ──────────────────────────────────────────────────────────
    if local_config.dashboard_secret():
        await _start_dashboard()
    elif not local_config.is_railway_mode():
        # No secret yet — start dashboard anyway so the setup wizard is reachable.
        await _start_dashboard()

    # ── Browser (exe / local mode only) ───────────────────────────────────
    if not local_config.is_railway_mode():
        _open_browser(local_config.dashboard_port())

    # ── Bot startup ────────────────────────────────────────────────────────
    if local_config.is_railway_mode():
        # Railway: env vars drive everything, start bot immediately.
        token = _get_token()
        await _run_bot(token)

    elif local_config.is_setup_complete():
        # Exe: config already saved from a previous run, start bot immediately.
        shared_state.setup_complete_event.set()
        token = _get_token()
        await _run_bot(token)

    else:
        # Exe: first run — wait for the setup wizard to complete.
        log.info("No config found — waiting for setup wizard at http://localhost:%d/setup",
                 local_config.dashboard_port())
        await shared_state.setup_complete_event.wait()
        log.info("Setup complete — starting bot.")
        token = _get_token()
        await _run_bot(token)


if __name__ == "__main__":
    # Railway mode: use plain Flask keep-alive when dashboard is not enabled.
    if local_config.is_railway_mode() and not local_config.dashboard_secret():
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
