import re
from urllib.parse import urlparse


INVITE_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?"
    r"(?:discord(?:app)?\.com/invite/|discord\.gg/|dsc\.gg/)"
    r"[\w-]+",
    re.IGNORECASE,
)

URL_PATTERN = re.compile(
    r"(?:https?://|www\.)[^\s<>\"'`)\]]+",
    re.IGNORECASE,
)

PHISHING_DOMAINS: set[str] = {
    "discordnitro",
    "discord-nitro",
    "free-nitro",
    "discordgift",
    "discord-gift",
    "steamcommunity.ru",
    "steamgift",
    "csgo-skins",
    "free-steam",
    "nitro-gift",
    "claimnitro",
    "getnitro",
}


def scan_message(content: str) -> tuple[bool, bool]:
    """Return (has_invite, has_phishing) for the given message content."""
    if not content:
        return (False, False)

    has_invite = INVITE_PATTERN.search(content) is not None

    # Pass 1 — structured URL extraction (catches http/https/www prefixed URLs)
    has_phishing = False
    for match in URL_PATTERN.finditer(content):
        raw = match.group(0).lower()
        try:
            to_parse = raw if raw.startswith(("http://", "https://")) else f"http://{raw}"
            parsed = urlparse(to_parse)
            target = f"{(parsed.hostname or '')}{(parsed.path or '')}".lower()
        except Exception:
            target = raw
        for pattern in PHISHING_DOMAINS:
            if pattern in target or pattern in raw:
                has_phishing = True
                break
        if has_phishing:
            break

    # Pass 2 — raw substring scan as fallback for scheme-less URLs
    # e.g. "discord-nitro.ru/promo" has no http:// so URL_PATTERN skips it,
    # but the phishing keyword is still visible in plain text.
    if not has_phishing:
        content_lower = content.lower()
        for pattern in PHISHING_DOMAINS:
            if pattern in content_lower:
                has_phishing = True
                break

    return (has_invite, has_phishing)
