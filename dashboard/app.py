import datetime
import os
import time
from pathlib import Path

import aiosqlite
import discord
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import shared_state
from config import DB_PATH
from database import (
    GUILD_CONFIG_DEFAULTS,
    get_guild_config,
    get_lockdown,
    get_slur_list,
    add_slur,
    remove_slur,
    remove_mute,
    reset_guild_config,
    set_guild_config,
)

SESSION_COOKIE = "dashboard_session"
SESSION_MAX_AGE = 3600  # 1 hour

DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET")
if not DASHBOARD_SECRET:
    raise RuntimeError(
        "dashboard.app imported but DASHBOARD_SECRET is not set. "
        "bot.py should check the env var before importing this module."
    )

serializer = URLSafeTimedSerializer(DASHBOARD_SECRET, salt="dashboard-session")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _format_ts(ts: int) -> str:
    return datetime.datetime.fromtimestamp(
        int(ts), tz=datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")


templates.env.filters["ts"] = _format_ts

# Config field groups for the editable form
CONFIG_GROUPS = [
    ("Spam Detection", [
        ("spam_message_count",      "Messages per window",        "Messages in the time window that trigger flood detection"),
        ("spam_time_window",        "Time window (seconds)",      "Window for flood detection"),
        ("duplicate_message_limit", "Duplicate message limit",    "Same message repeated N times triggers a mute"),
        ("mass_mention_limit",      "Mass mention limit",         "Unique pings before kick"),
        ("spam_mute_duration",      "Spam mute (seconds)",        "Duration of spam mutes"),
    ]),
    ("Slur Filter", [
        ("slur_mute_duration",      "Slur mute (seconds)",        "Mute duration for first slur offence"),
        ("fuzzy_threshold",         "Fuzzy threshold (%)",        "Similarity % for leetspeak matching (0–100)"),
    ]),
    ("Nuke Protection", [
        ("nuke_channel_delete_limit", "Channel delete limit",     "Channel deletes in window before neutralisation"),
        ("nuke_channel_create_limit", "Channel create limit",     "Channel creates in window before neutralisation"),
        ("nuke_role_create_limit",    "Role create limit",        "Role creates in window before neutralisation"),
        ("nuke_ban_limit",            "Ban limit",                "Bans in window before neutralisation"),
        ("nuke_time_window",          "Time window (seconds)",    "Detection window for nuke events"),
    ]),
    ("Join / Raid Protection", [
        ("join_raid_window",        "Raid window (seconds)",      "Time window for join surge detection"),
        ("join_raid_limit",         "Raid join limit",            "Joins in window to trigger raid mode"),
        ("min_account_age_days",    "Min account age (days)",     "Accounts younger than this are flagged on join"),
    ]),
    ("Channel & Role IDs", [
        ("mod_log_channel_id",      "Mod log channel ID",         "Channel for Medium-severity logs (0 = disabled)"),
        ("mod_alerts_channel_id",   "Alerts channel ID",          "Channel for Severe alerts and admin pings (0 = disabled)"),
        ("admin_role_id",           "Admin role ID",              "Role to ping on Severe alerts (0 = disabled)"),
        ("muted_role_id",           "Muted role ID",              "Fallback muted role (0 = Discord timeouts only)"),
    ]),
]

# Boolean toggles rendered as checkboxes (stored as 0/1 integers)
BOOL_FIELDS = {"block_invites", "block_phishing"}


def _valid_session(request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    try:
        serializer.loads(cookie, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _bot_online() -> bool:
    b = shared_state.bot_instance
    return b is not None and b.is_ready()


app = FastAPI(title="Mod Bot Dashboard", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path in ("/login", "/logout", "/favicon.ico", "/api/status"):
        return await call_next(request)
    if not _valid_session(request):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# ---------------- Auth routes ------------------------------------------

@app.get("/login")
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if password == DASHBOARD_SECRET:
        token = serializer.dumps("owner")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE, token,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        return response
    return templates.TemplateResponse(
        request, "login.html",
        {"error": "Wrong password"},
        status_code=401,
    )


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


# ---------------- API --------------------------------------------------

@app.get("/api/status")
async def api_status():
    b = shared_state.bot_instance
    online = b is not None and b.is_ready()
    guilds = len(b.guilds) if online else 0
    return JSONResponse({"online": online, "guilds": guilds})


# ---------------- Pages ------------------------------------------------

@app.get("/")
async def home(request: Request):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM infractions") as cur:
            total = (await cur.fetchone())[0]

        cutoff = int(time.time()) - 86400
        async with db.execute(
            "SELECT COUNT(*) FROM infractions WHERE timestamp >= ?",
            (cutoff,),
        ) as cur:
            recent = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT user_id, COUNT(*) AS c FROM infractions "
            "WHERE user_id != 0 "
            "GROUP BY user_id ORDER BY c DESC LIMIT 5"
        ) as cur:
            top_users = await cur.fetchall()

        async with db.execute(
            "SELECT offence_type, COUNT(*) FROM infractions "
            "GROUP BY offence_type ORDER BY COUNT(*) DESC"
        ) as cur:
            breakdown = await cur.fetchall()

    labels = [row[0] for row in breakdown]
    counts = [row[1] for row in breakdown]

    b = shared_state.bot_instance
    online = b is not None and b.is_ready()
    status = "Online" if online else "Offline"
    guild_count = len(b.guilds) if online else 0

    return templates.TemplateResponse(request, "index.html", {
        "total": total,
        "recent": recent,
        "top_users": top_users,
        "breakdown": breakdown,
        "labels": labels,
        "counts": counts,
        "status": status,
        "guild_count": guild_count,
    })


@app.get("/guilds")
async def guilds(request: Request):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT guild_id, COUNT(*) AS c, MAX(timestamp) "
            "FROM infractions GROUP BY guild_id ORDER BY c DESC"
        ) as cur:
            rows = await cur.fetchall()

    return templates.TemplateResponse(request, "guilds.html", {
        "guilds": rows,
    })


@app.get("/guilds/{guild_id}")
async def guild_detail(request: Request, guild_id: int):
    config = await get_guild_config(guild_id)
    lockdown = await get_lockdown(guild_id)
    slurs = await get_slur_list(guild_id)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, offence_type, severity, action_taken, timestamp "
            "FROM infractions WHERE guild_id = ? "
            "ORDER BY timestamp DESC LIMIT 50",
            (guild_id,),
        ) as cur:
            infractions = await cur.fetchall()

    flash = request.query_params.get("flash")

    return templates.TemplateResponse(request, "guild.html", {
        "guild_id": guild_id,
        "config": config,
        "config_groups": CONFIG_GROUPS,
        "bool_fields": BOOL_FIELDS,
        "lockdown": lockdown,
        "slurs": slurs,
        "infractions": infractions,
        "bot_online": _bot_online(),
        "flash": flash,
    })


@app.get("/users/{user_id}")
async def user_detail(request: Request, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT guild_id, offence_type, severity, action_taken, timestamp "
            "FROM infractions WHERE user_id = ? "
            "ORDER BY timestamp DESC",
            (user_id,),
        ) as cur:
            infractions = await cur.fetchall()

    return templates.TemplateResponse(request, "user.html", {
        "user_id": user_id,
        "infractions": infractions,
    })


# ---------------- Guild config -----------------------------------------

@app.post("/guilds/{guild_id}/config")
async def guild_save_config(guild_id: int, request: Request):
    form = await request.form()
    updates: dict[str, int] = {}

    for key in GUILD_CONFIG_DEFAULTS:
        if key in BOOL_FIELDS:
            updates[key] = 1 if form.get(key) else 0
        elif key in form:
            try:
                updates[key] = int(form[key])
            except (ValueError, TypeError):
                raise HTTPException(400, f"Invalid value for {key}")

    await set_guild_config(guild_id, **updates)
    return RedirectResponse(f"/guilds/{guild_id}?flash=config_saved", status_code=303)


@app.post("/guilds/{guild_id}/reset-config")
async def guild_reset_config(guild_id: int):
    await reset_guild_config(guild_id)
    return RedirectResponse(f"/guilds/{guild_id}?flash=config_reset", status_code=303)


# ---------------- Slur list --------------------------------------------

@app.post("/guilds/{guild_id}/slurs/add")
async def guild_add_slur(guild_id: int, slur: str = Form(...)):
    await add_slur(guild_id, slur.strip().lower())
    return RedirectResponse(f"/guilds/{guild_id}?flash=slur_added#slurs", status_code=303)


@app.post("/guilds/{guild_id}/slurs/remove")
async def guild_remove_slur(guild_id: int, slur: str = Form(...)):
    await remove_slur(guild_id, slur)
    return RedirectResponse(f"/guilds/{guild_id}?flash=slur_removed#slurs", status_code=303)


# ---------------- Mod actions ------------------------------------------

def _require_bot(guild_id: int):
    """Return (bot, guild) or raise HTTPException."""
    b = shared_state.bot_instance
    if b is None or not b.is_ready():
        raise HTTPException(503, "Bot is offline — cannot perform Discord actions")
    guild = b.get_guild(guild_id)
    if guild is None:
        raise HTTPException(404, f"Bot is not in guild {guild_id}")
    return b, guild


@app.post("/guilds/{guild_id}/unban")
async def guild_unban(guild_id: int, user_id: str = Form(...)):
    _, guild = _require_bot(guild_id)
    try:
        await guild.unban(discord.Object(id=int(user_id)), reason="Dashboard unban")
    except discord.NotFound:
        pass  # already unbanned — not an error
    except discord.HTTPException as e:
        raise HTTPException(502, f"Discord API error: {e}")
    return RedirectResponse(f"/guilds/{guild_id}?flash=unbanned", status_code=303)


@app.post("/guilds/{guild_id}/unmute")
async def guild_unmute(guild_id: int, user_id: str = Form(...)):
    _, guild = _require_bot(guild_id)
    uid = int(user_id)
    try:
        member = guild.get_member(uid) or await guild.fetch_member(uid)
        await member.timeout(None, reason="Dashboard unmute")
    except discord.NotFound:
        pass  # member left — still clean up DB
    except discord.HTTPException as e:
        raise HTTPException(502, f"Discord API error: {e}")
    await remove_mute(uid, guild_id)
    return RedirectResponse(f"/guilds/{guild_id}?flash=unmuted", status_code=303)


@app.post("/guilds/{guild_id}/lockdown")
async def guild_toggle_lockdown(guild_id: int, enable: str = Form(...)):
    _, guild = _require_bot(guild_id)
    on = enable == "1"
    from utils.actions import apply_lockdown
    await apply_lockdown(guild, on, reason="Dashboard lockdown toggle")
    flash = "lockdown_on" if on else "lockdown_off"
    return RedirectResponse(f"/guilds/{guild_id}?flash={flash}", status_code=303)
