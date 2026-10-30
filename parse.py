"""Turn messy Telegram filenames into structured data.

    "Adam.2019.720p.WEBRip.800MB.x264-GalaxyRG.mkv"
 -> title "Adam", year 2019, 720p, WEBRip, x264, encoder "GalaxyRG", MKV

Everything after the first "technical" token (year, resolution, source,
codec, season marker) is metadata; everything before it is the title.
The encoder/uploader is a best guess (see _pick_group) and is simply left
blank when the filename gives no clear sign of one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from normalize import normalize

VIDEO_EXTS = {"mkv", "mp4", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "3gp", "ts", "vob", "divx"}
SUB_EXTS = {"srt", "ass", "ssa", "vtt", "sub"}
_STRIP_EXTS = VIDEO_EXTS | SUB_EXTS | {"zip", "rar", "7z", "nfo", "txt", "jpg", "png", "idx"}
_EXT = re.compile(r"\.([A-Za-z0-9]{2,4})$")
_LEAD_AT = re.compile(r"^\s*@\w+\s*[-–—_.:|]*\s*")
_LEAD_AT_UNDERSCORE = re.compile(r"^\s*@[^_\s.]+[_\-:|\s.]*")   # "@Handle_Some_Title_2019": handle = first piece only
# A site at the very start is promo only if it looks like one: a www./http prefix, or a plain
# domain followed by a SPACED dash/colon ("Site.com - Title"). Dotted title words never match.
_LEAD_SITE = re.compile(
    r"^\s*(?:(?:https?://)?www\.\S+?|[\w-]+\.(?:com|org|info|xyz|cc|site|in|ng|io|net))"
    r"(?:\s+[-–—|:]\s+|\s*:\s+)", re.I)
_LEAD_BRACKET = re.compile(r"^\s*[\[\(\{]([^\]\)\}]{0,60})[\]\)\}]\s*[-–—_.:|]*\s*")
_BRACKET_YEAR = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")
_BRACKET_ANY = re.compile(r"[\[\(\{]([^\]\)\}]+)[\]\)\}]")
_TLDS = {"com", "net", "org", "info", "in", "cc", "io", "tv", "me", "to", "co", "ng", "xyz", "site", "club", "world"}
_DOMAIN_FULL = re.compile(r"(?:www\.)?[\w-]+\.(?:" + "|".join(sorted(_TLDS)) + r")", re.I)
_HANDLE = re.compile(r"[-–—_.\s]*@\w+")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_SXXEYY = re.compile(r"^s(\d{1,2})[ ._-]?e(\d{1,3})(?:-?e?(\d{1,3}))?$", re.I)
_SONLY = re.compile(r"^s(\d{1,2})$", re.I)
_NXNN = re.compile(r"^(\d{1,2})x(\d{2,3})$", re.I)
_RES = re.compile(r"^(2160|1440|1080|720|576|480|360|240)p$")
_DOT = "\x03"    # stands in for the dot inside sizes like 1.4GB while we split on dots
_SIZE = re.compile(r"^\d+([.\x03]\d+)?(kb|mb|gb|tb)$", re.I)
_TLD_WORDS = re.compile(r"(?i)^([\w-]+)\s+(com|net|org|cc|xyz|info|site|club|world|in)$")
_BR = "\x00"      # bracketed year marker
_REF = "\x01"     # bracketed text placeholder

_SOURCES = {
    "webdl": "WEB-DL", "web": "WEB", "webrip": "WEBRip", "bluray": "BluRay",
    "bdrip": "BDRip", "brrip": "BRRip", "hdrip": "HDRip", "hdtv": "HDTV",
    "dvdrip": "DVDRip", "dvdscr": "DVDScr", "hdcam": "CAM", "cam": "CAM",
    "camrip": "CAM", "hdts": "TS", "predvd": "PreDVD", "remux": "Remux",
}
_SOURCE_RANK = {"CAM": 0, "TS": 1, "PreDVD": 1, "DVDScr": 2, "HDTV": 3, "HDRip": 4,
                "DVDRip": 4, "WEBRip": 5, "WEB": 5, "WEB-DL": 6, "BRRip": 6,
                "BDRip": 6, "BluRay": 7, "Remux": 8}
_CODECS = {"x264": "x264", "h264": "x264", "avc": "x264",
           "x265": "x265", "h265": "x265", "hevc": "x265", "xvid": "XviD", "divx": "XviD"}
# Words that end the title but aren't shown on their own.
_OTHER_TECH = {"aac", "ac3", "dts", "atmos", "ddp5", "dd5", "10bit", "hdr", "hdr10", "uhd", "hq",
               "proper", "repack", "extended", "unrated", "imax", "esub", "esubs",
               "hc", "hardsub", "dubbed", "dub", "dual", "multi", "uncut", "remastered"}
# Never an encoder name.
_NOISE = {"mkv", "mp4", "avi", "sub", "subs", "subbed", "audio", "dl", "rip", "web", "hd", "fhd", "sd",
          "dv", "sdr", "8bit", "ddp", "aac2", "truehd", "flac", "opus", "eac3", "nf", "amzn", "dsnp",
          "hmax", "atvp", "hulu", "pcok", "line", "org", "original", "clean", "internal", "limited",
          "criterion", "complete", "full", "movie", "film", "by", "www", "com", "net", "cc", "tv",
          "me", "to", "in", "io", "co", "ng", "xyz", "info", "site", "club", "world", "bluray",
          "hindi", "english", "dubbed", "ms", "mb", "gb", "esub", "esubs", "cut", "directors", "aud", "auds",
          "multi", "dual", "h", "mal"}
_LANGS = {
    "english": "English", "eng": "English", "hindi": "Hindi", "hin": "Hindi",
    "hausa": "Hausa", "yoruba": "Yoruba", "igbo": "Igbo", "pidgin": "Pidgin",
    "french": "French", "fre": "French", "fra": "French", "spanish": "Spanish",
    "spa": "Spanish", "portuguese": "Portuguese", "arabic": "Arabic",
    "korean": "Korean", "kor": "Korean", "japanese": "Japanese", "jpn": "Japanese",
    "chinese": "Chinese", "mandarin": "Chinese", "tamil": "Tamil", "telugu": "Telugu",
    "malayalam": "Malayalam", "kannada": "Kannada", "bengali": "Bengali",
    "urdu": "Urdu", "punjabi": "Punjabi", "russian": "Russian", "german": "German",
    "italian": "Italian", "turkish": "Turkish", "swahili": "Swahili",
    "amharic": "Amharic", "zulu": "Zulu", "xhosa": "Xhosa", "afrikaans": "Afrikaans",
    "twi": "Twi", "akan": "Akan",
}
_TAGS = {"dual": "Dual Audio", "multi": "Multi Audio", "esub": "ESub", "esubs": "ESub", "sub": "ESub",
         "subs": "ESub", "subbed": "ESub", "hdr": "HDR", "hdr10": "HDR", "10bit": "10-bit",
         "extended": "Extended", "uncut": "Uncut", "remastered": "Remastered", "imax": "IMAX",
         "dubbed": "Dubbed", "dub": "Dubbed"}
_SMALL = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "vs", "with"}
_RES_PX = {"2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720,
           "576p": 576, "480p": 480, "360p": 360, "240p": 240}


def quality_key_of(resolution: str | None, source: str | None) -> tuple:
    """Sort key; the biggest value is the best version: (not-cam, resolution, source)."""
    return (source not in ("CAM", "TS", "PreDVD"), _RES_PX.get(resolution or "", 0),
            _SOURCE_RANK.get(source or "", 4))


@dataclass(frozen=True)
class Parsed:
    raw: str
    title: str                      # display title, "" if nothing usable
    year: int | None = None
    kind: str = "movie"             # "movie" | "series"
    season: int | None = None
    episode: int | None = None
    resolution: str | None = None
    source: str | None = None
    codec: str | None = None
    languages: tuple[str, ...] = ()
    group: str | None = None        # encoder / uploader, best guess
    tags: tuple[str, ...] = ()      # Dubbed, Dual Audio, ESub, HDR, ...
    ext: str | None = None          # MKV, MP4, SRT, ...

    @property
    def norm_title(self) -> str:
        return normalize(self.title)

    @property
    def is_cam(self) -> bool:
        return self.source in ("CAM", "TS", "PreDVD")

    @property
    def is_sub(self) -> bool:
        return (self.ext or "").lower() in SUB_EXTS

    @property
    def quality_key(self) -> tuple:
        return quality_key_of(self.resolution, self.source)


def _valid_year(tok: str) -> bool:
    return bool(_YEAR.match(tok)) and 1888 <= int(tok) <= date.today().year + 1


def _smart_case(title: str) -> str:
    """Fix ALL-CAPS and all-lowercase names; leave mixed case alone."""
    if title != title.lower() and title != title.upper():
        return title
    words = []
    for i, w in enumerate(title.split()):
        lw = w.lower()
        if i > 0 and lw in _SMALL:
            words.append(lw)
        else:
            words.append("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")))
    return " ".join(words)


def _strip_promo(name: str) -> str:
    for _ in range(3):
        before = name
        bare = name.strip()
        if bare.startswith("@") and " " not in bare and bare.count("_") >= 2:
            name = _LEAD_AT_UNDERSCORE.sub("", name)
        else:
            name = _LEAD_AT.sub("", name)
        name = _LEAD_SITE.sub("", name)
        m = _LEAD_BRACKET.match(name)
        if m and not re.search(r"(?:19|20)\d{2}", m.group(1)):
            b_toks = re.sub(r"[._+~-]", " ", m.group(1)).split()
            if _series_tag(b_toks) is None:
                name = name[m.end():]
        if name == before:
            break
    return name


def _known(low: str) -> bool:
    flat = low.replace("-", "")
    return bool(_RES.match(low) or low in ("4k", "uhd") or flat in _SOURCES or flat in _CODECS
                or flat in _OTHER_TECH or flat in _NOISE or flat in _LANGS or low.isdigit()
                or _SIZE.match(low) or _YEAR.match(low))


def _valid_group(g: str) -> bool:
    g = g.strip()
    return 2 <= len(g) <= 30 and bool(re.search(r"[A-Za-z]", g)) and g.lower() not in _NOISE \
        and not _known(g.lower())


def _clean_group(txt: str) -> str:
    txt = " ".join(txt.split())
    m = _TLD_WORDS.match(txt)
    return f"{m.group(1)}.{m.group(2).lower()}" if m else txt


def _series_tag(toks: list[str], lead_nxnn: bool = True):
    """First season/episode tag anywhere in the tokens: (index, season, episode, tokens used) or None.
    Looks like S01E01, S1E1, S01, 1x01, "Season 2". 'S1m0ne' and 'Se7en' are not tags. A leading '10x10'
    is not treated as one, so the film 10x10 stays a film."""
    for i, tok in enumerate(toks):
        if tok.startswith((_BR, _REF)):
            continue
        low = tok.lower().strip("-")
        m = _SXXEYY.match(low)
        if m:
            return i, int(m.group(1)), int(m.group(2)), 1
        m = _SONLY.match(low)
        if m:
            return i, int(m.group(1)), None, 1
        m = _NXNN.match(low)
        if m and (i > 0 or (lead_nxnn and not any(_valid_year(t.lstrip(_BR)) for t in toks))):
            return i, int(m.group(1)), int(m.group(2)), 1
        if low == "season" and i + 1 < len(toks) and re.fullmatch(r"\d{1,2}", toks[i + 1]):
            return i, int(toks[i + 1]), None, 2
    return None


def looks_like_series(text: str) -> bool:
    """For what people type into the search box: 'friends s01', 'show season 2', 'S01E03'."""
    return _series_tag(normalize(text).split(), lead_nxnn=False) is not None      # a lone "10x10" is a film title


def _is_tech(low: str) -> bool:
    flat = low.replace("-", "")
    return bool(_RES.match(low) or low in ("4k",) or flat in _SOURCES or flat in _CODECS or flat in _OTHER_TECH
                or _SXXEYY.match(low) or _SONLY.match(low) or _NXNN.match(low))


def _parse(raw: str, name: str) -> Parsed:
    name = name.strip()
    ext = None
    m = _EXT.search(name)
    if m and m.group(1).lower() in _STRIP_EXTS:
        ext, name = m.group(1).upper(), name[:m.start()]
    name = _HANDLE.sub(" ", name)                      # "-@Channel", "BRRip@Channel", "[@Channel]": promo, never title
    name = re.sub(r"(?<=\d)\.(?=\d+\s?(?:gb|mb|kb|tb)\b)", _DOT, name, flags=re.I)    # 1.4GB stays one piece
    name = _BRACKET_YEAR.sub(lambda mm: f" {_BR}{mm.group(1)} ", name)
    refs: list[str] = []

    def stash(mm):
        content = mm.group(1).strip()
        cleaned = re.sub(r"[._+~-]", " ", content).split()
        if _series_tag(cleaned) is not None:
            return f" {content} "
        refs.append(content)
        return f" {_REF}{len(refs) - 1} "
    name = _BRACKET_ANY.sub(stash, name)
    name = re.sub(r"[._+~]", " ", name)
    toks = [t for t in name.split() if t.strip("-–—")]
    while len(toks) > 1 and toks[-1].lower() in _STRIP_EXTS:          # a literal "mkv" word at the end
        ext = ext or toks[-1].upper()
        toks.pop()
    tag = _series_tag(toks)
    kind, season, episode = ("series", tag[1], tag[2]) if tag else ("movie", None, None)
    if tag and tag[0] == 0 and len(toks) > tag[3]:                                  # "S01E01 Better Call Saul ..."
        toks = toks[tag[3]:]
    lead_year = None
    if len(toks) > 1 and toks[0].startswith(_BR) and _valid_year(toks[0][1:]):      # "(1987) Title ..."
        if _is_tech(toks[1].lower()):                    # "(2007) 720p": the title must be elsewhere
            return Parsed(raw=raw, title="")
        lead_year, toks = int(toks[0][1:]), toks[1:]

    # 1) first technical token after the title's first word ends the title
    tech_idx = None
    for i, tok in enumerate(toks):
        if i == 0 or tok.startswith(_REF):
            continue
        low = tok.lower().strip("-")
        flat = low.replace("-", "")
        if (_SXXEYY.match(low) or _SONLY.match(low) or _NXNN.match(low)
                or (low == "season" and i + 1 < len(toks) and re.fullmatch(r"\d{1,2}", toks[i + 1]))
                or _RES.match(low) or low == "4k" or flat in _SOURCES or flat in _CODECS
                or flat in _OTHER_TECH or _SIZE.match(low)):
            tech_idx = i
            break

    # 2) pick the release year
    strong_idx = next((i for i, t in enumerate(toks) if i > 0 and not t.startswith(_REF) and (
        _RES.match(t.lower()) or t.lower() == "4k" or t.lower().replace("-", "") in _SOURCES
        or t.lower().replace("-", "") in _CODECS or _SXXEYY.match(t) or _SONLY.match(t) or _NXNN.match(t))), None)
    limit = strong_idx if strong_idx is not None else len(toks)
    bracketed = [i for i, t in enumerate(toks) if t.startswith(_BR) and i > 0 and _valid_year(t[1:])]
    plain = [i for i, t in enumerate(toks) if 0 < i < limit and not t.startswith((_BR, _REF)) and _valid_year(t)]
    year_idx = bracketed[-1] if bracketed else (plain[-1] if plain else None)
    year = int(toks[year_idx].lstrip(_BR)) if year_idx is not None else lead_year

    if toks and toks[0].startswith(_BR):            # "(2007) 720p": nothing before the year
        return Parsed(raw=raw, title="")
    ends = [x for x in (tech_idx, year_idx) if x is not None]
    boundary = min(ends) if ends else len(toks)
    title_toks = [t.lstrip(_BR) for t in toks[:boundary] if not t.startswith(_REF)]

    tags: list[str] = []
    langs: list[str] = []

    def add_tag(t: str) -> None:
        if t not in tags:
            tags.append(t)

    def add_lang(l: str) -> None:
        if l not in langs:
            langs.append(l)

    if boundary < len(toks) and toks[boundary].lower() in ("dubbed", "dub") and len(title_toks) > 1 \
            and title_toks[-1].lower() in _LANGS:                         # "Adam Hindi Dubbed 2019"
        add_lang(_LANGS[title_toks.pop().lower()])
    while title_toks and title_toks[-1].lower() in ("dubbed", "dub"):
        title_toks.pop()
        add_tag("Dubbed")
        if title_toks and title_toks[-1].lower() in _LANGS and len(title_toks) > 1:
            add_lang(_LANGS[title_toks.pop().lower()])
    title = " ".join(title_toks).strip(" -–—:")
    if not title and toks and toks[0].startswith(_REF):                  # "[REC] (2007)": the bracket IS the title
        title = refs[int(toks[0][1:])]

    # 3) metadata lives in the tail only, so "Hindi Medium" keeps its title
    resolution = source = codec = None
    group: str | None = None
    unknown: list[str] = []
    tail: list[tuple[str, bool]] = []          # (token, is hyphen-suffix candidate)
    for tok in toks[boundary:]:
        if tok.startswith(_REF):
            seg = refs[int(tok[1:])]
            low = seg.lower()
            words = [w for w in re.split(r"[^a-z0-9]+", low) if w]
            if "." in seg and _DOMAIN_FULL.fullmatch(seg):
                group = re.sub(r"(?i)^www\.", "", seg)
            elif words and all(_known(w) for w in words):
                for w in words:
                    if w in _LANGS:
                        add_lang(_LANGS[w])
                    if w in _TAGS:
                        add_tag(_TAGS[w])
            elif _valid_group(_clean_group(seg)):
                group = _clean_group(seg)
            continue
        low = tok.lstrip(_BR).lower().strip("-")
        if "-" in low and not _known(low):
            parts = [p for p in low.split("-") if p]
            orig = [p for p in tok.lstrip(_BR).strip("-").split("-") if p]
            for j, (p, o) in enumerate(zip(parts, orig)):
                tail.append((o, j == len(parts) - 1 and len(parts) > 1 and _known(parts[0])))
        else:
            tail.append((tok.lstrip(_BR).strip("-"), False))

    # "Pahe" + "in", "www" + "phonofilm" + "net": a site name after the title is promo, so join it up
    merged: list[tuple[str, bool]] = []
    i = 0
    while i < len(tail):
        tok, hy = tail[i]
        nxt = tail[i + 1][0].lower() if i + 1 < len(tail) else ""
        if nxt in _TLDS and re.search(r"[A-Za-z]", tok) and not _known(tok.lower()) and tok.lower() != "www":
            if merged and merged[-1][0].lower() == "www":
                merged.pop()
            merged.append((f"{tok}.{nxt}", True))
            i += 2
        else:
            merged.append((tok, hy))
            i += 1
    tail = merged

    hy_group = None
    for idx, (tok, hy) in enumerate(tail):
        low = tok.lower()
        flat = low.replace("-", "")
        if resolution is None and (_RES.match(low) or low in ("4k", "uhd")):
            resolution = "2160p" if low in ("4k", "uhd") else low
        elif source is None and flat in _SOURCES:
            source = _SOURCES[flat]
        elif codec is None and flat in _CODECS:
            codec = _CODECS[flat]
        if flat in _LANGS:
            add_lang(_LANGS[flat])
        if flat in _TAGS:
            add_tag(_TAGS[flat])
        if hy and not _known(low) and _valid_group(tok):
            hy_group = tok
        elif not _known(low):
            unknown.append(tok if idx != len(tail) - 1 else tok + "\x02")   # \x02 = was the last token

    if group is None:
        group = hy_group
    if group is None and unknown and unknown[-1].endswith("\x02"):
        cand = unknown[-1][:-1]
        if _valid_group(cand):
            group = cand
    if group is not None and not _valid_group(group) and not _DOMAIN_FULL.fullmatch(group) \
            and not re.fullmatch(r"[\w-]+\.[a-z]{2,5}", group, re.I):
        group = None

    return Parsed(raw=raw, title=_smart_case(title), year=year, kind=kind,
                  season=season, episode=episode, resolution=resolution,
                  source=source, codec=codec, languages=tuple(langs), group=group,
                  tags=tuple(tags), ext=ext)


def parse_filename(raw: str) -> Parsed:
    """Parse one filename. Falls back to the unstripped name if cleaning ate the title
    (e.g. '[REC] (2007)')."""
    parsed = _parse(raw, _strip_promo(raw))
    if not parsed.title:
        parsed = _parse(raw, raw)
    return parsed


_JUNK = re.compile(r"^(video|document|file|audio|img|vid|movie)[\s_-]*\d*$", re.I)


def is_junk(title: str) -> bool:
    """Generic names like 'Video 123' carry no title information."""
    return not title or bool(_JUNK.match(title))


def parse_best(file_name: str | None, caption: str | None = None) -> Parsed:
    """Use the filename; if it's junk ('video_123.mp4'), try the caption's first line."""
    parsed = parse_filename(file_name or "")
    if is_junk(parsed.title) and caption:
        first = caption.strip().splitlines()[0] if caption.strip() else ""
        alt = parse_filename(first)
        if alt.title:
            return Parsed(**{**alt.__dict__, "ext": alt.ext or parsed.ext})
    return parsed


def human_size(n: int | None) -> str:
    if not n:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.0f} {unit}" if size >= 100 else f"{size:.1f} {unit}"
        size /= 1024
    return "?"


def label_of(resolution: str | None, source: str | None, size: int | None,
             languages: tuple[str, ...] | list[str] = (), codec: str | None = None) -> str:
    """One-line summary: '1080p · WEB-DL · x265 · 1.8 GB · Hindi'."""
    bits = [resolution, source, codec, human_size(size), ", ".join(languages) or None]
    return " · ".join(b for b in bits if b)


def detail_lines(res, src, codec, size, langs, tags, ext, group) -> tuple[str, str]:
    """Two lines for a version: quality/size, then language, tags, format, encoder."""
    if (ext or "").lower() in SUB_EXTS:
        first = " · ".join(b for b in ("Subtitles", ext.upper(), human_size(size)) if b)
    else:
        first = " · ".join(b for b in (res, src, codec, human_size(size)) if b)
    second = " · ".join(b for b in (", ".join(langs) or None, *tags,
                                    ext.upper() if ext and (ext.lower() not in SUB_EXTS) else None,
                                    f"by {group}" if group else None) if b)
    return first, second


def button_label(res, src, codec, size, langs, tags, ext, group) -> str:
    """One line for a version button, most important first so a cut-off end loses only the encoder:
    '895 MB · 720p · BluRay · x265 · Hindi · Dubbed · MKV · GalaxyRG'."""
    if (ext or "").lower() in SUB_EXTS:
        bits = ["Subtitles", ext.upper(), human_size(size) if size else None, ", ".join(langs) or None, group]
    else:
        bits = [human_size(size) if size else None, res, src, codec, ", ".join(langs) or None, *tags,
                ext.upper() if ext else None, group]
    return " · ".join(b for b in bits if b)


def variant_label(p: Parsed, size: int | None) -> str:
    return label_of(p.resolution, p.source, size, p.languages, p.codec)


_QUERY_NOISE_RE = re.compile(
    r'(?i)\b(?:part\s*(?:[1-9]|one|two|three|four|i{1,3}|iv|v|vi{0,3}|ix|x)|'
    r'pt\s*[1-9]|movie|film|cinema|full\s*movie|free|download|hd|rip|cam|dvd)\b'
)

_ROMAN_MAP = {
    'ii': '2', 'iii': '3', 'iv': '4', 'v': '5',
    'vi': '6', 'vii': '7', 'viii': '8', 'ix': '9', 'x': '10'
}

def clean_query_retry(text: str) -> list[str]:
    """Generates fallback candidate queries when primary search finds nothing."""
    candidates = []
    cleaned = _QUERY_NOISE_RE.sub(' ', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if cleaned and cleaned.lower() != text.lower():
        candidates.append(cleaned)

    words = text.split()
    converted = [_ROMAN_MAP.get(w.lower(), w) for w in words]
    cand_roman = " ".join(converted)
    if cand_roman.lower() != text.lower() and cand_roman not in candidates:
        candidates.append(cand_roman)

    if cleaned:
        words_cl = cleaned.split()
        converted_cl = [_ROMAN_MAP.get(w.lower(), w) for w in words_cl]
        cand_cl_roman = " ".join(converted_cl)
        if cand_cl_roman.lower() != text.lower() and cand_cl_roman not in candidates:
            candidates.append(cand_cl_roman)

    return candidates


# Series parsing is colocated here so the repository has one parser module.

@dataclass(frozen=True)
class SeriesParsed:
    raw: str
    kind: str
    show: str = ""
    year: int | None = None
    season: int = 0
    episodes: tuple[int, ...] = ()
    absolute: bool = False
    ep_title: str = ""
    resolution: str | None = None
    source: str | None = None
    codec: str | None = None
    languages: tuple[str, ...] = ()
    group: str | None = None
    tags: tuple[str, ...] = ()
    ext: str | None = None

    @property
    def first(self): return self.episodes[0] if self.episodes else None
    @property
    def last(self): return self.episodes[-1] if self.episodes else None

# These expressions deliberately allow punctuation and whitespace between markers.
_SEASON_WORD = re.compile(r'(?i)\bseason\s*[\[({._ -]*\s*(\d{1,2})\s*[\])}._ -]*')
_S_EP = re.compile(r'(?i)(?<![A-Za-z0-9])S\s*0*(\d{1,2})\s*[\]. _-]*E(?:P(?:ISODE)?)?\s*0*(\d{1,4})(?:\s*[-–]\s*E?\s*0*(\d{1,4}))?')
_X_EP = re.compile(r'(?i)(?<!\d)(\d{1,2})\s*x\s*(\d{1,4})(?!\d)')
_EP_WORD = re.compile(r'(?i)(?<![A-Za-z0-9])(?:episode|ep|e)\.?\s*0*(\d{1,4})(?:\s*[-–]\s*E?\.?\s*0*(\d{1,4}))?(?!\d)')
_HASH_EP = re.compile(r'(?<!\w)#\s*0*(\d{1,4})(?!\d)')
_PACK_CUE = re.compile(r'(?i)\b(?:complete|full\s+season|season\s+pack|complete\s+season|full\s+series|complete\s+series)\b')
_SEASONS_RANGE = re.compile(r'(?i)\bseasons?\s*\d{1,2}\s*[-–]\s*\d{1,2}\b')
_TECH_WORDS = re.compile(r'(?i)\b(?:2160p|1440p|1080p|720p|576p|480p|360p|240p|4k|uhd|x26[45]|h26[45]|hevc|avc|hdr10?|10[- ]?bit|web[- ]?dl|webrip|bluray|brrip|bdrip|hdtv|dvdrip|aac|ac3|ddp5?\.?1|dts|proper|repack|extended|unrated|dual|multi|dubbed|subbed|esubs?|eng|hindi|english|mkv|mp4|avi|m4v|webm)\b', re.I)
_EXT = re.compile(r'\.([A-Za-z0-9]{2,4})$')

# Add uploader/ad boilerplate here; stripping is restricted to filename edges.
SERIES_PROMO_PREFIXES = (
    r'join(?:\s+our)?(?:\s+channel)?', r'downloaded\s+from', r'uploaded\s+by',
    r'powered\s+by', r'subscribe(?:\s+to)?', r'channel\s*:',
)
SERIES_PROMO_SUFFIXES = SERIES_PROMO_PREFIXES
SERIES_PROMO_TLDS = ("com", "net", "org", "in", "to", "me", "io", "tv", "co", "ng", "cc", "xyz", "site", "club", "info", "online", "app")
_PROMO_DOMAIN = r'(?:https?://)?(?:www\.)?[\w-]+\.(?:' + '|'.join(SERIES_PROMO_TLDS) + r')(?:/[^\s]*)?'
_PROMO_TOKEN = re.compile(r'(?:@\w[\w.-]*|t\.me/[^\s\])}>]+|telegram\.me/[^\s\])}>]+|'+_PROMO_DOMAIN+r')', re.I)
_PROMO_BRACKET = re.compile(r'[\[({]([^\])}]+)[\])}]')

def strip_series_promos(value: str) -> str:
    """Remove uploader advertising at filename/caption edges, preserving title interiors."""
    text=(value or '').strip()
    def clean_bracket(m):
        return ' ' if _PROMO_TOKEN.search(m.group(1)) else m.group(0)
    text=_PROMO_BRACKET.sub(clean_bracket,text)
    text=re.sub(r'\(\s*\)|\[\s*\]|\{\s*\}', ' ', text)
    phrase=r'(?:'+'|'.join(SERIES_PROMO_PREFIXES)+r')'
    for _ in range(8):
        old=text
        # A leading Join/Subscribe/Downloaded phrase is removable only when it
        # introduces an identifiable promo token; ordinary title words survive.
        text=re.sub(r'^\s*(?:join(?:\s+our)?(?:\s+channel)?|downloaded\s+from|uploaded\s+by|powered\s+by|subscribe(?:\s+to)?|channel\s*:)[\s:：|\-–—]*'+_PROMO_TOKEN.pattern+r'\s*[-–—:|_]*\s*', '', text, flags=re.I)
        text=re.sub(r'^\s*'+_PROMO_TOKEN.pattern+r'\s*[-–—:|_]*\s*', '', text, flags=re.I)
        # Promo can sit between the title and an episode marker, not only at
        # the absolute end of the filename (e.g. "Show Channel: @x EP04").
        text=re.sub(r'(?i)(?<=\s)(?:channel\s*:\s*|join(?:\s+our)?(?:\s+channel)?\s+|downloaded\s+from\s+|uploaded\s+by\s+|powered\s+by\s+|subscribe(?:\s+to)?\s+)'+_PROMO_TOKEN.pattern+r'(?=\s|[-–—|:]|$)', ' ', text)
        text=re.sub(r'(?i)(?<=\s)(?:downloaded\s+from|uploaded\s+by|powered\s+by|channel\s*:)[\s]+[\w.-]+(?=\s|[-–—|:]|$)', ' ', text)
        text=re.sub(r'(?i)(?<=\s)'+_PROMO_DOMAIN+r'(?=\s|[-–—|:]|$)', ' ', text)
        text=re.sub(r'(?i)(?<=\s)'+_PROMO_TOKEN.pattern+r'(?=\s|[-–—|:]|$)', ' ', text)
        text=re.sub(r'\s+[-–—|:]\s*'+phrase+r'\b.*$', '', text, flags=re.I)
        text=re.sub(r'(?:\s*[-–—|:]?\s*'+_PROMO_TOKEN.pattern+r')+\s*$', '', text, flags=re.I)
        text=text.strip(' -–—|:,.')
        if text==old: break
    return re.sub(r'\s+',' ',text).strip()

def _nums(a, b=None):
    a=int(a)
    if b is None: return [a]
    b=int(b)
    if a < b and b-a <= 12: return list(range(a,b+1))
    return [a,b]

def _clean_title(s):
    s=re.sub(r'(?i)\b(?:complete|full\s+season|season\s+pack|complete\s+season|full\s+series|complete\s+series)\b',' ',s)
    s=_SEASONS_RANGE.sub(' ',s)
    s=_TECH_WORDS.sub(' ',s)
    s=re.sub(r'(?i)\b(?:S\d{1,2}|Season\s*\d{1,2})\b',' ',s)
    s=re.sub(r'(?i)\b(?:GalaxyRG|PSA|RARBG|YIFY|YTS|MeGusta|Pahe|Phun|Mkvking)\b',' ',s)
    s=re.sub(r'\b(?:19|20)\d{2}\b',' ',s)
    s=re.sub(r'[\[\](){}]', ' ', s)
    s=re.sub(r'[._+~|]+',' ',s)
    s=re.sub(r'\s+',' ',s).strip(' -–—:,.')
    return s

def parse_series(raw: str) -> SeriesParsed:
    raw=(raw or '').strip()
    name=raw
    ext=None
    m=_EXT.search(name)
    if m and m.group(1).lower() in (VIDEO_EXTS|SUB_EXTS|{"idx","zip","rar","nfo","txt"}):
        ext=m.group(1).upper(); name=name[:m.start()]
    name=strip_series_promos(name)
    year_m=re.search(r'(?<!\d)((?:19|20)\d{2})(?!\d)',name)
    year=int(year_m.group(1)) if year_m else None
    # Remove common leading release groups without treating them as the show title.
    name=re.sub(r'^\s*\[(?:SubsPlease|Erai[- ]?Raws|HorribleSubs|Judas|EMBER|PSA)\]\s*','',name,flags=re.I)
    season=None; episodes=[]; spans=[]; absolute=False
    # Explicit season+episode markers have priority, but season and episode are retained separately.
    for pat in (_S_EP, _X_EP):
        m=pat.search(name)
        if m:
            if pat is _S_EP:
                season=int(m.group(1)); episodes=_nums(m.group(2),m.group(3))
            else:
                season=int(m.group(1)); episodes=[int(m.group(2))]
            spans.append(m.span()); break
    # "Season 15 - EP18", "EP18 Guy Season 15", and "Season 1 Episode 1".
    sm=_SEASON_WORD.search(name)
    if sm:
        season=int(sm.group(1)); spans.append(sm.span())
    if not episodes:
        em=_EP_WORD.search(name) or _HASH_EP.search(name)
        if em:
            if em.re is _EP_WORD:
                episodes=_nums(em.group(1),em.group(2))
            else:
                episodes=[int(em.group(1))]
            spans.append(em.span())
    # Season X Episode Y: season-word and episode-word are independent.
    if not episodes and sm:
        tail=name[sm.end():]
        em=_EP_WORD.search(tail)
        if em:
            episodes=_nums(em.group(1),em.group(2))
            spans.append((sm.end()+em.start(),sm.end()+em.end()))
    # "S01E01E02" and "S01E01-E03" variants.
    if episodes:
        for m in re.finditer(r'(?i)(?<=\d)E\s*0*(\d{1,4})(?!\d)',name):
            n=int(m.group(1))
            if n not in episodes and abs(n-episodes[-1])<=12:
                episodes.append(n); spans.append(m.span())
        episodes=sorted(set(episodes))
    # Anime-style title - 18, only if no explicit episode marker or explicit season marker.
    if not episodes and season is None and sm is None and not _PACK_CUE.search(name) and not _SEASONS_RANGE.search(name) and not re.search(r'(?i)(?<!\w)S\s*\d{1,2}(?![A-Za-z0-9])', name):
        ms=list(re.finditer(r'(?<!\w)(\d{1,3})(?!\w)',name))
        for m in reversed(ms):
            n=int(m.group(1))
            if 1 <= n <= 999 and not (1900 <= n <= 2099) and m.start()>0:
                before=name[:m.start()]
                if not _TECH_WORDS.search(m.group(0)) and _clean_title(before):
                    episodes=[n]; absolute=True
                    # Anime filenames often use a standalone marker before the
                    # number (e.g. "One Piece Ep 18"). The number is detected
                    # by the absolute-episode fallback, so include that marker
                    # in the removed span rather than leaving "Ep" in the show.
                    marker=re.search(r'(?i)(?:episode|ep|e)\.?[\s._-]*$', before)
                    span_start=marker.start() if marker else m.start()
                    spans.append((span_start,m.end())); break
    # Remove season / episode tags before extracting the remaining show title.
    primary_span=min(spans,key=lambda span:span[0]) if spans else None
    cleaned=name
    merged_spans=[]
    for a,b in sorted(set(spans)):
        if merged_spans and a <= merged_spans[-1][1]:
            merged_spans[-1]=(merged_spans[-1][0],max(b,merged_spans[-1][1]))
        else:
            merged_spans.append((a,b))
    for a,b in reversed(merged_spans):
        cleaned=cleaned[:a]+' '+cleaned[b:]
    # Pack is allowed only without an episode number and with an actual pack cue.
    pack_cue=bool(_PACK_CUE.search(name) or _SEASONS_RANGE.search(name) or re.search(r'(?i)(?<!\w)S\d{1,2}(?![A-Za-z0-9])',name))
    if episodes:
        kind='episode'
        if season is None: season=0
    elif pack_cue:
        kind='pack'; episodes=[]
        if season is None:
            sm2=_SEASON_WORD.search(name); st=re.search(r'(?i)(?<!\w)S\s*0*(\d{1,2})(?!\d)',name)
            season=int(sm2.group(1) if sm2 else st.group(1)) if (sm2 or st) else 0
    else:
        return SeriesParsed(raw=raw,kind='none',ext=ext)
    # A title before a tag is the show name; trailing text is usually the episode title.
    # If the tag leads the filename, the title is taken from what remains after it.
    ep_title=""
    show=""
    if primary_span:
        a,b=primary_span
        before=_clean_title(name[:a])
        after=_clean_title(name[b:])
        if before:
            show=before
            ep_title=after[:80]
        else:
            show=_clean_title(cleaned)
    else:
        show=_clean_title(cleaned)
    if not show:
        show=_clean_title(re.sub(r'(?i)\b(?:episode|ep|e)\.?\s*\d{1,4}\b',' ',raw))
    meta=parse_filename(name)
    # Read technical markers from either side of the episode tag, including bracketed suffixes.
    tech_tokens=_TECH_WORDS.findall(name)
    probe=parse_filename("Series " + " ".join(tech_tokens)) if tech_tokens else meta
    from dataclasses import replace
    meta=replace(meta,resolution=meta.resolution or probe.resolution,
                 source=meta.source or probe.source,codec=meta.codec or probe.codec,
                 languages=meta.languages or probe.languages,tags=meta.tags or probe.tags,
                 group=meta.group or probe.group)
    if meta.group and re.fullmatch(r'(?i)(?:S\d{1,2}(?:E\d{1,4}(?:E\d{1,4})?)?|EP?\.?\d{1,4}|E\d{1,4})',meta.group):
        meta=replace(meta,group=None)
    if not meta.resolution:
        rm=re.search(r'(?i)\b(2160|1440|1080|720|576|480|360|240)p\b',name)
        if rm: meta=replace(meta,resolution=rm.group(1)+'p')
    return SeriesParsed(raw=raw,kind=kind,show=_smart_case(show) if show else '',year=year,
        season=int(season or 0),episodes=tuple(episodes),absolute=absolute,ep_title=ep_title,
        resolution=meta.resolution,source=meta.source,codec=meta.codec,languages=meta.languages,
        group=meta.group,tags=meta.tags,ext=ext or meta.ext)

