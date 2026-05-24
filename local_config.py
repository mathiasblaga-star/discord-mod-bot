"""
Local config file manager for the packaged .exe distribution.

Stores credentials in botconfig.json next to the exe (or project root in dev).
This replaces .env + token_store.py for customer deployments.

Config precedence (highest → lowest):
  1. botconfig.json   — exe / setup-wizard mode
  2. Environment vars — Railway / Docker deployments
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Fields stored in botconfig.json and their defaults.
_FIELDS: dict[str, object] = {
    "discord_token":        "",
    "guild_id":             0,
    "mod_log_channel_id":   0,
    "mod_alerts_channel_id": 0,
    "admin_role_id":        0,
    "muted_role_id":        0,
    "dashboard_secret":     "",
    "dashboard_port":       8080,
    "setup_complete":       False,
}


def _config_path() -> Path:
    """botconfig.json lives next to the exe when frozen, else in the project root."""
    if getattr(sys, "frozen", False):          # PyInstaller exe
        return Path(sys.executable).parent / "botconfig.json"
    return Path(__file__).parent / "botconfig.json"


def load() -> dict:
    """Return the full config dict, merged with defaults."""
    data = dict(_FIELDS)
    path = _config_path()
    if path.exists():
        try:
            saved = json.loads(path.read_text("utf-8"))
            data.update({k: v for k, v in saved.items() if k in _FIELDS})
        except Exception:
            pass
    return data


def save(updates: dict) -> None:
    """Persist only known keys; merge with whatever is already on disk."""
    current = load()
    current.update({k: v for k, v in updates.items() if k in _FIELDS})
    _config_path().write_text(json.dumps(current, indent=2), "utf-8")


def get(key: str, default=None):
    return load().get(key, default)


def is_setup_complete() -> bool:
    """True when the required credentials have been saved via the setup wizard."""
    cfg = load()
    return bool(
        cfg.get("setup_complete")
        and cfg.get("discord_token")
        and cfg.get("guild_id")
        and cfg.get("dashboard_secret")
    )


# ---------------------------------------------------------------------------
# Unified getters — check local config first, fall back to env vars so the
# Railway deployment continues to work without any botconfig.json.
# ---------------------------------------------------------------------------

def discord_token() -> str:
    return get("discord_token") or os.getenv("DISCORD_TOKEN", "")


def guild_id() -> int:
    local = get("guild_id", 0)
    if local:
        return int(local)
    val = os.getenv("GUILD_ID", "0")
    try:
        return int(val)
    except ValueError:
        return 0


def dashboard_secret() -> str:
    return get("dashboard_secret") or os.getenv("DASHBOARD_SECRET", "")


def dashboard_port() -> int:
    local = get("dashboard_port", 0)
    if local:
        return int(local)
    try:
        return int(os.getenv("PORT", "8080"))
    except ValueError:
        return 8080


def is_railway_mode() -> bool:
    """True when running on Railway/Docker (env vars set, no setup wizard needed)."""
    return bool(os.getenv("DASHBOARD_SECRET")) or bool(os.getenv("MASTER_PASSWORD"))
