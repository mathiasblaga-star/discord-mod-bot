import re
from typing import Iterable

from rapidfuzz import fuzz

LEET_MAP = str.maketrans({
    "0": "o", "1": "i", "!": "i", "|": "i",
    "3": "e", "4": "a", "@": "a",
    "5": "s", "$": "s",
    "7": "t", "9": "g", "8": "b",
})

_NON_ALPHA = re.compile(r"[^a-z]+")


def normalise(text: str) -> str:
    if not text:
        return ""
    return _NON_ALPHA.sub("", text.lower().translate(LEET_MAP))


def contains_slur(message: str, slurs: Iterable[str], threshold: int = 88):
    """Return the canonical slur that matched, or None."""
    if not slurs:
        return None
    norm = normalise(message)
    if not norm:
        return None
    for slur in slurs:
        target = normalise(slur)
        if not target:
            continue
        if target in norm:
            return slur
        if len(norm) >= len(target):
            for i in range(len(norm) - len(target) + 1):
                window = norm[i:i + len(target)]
                if fuzz.ratio(window, target) >= threshold:
                    return slur
    return None
