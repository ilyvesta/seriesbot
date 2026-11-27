"""Hand a search from one bot to another through a t.me/<bot>?start=<payload> link.

Telegram allows only A-Z a-z 0-9 _ - in a start payload, at most 64 characters, so the search text is
base64url-encoded (no padding) behind a short prefix and cut at a word boundary if it's too long.
"""
from __future__ import annotations

import base64
import binascii

PREFIX = "q_"
MAX_PAYLOAD = 64


def encode_search(text: str) -> str:
    words = " ".join((text or "").split()).split(" ")
    while words:
        raw = base64.urlsafe_b64encode(" ".join(words).encode("utf-8")).decode().rstrip("=")
        if len(PREFIX) + len(raw) <= MAX_PAYLOAD:
            return PREFIX + raw
        words.pop()                                   # drop the last word and try again
    return ""


def decode_search(payload: str | None) -> str | None:
    """The search text, or None for anything that isn't a valid payload from us."""
    if not payload or not payload.startswith(PREFIX) or len(payload) > MAX_PAYLOAD:
        return None
    body = payload[len(PREFIX):]
    try:
        text = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    text = " ".join(text.split())
    return text[:100] or None


ANIME_PREFIX = "ae_"


def encode_anime(sid: str, season: int, episode: int) -> str:
    """Exact stored identity; season zero is an absolute-numbered catalogue season."""
    import re
    if not isinstance(sid, str) or not re.fullmatch(r"[0-9a-f]{12}", sid):
        raise ValueError("Invalid show ID")
    if type(season) is not int or type(episode) is not int or not (0 <= season <= 99999 and 1 <= episode <= 999999):
        raise ValueError("Invalid season or episode")
    return f"{ANIME_PREFIX}{sid}_{season}_{episode}"


def decode_anime(value: str | None) -> tuple[str, int, int] | None:
    import re
    if not isinstance(value, str) or len(value) > MAX_PAYLOAD:
        return None
    m = re.fullmatch(r"ae_([0-9a-f]{12})_(0|[1-9][0-9]{0,4})_([1-9][0-9]{0,5})", value)
    return (m[1], int(m[2]), int(m[3])) if m else None
