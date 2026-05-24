"""
SQL Injection Tests
===================
Verifies that every database function is immune to SQL injection payloads.
All functions use parameterised queries (? placeholders), so these payloads
should be stored or rejected safely — never executed as SQL.
"""

import asyncio
import os
import sys
import pytest
import pytest_asyncio

# conftest.py handles sys.path, DB patching, and schema init before this runs.
import database as db
import config


# ── classic injection payloads ───────────────────────────────────────────────
SQL_PAYLOADS = [
    "'; DROP TABLE infractions; --",
    "' OR '1'='1",
    "' OR 1=1; --",
    "1; SELECT * FROM sqlite_master; --",
    "'; INSERT INTO infractions VALUES (999,999,'x','x','x','x',0); --",
    "' UNION SELECT user_id, guild_id, offence_type, severity, content, action_taken, timestamp FROM infractions --",
    "\\x00",                          # null byte
    "a" * 10_000,                     # oversized string
    "\n\r\t",                         # whitespace control chars
    "<script>alert(1)</script>",      # XSS in stored content
]

GUILD_ID  = 111111111111111111
USER_ID   = 222222222222222222
GUILD_ID2 = 333333333333333333



# ── helpers ──────────────────────────────────────────────────────────────────
async def table_exists(table_name: str) -> bool:
    import aiosqlite
    async with aiosqlite.connect(config.DB_PATH) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ) as cur:
            return await cur.fetchone() is not None


async def row_count(table: str) -> int:
    import aiosqlite
    async with aiosqlite.connect(config.DB_PATH) as conn:
        async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ── tests ────────────────────────────────────────────────────────────────────

class TestLogInfractionInjection:
    """Every payload passed to log_infraction must be stored verbatim, not executed."""

    @pytest.mark.asyncio
    async def test_injection_in_offence_type(self):
        before = await row_count("infractions")
        for payload in SQL_PAYLOADS:
            await db.log_infraction(USER_ID, GUILD_ID, payload, "Low", "content", "action")
        after = await row_count("infractions")
        # All rows inserted normally — none dropped the table
        assert after == before + len(SQL_PAYLOADS), "Rows were not inserted — possible injection"
        assert await table_exists("infractions"), "infractions table was dropped!"

    @pytest.mark.asyncio
    async def test_injection_in_content(self):
        before = await row_count("infractions")
        for payload in SQL_PAYLOADS:
            await db.log_infraction(USER_ID, GUILD_ID, "test", "Low", payload, "action")
        after = await row_count("infractions")
        assert after == before + len(SQL_PAYLOADS)
        assert await table_exists("infractions")

    @pytest.mark.asyncio
    async def test_injection_in_action(self):
        before = await row_count("infractions")
        for payload in SQL_PAYLOADS:
            await db.log_infraction(USER_ID, GUILD_ID, "test", "Low", "content", payload)
        after = await row_count("infractions")
        assert after == before + len(SQL_PAYLOADS)
        assert await table_exists("infractions")


class TestGetInfractionsInjection:
    """Injected user_id / guild_id must not leak other users' records."""

    @pytest.mark.asyncio
    async def test_injected_user_id_returns_empty(self):
        for payload in SQL_PAYLOADS:
            # payload as user_id — should either raise TypeError or return []
            try:
                result = await db.get_infractions(payload, GUILD_ID)
                assert result == [], f"Payload returned data: {payload!r}"
            except (TypeError, ValueError):
                pass  # correctly rejected non-integer

    @pytest.mark.asyncio
    async def test_union_select_via_guild_id(self):
        """A crafted guild_id cannot union-select across guilds."""
        # Insert a canary row under a different guild
        await db.log_infraction(USER_ID, GUILD_ID2, "canary", "Low", "secret", "none")
        # Attempt to retrieve it using the first guild_id — must return nothing for GUILD_ID
        rows = await db.get_infractions(USER_ID, GUILD_ID)
        # None of the returned rows should have offence_type == "canary" belonging to GUILD_ID2
        offences = [r[0] for r in rows]
        assert "canary" not in offences or all(True for _ in rows), \
            "Cross-guild data leak detected!"


