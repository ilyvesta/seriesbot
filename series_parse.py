"""Series filename parser.

One pass, in a fixed order:
  1. strip the extension, promo text, hash codes and [Group] tags
  2. find the season/episode marker (S01E05, S1 - 24, 1x05, Ep 5, " - 112" ...)
  3. everything before the marker is the title; everything after is metadata

Anything this cannot read confidently returns kind="none" so the file is
skipped (and can be re-checked later) instead of becoming a wrong show.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from normalize import normalize as _fold
from registries import (LEGACY_SSEE_TITLES, NUMBER_FIRST_GROUPS, TITLE_CORRECTIONS, US_DATE_CUES,
                        deobfuscate_programme, number_first_pattern, strip_leading_handle, strip_uploader_prefix,
                        DECORATED_PROGRAMME, PROGRAMME_WEEKDAYS, SRV_ABBREVIATION,
                        WWE_NEEDS_WORD, WWE_PPV, WWE_PROGRAMME, WWE_SHOW)

VIDEO_EXTS = {"mkv", "mp4", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "3gp", "ts", "vob", "divx"}
SUB_EXTS = {"srt", "ass", "ssa", "vtt", "sub"}
_KNOWN_EXTS = VIDEO_EXTS | SUB_EXTS | {"idx", "zip", "rar", "7z", "nfo", "txt"}

_RES_SET = {2160, 1440, 1080, 720, 576, 540, 480, 360, 240}
_SOURCES = {"webdl": "WEB-DL", "web": "WEB", "webrip": "WEBRip", "bluray": "BluRay", "bd": "BluRay",
            "bdrip": "BDRip", "brrip": "BRRip", "hdrip": "HDRip", "hdtv": "HDTV", "dvdrip": "DVDRip",
            "remux": "Remux"}
_CODECS = {"x264": "x264", "h264": "x264", "avc": "x264", "x265": "x265", "h265": "x265",
           "hevc": "x265", "xvid": "XviD", "divx": "XviD"}
_LANGS = {"english": "English", "eng": "English", "hindi": "Hindi", "hin": "Hindi", "french": "French",
          "spanish": "Spanish", "korean": "Korean", "japanese": "Japanese", "jpn": "Japanese",
          "chinese": "Chinese", "tamil": "Tamil", "telugu": "Telugu", "german": "German",
          "italian": "Italian", "russian": "Russian", "arabic": "Arabic", "portuguese": "Portuguese"}
_TAGS = {"dual": "Dual Audio", "multi": "Multi Audio", "multiple": "Multi Sub", "esub": "ESub",
         "esubs": "ESub", "subbed": "ESub", "hdr": "HDR", "hdr10": "HDR", "10bit": "10-bit",
         "dubbed": "Dubbed", "dub": "Dubbed", "uncensored": "Uncensored"}
# Words that belong to release metadata, never to a title.
_TECH = (set(_SOURCES) | set(_CODECS) | set(_LANGS) | set(_TAGS) |
         {"aac", "ac3", "dts", "ddp5", "dd5", "ddp", "atmos", "flac", "opus", "uhd", "hd", "fhd", "sd",
          "4k", "proper", "repack", "extended", "unrated", "remastered", "audio", "subs", "sub",
          "subtitle", "subtitles", "dl", "rip", "mkv", "mp4", "avi", "webm", "batch", "dubs", "bit",
          "8bit", "nf", "amzn", "dsnp", "hmax", "atvp", "hulu", "cr", "sdr", "av1", "vostfr", "multisub",
          "wetv", "iqiyi", "viki", "hbomax", "5_1", "7_1", "2_0", "2_1"})   # streaming platforms & audio channels that appear in release names
_KNOWN_GROUPS = {"galaxyrg", "psa", "rarbg", "yify", "yts", "megusta", "pahe", "phun", "mkvking",
                 "subsplease", "erai-raws", "eraraws", "animerg", "horriblesubs",
                 "anime time", "animetime", "tigole", "qxr", "ntb", "ntg", "ion10", "cakes",
                 "dkb", "sallysubs", "bonkai77", "df68", "planetdizi", "pseudo"}

_PLATFORMS = {"wetv", "iqiyi", "iq", "viu", "netflix", "amazon", "amzn", "nf", "disney", "dsnp", "hulu", "hbo", "hmax",
              "atvp", "appletv", "youku", "viki", "kocowa", "tving", "wavve", "mxplayer", "zee5", "hotstar", "sonyliv",
              "jiocinema", "crunchyroll", "funimation", "hidive", "bilibili", "abema", "tubi", "peacock", "paramount",
              "max", "mubi", "sbs", "kbs", "mbc", "jtbc", "tvn", "ocn", "nhk"}
_PACK_CUE = re.compile(r"(?i)\b(?:complete\s+series|complete\s+season|complete|full\s+season|season\s+pack|full\s+series|batch|collection|all\s+episodes)\b")
_SEASON_RANGE = re.compile(r"(?i)(?:\bseasons?\s*\d{1,2}\s*[-–~]\s*\d{1,2}\b|(?<![A-Za-z0-9])S\s*0*\d{1,2}\s*[-–~]\s*S\s*0*\d{1,2}(?![A-Za-z0-9]))")
_PROMO_WORDS = re.compile(r"(?i)\b(?:join(?:\s+our)?(?:\s+channel)?|downloaded\s+from|uploaded\s+by|powered\s+by|subscribe(?:\s+to)?|channel\s*:)\s*(?:@\w+|t\.me/\S+|telegram\.me/\S+|(?:www\.)?[\w-]{3,}\.(?:com|net|org|xyz|io|cc|ng|tv|me)(?![A-Za-z0-9]))")
_PROMO_HANDLE = re.compile(r"@\w+")      # Telegram usernames: letters, digits, underscore - never dots
_PROMO_TOKEN = re.compile(r"(?i)(?:\bt\.me/\S+|\btelegram\.me/\S+|\bhttps?://\S+|\bwww\.[\w.-]+|(?<![A-Za-z0-9])[\w-]{3,}\.(?:com|net|org|xyz|io|cc|ng)(?![A-Za-z0-9]))")


def _strip_promo_tokens(text: str) -> str:
    """Remove @handles, links and site names.

    Order matters: handles go first (underscores belong to a handle), THEN underscores become spaces,
    THEN site names are matched. Matching a site name while underscores were still word characters let
    "Poldark_S01E08_720p_BLAXUP.COM" be swallowed whole as one 'domain'.
    """
    text = _PROMO_HANDLE.sub(" ", text)
    text = re.sub(r"(?<![A-Za-z0-9])([257])_([01])(?![A-Za-z0-9])", r"\1.\2", text)
    text = text.replace("_", " ")
    return _PROMO_TOKEN.sub(" ", text)


_HASH = re.compile(r"^[0-9A-Fa-f]{8}$")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_RES_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(2160|1440|1080|720|576|540|480|360|240)p(?![A-Za-z0-9])")
_BRACKET = re.compile(r"\[([^\]]*)\]|\(([^)]*)\)|\{([^}]*)\}")

# Season/episode markers, tried in this order.
_S_EP = re.compile(r"(?i)(?<![A-Za-z0-9])[ST]\s*0*(\d{1,2})\s*[ ._-]*E(?:P(?:ISODE)?)?\s*0*(\d{1,4})(?:v\d)?((?:\s*(?:-|–|&|\+|E|and)\s*E?\s*0*\d{1,4}(?:v\d)?)*)(?![A-Za-z0-9])")
_S_EP_TAIL = re.compile(r"(?i)(-|–|&|\+|E|and)\s*E?\s*0*(\d{1,4})")
_S_DASH_EP = re.compile(r"(?i)(?<![A-Za-z0-9])S\s*0*(\d{1,2})\s*[-–:]\s*(?:E(?:P(?:ISODE)?)?\s*)?0*(\d{1,4})(?:v\d)?(?:\s*[-–]\s*0*(\d{1,4}))?(?![A-Za-z0-9])")
_X_EP = re.compile(r"(?i)(?<![A-Za-z0-9])(\d{1,2})\s*[x×]\s*0*(\d{1,4})(?![A-Za-z0-9])")
_YEAR_X_EP = re.compile(r"(?i)(?<![A-Za-z0-9])((?:19|20)\d{2})\s*[x×]\s*0*(\d{1,3})(?![A-Za-z0-9])")   # "El Chavo 1973x16"
_S_S_TYPO = re.compile(r"(?i)(?<![A-Za-z0-9])S0*(\d{1,2})S0*(\d{1,3})(?![A-Za-z0-9])")                  # encoder typo "S01S01" for S01E01
_S_X_EP = re.compile(r"(?i)(?<![A-Za-z0-9])S0*(\d{1,2})X0*(\d{1,3})(?![A-Za-z0-9])")                    # "S03X02" (never "S01 x264")
_NUM_BEFORE_EP = re.compile(r"(?i)(?<![A-Za-z0-9])0*(\d{1,3})\s*(?:серия|серии|серія|episodes?|cap[ií]tulo)(?![A-Za-z0-9\u0400-\u04FF])")
_SCENE_SSEE = re.compile(r"(?i)^(?P<title>.+?)\s+0*(?P<s>\d{1,2})(?P<e>\d{2})\s+(?:hdtv|pdtv|dsr)(?![A-Za-z0-9])")  # house.of.lies.409.hdtv-lol
_SS_EP = re.compile(r"(?i)(?<![A-Za-z0-9])SS0*(\d{1,2})E0*(\d{1,3})(?![A-Za-z0-9])")                       # steven.universe.ss03e01
_S_DOT_EP = re.compile(r"(?i)(?<![A-Za-z0-9])S0*(\d{1,2})\.0*(\d{1,3})(?![A-Za-z0-9]|\.\d)\.?")                         # "S4.01. Attack On Titan", "S4.16 Attack On Titan"
_S_DASH_SPACE_EP = re.compile(r"(?i)(?<![A-Za-z0-9])S0*(\d{1,2})-\s+0*(\d{1,3})(?![A-Za-z0-9])")          # "S2- 5 Too Hot To Handle"
_FILE_PART = re.compile(r"(?i)[._ -]*(?:mp4|mkv|avi)?[._ -]*part[._ -]*(\d{1,6})(?:\.(?:mp4|mkv|avi))?$")   # ".mp4_PART_00001", " PART-2", ".part001"
_CAP_PART = re.compile(r"(?i)(cap[ií]tulo|capitulo)\s*0*(\d{1,3})\s*p(\d)(?![A-Za-z0-9])")                  # "capítulo 08p2" = chapter 8 part 2
_SEASON_NAMES = "seasons?|staffel|saison|temporada|stagione|seizoen|sezon|säsong|сезон|sæson|kausi"
_EP_NAMES = "episodes?|eps?|folge|épisode|episodio|episódio|capitulo|capítulo|cap|puntata|aflevering|bölüm|bolum|odcinek|avsnitt|серия|jakso"
_SEASON_WORD = re.compile(r"(?i)(?<![A-Za-z0-9])(?:%s)\.?\s*0*(\d{1,2})(?![A-Za-z0-9])" % _SEASON_NAMES)
_S_ONLY = re.compile(r"(?i)(?<![A-Za-z0-9])S\s*0*(\d{1,2})(?![A-Za-z0-9])")
_EP_WORD = re.compile(r"(?i)(?<![A-Za-z0-9])(?:%s|e)[-.]?\s*0*(\d{1,4})(?:v\d)?(?:(?:\s*[-–]\s*E?|E)\s*0*(\d{1,4}))?(?![A-Za-z0-9])" % _EP_NAMES)
_DATE_YMD = re.compile(r"(?<!\d)((?:19|20)\d{2})[ ._/-](0?[1-9]|1[0-2])[ ._/-](0?[1-9]|[12]\d|3[01])(?!\d)")
_DATE_DMY = re.compile(r"(?<!\d)(0?[1-9]|[12]\d|3[01])[ ._/-](0?[1-9]|1[0-2])[ ._/-]((?:19|20)\d{2})(?!\d)")
_HASH_EP = re.compile(r"(?<![A-Za-z0-9])#\s*0*(\d{1,4})(?![A-Za-z0-9])")
_SPECIAL = re.compile(r"(?i)(?<![A-Za-z0-9])(OVA|OAD|ONA|SPECIALS?|SP|EXTRAS?)\s*[-._ ]*0*(\d{1,3})(?:\s*[&+]\s*0*(\d{1,3}))?(?![A-Za-z0-9])")
_PART = re.compile(r"(?i)(?<![A-Za-z0-9])(Part|Cour)\s*0*(\d{1,2})(?![A-Za-z0-9])")
# "OVA 02" is handled by _SPECIAL; "00 [OVA]" / "00 OVA" (number first) here.
_SPECIAL_AFTER = re.compile(r"(?i)(?<![A-Za-z0-9])0*(\d{1,3})\s*[-._ ]*(OVA|OAD|ONA|SPECIALS?|SP)(?![A-Za-z0-9])")
_SPECIAL_BARE = re.compile(r"(?i)^(OVA|OAD|ONA|SPECIAL|SPECIALS|SP)$")
# Turkish: "32 Bölüm" (number BEFORE the word), optionally "5 Parça" / "Parça 5" (file part).
_TR_PART = r"(?:\s*[-–]?\s*(?:0*(\d{1,2})\.?\s*par[cç]a|par[cç]a\s*0*(\d{1,2}))(?![A-Za-z0-9]))?"
_TR_EP = re.compile(r"(?i)(?<![A-Za-z0-9])0*(\d{1,4})\.?\s*b[öo]l[üu]m(?![A-Za-z0-9])" + _TR_PART)
_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
           "september": 9, "october": 10, "november": 11, "december": 12, "jan": 1, "feb": 2, "mar": 3,
           "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_TEXT_DMY = re.compile(r"(?i)(?<![A-Za-z0-9])(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?[ ._,-]+(" + _MONTH_RE + r")[ ._,-]+((?:19|20)\d{2})(?!\d)")
_DATE_TEXT_MDY = re.compile(r"(?i)(?<![A-Za-z0-9])(" + _MONTH_RE + r")[ ._,-]+(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?[ ._,-]+((?:19|20)\d{2})(?!\d)")
_DATE_MDY = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])[ ._/-](0?[1-9]|[12]\d|3[01])[ ._/-]((?:19|20)\d{2})(?!\d)")
_LEGACY_CANDIDATE = re.compile(r"(?i)^(?P<title>[^\d]+?)[. ]+0*(?P<n>\d{3,4})(?=[. ]|$)")


@dataclass
class SeriesParsed:
    raw: str
    kind: str
    show: str = ""
    year: int | None = None
    season: int = 0
    episodes: tuple[int, ...] = ()
    absolute: bool = False
    ep_title: str = ""
    special: bool = False
    special_label: str = ""
    resolution: str | None = None
    source: str | None = None
    codec: str | None = None
    languages: tuple[str, ...] = ()
    group: str | None = None
    tags: tuple[str, ...] = ()
    ext: str | None = None
    part: int | None = None          # file part of one episode (Turkish "5 Parça"); NOT an episode
    hint: str = ""                   # non-empty on kind="none" results that need a human (e.g. manual_adoption_candidate)
    uploader: str | None = None      # known uploader identity removed from the filename
    explicit: bool = False           # an unambiguous S01E01 / 1x01 marker was found (so a short acronym title is trustworthy)
    from_caption: bool = False       # the result came from the post caption, not the filename

    @property
    def first(self): return self.episodes[0] if self.episodes else None
    @property
    def last(self): return self.episodes[-1] if self.episodes else None



GENERIC_MEDIA_NAMES = {"video", "vid", "document", "file", "audio", "photo", "img", "dsc", "pxl",
                       "screenshot", "recording", "untitled", "download", "new", "movie", "film"}

# Files that can never be an episode, even with S01E01 in the name (subtitles, audio, images, text).
NON_INDEXABLE_EXTS = frozenset("srt ass ssa sub vtt idx txt nfo flac mp3 aac m4a wav ogg opus jpg jpeg png gif pdf epub exe apk".split())


def is_generic_media_name(value: str) -> bool:
    """Recognise generic device/app filenames, allowing trailing release metadata."""
    text = str(value or "").strip()
    text = re.sub(r"(?i)\.[a-z0-9]{2,5}$", "", text)
    # Bracketed quality/codec labels are metadata, not part of the base name.
    text = re.sub(r"[\[({][^\])}]*[\])}]", " ", text)
    tokens = [t for t in re.split(r"[\s._-]+", text) if t]
    while tokens:
        token = tokens[-1].strip("[](){}.,")
        low = token.casefold()
        if (_RES_RE.fullmatch(token) or low in _TECH or _tok(low) in _TECH
                or low in _KNOWN_GROUPS or _tok(low) in _KNOWN_GROUPS
                or _tok(low) in _SOURCES or _tok(low) in _CODECS
                or re.fullmatch(r"(?:ddp?|aac|ac3|dts)\d?(?:\.\d)?", low)
                or re.fullmatch(r"\d+(?:\.\d+)?(?:kb|mb|gb|tb)", low)):
            tokens.pop()
            continue
        break
    base = " ".join(tokens).strip()
    return bool(re.fullmatch(r"(?i)(" + "|".join(re.escape(x) for x in sorted(GENERIC_MEDIA_NAMES)) + r")\s+\d+", base))

_KEEP_CAPS_MAX_LETTERS = 5
_SMALL = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "vs", "with", "no"}


def _smart_case(title: str) -> str:
    """Fix ALL-CAPS and all-lowercase names; leave mixed case alone."""
    if title != title.lower() and title != title.upper():
        return title
    # Short all-caps titles are acronyms as released (NCIS, FBI, CSI, SWAT): keep them as typed.
    # Only an ALL-CAPS title qualifies (an all-lowercase "ncis" is still title-cased), and the
    # limit counts letters across the whole title, so longer shouty names (BLEACH, ALL OF US) are fixed.
    if title == title.upper() and len(re.findall(r"[^\W\d_]", title)) <= _KEEP_CAPS_MAX_LETTERS:
        return title
    out = []
    for i, w in enumerate(title.split()):
        lw = w.lower()
        out.append(lw if (i > 0 and lw in _SMALL) else "-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")))
    return " ".join(out)


def strip_series_promos(value: str) -> str:
    """Remove channel ads (@handles, links, 'Join ...') from a filename or caption line."""
    text = (value or "").strip()
    text = _PROMO_WORDS.sub(" ", text)
    text = _strip_promo_tokens(text)
    text = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -–—|:,.")


def _tok(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _is_tech_token(t: str) -> bool:
    low = t.lower().strip("-–—.,")
    if not low: return True
    if _RES_RE.fullmatch(low) or low in _TECH or _tok(low) in _TECH or _tok(low) in _SOURCES or _tok(low) in _CODECS:
        return True
    if re.fullmatch(r"\d+(?:\.\d+)?(?:kb|mb|gb|tb)", low): return True
    if re.fullmatch(r"(?:ddp?|aac|ac3|dts)\d?(?:\.\d)?", low): return True
    if re.fullmatch(r"\d(?:\.\d)", low): return True        # 5.1 / 2.0 audio
    return False


def _nums(a, b=None):
    a = int(a)
    if b is None: return [a]
    b = int(b)
    return list(range(a, b + 1)) if a < b <= a + 12 else [a, b] if a != b else [a]


def _s_ep_numbers(first, tail) -> list[int]:
    """Episodes of an S01E01 marker: "-E03" is a range; "E01E02E03" / "&" / "+" are explicit lists."""
    seps = _S_EP_TAIL.findall(tail or "")
    if not seps:
        return [int(first)]
    if len(seps) == 1 and seps[0][0] in "-–":
        return _nums(first, seps[0][1])
    return sorted({int(first), *(int(n) for _, n in seps)})


def canonical_title(title: str) -> str:
    """Display-form correction from the registry; identity is unchanged (ids are normalised)."""
    return TITLE_CORRECTIONS.get(_fold(title), title)


def _find_date(text: str):
    """Broadcast date in the text -> (span, year, month, day) or None. Handles 2026.09.16, 31.01.2025,
    US 02.23.2022 (only for US programmes) and textual dates such as '2nd September 2024'."""
    import datetime as _dt
    cands = []
    m = _DATE_YMD.search(text)
    if m: cands.append((m, int(m.group(1)), int(m.group(2)), int(m.group(3))))
    m = _DATE_TEXT_DMY.search(text)
    if m: cands.append((m, int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))))
    m = _DATE_TEXT_MDY.search(text)
    if m: cands.append((m, int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))))
    us = bool(set(re.findall(r"[a-z]+", text.lower())) & US_DATE_CUES)
    weekday = next((wd for key, wd in PROGRAMME_WEEKDAYS if key in text.lower()), None)
    numeric = []
    d = _DATE_DMY.search(text)
    if d: numeric.append((d, int(d.group(3)), int(d.group(2)), int(d.group(1))))
    u = _DATE_MDY.search(text)
    if u: numeric.append((u, int(u.group(3)), int(u.group(1)), int(u.group(2))))
    if weekday is not None and len(numeric) == 2:
        # Both readings are valid dates (08.07.2024): the programme's air day decides (Raw is a Monday -> 8 July).
        ok = []
        for cand in numeric:
            try:
                if _dt.date(cand[1], cand[2], cand[3]).weekday() == weekday: ok.append(cand)
            except ValueError:
                pass
        if len(ok) == 1:
            numeric = ok
        elif us:
            numeric.reverse()
    elif us:
        numeric.reverse()                      # month-first wins for US programmes
    for m, y, mo, dd in cands + numeric:
        try:
            _dt.date(y, mo, dd)
        except ValueError:
            continue
        return m.span(), y, mo, dd
    return None


def _meta(text: str):
    res = None; source = None; codec = None; langs = []; tags = []
    rm = _RES_RE.search(text)
    if rm: res = rm.group(1) + "p"
    elif re.search(r"(?i)\b4k\b", text): res = "2160p"
    flat = re.sub(r"(?i)web[\s._-]?dl", "webdl", text)
    flat = re.sub(r"(?i)blu[\s._-]?ray", "bluray", flat)
    for w in re.split(r"[^A-Za-z0-9]+", flat):
        lw = w.lower()
        if not lw: continue
        if lw in _SOURCES and not source: source = _SOURCES[lw]
        elif lw in _CODECS and not codec: codec = _CODECS[lw]
        elif lw in _LANGS and _LANGS[lw] not in langs: langs.append(_LANGS[lw])
        elif lw in _TAGS and _TAGS[lw] not in tags: tags.append(_TAGS[lw])
    if re.search(r"(?i)multi(?:ple)?[\s._-]*sub", text):
        tags = [t for t in tags if t != "Multi Audio"]
        if "Multi Sub" not in tags: tags.append("Multi Sub")
    return res, source, codec, tuple(langs), tuple(tags[:3])


def _classify_bracket(inner: str, at_start: bool):
    """Return (kind, payload). kinds: drop, tag, tech, year, group, keep."""
    s = inner.strip()
    if not s: return "drop", None
    if _PROMO_HANDLE.search(s) or _PROMO_TOKEN.search(s) or _PROMO_WORDS.search(s): return "drop", None
    if _HASH.fullmatch(s) and re.search(r"\d", s) and re.search(r"[A-Fa-f]", s): return "drop", None
    if _YEAR.fullmatch(s): return "year", int(s)
    if _SPECIAL_BARE.fullmatch(s): return "tag", s.upper()
    m_s = re.fullmatch(r"(?i)S[-._\s]*0*(\d{1,2})", s)
    if m_s: return "tag", f"S{int(m_s.group(1)):02d}"
    m_e = re.fullmatch(r"(?i)E(?:P(?:ISODE)?)?[-._\s]*0*(\d{1,4})", s)
    if m_e: return "tag", f"E{int(m_e.group(1)):02d}"
    if re.fullmatch(r"(?i)(?:S[-._\s]*\d{1,2}.*|E(?:P(?:ISODE)?)?[-._\s]*\d{1,4}.*|\d{1,2}x\d{2,4}|\d{1,4}|(?:OVA|OAD|ONA|SP|SPECIAL)\s*\d{1,3}|(?:" + _SEASON_NAMES + r")\s*\d{1,2}.*|(?:" + _EP_NAMES + r")\s*\d{1,4}.*)", s):
        return "tag", s
    words = [w for w in re.split(r"[\s,._+/-]+", s) if w]
    if _RES_RE.search(s) or (words and all(_is_tech_token(w) or _tok(w) in ("multiple", "subtitle", "subtitles", "sub", "subs", "bd", "web", "dual", "audio") for w in words)):
        return "tech", s
    if any(_RES_RE.fullmatch(w) or _tok(w) in _TECH for w in words) and len(words) <= 6:
        return "tech", s
    if at_start: return "group", s
    if re.fullmatch(r"[A-Z]{2,4}", s): return "keep", s      # (US) / (UK) country tags
    return "drop", None


def parse_series(raw: str) -> SeriesParsed:
    raw = (raw or "").strip()
    name = unicodedata.normalize("NFC", raw)
    ext = None
    m = re.search(r"\.([A-Za-z0-9]{2,4})$", name)
    if m and m.group(1).lower() in _KNOWN_EXTS:
        ext = m.group(1).upper(); name = name[:m.start()]
    tr_part = None; ab_label = None
    pf0 = _FILE_PART.search(name)
    if pf0 and re.search(r"[A-Za-z]{3,}", name[:pf0.start()]):      # "...Full.Gear_HD.mp4_PART_00001", "... PART-2", ".part001"
        digits = pf0.group(1)
        tr_part = int(digits) + (1 if len(digits) >= 5 else 0)       # 00000-based splitter output starts at part 0
        ab_label = f"Part {tr_part}"
        name = name[:pf0.start()]
    em_ = re.search(r"(?i)\.(mkv|mp4|avi|mov|wmv|flv|webm|m4v|mpg|mpeg)(?![A-Za-z0-9])", name)
    if em_ and em_.end() < len(name):            # "Show.S01E01.720p.mkv ▓▓ Join Group ▓▓" -> drop the trailer
        ext = em_.group(1).upper(); name = name[:em_.start()]
    # --- filename normalisation: uploader identity, obfuscation and number-first release groups.
    # This runs BEFORE promo stripping and episode extraction, because the generic @handle
    # remover would otherwise eat the title ("@Givenaway.Coroner.[S01E03]").
    uploader = None; group_hint = None; keep_year = False
    name, _up = strip_uploader_prefix(name)
    if _up:
        uploader = _up.name
        if _up.dotted:
            name = re.sub(r"(?<=[^\s.])\.(?=[^\s.])", " ", name)
    name, _obf = deobfuscate_programme(name)
    if uploader == "ADL-DRAMA":                       # "E16.END.<title>.2009.540p.ADL-DRAMA™": END and the group tag are not title
        name = re.sub(r"(?i)(?<=\d)[._ ]END(?=[._ ])", " ", name)
        name = re.sub(r"(?i)[._ -]*ADL-DRAMA\W*$", "", name)
    # second extension left behind by uploaders: "show-s01e03.mkv.mkv"
    m2 = re.search(r"(?i)\.(mkv|mp4|avi|mov|webm|m4v|mpg)$", name)
    if m2:
        ext = ext or m2.group(1).upper(); name = name[:m2.start()]
    # slug-style names: "superman-and-louis-season-1-episode-3-720p"
    if " " not in name and "_" not in name and "." not in name and name.count("-") >= 3:
        name = name.replace("-", " ")
    # decorated one-line programme names (WWE) -> "<programme> DD.MM.YYYY"; the weekday later picks day/month order
    dm = DECORATED_PROGRAMME.match(name)
    if dm:
        name = f"{dm.group(1)} {dm.group(2)}.{dm.group(3)}.{dm.group(4)}"
    # "SRV28-2022_2" -> "Survivor 2022 28 Bölüm 2 Parça"
    sv = SRV_ABBREVIATION.match(name)
    if sv:
        name = f"Survivor {sv.group(2)} {int(sv.group(1))} Bölüm" + (f" {sv.group(3)} Parça" if sv.group(3) else "") + (f" {sv.group(4)}p" if sv.group(4) else "")
    # "=ENG_SUB=與君初相識_The_Blue_Whisper_I_01_迪麗熱巴": drop the =tag= and CJK words, keep the Latin title + number
    if name.startswith("="):
        name = re.sub(r"^=[^=]*=", "", name)
        name = re.sub(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+", " ", name)
        et = re.match(r"(?s)\s*(.+?)[_ ]+0*(\d{1,3})(?:[_ ].*)?$", name)         # drop cast/channel words after the number
        if et and re.search(r"[A-Za-z]{3,}", et.group(1)):
            name = f"{et.group(1).replace('_', ' ').strip()} - {int(et.group(2))}"
    # "capítulo 08p2" -> chapter 8, file part 2
    cp = _CAP_PART.search(name)
    if cp:
        name = name[:cp.start()] + f"Capitulo {int(cp.group(2))}" + name[cp.end():]
        tr_part = int(cp.group(3)); ab_label = f"Part {tr_part}"
    # French release "01_La_Sorciere_..._Saison": number first, season word LAST and without a number
    fr = re.match(r"(?is)^\s*0*(\d{1,3})[_ ](.+?)[_ ](?:Saison|Season|Staffel|Temporada|Stagione)\s*$", name)
    if fr and re.search(r"[A-Za-z]{3,}", fr.group(2)):
        name = f"{fr.group(2).replace('_', ' ')} - {int(fr.group(1))}"
    # "Ep-01 [480p] The Strongest Sage ..." : title AFTER the marker, accepted only with a resolution bracket
    vr = re.match(r"(?is)^\s*(?:\[[^\]]*\]\s*)?Ver[ _]+Cap[ií]tulo[ _]+0*(\d{1,3})[ _]+(.+?)[ _]+Online\b.*$", name)
    if vr:                                              # "[848x480] Ver Capitulo 28 Las Amazonas Online Gratis"
        name = f"{vr.group(2).replace('_', ' ').strip()} - {int(vr.group(1))}"
    pf = _FILE_PART.search(name)
    if pf and re.search(r"[A-Za-z]{3,}", name[:pf.start()]):      # "...Full.Gear_HD.mp4_PART_00001", "... PART-2", ".part001"
        digits = pf.group(1)
        tr_part = int(digits) + (1 if len(digits) >= 5 else 0)     # 00000-based splitter output starts at part 0
        ab_label = f"Part {tr_part}"
        name = name[:pf.start()]
    ep1 = re.match(r"(?is)^\s*(?:Ep|E|Episode)[-. ]?0*(\d{1,3})\s*(\[\s*\d{3,4}p\s*\])\s*(?P<t>[^\[\]]*[A-Za-z]{3,}[^\[\]]*)$", name)
    if ep1:
        name = f"{ep1.group('t').strip()} - {int(ep1.group(1))} {ep1.group(2)}"
    # Anime uploads that put the number first WITHOUT a group tag: "07 - Demon Slayer [1080p] [Sub] @Animax_Industry".
    # Only trusted when the name carries BOTH a resolution bracket and a language/sub bracket; a bare
    # "14 - Transformation" stays unidentified.
    _nb = re.match(r"(?is)^\s*0*(\d{1,4})\s*-\s*(?P<title>[^\[\]]*?[A-Za-z][^\[\]]*?)\s*(?P<tags>(?:\[[^\]]*\]\s*)+)(?:@\w+)?\s*$", name)
    if _nb and re.search(r"(?i)\b(?:2160|1440|1080|720|576|540|480|360)p\b", _nb.group("tags")) \
            and re.search(r"(?i)\[[^\]]*\b(?:sub|subs|dub|dubbed|dual|eng|english)\b[^\]]*\]", _nb.group("tags")):
        name = f"{_nb.group('title').strip()} - {int(_nb.group(1))} {_nb.group('tags').strip()}"
    _nf = number_first_pattern().match(name)
    if _nf:                                    # "[AI] 08 - Title" -> "Title - 8" (absolute number)
        group_hint = NUMBER_FIRST_GROUPS[_nf.group(1).lower()]
        name = f"{_nf.group(3)} - {int(_nf.group(2))}"
    # Older Turkish-drama uploads encode the episode number immediately before
    # a Telegram handle: "Esaret 1-@PlanetDizi.mkv" or
    # "Esaret 2 -@PlanetDizi.mkv". Preserve the number as an episode marker
    # before removing the handle, or the remaining bare number is ambiguous.
    # Telegram uploader captions sometimes append the literal word "telegram"
    # after the handle: "Emanet 232-@PlanetDizi telegram". Treat that as
    # distribution noise, while preserving the episode number before stripping
    # the handle itself.
    handle_episode = re.match(
        r"(?is)^(?P<title>.+?)\s+(?P<episode>0*\d{1,4})\s*[-–—|:,]\s*@(?P<handle>[A-Za-z0-9_.]+)(?:\s+telegram)?$",
        name,
    )
    if handle_episode and handle_episode.group("title").strip():
        name = f"{handle_episode.group('title').strip()} - {int(handle_episode.group('episode'))}"
    name = _PROMO_WORDS.sub(" ", name)
    # Some Asian-drama/anime uploaders prefix filenames as
    # "@Uploader.E01.Show.Title...". The generic @handle expression accepts
    # dots, so it would otherwise consume the episode marker and much of the
    # actual title (especially titles containing apostrophes). Preserve the
    # episode/title portion by removing only the leading uploader handle when
    # an episode marker immediately follows it.
    name = re.sub(
        r"(?i)^\s*@[^.\s]+(?=\.E(?:P(?:ISODE)?)?\d{1,4}\.)",
        "",
        name,
        count=1,
    )
    name = strip_leading_handle(name)
    name = _strip_promo_tokens(name)
    name = re.sub(r"(?i)(?<![A-Za-z0-9])WEB[ ._]DL(?![A-Za-z0-9])", "WEB-DL", name)
    # a known release group AFTER release metadata ("... 1080p x265 pseudo") is not part of the title or the number
    name = re.sub(r"(?i)((?:x26[45]|h26[45]|hevc|2160p|1080p|720p|576p|540p|480p|10bit|bluray|webrip|web-dl|hdtv|dvdrip|aac\d?)[\s._-]+)(?:"
                  + "|".join(re.escape(g) for g in sorted(_KNOWN_GROUPS, key=len, reverse=True)) + r")\s*$", r"\1", name)   # "WEB_DL" is one release token
    # Protect "S4.16." / "S4.16" episode markers before dot-to-space normalization turns them into "S4 16".
    # Guard against resolutions (S01.720p), codecs (S01.265), audio channels (S01.5.1), and years.
    def _keep_s_dot_ep(m):
        ep_num = int(m.group(2))
        if ep_num in _RES_SET or ep_num in (264, 265) or (1900 <= ep_num <= 2099):
            return m.group(0)
        return f"S{int(m.group(1)):02d}E{ep_num:02d} "
    name = re.sub(r"(?i)(?<![A-Za-z0-9])S0*(\d{1,2})\.0*(\d{1,3})(?![A-Za-z0-9]|\.\d)\s*\.?\s*", _keep_s_dot_ep, name)
    # Dots as word separators ("Show.Name.S01E01.720p") but not in "Mr. Robot S01E01" or "5.1"/"2.0" audio.
    if len(re.findall(r"(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])", name)) >= 2:
        def _dot_sep(m):
            i = m.start()
            if i > 0 and i + 1 < len(name) and name[i - 1] in "257" and name[i + 1] in "01":
                prev_ok = (i == 1 or not name[i - 2].isalnum())
                next_ok = (i + 2 == len(name) or not name[i + 2].isalnum())
                if prev_ok and next_ok:
                    return "."
            return " "
        name = re.sub(r"(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])", _dot_sep, name)

    # --- brackets: classify, extract, and remove ---------------------------------
    year = None; group = None; tech_parts = []; pieces = []; last = 0; first_text = None
    for bm in _BRACKET.finditer(name):
        pieces.append(name[last:bm.start()])
        inner = next(g for g in bm.groups() if g is not None)
        at_start = not "".join(pieces).strip()
        kind, payload = _classify_bracket(inner, at_start)
        if kind == "tag": pieces.append(f" - {payload} " if re.fullmatch(r"\d{1,4}", payload) else f" {payload} ")
        elif kind == "keep": pieces.append(f" ({payload}) ")
        elif kind == "year":
            year = year or payload; pieces.append(" ")
        elif kind == "tech": tech_parts.append(payload); pieces.append(" ")
        elif kind == "group":
            if group is None and len(payload) <= 30: group = payload.strip()
            pieces.append(" ")
        else: pieces.append(" ")
        last = bm.end()
    pieces.append(name[last:])
    text = re.sub(r"\s+", " ", "".join(pieces)).strip()

    # Leading unbracketed release group from the known list ("Judas Naruto - S01E24").
    low = text.lower()
    for g in sorted(_KNOWN_GROUPS, key=len, reverse=True):
        if low.startswith(g + " ") or low.startswith(g + " -"):
            group = group or text[:len(g)]
            text = text[len(g):].lstrip(" -–—:|"); low = text.lower(); break

    # --- locate the season / episode marker ------------------------------------
    season = None; episodes: list[int] = []; spans = []
    absolute = False; special = False; special_label = ""; part_label = ""

    def take(match, s, eps):
        nonlocal season, episodes
        season = s; episodes = eps; spans.append(match.span())

    mm = _S_EP.search(text)
    if mm: take(mm, int(mm.group(1)), _s_ep_numbers(mm.group(2), mm.group(3)))
    if not episodes:
        mm = _S_DASH_EP.search(text)
        if mm and int(mm.group(2)) not in _RES_SET and not (1900 <= int(mm.group(2)) <= 2099) \
                and not re.match(r"(?i)\s*S\s*\d{1,2}\s*[-–]\s*S", mm.group(0)):
            take(mm, int(mm.group(1)), _nums(mm.group(2), mm.group(3)))
    if not episodes:
        mm = _X_EP.search(text)
        if mm and int(mm.group(1)) < 100 and int(mm.group(2)) not in _RES_SET and int(mm.group(2)) not in (264, 265):
            take(mm, int(mm.group(1)), [int(mm.group(2))])
    if not episodes:
        mm = _YEAR_X_EP.search(text)
        if mm: take(mm, int(mm.group(1)), [int(mm.group(2))])
    if not episodes:
        mm = _S_S_TYPO.search(text)
        if mm: take(mm, int(mm.group(1)), [int(mm.group(2))])
    if not episodes:
        mm = _S_X_EP.search(text)
        if mm and int(mm.group(2)) not in (264, 265):          # S01X265 is a codec, not an episode
            take(mm, int(mm.group(1)), [int(mm.group(2))])
    if not episodes:
        mm = _SS_EP.search(text)
        if mm: take(mm, int(mm.group(1)), [int(mm.group(2))])
    if not episodes:
        mm = _S_DOT_EP.search(text)
        if mm and int(mm.group(2)) not in _RES_SET and int(mm.group(2)) not in (264, 265):
            take(mm, int(mm.group(1)), [int(mm.group(2))])
    if not episodes:
        mm = _S_DASH_SPACE_EP.search(text)
        if mm and int(mm.group(2)) not in _RES_SET: take(mm, int(mm.group(1)), [int(mm.group(2))])
    if not episodes:
        sm = _SCENE_SSEE.match(text)                           # TV-capture sources only (hdtv/pdtv/dsr): 409 = S04E09
        if sm and 1 <= int(sm.group("s")) <= 30 and int(sm.group("e")) >= 1:
            season = int(sm.group("s")); episodes = [int(sm.group("e"))]; spans.append((sm.end("title"), sm.end()))
    if not episodes:
        tm = _TR_EP.search(text)               # "Kalp Yarası 32 Bölüm [5 Parça]"
        if tm:
            episodes = [int(tm.group(1))]; spans.append(tm.span()); keep_year = True
            pn = tm.group(2) or tm.group(3)
            tr_part = int(pn) if pn else None
    if not episodes:
        nb = _NUM_BEFORE_EP.search(text)                       # "Дочери Гюнеш 1 серия", "Call My Agent 8 episode"
        if nb and (re.match(r"\s*[-.]?\s*\d", text[nb.end():]) or re.search(r"(?i)(?:%s)\.?\s*$" % _SEASON_NAMES, text[:nb.start()])):
            nb = None                                          # "Season 2 Episode 7": the 2 belongs to Season
        if nb and text[:nb.start()].strip(" -–—:|,._()"):
            episodes = [int(nb.group(1))]; spans.append(nb.span())
    if not episodes and not _SEASON_RANGE.search(text):
        sw = _SEASON_WORD.search(text)
        if sw:
            em = _EP_WORD.search(text, sw.end())
            if em:
                season = int(sw.group(1)); episodes = _nums(em.group(1), em.group(2)); spans += [sw.span(), em.span()]
            else:
                dm = re.match(r"\s*[-–:]\s*0*(\d{1,4})(?![A-Za-z0-9])", text[sw.end():])
                if dm and not re.match(r"\s*[-–]\s*\d{1,2}\s*[-–]", text[sw.end():]):
                    season = int(sw.group(1)); episodes = [int(dm.group(1))]
                    spans += [sw.span(), (sw.end(), sw.end() + dm.end())]
    if not episodes:
        sp = _SPECIAL.search(text)
        if sp:
            kw = sp.group(1).upper()
            kw = "SPECIAL" if kw.startswith("SPECIAL") else "EXTRA" if kw.startswith("EXTRA") else kw
            special = True; special_label = f"{kw} {int(sp.group(2)):02d}" + (f"&{int(sp.group(3)):02d}" if sp.group(3) else "")
            episodes = [int(sp.group(2))] + ([int(sp.group(3))] if sp.group(3) else []); spans.append(sp.span())
    if not episodes:
        sa = _SPECIAL_AFTER.search(text)       # "Show - 00 [OVA]": keep the special designation
        if sa:
            kw = sa.group(2).upper()
            kw = "SPECIAL" if kw.startswith("SPECIAL") else kw
            special = True; special_label = f"{kw} {int(sa.group(1)):02d}"
            episodes = [int(sa.group(1))]; spans.append(sa.span())
    if not episodes:
        mv = re.search(r"(?i)(?<![A-Za-z0-9])(?:movie|film|gekijouban)[\s._-]*0*(\d{1,2})(?![A-Za-z0-9])", text)
        if mv and text[:mv.start()].strip(" -–—:|,._()[]"):      # "One Piece - Movie 14": a movie of the show, listed with its specials
            special = True; special_label = f"Movie {int(mv.group(1)):02d}"
            episodes = [int(mv.group(1))]; spans.append(mv.span())
    if not episodes:
        em = _EP_WORD.search(text)
        if em: episodes = _nums(em.group(1), em.group(2)); spans.append(em.span())
    if not episodes:
        hm = _HASH_EP.search(text)
        if hm: episodes = [int(hm.group(1))]; spans.append(hm.span())
    date_label = ""
    if not episodes:
        found = _find_date(text)
        if found:
            span, y, mo, d = found
            season = y; episodes = [mo * 100 + d]; spans.append(span); date_label = f"{y}-{mo:02d}-{d:02d}"
    if not episodes and not _SEASON_RANGE.search(text) and not _PACK_CUE.search(text):
        # Legacy "SSEE" numbers (sabrina.412) are a catalogue correction, never a parser rule:
        # only titles in the explicit registry are held for /adopt; "One Piece 411" is unaffected.
        lm = _LEGACY_CANDIDATE.match(text)
        if lm and int(lm.group("n")) < 1000 and _fold(lm.group("title")) in LEGACY_SSEE_TITLES:
            return SeriesParsed(raw=raw, kind="none", show=canonical_title(_smart_case(lm.group("title").strip(" .-_"))),
                                episodes=(int(lm.group("n")),), absolute=True, hint="manual_adoption_candidate",
                                ext=ext, uploader=uploader)
    _season_or_part_spans = [m.span() for m in _SEASON_WORD.finditer(text)] + [m.span() for m in _PART.finditer(text)]
    def _in_season_or_part(a, b):
        return any(sa <= a and b <= sb for sa, sb in _season_or_part_spans)

    if not episodes and not _PACK_CUE.search(text) and not _SEASON_RANGE.search(text):
        # Anime releases commonly use a bare, zero-padded absolute number at
        # the end: "Bleach 048.mkv" / "One Piece 836".
        # a known release group after the number ("... 01 1080p x265 pseudo") must not hide a trailing episode number
        ttext = re.sub(r"(?i)(?:[\s._-]+(?:" + "|".join(re.escape(g) for g in _KNOWN_GROUPS) + r"))+\s*$", "", text)
        trailing=re.search(r"(?<![A-Za-z0-9])0*(\d{2,4})(?:\.(\d))?([ABab])?\s*$",ttext)
        generic_leftover={"video", "vid", "document", "file", "audio", "photo", "img", "dsc", "pxl", "screenshot", "recording", "untitled", "download", "new", "movie", "film"}
        leftover=re.sub(r"[ _-]+", " ", ttext[:trailing.start()].strip(" -–—:|,._")).strip().casefold() if trailing else ""
        season_prefix = bool(trailing and re.search(r"(?i)(?<![A-Za-z0-9])(?:%s|szn|part|cour)\.?\s*$" % _SEASON_NAMES, ttext[:trailing.start()]))
        if trailing and not season_prefix and leftover not in generic_leftover and leftover and not 1900<=int(trailing.group(1))<=2099 and not re.search(r"(?i)\b(?:movie|film)\b",ttext[:trailing.start()]):
            episodes=[int(trailing.group(1))]; absolute=True; spans.append(trailing.span())
            if trailing.group(2):               # "1004.5": a between-episodes special, kept apart from episode 1004
                special=True; special_label=f"Episode {int(trailing.group(1))}.{trailing.group(2)}"
            if trailing.group(3):               # "0492A"/"0492B": file parts of one episode
                tr_part=1 if trailing.group(3).upper()=="A" else 2; ab_label=f"Part {trailing.group(3).upper()}"
    if not episodes and not _PACK_CUE.search(text) and not _SEASON_RANGE.search(text):
        # Bare number: "Show - 112", "Show - 05 - Title", "Show 1145 720p".
        toks = [(t.group(0), t.start(), t.end()) for t in re.finditer(r"\S+", text)]
        for i, (tk, a, b) in enumerate(toks):
            if i == 0 or not re.fullmatch(r"0*\d{1,4}(?:v\d)?", tk): continue
            if _in_season_or_part(a, b): continue
            n = int(re.match(r"\d+", tk).group(0))
            if n == 0 or 1900 <= n <= 2099: continue
            prev = toks[i - 1][0]; rest = [t[0] for t in toks[i + 1:]]
            if re.fullmatch(r"(?i)(?:%s|szn|part|cour)\.?" % _SEASON_NAMES, prev): continue
            prev_dash = prev in ("-", "–", "—")
            if prev_dash:
                if i < 2: continue                       # nothing before the dash
            else:
                # a number glued to the title needs release metadata right after it
                if not rest or not all(_is_tech_token(r) for r in rest): continue
                if not any(_RES_RE.fullmatch(r.lower()) or _tok(r) in _TECH or _tok(r) in _SOURCES or _tok(r) in _CODECS for r in rest): continue
            episodes = [n]; absolute = True; spans.append((a, b))
            if prev_dash: spans.append((toks[i - 1][1], toks[i - 1][2]))
            break

    if tr_part is not None:
        part_label = ab_label or f"Part {tr_part}"
    pm = _PART.search(text)
    if pm and tr_part is None:
        part_label = f"{pm.group(1).title()} {int(pm.group(2))}"; spans.append(pm.span())
    if season is None:
        sw = _SEASON_WORD.search(text) or _S_ONLY.search(text)
        if sw:
            season = int(sw.group(1)); spans.append(sw.span())

    explicit_pack = bool(_PACK_CUE.search(text) or _SEASON_RANGE.search(text) or _PACK_CUE.search(" ".join(tech_parts)))
    if episodes:
        kind = "episode"; season = season or 0
    elif explicit_pack or season is not None:
        kind = "pack"; episodes = []
        sr = _SEASON_RANGE.search(text)
        if sr: spans.append(sr.span())
        if season is None:
            season = int(re.search(r"\d+", sr.group(0)).group(0)) if sr else 0
    else:
        return SeriesParsed(raw=raw, kind="none", ext=ext)

    # --- title: the first text segment left after the markers are cut out -------
    merged = []
    for a, b in sorted(set(spans)):
        if merged and a <= merged[-1][1]: merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else: merged.append((a, b))
    segs = []; pos = 0
    for a, b in merged:
        segs.append(text[pos:a]); pos = b
    segs.append(text[pos:])

    def clean_title(s):
        s = _SEASON_RANGE.sub(" ", s)
        if kind == "pack":
            s = _PACK_CUE.sub(" ", s)
        s = re.sub(r"(?i)\bseasons?\b", " ", s)
        s = _PART.sub(" ", s)
        kept = []
        for w in s.split():
            lw = w.lower().strip("-–—:,.")
            if lw in _LANGS or lw in _TAGS: kept.append(w); continue                # title words unless trailing (popped below)
            if _RES_RE.fullmatch(lw) or (lw in _TECH and lw not in _TITLE_SAFE) or lw in _KNOWN_GROUPS: continue
            if _tok(lw) in _SOURCES or _tok(lw) in _CODECS: continue          # WEB-DL, x265, BluRay ...
            if re.fullmatch(r"(?i)S\d{1,2}", w): continue
            kept.append(w)
        while len(kept) > 1 and (kept[-1].lower().strip("-–—:,.") in _PLATFORMS or _is_tech_token(kept[-1]) and kept[-1].lower() not in _TITLE_SAFE):
            kept.pop()                                                        # "... Where Dreams Begin WeTV"
        t = re.sub(r"\s+", " ", " ".join(kept)).strip(" -–—:|,._+~")
        return re.sub(r"[^\w\s'’:;,.!&()?\-]", "", t).strip(" -–—:|,._+~")   # drop emoji / box-drawing symbols

    title = ""; title_seg = ""
    for sg in segs:
        t = clean_title(sg)
        if t and re.search(r"[^\W_]", t):             # any letter/digit, so Cyrillic/Greek/Arabic titles qualify
            title = t; title_seg = sg; break
    ym = re.search(r"(?<![A-Za-z0-9])((?:19|20)\d{2})$", title)
    if ym and not keep_year and title[:ym.start()].strip(" -–—:|,."):
        year = year or int(ym.group(1)); title = title[:ym.start()].strip(" -–—:|,.")
    # A generic media label must never become a catalogue title, regardless
    # of which episode-number rule recognised the adjacent number.
    if title.casefold() in GENERIC_MEDIA_NAMES:
        return SeriesParsed(raw=raw, kind="none", ext=ext)
    # "Ep-02 Ex-Girlfriend and Girlfriend": when the marker comes FIRST, the words after it are as likely to be an
    # episode title as a show. Only trust them with extra evidence (a release year, or a known uploader convention).
    if merged and not text[:merged[0][0]].strip(" -–—:|,._()[]#") and title_seg is segs[-1] and len(segs) > 1 \
            and not year and not uploader and not group_hint:
        # Evidence that the words after a leading marker are the SHOW: a release year, a leading release-group
        # tag ("[TIF]", "[YDF]"), a resolution token ("... 480p"), or an explicit Season+Episode tag ("[S-1][Ep-8]").
        ym0 = re.search(r"(?<![A-Za-z0-9])(?:19|20)\d{2}(?![A-Za-z0-9])", text)
        lead_group = re.match(r"\s*\[[A-Za-z0-9_-]{2,8}\]", raw)
        has_bracketed_sep = bool(
            season and season > 0 and _S_EP.search(text)
            and re.match(r"^\s*\[", raw)
            and re.search(r"\[[^\]]+\]\s*$", re.sub(r"\.[A-Za-z0-9]{2,4}$", "", raw.strip()))
        )
        if not (ym0 or lead_group or _RES_RE.search(raw) or has_bracketed_sep):
            return SeriesParsed(raw=raw, kind="none", ext=ext)
    if not title or len(title) > 110:
        return SeriesParsed(raw=raw, kind="none", ext=ext)

    # --- metadata from brackets + everything outside the title ------------------
    after = " ".join(s for s in segs if s is not title_seg)
    tail = title_seg.replace(title, " ", 1) if title in title_seg else ""
    res, source, codec, langs, tags = _meta(" ".join(tech_parts + [after, tail]))
    if not group:
        gm = re.search(r"(?:x26[45]|h26[45]|hevc|web[ .-]?dl|webrip|bluray|bdrip|hdtv|aac\d?|ddp\d?)[ ._-]*-\s*([A-Za-z0-9]{2,20})\s*$", after + " " + tail, re.I)
        if gm and not _is_tech_token(gm.group(1)): group = gm.group(1)
        else:
            for w in re.split(r"\s+", after + " " + tail):
                if w.lower().strip("-[]()") in _KNOWN_GROUPS: group = w.strip("-[]()"); break
    if group:
        group = group.strip(" -[](){}")
        if len(group) < 2 or _is_tech_token(group) or _YEAR.fullmatch(group) or re.fullmatch(r"\d+", group):
            group = None
    if not group and group_hint:
        group = group_hint
    if episodes == [0] and not special:                # episode 00 / S01E00 is an extra, not a first episode
        special = True; special_label = special_label or "Episode 00"
    explicit = bool(episodes) and bool(_S_EP.search(text) or _SS_EP.search(text) or _S_X_EP.search(text)
                                       or _S_S_TYPO.search(text) or _X_EP.search(text) or _YEAR_X_EP.search(text))
    show_final = canonical_title(_smart_case(title))
    ep_title_final = f"{date_label} {part_label}".strip() if (date_label and part_label) else (date_label or part_label)
    if WWE_PROGRAMME.match(show_final) or re.match(r"(?i)^WWE\b", show_final):
        # Every WWE programme is an episode of ONE show: "WWE". The programme name rides in the episode title.
        programme = _smart_case(re.sub(r"(?i)^WWE\s*", "", show_final).strip() or "Event")
        programme = re.sub(r"(?i)^nxt", "NXT", programme)
        show_final = WWE_SHOW
        ep_title_final = f"{programme} {date_label}".strip() if date_label else (f"{programme} {part_label}".strip() if part_label else programme)
    return SeriesParsed(raw=raw, kind=kind, show=show_final, year=year, season=int(season or 0),
                        episodes=tuple(sorted(set(episodes))), absolute=absolute, ep_title=ep_title_final, explicit=explicit,
                        special=special, special_label=special_label, resolution=res, source=source,
                        codec=codec, languages=langs, group=group, tags=tags, ext=ext,
                        part=tr_part, uploader=uploader)


# Technical words that are also ordinary title words (kept when they sit in a title).
_TITLE_SAFE = {"web", "multi", "dual", "hd", "sd", "cr", "bit", "audio", "sub", "subs", "rip", "dl", "batch", "dub", "dubs", "collection", "complete"}


_SURE_GROUPS = {"animerg", "subsplease", "eraraws", "horriblesubs", "galaxyrg", "rarbg", "yify", "yts"}


def strip_title_groups(title: str, promos=()) -> str:
    """Remove complete leading/trailing uploader phrases without eating title words."""
    words=(title or "").split()
    custom=[]
    for value in (promos or ()):
        parts=[_tok(x) for x in re.split(r"[\s._-]+",str(value).strip()) if _tok(x)]
        if parts: custom.append(parts)
    sure=[[g] for g in _SURE_GROUPS]
    changed=True
    while changed and len(words)>1:
        changed=False
        for parts in sorted(custom,key=lambda x:-len(x)):
            n=len(parts)
            if len(words)>n and [_tok(x) for x in words[:n]]==parts:
                words=words[n:]; changed=True; break
            if len(words)>n and [_tok(x) for x in words[-n:]]==parts:
                words=words[:-n]; changed=True; break
        if changed: continue
        for parts in sure:
            n=len(parts)
            if len(words)>n and [_tok(x) for x in words[:n]]==parts:
                words=words[n:]; changed=True; break
            if len(words)>n and [_tok(x) for x in words[-n:]]==parts:
                words=words[:-n]; changed=True; break
    return " ".join(words)


# --------------------------------------------------------------------------------------------
# Caption enrichment: the caption often holds what a (length-limited) filename lost.
# --------------------------------------------------------------------------------------------
_LANG_WORD = "|".join(sorted({re.escape(k) for k in _LANGS if len(k) > 3}, key=len, reverse=True))
_LANG_LIST = re.compile(r"(?i)(?<![A-Za-z])((?:%s)(?:\s*(?:[,+&/|]|-|and)\s*(?:%s))*)(?![A-Za-z])" % (_LANG_WORD, _LANG_WORD))
_AUDIO_LABEL = re.compile(r"(?i)\b(?:audio|audios|language|languages|lang)\s*[:=\-–]\s*([^\n\r]{2,80})")
_SUB_LABEL = re.compile(r"(?i)\b(?:sub|subs|subtitle|subtitles|esubs?)\s*[:=\-–]?\s*$")
_ENCODER_LABEL = re.compile(r"(?i)\b(?:encoder|encoded\s+by|encode\s+by|release\s+group|ripped\s+by|group)\s*[:=\-–]\s*@?([A-Za-z0-9][A-Za-z0-9._-]{1,24})")


def _clean_caption(caption: str) -> str:
    text = _PROMO_WORDS.sub(" ", caption or "")
    text = _strip_promo_tokens(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def caption_languages(caption: str) -> tuple:
    """Audio languages a caption clearly states. A language word counts only when it is in an
    'Audio: ...' label, in a list of 2+ languages, or alone inside brackets - never when it is
    just part of a title ('French Kiss'), and never when it follows a 'Subs' label."""
    found: list = []

    def add_from(chunk: str):
        for w in re.split(r"[^A-Za-z]+", chunk):
            lw = w.lower()
            if lw in _LANGS and len(lw) > 3 and _LANGS[lw] not in found:
                found.append(_LANGS[lw])

    for m in _AUDIO_LABEL.finditer(caption or ""):
        add_from(m.group(1))
    for m in _LANG_LIST.finditer(caption or ""):
        chunk = m.group(1)
        if len([w for w in re.split(r"[^A-Za-z]+", chunk) if w.lower() in _LANGS]) < 2:
            continue
        if _SUB_LABEL.search((caption or "")[max(0, m.start() - 14):m.start()]):
            continue
        add_from(chunk)
    for m in re.finditer(r"[\[\(]\s*((?:%s))\s*[\]\)]" % _LANG_WORD, caption or "", re.I):
        if not _SUB_LABEL.search((caption or "")[max(0, m.start() - 14):m.start()]):
            add_from(m.group(1))
    return tuple(found[:5])


def _nk(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def enrich_from_caption(parsed: "SeriesParsed", name: str, caption: str) -> "SeriesParsed":
    """Fill what the filename lacks from the caption: resolution, source, codec, audio languages,
    tags and encoder, and repair a cut-off encoder or title (e.g. 'Megus' -> 'MeGusta').
    Season/episode/title are only taken from the caption when it describes the SAME file
    (same season and episode) and the filename title is a prefix of the caption's title."""
    if parsed.kind not in ("episode", "pack") or not (caption or "").strip():
        return parsed
    cap = _clean_caption(caption)
    if not cap:
        return parsed
    upd: dict = {}
    res, source, codec, _langs, tags = _meta(cap)
    if not parsed.resolution and res: upd["resolution"] = res
    if not parsed.source and source: upd["source"] = source
    if not parsed.codec and codec: upd["codec"] = codec
    cap_langs = caption_languages(cap)
    if cap_langs:
        merged = list(parsed.languages)
        for l in cap_langs:
            if l not in merged: merged.append(l)
        if tuple(merged) != tuple(parsed.languages): upd["languages"] = tuple(merged[:5])
    if tags:
        merged_t = list(parsed.tags)
        for t in tags:
            if t not in merged_t: merged_t.append(t)
        if tuple(merged_t[:4]) != tuple(parsed.tags): upd["tags"] = tuple(merged_t[:4])

    # Candidate descriptions of the same file: every caption line, and the whole caption.
    cands = [ln.strip() for ln in re.split(r"[\r\n]+", cap) if ln.strip()][:6]
    if len(cands) > 1: cands.append(" ".join(cands))
    same = None
    for cand in cands:
        cp = parse_series(cand)
        if cp.kind not in ("episode", "pack") or not cp.show:
            continue
        a, b = _nk(parsed.show), _nk(cp.show)
        if not a or not b or not (a == b or b.startswith(a) or a.startswith(b)):
            continue
        if parsed.kind == "episode" and cp.kind == "episode":
            if parsed.season != cp.season and parsed.season != 0 and cp.season != 0: continue
            if list(parsed.episodes) != list(cp.episodes): continue
        same = cp
        break
    if same is not None:
        if len(_nk(same.show)) > len(_nk(parsed.show)) and _nk(same.show).startswith(_nk(parsed.show)):
            upd["show"] = same.show                                # filename title was cut off
            if same.year and not parsed.year: upd["year"] = same.year
        if parsed.kind == "pack" and same.kind == "episode":      # filename was cut before the episode
            upd.update(kind="episode", season=same.season or parsed.season, episodes=same.episodes, absolute=same.absolute)
        for fld in ("resolution", "source", "codec"):
            if not getattr(parsed, fld) and getattr(same, fld) and fld not in upd: upd[fld] = getattr(same, fld)
        if not parsed.group and same.group: upd["group"] = same.group

    # Encoder cut off at the end of the filename ("...x265-Megus" vs caption "...-MeGusta").
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name or "")
    tail_m = re.search(r"(?:[-\[\(])([A-Za-z0-9]{3,})$", base)
    if tail_m and (re.fullmatch(r"(?i)[se]\d+(?:e\d+)?|\d+p?|x26[45]|h26[45]|hevc", tail_m.group(1))):
        tail_m = None
    group = upd.get("group", parsed.group)
    if tail_m:
        tail = tail_m.group(1)
        for tok in re.findall(r"[A-Za-z0-9]+", cap):
            if (len(tok) > len(tail) and tok.lower().startswith(tail.lower()) and not _is_tech_token(tok)
                    and not re.fullmatch(r"(?i)[se]\d+(?:e\d+)?|\d+p?", tok)):
                if group is None or group.lower() == tail.lower() or tail.lower().startswith(group.lower()):
                    upd["group"] = tok
                break
    if not upd.get("group", parsed.group):
        em = _ENCODER_LABEL.search(cap)
        if em and not _is_tech_token(em.group(1)) and not _YEAR.fullmatch(em.group(1)):
            upd["group"] = em.group(1)
    if not upd:
        return parsed
    from dataclasses import replace as _replace
    return _replace(parsed, **upd)
