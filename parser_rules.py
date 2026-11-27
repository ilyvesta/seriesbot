"""Persistent, typed, non-regex parser rules for SeriesBot.

Rules are stored inside the existing MongoDB ``settings`` document under
``parser_rules`` (no new collection, tiny memory footprint). Each rule has an
explicit type and scope so removing a finale marker never damages genuine titles
such as ``Platinum End`` or numbered titles such as ``Mob Psycho 100``.

Rule kinds:
  - ``suffix``    : standalone trailing marker word removed only in release-suffix context
  - ``exception`` : genuine show title protected from suffix stripping and dual-number holds
  - ``numbering`` : explicit episode-number choice for dual-number titles (e.g. Beyblade 330 [132])
  - ``pattern``   : simple placeholder template ({title}, {season}, {ep}, {year}, {ignore})
"""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from normalize import core_key, normalize as _fold


BUILTIN_TITLE_EXCEPTIONS: frozenset[str] = frozenset({
    "platinum end",
    "the end",
    "dead end",
    "dead end paranormal park",
    "world s end harem",
    "worlds end harem",
    "the end of the f ing world",
    "the end of the fucking world",
    "mob psycho 100",
    "the 100",
    "room 104",
    "station 19",
    "brooklyn nine nine",
    "brooklyn 99",
    "9 1 1",
    "911",
    "9 1 1 lone star",
    "halo 4",
    "46 days",
    "1899",
    "1923",
    "1883",
    "24",
    "4400",
    "the 4400",
    "babylon 5",
    "13 reasons why",
    "12 monkeys",
    "3 body problem",
    "86",
    "eighty six 86",
})

DEFAULT_PARSER_RULES: dict[str, Any] = {
    "builtin_end": True,
    "builtin_finale": True,
    "rules": [],
}

_ACTIVE_RULES: dict[str, Any] = copy.deepcopy(DEFAULT_PARSER_RULES)
_VALID_KINDS = frozenset({"suffix", "exception", "numbering", "pattern"})
_PLACEHOLDER_RE = re.compile(r"\{(title|season|ep|year|ignore)\}", re.IGNORECASE)


def normalize_parser_rules(raw: Any) -> dict[str, Any]:
    """Validate and normalise stored parser_rules settings into a clean dict."""
    if not isinstance(raw, dict):
        return copy.deepcopy(DEFAULT_PARSER_RULES)
    out: dict[str, Any] = {
        "builtin_end": bool(raw.get("builtin_end", True)),
        "builtin_finale": bool(raw.get("builtin_finale", True)),
        "rules": [],
    }
    seen: set[str] = set()
    items = raw.get("rules")
    if not isinstance(items, (list, tuple)):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in _VALID_KINDS:
            continue
        if kind in ("suffix", "exception") and not isinstance(item.get("value"), str):
            continue
        if kind == "numbering" and (item.get("pick") not in ("first", "second") or not isinstance(item.get("prefix"), str) or not item["prefix"].strip()):
            continue
        if kind == "pattern":
            try:
                if not isinstance(item.get("template"), str):
                    continue
                compile_template_rule(item["template"])
            except (ValueError, re.error):
                continue
        rid = str(item.get("id") or "").strip()
        if not rid or rid in seen:
            rid = _make_rule_id(kind, str(item.get("value") or item.get("prefix") or item.get("template") or len(seen)))
        seen.add(rid)
        entry = dict(item)
        entry["id"] = rid
        entry["kind"] = kind
        entry["enabled"] = bool(item.get("enabled", True))
        out["rules"].append(entry)
    return out


def _make_rule_id(kind: str, seed: str) -> str:
    h = hashlib.sha1(f"{kind}:{seed.strip().casefold()}".encode("utf-8")).hexdigest()[:8]
    return f"{kind[:3]}_{h}"


def set_active_parser_rules(raw: Any) -> dict[str, Any]:
    """Install the active runtime rules (called on settings load and updates)."""
    global _ACTIVE_RULES
    _ACTIVE_RULES = normalize_parser_rules(raw)
    return _ACTIVE_RULES


