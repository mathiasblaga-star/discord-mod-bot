import hashlib
import time
from pathlib import Path

import aiosqlite

import config
from config import DB_PATH


def _require_int(value, name: str) -> int:
    """
    Enforce that a value intended as a Discord snowflake / DB integer key is
    a strict integer (bool and float are rejected — int(3.14) silently
    truncates, int(True) == 1 which is misleading).
    Raises TypeError for wrong types and ValueError for out-of-range values
    so callers get a clean Python exception before any SQL is attempted.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{name} must be a plain int, got {type(value).__name__}: {value!r}"
        )
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")
    return value


GUILD_CONFIG_DEFAULTS: dict = {
    "spam_message_count":        config.SPAM_MESSAGE_COUNT,
    "spam_time_window":          config.SPAM_TIME_WINDOW,
    "duplicate_message_limit":   config.DUPLICATE_MESSAGE_LIMIT,
    "mass_mention_limit":        config.MASS_MENTION_LIMIT,
    "spam_mute_duration":        config.SPAM_MUTE_DURATION,
    "slur_mute_duration":        config.SLUR_MUTE_DURATION,
    "nuke_channel_delete_limit": config.NUKE_CHANNEL_DELETE_LIMIT,
    "nuke_channel_create_limit": config.NUKE_CHANNEL_CREATE_LIMIT,
    "nuke_role_create_limit":    config.NUKE_ROLE_CREATE_LIMIT,
    "nuke_ban_limit":            config.NUKE_BAN_LIMIT,
    "nuke_time_window":          config.NUKE_TIME_WINDOW,
    "fuzzy_threshold":           config.FUZZY_THRESHOLD,
    "join_raid_window":          config.JOIN_RAID_WINDOW,
    "join_raid_limit":           config.JOIN_RAID_LIMIT,
    "min_account_age_days":      config.MIN_ACCOUNT_AGE_DAYS,
    "block_invites":             1,
    "block_phishing":            1,
    "mod_log_channel_id":        0,
    "mod_alerts_channel_id":     0,
    "admin_role_id":             0,
    "muted_role_id":             0,
}

_GUILD_CONFIG_COLS = list(GUILD_CONFIG_DEFAULTS.keys())


async def init_db():
    new_path = Path(DB_PATH)
    new_path.parent.mkdir(parents=True, exist_ok=True)
    # One-time migration: if a legacy root-level moderation.db exists from
    # before DB_PATH moved to data/, relocate it so the user keeps their history.
    legacy = Path(__file__).parent / "moderation.db"
    if legacy.exists() and not new_path.exists():
        legacy.rename(new_path)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS infractions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                offence_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                content TEXT,
                action_taken TEXT,
                timestamp INTEGER NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_infractions_user "
            "ON infractions(user_id, guild_id)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS active_mutes (
                user_id  INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, guild_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS lockdown_state (
                guild_id INTEGER PRIMARY KEY,
                enabled  INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id                   INTEGER PRIMARY KEY,
                spam_message_count         INTEGER NOT NULL DEFAULT {config.SPAM_MESSAGE_COUNT},
                spam_time_window           INTEGER NOT NULL DEFAULT {config.SPAM_TIME_WINDOW},
                duplicate_message_limit    INTEGER NOT NULL DEFAULT {config.DUPLICATE_MESSAGE_LIMIT},
                mass_mention_limit         INTEGER NOT NULL DEFAULT {config.MASS_MENTION_LIMIT},
                spam_mute_duration         INTEGER NOT NULL DEFAULT {config.SPAM_MUTE_DURATION},
                slur_mute_duration         INTEGER NOT NULL DEFAULT {config.SLUR_MUTE_DURATION},
                nuke_channel_delete_limit  INTEGER NOT NULL DEFAULT {config.NUKE_CHANNEL_DELETE_LIMIT},
                nuke_channel_create_limit  INTEGER NOT NULL DEFAULT {config.NUKE_CHANNEL_CREATE_LIMIT},
                nuke_role_create_limit     INTEGER NOT NULL DEFAULT {config.NUKE_ROLE_CREATE_LIMIT},
                nuke_ban_limit             INTEGER NOT NULL DEFAULT {config.NUKE_BAN_LIMIT},
                nuke_time_window           INTEGER NOT NULL DEFAULT {config.NUKE_TIME_WINDOW},
                fuzzy_threshold            INTEGER NOT NULL DEFAULT {config.FUZZY_THRESHOLD},
                join_raid_window           INTEGER NOT NULL DEFAULT {config.JOIN_RAID_WINDOW},
                join_raid_limit            INTEGER NOT NULL DEFAULT {config.JOIN_RAID_LIMIT},
                min_account_age_days       INTEGER NOT NULL DEFAULT {config.MIN_ACCOUNT_AGE_DAYS},
                block_invites              INTEGER NOT NULL DEFAULT 1,
                block_phishing             INTEGER NOT NULL DEFAULT 1,
                mod_log_channel_id         INTEGER NOT NULL DEFAULT 0,
                mod_alerts_channel_id      INTEGER NOT NULL DEFAULT 0,
                admin_role_id              INTEGER NOT NULL DEFAULT 0,
                muted_role_id              INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Migrate existing databases that pre-date later columns.
        for _col, _default in (
            ("join_raid_window", config.JOIN_RAID_WINDOW),
            ("join_raid_limit", config.JOIN_RAID_LIMIT),
            ("min_account_age_days", config.MIN_ACCOUNT_AGE_DAYS),
            ("block_invites", 1),
            ("block_phishing", 1),
        ):
            try:
                await db.execute(
                    f"ALTER TABLE guild_config ADD COLUMN {_col} "
                    f"INTEGER NOT NULL DEFAULT {_default}"
                )
            except Exception:
                pass  # column already exists
        await db.execute("""
            CREATE TABLE IF NOT EXISTS slur_list (
                guild_id INTEGER NOT NULL,
                slur     TEXT    NOT NULL,
                PRIMARY KEY (guild_id, slur)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS temp_bans (
                user_id  INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                unban_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, guild_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS neutralised_actors (
                user_id        INTEGER NOT NULL,
                guild_id       INTEGER NOT NULL,
                neutralised_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, guild_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pardons (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                guild_id    INTEGER NOT NULL,
                pardoned_by INTEGER NOT NULL,
                reason      TEXT,
                timestamp   INTEGER NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_pardons_user "
            "ON pardons(user_id, guild_id)"
        )
        await db.commit()


async def log_infraction(user_id, guild_id, offence, severity, content, action, redact: bool = False):
    user_id  = _require_int(user_id,  "user_id")
    guild_id = _require_int(guild_id, "guild_id")
    if redact and content:
        digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:8]
        content = f"[REDACTED — {digest}]"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO infractions "
            "(user_id, guild_id, offence_type, severity, content, action_taken, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, guild_id, offence, severity, content, action, int(time.time())),
        )
        await db.commit()


async def get_infractions(user_id, guild_id):
    user_id  = _require_int(user_id,  "user_id")
    guild_id = _require_int(guild_id, "guild_id")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT offence_type, severity, content, action_taken, timestamp "
            "FROM infractions WHERE user_id = ? AND guild_id = ? "
            "ORDER BY timestamp DESC",
            (user_id, guild_id),
        ) as cur:
            return await cur.fetchall()


