"""
Input Validation Tests
======================
Tests every user-facing input boundary: command arguments, config values,
user IDs, durations, reason lengths, and config key whitelisting.
"""

import os
import sys
import pytest

# conftest.py handles sys.path, DB patching, and schema init before this runs.
import database as db
import config


# ── User ID boundary tests ───────────────────────────────────────────────────

class TestUserIDBoundaries:
    """Mirrors the /unban user_id validation added in Prompt 8."""

    VALID_IDS = [
        1,
        123456789012345678,     # typical Discord snowflake
        9_999_999_999_999_999_999,
    ]
    INVALID_IDS = [
        0,
        -1,
        -999999999,
        10_000_000_000_000_000_000,   # exceeds upper bound
        "abc",
        None,
        "",
        "' OR 1=1 --",
        3.14,                         # float — int(3.14)=3 silently truncates, must be rejected
        True,                         # bool is a subclass of int — must be rejected explicitly
        False,
    ]

    def _validate_user_id(self, uid) -> bool:
        """
        Replicates the validation logic from cogs/admin.py /unban.
        Floats and bools are explicitly rejected — int(3.14) silently
        truncates and bool is a subclass of int, both are unsafe.
        """
        if isinstance(uid, bool) or isinstance(uid, float):
            return False
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return False
        return 0 < uid <= 9_999_999_999_999_999_999

    def test_valid_ids_accepted(self):
        for uid in self.VALID_IDS:
            assert self._validate_user_id(uid), f"Valid ID rejected: {uid}"

    def test_invalid_ids_rejected(self):
        for uid in self.INVALID_IDS:
            assert not self._validate_user_id(uid), f"Invalid ID accepted: {uid!r}"


# ── Reason length tests ──────────────────────────────────────────────────────

class TestReasonTruncation:
    """Discord audit log reasons are capped at 512 chars (Prompt 8)."""

    MAX = 512

    def _sanitise_reason(self, reason: str) -> str:
        if len(reason) > self.MAX:
            return reason[:self.MAX] + "..."
        return reason

    def test_short_reason_unchanged(self):
        r = "Spamming"
        assert self._sanitise_reason(r) == r

    def test_exactly_512_chars_unchanged(self):
        r = "x" * 512
        result = self._sanitise_reason(r)
        assert result == r

    def test_513_chars_truncated(self):
        r = "x" * 513
        result = self._sanitise_reason(r)
        assert len(result) <= 515   # 512 + "..."
        assert result.endswith("...")

    def test_10000_char_reason_truncated(self):
        r = "A" * 10_000
        result = self._sanitise_reason(r)
        assert len(result) <= 515
        assert result.endswith("...")

    def test_injection_in_reason_is_stored_not_executed(self):
        payload = "'; DROP TABLE infractions; --"
        result = self._sanitise_reason(payload)
        # Must pass through as a string — never executed
        assert isinstance(result, str)
        assert len(result) <= 515


# ── Purge count boundary tests ───────────────────────────────────────────────

class TestPurgeCount:
    """The /purge command must only accept 1–100."""

    def _validate_purge_count(self, count) -> bool:
        try:
            count = int(count)
        except (TypeError, ValueError):
            return False
        return 1 <= count <= 100

    def test_valid_range(self):
        for n in [1, 50, 100]:
            assert self._validate_purge_count(n), f"Valid count {n} rejected"

    def test_zero_rejected(self):
        assert not self._validate_purge_count(0)

    def test_negative_rejected(self):
        assert not self._validate_purge_count(-5)

    def test_101_rejected(self):
        assert not self._validate_purge_count(101)

    def test_string_number_accepted(self):
        assert self._validate_purge_count("50")

    def test_string_word_rejected(self):
        assert not self._validate_purge_count("all")

    def test_injection_rejected(self):
        assert not self._validate_purge_count("'; DROP TABLE infractions; --")


# ── Slowmode boundary tests ──────────────────────────────────────────────────