def get_active_parser_rules() -> dict[str, Any]:
    return _ACTIVE_RULES


def effective_exceptions(rules: dict[str, Any] | None = None) -> frozenset[str]:
    cfg = normalize_parser_rules(rules) if rules is not None else _ACTIVE_RULES
    extra = set()
    for r in cfg["rules"]:
        if r.get("enabled", True) and r.get("kind") == "exception":
            k = core_key(_fold(str(r.get("value") or "")))
            if k:
                extra.add(k)
    return BUILTIN_TITLE_EXCEPTIONS | frozenset(extra)


def is_protected_title(title: str, rules: dict[str, Any] | None = None) -> bool:
    key = core_key(_fold(title or ""))
    if not key:
        return False
    return key in effective_exceptions(rules)


def custom_suffix_words(rules: dict[str, Any] | None = None) -> list[str]:
    cfg = normalize_parser_rules(rules) if rules is not None else _ACTIVE_RULES
    out: list[str] = []
    for r in cfg["rules"]:
        if r.get("enabled", True) and r.get("kind") == "suffix":
            w = str(r.get("value") or "").strip()
            if w and w.casefold() not in {x.casefold() for x in out}:
                out.append(w)
    return out


def find_numbering_rule(prefix: str, rules: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Look up an enabled dual-number rule matching a show prefix (exact normalised match or '*')."""
    cfg = normalize_parser_rules(rules) if rules is not None else _ACTIVE_RULES
    pk = core_key(_fold(prefix or ""))
    if not pk:
        return None
    wildcard = None
    for r in cfg["rules"]:
        if not r.get("enabled", True) or r.get("kind") != "numbering":
            continue
        rp = str(r.get("prefix") or "").strip()
        if rp == "*":
            wildcard = r
            continue
        if core_key(_fold(rp)) == pk:
            return r
    return wildcard


def compile_template_rule(template: str) -> re.Pattern:
    """Compile a safe placeholder template into an anchored regex.

    Supported placeholders:
      {title}  - show title text
      {season} - 1..2 digit season number
      {ep}     - 1..4 digit episode number
      {year}   - 4 digit release year (1900..2099)
      {ignore} - ignored token/segment (e.g. secondary number or tag)
    """
    tmpl = " ".join(str(template or "").split()).strip()
    if not tmpl:
        raise ValueError("Template cannot be empty.")
    found = [m.group(1).lower() for m in _PLACEHOLDER_RE.finditer(tmpl)]
    if "ep" not in found:
        raise ValueError("Template must include {ep} so the bot knows which number is the episode.")
    if len(found) != len(set(found)) and any(k != "ignore" and found.count(k) > 1 for k in set(found)):
        raise ValueError("Each of {title}, {season}, {ep}, {year} may appear only once.")

    parts: list[str] = []
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(tmpl):
        lit = tmpl[pos:m.start()]
        if lit:
            # Make whitespace flexible across space/dot/underscore separators.
            escaped = re.escape(lit)
            escaped = re.sub(r"\\ ", r"[\\s._]+", escaped)
            parts.append(escaped)
        key = m.group(1).lower()
        if key == "title":
            parts.append(r"(?P<title>[^\[\]()]+?)")
        elif key == "season":
            parts.append(r"0*(?P<season>\d{1,2})")
        elif key == "ep":
            parts.append(r"0*(?P<ep>\d{1,4})")
        elif key == "year":
            parts.append(r"(?P<year>(?:19|20)\d{2})")
        elif key == "ignore":
            parts.append(r"[^\[\]()\s]+?")
        pos = m.end()
    tail = tmpl[pos:]
    if tail:
        escaped = re.escape(tail)
        escaped = re.sub(r"\\ ", r"[\\s._]+", escaped)
        parts.append(escaped)
    rx = r"(?i)^\s*" + "".join(parts) + r"(?:[\s._\-].*)?$"
    return re.compile(rx)


def match_pattern_rules(name: str, rules: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Try enabled admin pattern rules against a cleaned filename stem."""
    cfg = normalize_parser_rules(rules) if rules is not None else _ACTIVE_RULES
    for r in cfg["rules"]:
        if not r.get("enabled", True) or r.get("kind") != "pattern":
            continue
        tmpl = str(r.get("template") or "").strip()
        if not tmpl:
            continue
        try:
            rx = compile_template_rule(tmpl)
        except ValueError:
            continue
        m = rx.match(name)
        if not m:
            continue
        gd = m.groupdict()
        show = str(r.get("show") or gd.get("title") or "").strip(" -–—:|,._")
        if not show:
            continue
        ep = int(gd["ep"])
        if ep <= 0 or (1900 <= ep <= 2099):
            continue
        season = int(gd["season"]) if gd.get("season") else 0
        year = int(gd["year"]) if gd.get("year") else None
        return {
            "rule_id": r["id"],
            "template": tmpl,
            "show": show,
            "season": season,
            "episodes": (ep,),
            "year": year,
        }
    return None


def make_suffix_rule(word: str) -> dict[str, Any]:
    w = " ".join(str(word or "").split()).strip(" -._[]()")
    if not w or len(w) < 2 or len(w) > 30 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 \-]*", w):
        raise ValueError("Suffix must be 2–30 letters/digits (e.g. FINAL, UNCUT, RAW).")
    return {"id": _make_rule_id("suffix", w), "kind": "suffix", "value": w, "enabled": True}


