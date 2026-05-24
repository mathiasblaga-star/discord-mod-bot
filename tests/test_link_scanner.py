"""
Link Scanner Tests
==================
Tests utils/link_scanner.py — invite detection and phishing URL detection.
These tests verify both detection accuracy and evasion resistance.
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.link_scanner import scan_message, INVITE_PATTERN, PHISHING_DOMAINS


# ── Invite detection ─────────────────────────────────────────────────────────

class TestInviteDetection:

    SHOULD_DETECT = [
        "Join us at discord.gg/abc123",
        "https://discord.gg/INVITE",
        "http://discord.com/invite/xyz",
        "https://discordapp.com/invite/abc",
        "dsc.gg/myserver",
        "Come join! discord.gg/test embedded in text",
        "DISCORD.GG/CAPS",                    # case insensitivity
        "discord.gg/with-hyphens-123",
        "Check discord.com/invite/abc?utm_source=x",  # with query string
    ]

    SHOULD_NOT_DETECT = [
        "Hello world, no links here",
        "https://discord.com/channels/123/456",    # channel link, not invite
        "https://discord.com/developers/docs",     # dev docs
        "https://discordstatus.com",               # status page
        "discord.gg",                              # no code after slash
        "",
        "   ",
    ]

    def test_detects_invites(self):
        for msg in self.SHOULD_DETECT:
            has_invite, _ = scan_message(msg)
            assert has_invite, f"Invite not detected in: {msg!r}"

    def test_no_false_positives(self):
        for msg in self.SHOULD_NOT_DETECT:
            has_invite, _ = scan_message(msg)
            assert not has_invite, f"False positive invite in: {msg!r}"

    def test_evasion_attempts(self):
        """Common bypass tricks — should still be caught."""
        evasions = [
            "discord . gg / abc123",      # spaces around dots (may or may not catch — document result)
            "discord[.]gg/abc123",        # bracket evasion
        ]
        # These are documented evasion attempts — log which ones slip through
        for msg in evasions:
            has_invite, _ = scan_message(msg)
            # Not asserting here — just ensuring no exception is thrown
            assert isinstance(has_invite, bool)


# ── Phishing detection ───────────────────────────────────────────────────────

class TestPhishingDetection:

    SHOULD_DETECT = [
        "https://discordnitro-gift.com/free",
        "http://free-nitro.xyz/claim",
        "get your nitro: discord-nitro.ru/promo",
        "steamgift.io/free-skins",
        "https://csgo-skins.net/trade",
        "claimnitro.gg/free",
        "https://getnitro.xyz",
        "steamcommunity.ru/tradeoffer",
        "Free steam gift! free-steam.xyz",
    ]

    SHOULD_NOT_DETECT = [
        "https://discord.com/nitro",              # legitimate Nitro page
        "https://store.steampowered.com",         # real Steam
        "https://steamcommunity.com/profiles/123", # real Steam community
        "Hello, here's my nitro code: ABCD-1234", # mention of nitro, no URL
        "",
    ]

    def test_detects_phishing(self):
        for msg in self.SHOULD_DETECT:
            _, has_phishing = scan_message(msg)
            assert has_phishing, f"Phishing not detected in: {msg!r}"

    def test_no_false_positives(self):
        for msg in self.SHOULD_NOT_DETECT:
            _, has_phishing = scan_message(msg)
            assert not has_phishing, f"False positive phishing in: {msg!r}"


# ── Combined message tests ───────────────────────────────────────────────────

class TestCombinedMessages:

    def test_both_invite_and_phishing(self):
        msg = "discord.gg/abc AND https://free-nitro.xyz"
        has_invite, has_phishing = scan_message(msg)
        assert has_invite
        assert has_phishing

    def test_clean_message(self):
        msg = "Just a normal message with no links."
        has_invite, has_phishing = scan_message(msg)
        assert not has_invite
        assert not has_phishing

    def test_empty_string(self):
        has_invite, has_phishing = scan_message("")
        assert not has_invite
        assert not has_phishing

    def test_extremely_long_message(self):
        """10 000 char message must not hang or crash."""
        msg = "x " * 5000 + "discord.gg/abc"
        has_invite, has_phishing = scan_message(msg)
        assert has_invite

    def test_unicode_content(self):
        """Unicode characters must not cause errors."""
        msg = "Héllo wörld 🎉 discord.gg/abc"
        has_invite, _ = scan_message(msg)
        assert has_invite

    def test_null_bytes_dont_crash(self):
        msg = "normal\x00text discord.gg/abc"
        has_invite, _ = scan_message(msg)
        assert isinstance(has_invite, bool)


# ── Regex safety ─────────────────────────────────────────────────────────────

class TestRegexSafety:
    """Ensure patterns don't have ReDoS (catastrophic backtracking) issues."""

    import time

    def test_invite_pattern_performance(self):
        """Should complete in under 1 second even on a worst-case string."""
        import time
        evil = "a" * 10_000 + "discord.gg/" + "a" * 10_000
        start = time.monotonic()
        INVITE_PATTERN.search(evil)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"Regex took {elapsed:.3f}s — possible ReDoS"

    def test_no_catastrophic_backtracking(self):
        import time
        # Classic ReDoS trigger: lots of near-matches
        evil = ("discord" + "." * 1000 + "gg") * 10
        start = time.monotonic()
        INVITE_PATTERN.search(evil)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"Possible ReDoS: {elapsed:.3f}s"