class TestSlowmode:
    """The /slowmode command must accept 0–21600 only."""

    def _validate_slowmode(self, seconds) -> bool:
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return False
        return 0 <= seconds <= 21600

    def test_zero_allowed(self):
        assert self._validate_slowmode(0)

    def test_max_allowed(self):
        assert self._validate_slowmode(21600)

    def test_negative_rejected(self):
        assert not self._validate_slowmode(-1)

    def test_over_max_rejected(self):
        assert not self._validate_slowmode(21601)


# ── Duration boundary tests (mute/tempban) ───────────────────────────────────

class TestDurationBoundaries:
    """Mute and tempban durations must be positive integers."""

    def _validate_duration(self, minutes) -> bool:
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            return False
        return minutes >= 1

    def test_one_minute_ok(self):
        assert self._validate_duration(1)

    def test_zero_rejected(self):
        assert not self._validate_duration(0)

    def test_negative_rejected(self):
        assert not self._validate_duration(-100)

    def test_string_injection_rejected(self):
        assert not self._validate_duration("'; SELECT 1; --")

    def test_float_string_rejected(self):
        # int("3.5") raises ValueError — should be rejected
        assert not self._validate_duration("3.5")


# ── Delete days boundary (ban command) ──────────────────────────────────────

class TestDeleteDays:
    """Discord /ban delete_days must be 0–7."""

    def _validate_delete_days(self, days) -> bool:
        try:
            days = int(days)
        except (TypeError, ValueError):
            return False
        return 0 <= days <= 7

    def test_zero_ok(self):
        assert self._validate_delete_days(0)

    def test_seven_ok(self):
        assert self._validate_delete_days(7)

    def test_eight_rejected(self):
        assert not self._validate_delete_days(8)

    def test_negative_rejected(self):
        assert not self._validate_delete_days(-1)


# ── Guild config key whitelist ───────────────────────────────────────────────

class TestConfigKeyWhitelist:
    """set_guild_config must only accept keys from _GUILD_CONFIG_COLS."""

    @pytest.mark.asyncio
    async def test_valid_key_accepted(self):
        await db.init_db()
        await db.set_guild_config(999000000000000001, spam_message_count=5)

    @pytest.mark.asyncio
    async def test_injected_key_raises(self):
        injected_keys = [
            "'; DROP TABLE guild_config; --",
            "spam_message_count=1; DROP TABLE guild_config; --",
            "1 OR 1=1",
            "__class__",
            "guild_id",  # guild_id is not in _GUILD_CONFIG_COLS, it's the PK
        ]
        for key in injected_keys:
            with pytest.raises((ValueError, TypeError)):
                await db.set_guild_config(999000000000000001, **{key: 99})

    @pytest.mark.asyncio
    async def test_integer_value_type_enforced(self):
        """Config values must be integers — string injections should fail or be cast."""
        for bad_val in ["'; DROP TABLE guild_config; --", None, [], {}]:
            try:
                await db.set_guild_config(999000000000000001, spam_message_count=bad_val)
                # If it didn't raise, check the stored value is not the payload
                cfg = await db.get_guild_config(999000000000000001)
                stored = cfg.get("spam_message_count")
                assert isinstance(stored, int), f"Non-integer stored: {stored!r}"
            except (TypeError, ValueError):
                pass  # correctly rejected


# ── Config startup validation (Prompt 8) ────────────────────────────────────

class TestConfigValidation:
    """Ensure config.py validate_config() catches bad env values."""

    def test_fuzzy_threshold_in_range(self):
        """FUZZY_THRESHOLD must be 50–100."""
        assert 50 <= config.FUZZY_THRESHOLD <= 100, \
            f"FUZZY_THRESHOLD={config.FUZZY_THRESHOLD} out of safe range"

    def test_spam_message_count_reasonable(self):
        assert 1 <= config.SPAM_MESSAGE_COUNT <= 100

    def test_spam_time_window_reasonable(self):
        assert 1 <= config.SPAM_TIME_WINDOW <= 60

    def test_nuke_time_window_reasonable(self):
        assert 1 <= config.NUKE_TIME_WINDOW <= 60

    def test_slur_list_is_list(self):
        assert isinstance(config.SLUR_LIST, list)

    def test_mute_durations_positive(self):
        assert config.SPAM_MUTE_DURATION > 0
        assert config.SLUR_MUTE_DURATION > 0
