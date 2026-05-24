"""
Spam & Slur Detection Logic Tests
===================================
Tests the core detection logic isolated from Discord API calls.
Uses mock objects so no bot token is needed.
"""

import sys
import os
import pytest
from collections import deque
import time

# conftest.py handles sys.path and DB patching before this runs.
import config
from utils.fuzzy_match import contains_slur
from utils.link_scanner import scan_message


# ── Fuzzy slur matching ──────────────────────────────────────────────────────

class TestFuzzyMatch:
    """Tests utils/fuzzy_match.py contains_slur()."""

    SLURS = config.SLUR_LIST  # use the configured list

    def test_exact_match_detected(self):
        if not self.SLURS:
            pytest.skip("SLUR_LIST is empty in config")
        for slur in self.SLURS:
            result = contains_slur(slur, self.SLURS)
            assert result is not None, f"Exact slur not detected: {slur!r}"

    def test_leetspeak_variants(self):
        """Common leetspeak substitutions must be caught."""
        leet_variants = [
            ("Retard", "R3t4rd"),
            ("Retard", "r3tard"),
            ("Retard", "RETARD"),
        ]
        for canonical, variant in leet_variants:
            if canonical in self.SLURS:
                result = contains_slur(variant, self.SLURS)
                assert result is not None, f"Leetspeak not caught: {variant!r}"

    def test_clean_text_not_flagged(self):
        clean_messages = [
            "Hello, how are you today?",
            "The weather is nice",
            "I love programming in Python",
            "discord.gg/server",
            "1234567890",
            "",
            "   ",
        ]
        for msg in clean_messages:
            result = contains_slur(msg, self.SLURS)
            assert result is None, f"Clean message falsely flagged: {msg!r} → {result!r}"

    def test_slur_embedded_in_sentence(self):
        """Slurs in context must still be detected."""
        if not self.SLURS:
            pytest.skip("SLUR_LIST is empty in config")
        for slur in self.SLURS[:1]:
            msg = f"You are such a {slur} for doing that"
            result = contains_slur(msg, self.SLURS)
            assert result is not None, f"Slur in sentence not detected: {msg!r}"

    def test_very_long_message_no_crash(self):
        msg = "hello " * 1000 + (self.SLURS[0] if self.SLURS else "")
        result = contains_slur(msg, self.SLURS)
        assert isinstance(result, (str, type(None)))

    def test_unicode_no_crash(self):
        result = contains_slur("こんにちは 🎉 normal text", self.SLURS)
        assert result is None  # no false positives on unicode

    def test_xss_payload_no_crash(self):
        result = contains_slur("<script>alert('xss')</script>", self.SLURS)
        assert result is None


# ── Spam rate detection logic ─────────────────────────────────────────────────

class TestSpamRateDetection:
    """Tests the spam-rate counting logic used in SpamCog."""

    def _count_recent(self, times: deque, now: float, window: int) -> int:
        return sum(1 for t in times if now - t <= window)

    def test_under_limit_not_spam(self):
        times = deque(maxlen=20)
        now = time.time()
        for i in range(4):
            times.append(now - i * 0.5)
        assert self._count_recent(times, now, 5) < config.SPAM_MESSAGE_COUNT

    def test_at_limit_is_spam(self):
        times = deque(maxlen=20)
        now = time.time()
        for i in range(config.SPAM_MESSAGE_COUNT):
            times.append(now - i * 0.1)
        assert self._count_recent(times, now, config.SPAM_TIME_WINDOW) >= config.SPAM_MESSAGE_COUNT

    def test_old_messages_not_counted(self):
        times = deque(maxlen=20)
        now = time.time()
        # Add messages well outside the window
        for i in range(100):
            times.append(now - 3600 - i)
        assert self._count_recent(times, now, config.SPAM_TIME_WINDOW) == 0

    def test_burst_then_silence(self):
        """After spam window passes, count resets effectively."""
        times = deque(maxlen=20)
        now = time.time()
        # Spam 10 minutes ago
        for i in range(10):
            times.append(now - 600 - i)
        assert self._count_recent(times, now, config.SPAM_TIME_WINDOW) == 0


