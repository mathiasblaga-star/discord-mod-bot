import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str) -> int:
    val = os.getenv(name, "0")
    try:
        return int(val)
    except ValueError:
        return 0


# IDs (loaded from .env). The bot token is no longer read from .env —
# it's loaded at startup via token_store.load_token() from the encrypted
# token.enc file. Run setup_token.py once to create it.
GUILD_ID = _int_env("GUILD_ID")
MOD_LOG_CHANNEL_ID = _int_env("MOD_LOG_CHANNEL_ID")
MOD_ALERTS_CHANNEL_ID = _int_env("MOD_ALERTS_CHANNEL_ID")
ADMIN_ROLE_ID = _int_env("ADMIN_ROLE_ID")
MUTED_ROLE_ID = _int_env("MUTED_ROLE_ID")  # optional fallback if Discord timeouts fail

# Database — stored next to the exe when frozen so data survives restarts.
# When running as a plain script, put it in the project root as before.
if getattr(sys, "frozen", False):
    DB_PATH = str(Path(sys.executable).parent / "data" / "moderation.db")
else:
    DB_PATH = str(Path(__file__).parent / "data" / "moderation.db")

# --- Spam thresholds -----------------------------------------------------
SPAM_MESSAGE_COUNT = 5          # messages within window
SPAM_TIME_WINDOW = 5            # seconds
DUPLICATE_MESSAGE_LIMIT = 3     # identical messages in a row
MASS_MENTION_LIMIT = 5          # distinct user mentions in a single message

# --- Mute durations (seconds) -------------------------------------------
SPAM_MUTE_DURATION = 10 * 60 
SLUR_MUTE_DURATION = 60 * 60

# --- Nuke protection thresholds (events within NUKE_TIME_WINDOW seconds)
NUKE_CHANNEL_DELETE_LIMIT = 2
NUKE_CHANNEL_CREATE_LIMIT = 2
NUKE_ROLE_CREATE_LIMIT = 2
NUKE_BAN_LIMIT = 2
NUKE_TIME_WINDOW = 10

# --- Join protection (raid + account-age gating) ----------------------
JOIN_RAID_WINDOW = 10           # seconds
JOIN_RAID_LIMIT = 8             # joins within window to trigger raid mode
MIN_ACCOUNT_AGE_DAYS = 0        # 0 = disabled; otherwise minimum age in days

# --- Slur list ----------------------------------------------------------
# IMPORTANT: populate this with the slurs you want filtered. Regular profanity
# (fuck/shit/damn/etc.) should NOT be added — this list is only for targeted
# slurs and hate speech. Each entry is matched with leetspeak / fuzzy matching,
# so you only need the canonical form.
SLUR_LIST: list[str] = [
    "Retard",
    "Gimp",
    "Slag",
]

# Fuzzy matching threshold (0–100). Higher = stricter (fewer false positives).
FUZZY_THRESHOLD = 88


def validate_config() -> None:
    """Fail fast on invalid config. Runs at import time."""
    errors: list[str] = []

    if GUILD_ID < 0:
        errors.append(f"GUILD_ID must be >= 0 (got {GUILD_ID})")
    if MOD_LOG_CHANNEL_ID < 0:
        errors.append(f"MOD_LOG_CHANNEL_ID must be >= 0 (got {MOD_LOG_CHANNEL_ID})")
    if MOD_ALERTS_CHANNEL_ID < 0:
        errors.append(f"MOD_ALERTS_CHANNEL_ID must be >= 0 (got {MOD_ALERTS_CHANNEL_ID})")
    if not (1 <= SPAM_MESSAGE_COUNT <= 100):
        errors.append(f"SPAM_MESSAGE_COUNT must be 1-100 (got {SPAM_MESSAGE_COUNT})")
    if not (1 <= SPAM_TIME_WINDOW <= 60):
        errors.append(f"SPAM_TIME_WINDOW must be 1-60 (got {SPAM_TIME_WINDOW})")
    if not (50 <= FUZZY_THRESHOLD <= 100):
        errors.append(f"FUZZY_THRESHOLD must be 50-100 (got {FUZZY_THRESHOLD})")
    if not (1 <= NUKE_TIME_WINDOW <= 60):
        errors.append(f"NUKE_TIME_WINDOW must be 1-60 (got {NUKE_TIME_WINDOW})")

    if errors:
        raise RuntimeError(
            "config.py validation failed:\n  - " + "\n  - ".join(errors)
        )

    # Non-fatal warnings.
    if GUILD_ID == 0:
        print("Warning: GUILD_ID is 0 — slash commands will sync globally "
              "(can take up to 1 hour to propagate).")
    if not SLUR_LIST:
        print("Warning: SLUR_LIST is empty — the slur filter is inert until populated.")
    if SPAM_MUTE_DURATION == 0:
        print("Warning: SPAM_MUTE_DURATION is 0 — duplicate-spam mutes will be no-ops.")


validate_config()