def make_exception_rule(title: str) -> dict[str, Any]:
    t = " ".join(str(title or "").split()).strip()
    if not t or len(t) < 2 or len(t) > 100:
        raise ValueError("Show title exception must be 2–100 characters (e.g. Platinum End).")
    return {"id": _make_rule_id("exception", t), "kind": "exception", "value": t, "enabled": True}


def make_numbering_rule(prefix: str, pick: str, show: str = "") -> dict[str, Any]:
    p = " ".join(str(prefix or "").split()).strip()
    if not p or len(p) > 80:
        raise ValueError("Provide the show prefix (e.g. Beyblade).")
    pk = str(pick or "").strip().lower()
    if pk not in ("first", "second"):
        raise ValueError("Pick must be 'first' (number before brackets) or 'second' (number inside brackets).")
    canonical = " ".join(str(show or "").split()).strip()
    return {
        "id": _make_rule_id("numbering", p),
        "kind": "numbering",
        "prefix": p,
        "pick": pk,
        "show": canonical or ("" if p == "*" else p),
        "enabled": True,
    }


def make_pattern_rule(template: str, show: str = "") -> dict[str, Any]:
    tmpl = " ".join(str(template or "").split()).strip()
    compile_template_rule(tmpl)  # validates placeholders
    canonical = " ".join(str(show or "").split()).strip()
    if "{title}" not in tmpl.lower() and not canonical:
        raise ValueError("Include {title} in the template, or specify a fixed show name after '|'.")
    return {
        "id": _make_rule_id("pattern", tmpl + "|" + canonical),
        "kind": "pattern",
        "template": tmpl,
        "show": canonical,
        "enabled": True,
    }


def describe_rule(r: dict[str, Any]) -> str:
    kind = r.get("kind", "")
    state = "ON" if r.get("enabled", True) else "OFF"
    if kind == "suffix":
        return f"[Suffix · {state}] Ignore trailing '{r.get('value', '')}'"
    if kind == "exception":
        return f"[Exception · {state}] Protect title '{r.get('value', '')}'"
    if kind == "numbering":
        which = "1st number (before brackets)" if r.get("pick") == "first" else "2nd number (inside brackets)"
        target = f" → {r['show']}" if r.get("show") else ""
        return f"[Numbering · {state}] {r.get('prefix', '')} uses {which}{target}"
    if kind == "pattern":
        target = f" → {r['show']}" if r.get("show") else ""
        return f"[Pattern · {state}] {r.get('template', '')}{target}"
    return f"[{kind} · {state}]"