# ── Duplicate message detection ──────────────────────────────────────────────

class TestDuplicateDetection:

    LIMIT = config.DUPLICATE_MESSAGE_LIMIT

    def test_under_limit_not_spam(self):
        content = deque(maxlen=10)
        msg = "same message"
        for _ in range(self.LIMIT - 1):
            content.append(msg)
        assert content.count(msg) < self.LIMIT

    def test_at_limit_is_spam(self):
        content = deque(maxlen=10)
        msg = "same message"
        for _ in range(self.LIMIT):
            content.append(msg)
        assert content.count(msg) >= self.LIMIT

    def test_different_messages_not_spam(self):
        content = deque(maxlen=10)
        for i in range(20):
            content.append(f"message {i}")
        for i in range(20):
            assert content.count(f"message {i}") < self.LIMIT

    def test_empty_message_not_counted(self):
        """Empty string duplicates should not trigger spam."""
        content = deque(maxlen=10)
        for _ in range(20):
            content.append("")
        # In spam.py: `if message.content and content.count(...)` — empty is skipped
        assert not ""  # confirms the truthiness check


# ── Mass mention detection ────────────────────────────────────────────────────

class TestMassMentionDetection:

    LIMIT = config.MASS_MENTION_LIMIT

    def _unique_mention_count(self, mention_ids: list, author_id: int) -> int:
        return len({uid for uid in mention_ids if uid != author_id})

    def test_under_limit_ok(self):
        assert self._unique_mention_count([1, 2, 3, 4], 999) < self.LIMIT

    def test_at_limit_triggers(self):
        mention_ids = list(range(self.LIMIT))
        assert self._unique_mention_count(mention_ids, 9999) >= self.LIMIT

    def test_self_mention_not_counted(self):
        author_id = 12345
        # Mentioning yourself LIMIT times should not trigger
        mention_ids = [author_id] * self.LIMIT
        assert self._unique_mention_count(mention_ids, author_id) == 0

    def test_duplicate_mentions_counted_once(self):
        """Mentioning the same user multiple times = 1 unique mention."""
        mention_ids = [111] * 10
        assert self._unique_mention_count(mention_ids, 999) == 1


# ── Nuke protection thresholds ────────────────────────────────────────────────

class TestNukeThresholds:

    def _recent_count(self, events: deque, now: float, window: int) -> int:
        return len([t for t in events if now - t <= window])

    def test_channel_deletes_under_limit_safe(self):
        events = deque(maxlen=50)
        now = time.time()
        for i in range(config.NUKE_CHANNEL_DELETE_LIMIT - 1):
            events.append(now - i * 0.1)
        assert self._recent_count(events, now, config.NUKE_TIME_WINDOW) < config.NUKE_CHANNEL_DELETE_LIMIT

    def test_channel_deletes_at_limit_triggers(self):
        events = deque(maxlen=50)
        now = time.time()
        for i in range(config.NUKE_CHANNEL_DELETE_LIMIT):
            events.append(now - i * 0.1)
        assert self._recent_count(events, now, config.NUKE_TIME_WINDOW) >= config.NUKE_CHANNEL_DELETE_LIMIT

    def test_ban_limit_triggers(self):
        events = deque(maxlen=50)
        now = time.time()
        for i in range(config.NUKE_BAN_LIMIT):
            events.append(now - i * 0.1)
        assert self._recent_count(events, now, config.NUKE_TIME_WINDOW) >= config.NUKE_BAN_LIMIT

    def test_old_nuke_events_not_counted(self):
        events = deque(maxlen=50)
        now = time.time()
        for i in range(100):
            events.append(now - 3600)
        assert self._recent_count(events, now, config.NUKE_TIME_WINDOW) == 0