async def count_infractions_of_type(user_id, guild_id, offence_type):
    user_id  = _require_int(user_id,  "user_id")
    guild_id = _require_int(guild_id, "guild_id")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM infractions "
            "WHERE user_id = ? AND guild_id = ? AND offence_type = ?",
            (user_id, guild_id, offence_type),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def add_mute(user_id, guild_id, duration_sec):
    user_id  = _require_int(user_id,  "user_id")
    guild_id = _require_int(guild_id, "guild_id")
    expires = int(time.time()) + int(duration_sec)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO active_mutes (user_id, guild_id, expires_at) "
            "VALUES (?, ?, ?)",
            (user_id, guild_id, expires),
        )
        await db.commit()
    return expires


async def remove_mute(user_id, guild_id):
    user_id  = _require_int(user_id,  "user_id")
    guild_id = _require_int(guild_id, "guild_id")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM active_mutes WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        )
        await db.commit()


async def get_expired_mutes():
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, guild_id FROM active_mutes WHERE expires_at <= ?",
            (now,),
        ) as cur:
            return await cur.fetchall()


async def set_lockdown(guild_id, enabled: bool):
    guild_id = _require_int(guild_id, "guild_id")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO lockdown_state (guild_id, enabled) VALUES (?, ?)",
            (guild_id, 1 if enabled else 0),
        )
        await db.commit()


async def get_lockdown(guild_id) -> bool:
    guild_id = _require_int(guild_id, "guild_id")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT enabled FROM lockdown_state WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row and row[0])


# ---------------- Guild config ------------------------------------------

