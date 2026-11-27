"""Small, explicit registries used by the filename pipeline.

Everything here is *data*: add a line to teach the parser a new uploader
convention, a programme obfuscation or a catalogue correction. Nothing in
this module may be a generic "remove these words everywhere" list - each
rule is anchored to one known convention so real titles are never damaged
(for example "The Lovers" is never touched by the ADrama_Lovers rule).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# 1. Known uploader identities (filename prefixes)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Uploader:
    name: str
    pattern: re.Pattern          # anchored at the start of the filename
    dotted: bool = False         # words in the remainder are separated by dots


def _prefix(handle: str, *, at_required: bool) -> re.Pattern:
    at = r"@" if at_required else r"@?"
    # The handle must be followed by a real separator (dot, underscore, space,
    # dash or an opening bracket) so "@Givenawayx.Title" never matches.
    return re.compile(r"(?i)^\s*" + at + handle + r"(?=[._\s\-\[(])[._\s\-]*")


UPLOADERS: tuple[Uploader, ...] = (
    Uploader("Givenaway", _prefix(r"Givenaway", at_required=True), dotted=True),
    Uploader("MovArchive", _prefix(r"MovArchive", at_required=True), dotted=True),
    Uploader("moviezday", _prefix(r"moviezday", at_required=True), dotted=True),
    # "ADrama_Lovers" (optional @). The whole prefix, including the separator
    # that follows "Lovers", is consumed - and nothing else. "Lovers" alone is
    # never a promo word.
    # "@ADrama_Lovers-Search.WWW.E16.480p..." is a promo that carries NO title; only the prefix goes,
    # so the file stays title-less (unreadable) unless its caption names the series.
    Uploader("ADrama_Lovers-Search", re.compile(r"(?i)^\s*@?ADrama_Lovers-Search\.WWW(?=[._ ])[._ ]+")),
    # "#ADL.E01.You.Are.Beautiful.2009.540p.ADL-DRAMA™" : the tag is the uploader, not the show.
    Uploader("ADL-DRAMA", re.compile(r"(?i)^\s*#ADL(?=[._ ])[._ ]+")),
    # Uploaders whose files read "<handle>.E01.<Title>.480p.WEBDL" / "<handle>_E01_<Title>_720p_WEB_DL".
    Uploader("DramaOST", _prefix(r"DramaOST", at_required=True), dotted=True),
    Uploader("ADrama_Lovers", re.compile(r"(?i)^\s*@?ADrama_Lovers(?=[_. \-])[_. \-]+")),
)


def strip_uploader_prefix(name: str) -> tuple[str, Uploader | None]:
    """Remove a complete known-uploader prefix. Returns (rest, uploader)."""
    for up in UPLOADERS:
        m = up.pattern.match(name)
        if m and name[m.end():].strip():
            return name[m.end():], up
    return name, None


def strip_leading_handle(name: str) -> str:
    """Remove ONE leading unknown ``@handle`` uploader prefix, keeping the title that follows.

    Two conventions exist and are told apart by the separator style of the rest of the name:
      "@korean_series.The.Silent.Sea.E01"  dot/space style: the handle (underscores allowed) ends at the first dot
      "@TVSeries_FC.The World at war"      the same: "FC" belongs to the handle, not the title
      "@MoviezzClub_Big_Time_Rush_S01E01"  underscore style: the handle ends at the first underscore
    A name that is only a handle is left alone.
    """
    m = re.match(r"\s*@(\w+)[. ]+(?=\S)", name)                      # the handle is followed by a dot or space
    if m:
        return name[m.end():]
    m = re.match(r"\s*@([A-Za-z0-9]+)_(?=[A-Za-z0-9])", name)        # underscore style
    if m and "." not in name[:m.end()]:
        return name[m.end():]
    return name


# ---------------------------------------------------------------------------
# 2. Release-group tags that put the episode number BEFORE the title
#    "[AI]08-World's End Harem"  "[AI] 1012 - One Piece"
# ---------------------------------------------------------------------------
NUMBER_FIRST_GROUPS = {"ai": "AI"}


def number_first_pattern() -> re.Pattern:
    tags = "|".join(re.escape(t) for t in NUMBER_FIRST_GROUPS)
    return re.compile(r"(?i)^\s*\[(" + tags + r")\]\s*0*(\d{1,4})\s*[-–]\s*(.+?)\s*$")


# ---------------------------------------------------------------------------
# 3. Obfuscated programme names (de-leeted skeleton -> canonical title)
#    Only applied when the span really contains obfuscation characters.
# ---------------------------------------------------------------------------
OBFUSCATION_MAP = str.maketrans({"0": "o", "1": "i", "3": "e", "@": "a", "$": "s", "4": "a", "5": "s", "!": "i"})

OBFUSCATED_PROGRAMMES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bwwe\s+monday\s+night\s+raw\b"), "WWE Monday Night Raw"),
    (re.compile(r"\bwwe\s+friday\s+night\s+smackdown\b"), "WWE Friday Night SmackDown"),
    (re.compile(r"\bwwe\s+smackdown\b"), "WWE SmackDown"),
    (re.compile(r"\bwwe\s+nxt\b"), "WWE NXT"),
)


def deobfuscate_programme(name: str) -> tuple[str, bool]:
    """Fix leetspeak/obfuscated spellings of KNOWN programmes only.

    "WW3_M0nd@y_N1ght_R@w_2nd_September_2024" -> "WWE Monday Night Raw 2nd September 2024".
    A name that matches no registered programme is returned unchanged.
    """
    flat = re.sub(r"[_.]+", " ", name)                  # 1:1 positions are preserved below
    skeleton = flat.lower().translate(OBFUSCATION_MAP)
    for pat, canonical in OBFUSCATED_PROGRAMMES:
        m = pat.search(skeleton)
        if not m:
            continue
        original_span = flat[m.start():m.end()]
        if original_span.lower() == canonical.lower():   # already clean: leave alone
            return name, False
        return flat[:m.start()] + canonical + flat[m.end():], True
    return name, False


# ---------------------------------------------------------------------------
# 4. Display-title corrections (normalised key -> display form).
#    Identity is unaffected: show_id() normalises, so both forms share one id.
# ---------------------------------------------------------------------------
TITLE_CORRECTIONS = {
    "uc kurus": "Üç Kuruş",
    "bts in the soop": "BTS In the Soop",
}


# ---------------------------------------------------------------------------
# 5. Legacy "SSEE" catalogue corrections: titles whose three-digit numbers
#    are season+episode (412 = S04E12). This is NEVER a parser rule; files
#    for these titles are routed to manual adoption (/adopt ... legacy).
# ---------------------------------------------------------------------------
LEGACY_SSEE_TITLES = {"sabrina", "sabrina the teenage witch"}


# ---------------------------------------------------------------------------
# 6. Programmes that use US month-first numeric dates (02.23.2022)
# ---------------------------------------------------------------------------
US_DATE_CUES = {"aew", "wwe", "wrestling", "raw", "smackdown", "nxt", "dynamite", "rampage",
                "collision", "impact", "tna", "wcw", "ecw", "roh"}


# ---------------------------------------------------------------------------
# 7. Programme abbreviations used by one uploader convention (rewritten into the Turkish "N Bölüm" form)
#    "SRV28-2022_2" = Survivor 2022, episode 28, file part 2;  "SRV52-2022-480" adds a resolution.
# ---------------------------------------------------------------------------
SRV_ABBREVIATION = re.compile(r"(?i)^SRV0*(\d{1,3})-(20\d{2})(?:_(\d))?(?:-(\d{3,4})p?)?$")

# Weekday on which a programme airs (Monday=0). Used ONLY to choose between day-first and month-first
# readings of an ambiguous numeric date such as 08.07.2024 (WWE Raw is a Monday: 8 July, not 7 August).
PROGRAMME_WEEKDAYS = (
    ("monday night raw", 0), ("friday night smackdown", 4), ("smackdown", 4), ("dynamite", 2),
    ("rampage", 4), ("collision", 5), ("nxt", 1), ("raw", 0),
)

# Decorated one-line programme names:  "📂 WWE Monday Night Raw #1624 (08.07.2024) 𝕄𝔸𝕋ℂℍ…"
DECORATED_PROGRAMME = re.compile(
    r"(?i)^[^A-Za-z]*(WWE\s+(?:Monday\s+Night\s+Raw|Friday\s+Night\s+SmackDown|SmackDown|NXT))\s*#?\d*\s*"
    r"\((\d{1,2})[./](\d{1,2})[./]((?:19|20)\d{2})\)")


# ---------------------------------------------------------------------------
# 8. WWE: every WWE programme and event is an episode/special of ONE show titled "WWE"
# ---------------------------------------------------------------------------
WWE_SHOW = "WWE"
WWE_PROGRAMME = re.compile(
    r"(?i)^\s*(?:WWE\s+)?(Monday\s+Night\s+Raw|Raw|Friday\s+Night\s+SmackDown|SmackDown|NXT(?:\s+(?:UK|Level\s+Up|2\.0))?|"
    r"Main\s+Event|Superstars|Saturday\s+Night'?s\s+Main\s+Event)\s*$")
# Premium live events. Names unique to WWE are recognised without the "WWE" word; the others need it.
WWE_PPV = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:WWE[ ._]+)?(Royal[ ._]Rumble|Wrestle[ ._]?Mania|Summer[ ._]?Slam|Survivor[ ._]Series|"
    r"Hall[ ._]of[ ._]Fame|Money[ ._]in[ ._]the[ ._]Bank|Elimination[ ._]Chamber|Backlash|Extreme[ ._]Rules|Hell[ ._]in[ ._]a[ ._]Cell|"
    r"Clash[ ._]at[ ._]the[ ._]Castle|Crown[ ._]Jewel|Night[ ._]of[ ._]Champions|Fastlane|Payback|Judgment[ ._]Day|"
    r"No[ ._]Mercy|Vengeance|Armageddon|Unforgiven|Bad[ ._]Blood|Great[ ._]American[ ._]Bash|King[ ._]and[ ._]Queen[ ._]of[ ._]the[ ._]Ring)"
    r"(?:[ ._]+(?P<year>(?:19|20)\d{2})|[ ._]+(?P<num>\d{1,2})(?![A-Za-z0-9]))?(?:[ ._]+Day[ ._]*(?P<day>\d))?")
WWE_NEEDS_WORD = {"hall of fame", "backlash", "payback", "fastlane", "vengeance", "armageddon", "unforgiven", "bad blood",
                  "no mercy", "judgment day", "extreme rules", "crown jewel", "night of champions", "elimination chamber",
                  "hell in a cell", "great american bash", "clash at the castle", "money in the bank", "survivor series"}
