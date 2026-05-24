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

import local_config
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

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_ts(ts: int) -> str:
    return datetime.datetime.fromtimestamp(
        int(ts), tz=datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")


templates.env.filters["ts"] = _format_ts

# Lazy serializer — re-created when the secret changes (e.g. right after setup).
_serializer_cache: tuple[str, URLSafeTimedSerializer] | None = None


def _get_serializer() -> URLSafeTimedSerializer | None:
    global _serializer_cache
    secret = local_config.dashboard_secret()
    if not secret:
        return None
    if _serializer_cache is None or _serializer_cache[0] != secret:
        _serializer_cache = (secret, URLSafeTimedSerializer(secret, salt="dashboard-session"))
    return _serializer_cache[1]


def _valid_session(request: Request) -> bool:
    ser = _get_serializer()
    if ser is None:
        return False
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    try:
        ser.loads(cookie, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _bot_online() -> bool:
    b = shared_state.bot_instance
    return b is not None and b.is_ready()


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

BOOL_FIELDS = {"block_invites", "block_phishing"}

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Mod Bot Dashboard", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

_ALWAYS_PUBLIC = {"/login", "/logout", "/favicon.ico", "/setup", "/api/status", "/api/verify-token"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path in _ALWAYS_PUBLIC or path.startswith("/api/"):
        return await call_next(request)

    # If setup is not done, redirect everything to the setup wizard.
    if not local_config.is_setup_complete() and not local_config.is_railway_mode():
        return RedirectResponse("/setup", status_code=303)

    if not _valid_session(request):
        return RedirectResponse("/login", status_code=303)

    return await call_next(request)


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

@app.get("/setup")
async def setup_form(request: Request):
    # Already configured — go straight to login.
    if local_config.is_setup_complete() or local_config.is_railway_mode():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@app.post("/setup")
async def setup_submit(
    request: Request,
    discord_token: str    = Form(...),
    guild_id: str         = Form(...),
    mod_log_channel_id: str    = Form("0"),
    mod_alerts_channel_id: str = Form("0"),
    admin_role_id: str    = Form("0"),
    muted_role_id: str    = Form("0"),
    dashboard_password: str    = Form(...),
    confirm_password: str      = Form(...),
):
    errors = []

    if not discord_token.strip():
        errors.append("Discord Bot Token is required.")
    if not guild_id.strip() or not guild_id.strip().isdigit():
        errors.append("Guild ID must be a valid number.")
    if not dashboard_password:
        errors.append("Dashboard password is required.")
    if dashboard_password != confirm_password:
        errors.append("Passwords do not match.")

    if errors:
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": " ".join(errors)},
            status_code=422,
        )

    def _to_int(val: str) -> int:
        try:
            return max(0, int(val.strip()))
        except (ValueError, AttributeError):
            return 0

    local_config.save({
        "discord_token":        discord_token.strip(),
        "guild_id":             _to_int(guild_id),
        "mod_log_channel_id":   _to_int(mod_log_channel_id),
        "mod_alerts_channel_id": _to_int(mod_alerts_channel_id),
        "admin_role_id":        _to_int(admin_role_id),
        "muted_role_id":        _to_int(muted_role_id),
        "dashboard_secret":     dashboard_password,
        "setup_complete":       True,
    })

    # Signal bot.py's main() to start the Discord bot.
    shared_state.setup_complete_event.set()

    # Log the user in automatically.
    ser = _get_serializer()
    session_token = ser.dumps("owner") if ser else ""
    response = RedirectResponse("/", status_code=303)
    if session_token:
        response.set_cookie(
            SESSION_COOKIE, session_token,
            max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
        )
    return response


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.get("/login")
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    secret = local_config.dashboard_secret()
    if secret and password == secret:
        ser = _get_serializer()
        token = ser.dumps("owner")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE, token,
            max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
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


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status():
    b = shared_state.bot_instance
    online = b is not None and b.is_ready()
    guilds = len(b.guilds) if online else 0
    return JSONResponse({"online": online, "guilds": guilds})


@app.post("/api/verify-token")
async def api_verify_token(token: str = Form(...)):
    """Quick check that a bot token is valid against the Discord API."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {token.strip()}"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return JSONResponse({"ok": True, "username": data.get("username", "Bot"), "id": data.get("id")})
                return JSONResponse({"ok": False, "error": f"Discord returned HTTP {resp.status}"})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/")
async def home(request: Request):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM infractions") as cur:
            total = (await cur.fetchone())[0]

        cutoff = int(time.time()) - 86400
        async with db.execute(
            "SELECT COUNT(*) FROM infractions WHERE timestamp >= ?", (cutoff,),
        ) as cur:
            recent = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT user_id, COUNT(*) AS c FROM infractions "
            "WHERE user_id != 0 GROUP BY user_id ORDER BY c DESC LIMIT 5"
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

    return templates.TemplateResponse(request, "index.html", {
        "total": total,
        "recent": recent,
        "top_users": top_users,
        "breakdown": breakdown,
        "labels": labels,
        "counts": counts,
        "status": "Online" if online else "Offline",
        "guild_count": len(b.guilds) if online else 0,
    })


@app.get("/guilds")
async def guilds_page(request: Request):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT guild_id, COUNT(*) AS c, MAX(timestamp) "
            "FROM infractions GROUP BY guild_id ORDER BY c DESC"
        ) as cur:
            rows = await cur.fetchall()
    return templates.TemplateResponse(request, "guilds.html", {"guilds": rows})


@app.get("/guilds/{guild_id}")
async def guild_detail(request: Request, guild_id: int):
    config = await get_guild_config(guild_id)
    lockdown = await get_lockdown(guild_id)
    slurs = await get_slur_list(guild_id)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, offence_type, severity, action_taken, timestamp "
            "FROM infractions WHERE guild_id = ? ORDER BY timestamp DESC LIMIT 50",
            (guild_id,),
        ) as cur:
            infractions = await cur.fetchall()

    return templates.TemplateResponse(request, "guild.html", {
        "guild_id": guild_id,
        "config": config,
        "config_groups": CONFIG_GROUPS,
        "bool_fields": BOOL_FIELDS,
        "lockdown": lockdown,
        "slurs": slurs,
        "infractions": infractions,
        "bot_online": _bot_online(),
        "flash": request.query_params.get("flash"),
    })


@app.get("/users/{user_id}")
async def user_detail(request: Request, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT guild_id, offence_type, severity, action_taken, timestamp "
            "FROM infractions WHERE user_id = ? ORDER BY timestamp DESC",
            (user_id,),
        ) as cur:
            infractions = await cur.fetchall()
    return templates.TemplateResponse(request, "user.html", {
        "user_id": user_id,
        "infractions": infractions,
    })


# ---------------------------------------------------------------------------
# Guild config
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Slur list
# ---------------------------------------------------------------------------

@app.post("/guilds/{guild_id}/slurs/add")
async def guild_add_slur(guild_id: int, slur: str = Form(...)):
    await add_slur(guild_id, slur.strip().lower())
    return RedirectResponse(f"/guilds/{guild_id}?flash=slur_added#slurs", status_code=303)


@app.post("/guilds/{guild_id}/slurs/remove")
async def guild_remove_slur(guild_id: int, slur: str = Form(...)):
    await remove_slur(guild_id, slur)
    return RedirectResponse(f"/guilds/{guild_id}?flash=slur_removed#slurs", status_code=303)


# ---------------------------------------------------------------------------
# Mod actions
# ---------------------------------------------------------------------------

def _require_bot(guild_id: int):
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
        pass
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
        pass
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
    return RedirectResponse(
        f"/guilds/{guild_id}?flash={'lockdown_on' if on else 'lockdown_off'}",
        status_code=303,
    )