async def get_guild_config(guild_id: int) -> dict:
    """Return the merged config for `guild_id`. Falls back to defaults when no row exists."""
    cols = ", ".join(_GUILD_CONFIG_COLS)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            f"SELECT {cols} FROM guild_config WHERE guild_id = ?", (guild_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return {"guild_id": guild_id, **GUILD_CONFIG_DEFAULTS}
    return {"guild_id": guild_id, **dict(zip(_GUILD_CONFIG_COLS, row))}


async def set_guild_config(guild_id: int, **kwargs) -> None:
    """Upsert only the supplied keys, merged with existing values."""
    invalid = set(kwargs) - set(_GUILD_CONFIG_COLS)
    if invalid:
        raise ValueError(f"Unknown config keys: {sorted(invalid)}")

    # All config values are integers — reject anything that isn't.
    # This blocks payloads like "'; DROP TABLE guild_config; --" being stored
    # verbatim (SQLite's dynamic typing would otherwise accept them).
    for key, val in kwargs.items():
        if isinstance(val, bool) or not isinstance(val, int):
            raise TypeError(
                f"Config value for '{key}' must be an int, "
                f"got {type(val).__name__}: {val!r}"
            )

    current = await get_guild_config(guild_id)
    current.update(kwargs)
    cols = ["guild_id"] + _GUILD_CONFIG_COLS
    placeholders = ",".join("?" for _ in cols)
    values = [guild_id] + [current[k] for k in _GUILD_CONFIG_COLS]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"INSERT OR REPLACE INTO guild_config ({','.join(cols)}) VALUES ({placeholders})",
            values,
        )
        await db.commit()


async def reset_guild_config(guild_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM guild_config WHERE guild_id = ?", (guild_id,))
        await db.commit()


# ---------------- Slur list ---------------------------------------------

async def get_slur_list(guild_id: int) -> list[str]:
    """Return the guild's custom slurs, or fall back to config.SLUR_LIST when empty."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT slur FROM slur_list WHERE guild_id = ?", (guild_id,),
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return list(config.SLUR_LIST)
    return [r[0] for r in rows]


async def add_slur(guild_id: int, slur: str) -> bool:
    slur = slur.strip().lower()
    if not slur:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO slur_list (guild_id, slur) VALUES (?, ?)",
            (guild_id, slur),
        )
        await db.commit()
        return cur.rowcount > 0


async def remove_slur(guild_id: int, slur: str) -> bool:
    slur = slur.strip().lower()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM slur_list WHERE guild_id = ? AND slur = ?",
            (guild_id, slur),
        )
        await db.commit()
        return cur.rowcount > 0


# ---------------- Temporary bans ----------------------------------------

async def add_temp_ban(user_id: int, guild_id: int, unban_at: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO temp_bans (user_id, guild_id, unban_at) "
            "VALUES (?, ?, ?)",
            (user_id, guild_id, unban_at),
        )
        await db.commit()


async def get_expired_temp_bans():
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, guild_id FROM temp_bans WHERE unban_at <= ?",
            (now,),
        ) as cur:
            return await cur.fetchall()


async def remove_temp_ban(user_id: int, guild_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM temp_bans WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        )
        await db.commit()


# ---------------- Neutralised nuke actors -------------------------------

async def add_neutralised(user_id: int, guild_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO neutralised_actors "
            "(user_id, guild_id, neutralised_at) VALUES (?, ?, ?)",
            (user_id, guild_id, int(time.time())),
        )
        await db.commit()


async def is_neutralised(user_id: int, guild_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM neutralised_actors WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ) as cur:
            return (await cur.fetchone()) is not None


async def remove_neutralised(user_id: int, guild_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM neutralised_actors WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        )
        await db.commit()


async def cleanup_neutralised(older_than_seconds: int = 86400) -> int:
    cutoff = int(time.time()) - older_than_seconds
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM neutralised_actors WHERE neutralised_at < ?",
            (cutoff,),
        )
        await db.commit()
        return cur.rowcount


# ---------------- Pardons / record expunge ------------------------------

async def add_pardon(user_id: int, guild_id: int, pardoned_by: int, reason: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO pardons (user_id, guild_id, pardoned_by, reason, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, guild_id, pardoned_by, reason, int(time.time())),
        )
        await db.commit()


async def get_pardons(user_id: int, guild_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT pardoned_by, reason, timestamp FROM pardons "
            "WHERE user_id = ? AND guild_id = ? ORDER BY timestamp DESC",
            (user_id, guild_id),
        ) as cur:
            return await cur.fetchall()


async def count_pardons(user_id: int, guild_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM pardons WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def delete_infractions_for(user_id: int, guild_id: int) -> int:
    """Delete all infractions for a user in a guild. Returns the row count."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM infractions WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        )
        await db.commit()
        return cur.rowcount


async def get_all_infractions(guild_id: int):
    """Return every infraction in a guild with all 8 columns. For export."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, user_id, guild_id, offence_type, severity, content, "
            "action_taken, timestamp FROM infractions "
            "WHERE guild_id = ? ORDER BY timestamp DESC",
            (guild_id,),
        ) as cur:
            return await cur.fetchall()
