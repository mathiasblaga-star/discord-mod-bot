import datetime
import os
import time
from pathlib import Path

import aiosqlite
from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import DB_PATH
from database import GUILD_CONFIG_DEFAULTS, reset_guild_config

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


def _valid_session(request: Request) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    try:
        serializer.loads(cookie, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


app = FastAPI(title="Mod Bot Dashboard", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path in ("/login", "/logout", "/favicon.ico"):
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

    return templates.TemplateResponse(request, "index.html", {
        "total": total,
        "recent": recent,
        "top_users": top_users,
        "breakdown": breakdown,
        "labels": labels,
        "counts": counts,
        "status": "Online",
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
    config_row = None
    config_keys = list(GUILD_CONFIG_DEFAULTS.keys())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT " + ", ".join(config_keys) + " FROM guild_config WHERE guild_id = ?",
            (guild_id,),
        ) as cur:
            config_row = await cur.fetchone()

        async with db.execute(
            "SELECT user_id, offence_type, severity, action_taken, timestamp "
            "FROM infractions WHERE guild_id = ? "
            "ORDER BY timestamp DESC LIMIT 50",
            (guild_id,),
        ) as cur:
            infractions = await cur.fetchall()

    if config_row is not None:
        config = dict(zip(config_keys, config_row))
        config_source = "database"
    else:
        config = dict(GUILD_CONFIG_DEFAULTS)
        config_source = "defaults (no row in DB)"

    return templates.TemplateResponse(request, "guild.html", {
        "guild_id": guild_id,
        "config": config,
        "config_source": config_source,
        "infractions": infractions,
    })


@app.post("/guilds/{guild_id}/reset-config")
async def guild_reset_config(guild_id: int):
    await reset_guild_config(guild_id)
    return RedirectResponse(f"/guilds/{guild_id}", status_code=303)


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