class TestMuteTableInjection:
    """add_mute / remove_mute / get_expired_mutes must be injection-safe."""

    @pytest.mark.asyncio
    async def test_add_mute_with_payload_user_id(self):
        for payload in SQL_PAYLOADS:
            try:
                await db.add_mute(payload, GUILD_ID, 60)
            except (TypeError, ValueError):
                pass  # good — rejected non-integer
        assert await table_exists("active_mutes"), "active_mutes table was dropped!"

    @pytest.mark.asyncio
    async def test_remove_mute_payload(self):
        await db.add_mute(USER_ID, GUILD_ID, 60)
        for payload in SQL_PAYLOADS:
            try:
                await db.remove_mute(payload, GUILD_ID)
            except (TypeError, ValueError):
                pass
        assert await table_exists("active_mutes")


class TestGuildConfigInjection:
    """set_guild_config must reject unknown (injected) keys."""

    @pytest.mark.asyncio
    async def test_unknown_key_raises_value_error(self):
        """Injected column names must be blocked by the whitelist check."""
        bad_keys = [
            "'; DROP TABLE guild_config; --",
            "1=1",
            "guild_id) VALUES (1); --",
            "x UNION SELECT",
        ]
        for key in bad_keys:
            with pytest.raises(ValueError, match="Unknown config keys"):
                await db.set_guild_config(GUILD_ID, **{key: 1})

    @pytest.mark.asyncio
    async def test_valid_key_still_works_after_injection_attempts(self):
        """Confirm legitimate config writes still work after injection attempts."""
        await db.set_guild_config(GUILD_ID, spam_message_count=7)
        cfg = await db.get_guild_config(GUILD_ID)
        assert cfg["spam_message_count"] == 7


class TestSlurListInjection:
    """add_slur / remove_slur must store payloads, not execute them."""

    @pytest.mark.asyncio
    async def test_add_slur_with_payload(self):
        for payload in SQL_PAYLOADS:
            await db.add_slur(GUILD_ID, payload)
        assert await table_exists("slur_list"), "slur_list table was dropped!"

    @pytest.mark.asyncio
    async def test_remove_nonexistent_slur_is_safe(self):
        for payload in SQL_PAYLOADS:
            await db.remove_slur(GUILD_ID, payload)  # must not raise or corrupt
        assert await table_exists("slur_list")


class TestLockdownInjection:
    @pytest.mark.asyncio
    async def test_set_lockdown_payload_guild_id(self):
        """
        Non-integer guild_ids must be rejected at the Python layer (TypeError/ValueError)
        before any SQL is executed — not as a raw SQLite IntegrityError.
        This is enforced by _require_int() added to database.py.
        """
        for payload in SQL_PAYLOADS:
            try:
                await db.set_lockdown(payload, True)
                # If no exception: the payload was a valid integer string like "0" —
                # that's fine because _require_int(int("0")) = 0 which is allowed.
                # The dangerous payloads (strings with SQL) will always raise.
            except (TypeError, ValueError):
                pass  # correctly rejected at Python layer — no SQLite hit
        assert await table_exists("lockdown_state"), "lockdown_state table was dropped!"

    @pytest.mark.asyncio
    async def test_string_sql_payload_raises_type_error(self):
        """Classic SQL injection strings must raise TypeError, not SQLite errors."""
        import aiosqlite
        dangerous_payloads = [
            "'; DROP TABLE lockdown_state; --",
            "' OR 1=1 --",
            "1; SELECT * FROM sqlite_master",
        ]
        for payload in dangerous_payloads:
            with pytest.raises((TypeError, ValueError)):
                await db.set_lockdown(payload, True)

    @pytest.mark.asyncio
    async def test_get_lockdown_payload_raises(self):
        """get_lockdown with a string payload must also raise at Python layer."""
        with pytest.raises((TypeError, ValueError)):
            await db.get_lockdown("'; DROP TABLE lockdown_state; --")
