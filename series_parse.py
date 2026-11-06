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
from dataclasses import dataclass

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
          "8bit", "nf", "amzn", "dsnp", "hmax", "atvp", "hulu", "cr", "sdr", "av1", "vostfr", "multisub"})
_KNOWN_GROUPS = {"galaxyrg", "psa", "rarbg", "yify", "yts", "megusta", "pahe", "phun", "mkvking",
                 "subsplease", "erai-raws", "eraraws", "animerg", "horriblesubs",
                 "anime time", "animetime", "tigole", "qxr", "ntb", "ntg", "ion10", "cakes",
                 "dkb", "sallysubs", "bonkai77", "df68"}

_PLATFORMS = {"wetv", "iqiyi", "iq", "viu", "netflix", "amazon", "amzn", "nf", "disney", "dsnp", "hulu", "hbo", "hmax",
              "atvp", "appletv", "youku", "viki", "kocowa", "tving", "wavve", "mxplayer", "zee5", "hotstar", "sonyliv",
              "jiocinema", "crunchyroll", "funimation", "hidive", "bilibili", "abema", "tubi", "peacock", "paramount",
              "max", "mubi", "sbs", "kbs", "mbc", "jtbc", "tvn", "ocn", "nhk"}
_PACK_CUE = re.compile(r"(?i)\b(?:complete\s+series|complete\s+season|complete|full\s+season|season\s+pack|full\s+series|batch|collection|all\s+episodes)\b")
_SEASON_RANGE = re.compile(r"(?i)(?:\bseasons?\s*\d{1,2}\s*[-–~]\s*\d{1,2}\b|(?<![A-Za-z0-9])S\s*0*\d{1,2}\s*[-–~]\s*S\s*0*\d{1,2}(?![A-Za-z0-9]))")
_PROMO_WORDS = re.compile(r"(?i)\b(?:join(?:\s+our)?(?:\s+channel)?|downloaded\s+from|uploaded\s+by|powered\s+by|subscribe(?:\s+to)?|channel\s*:)\s*(?:@[\w.]+|t\.me/\S+|telegram\.me/\S+|(?:www\.)?[\w-]{3,}\.(?:com|net|org|xyz|io|cc|ng|tv|me)(?![A-Za-z0-9]))")
_PROMO_TOKEN = re.compile(r"(?i)(?:@[\w.]+|\bt\.me/\S+|\btelegram\.me/\S+|\bhttps?://\S+|\bwww\.[\w.-]+|(?<![A-Za-z0-9])[\w-]{3,}\.(?:com|net|org|xyz|io|cc|ng)(?![A-Za-z0-9]))")
_HASH = re.compile(r"^[0-9A-Fa-f]{8}$")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_RES_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(2160|1440|1080|720|576|540|480|360|240)p(?![A-Za-z0-9])")
_BRACKET = re.compile(r"\[([^\]]*)\]|\(([^)]*)\)|\{([^}]*)\}")

# Season/episode markers, tried in this order.
_S_EP = re.compile(r"(?i)(?<![A-Za-z0-9])S\s*0*(\d{1,2})\s*[ ._-]*E(?:P(?:ISODE)?)?\s*0*(\d{1,4})(?:v\d)?(?:\s*(?:-|–|&|E|and)\s*E?\s*0*(\d{1,4}))?(?![A-Za-z0-9])")
_S_DASH_EP = re.compile(r"(?i)(?<![A-Za-z0-9])S\s*0*(\d{1,2})\s*[-–:]\s*(?:E(?:P(?:ISODE)?)?\s*)?0*(\d{1,4})(?:v\d)?(?:\s*[-–]\s*0*(\d{1,4}))?(?![A-Za-z0-9])")
_X_EP = re.compile(r"(?i)(?<![A-Za-z0-9])(\d{1,2})\s*x\s*0*(\d{2,4})(?![A-Za-z0-9])")
_SEASON_WORD = re.compile(r"(?i)(?<![A-Za-z0-9])season\s*0*(\d{1,2})(?![A-Za-z0-9])")
_S_ONLY = re.compile(r"(?i)(?<![A-Za-z0-9])S\s*0*(\d{1,2})(?![A-Za-z0-9])")
_EP_WORD = re.compile(r"(?i)(?<![A-Za-z0-9])(?:episodes?|eps?|e)\.?\s*0*(\d{1,4})(?:v\d)?(?:\s*[-–]\s*E?\s*0*(\d{1,4}))?(?![A-Za-z0-9])")
_HASH_EP = re.compile(r"(?<![A-Za-z0-9])#\s*0*(\d{1,4})(?![A-Za-z0-9])")
_SPECIAL = re.compile(r"(?i)(?<![A-Za-z0-9])(OVA|OAD|ONA|SPECIALS?|SP|EXTRAS?)\s*[-._ ]*0*(\d{1,3})(?![A-Za-z0-9])")
_PART = re.compile(r"(?i)(?<![A-Za-z0-9])(Part|Cour)\s*0*(\d{1,2})(?![A-Za-z0-9])")


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

    @property
    def first(self): return self.episodes[0] if self.episodes else None
    @property
    def last(self): return self.episodes[-1] if self.episodes else None



GENERIC_MEDIA_NAMES = {"video", "vid", "document", "file", "audio", "photo", "img", "dsc", "pxl",
                       "screenshot", "recording", "untitled", "download", "new", "movie", "film"}

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

_SMALL = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "vs", "with", "no"}


def _smart_case(title: str) -> str:
    """Fix ALL-CAPS and all-lowercase names; leave mixed case alone."""
    if title != title.lower() and title != title.upper():
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
    text = _PROMO_TOKEN.sub(" ", text)
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
    if _PROMO_TOKEN.search(s) or _PROMO_WORDS.search(s): return "drop", None
    if _HASH.fullmatch(s) and re.search(r"\d", s) and re.search(r"[A-Fa-f]", s): return "drop", None
    if _YEAR.fullmatch(s): return "year", int(s)
    if re.fullmatch(r"(?i)(?:S\s*\d{1,2}.*|E(?:P(?:ISODE)?)?\s*\d{1,4}.*|\d{1,2}x\d{2,4}|\d{1,4}|(?:OVA|OAD|ONA|SP|SPECIAL)\s*\d{1,3}|season\s*\d{1,2}.*)", s):
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
    name = raw
    ext = None
    m = re.search(r"\.([A-Za-z0-9]{2,4})$", name)
    if m and m.group(1).lower() in _KNOWN_EXTS:
        ext = m.group(1).upper(); name = name[:m.start()]
    em_ = re.search(r"(?i)\.(mkv|mp4|avi|mov|wmv|flv|webm|m4v|mpg|mpeg)(?![A-Za-z0-9])", name)
    if em_ and em_.end() < len(name):            # "Show.S01E01.720p.mkv ▓▓ Join Group ▓▓" -> drop the trailer
        ext = em_.group(1).upper(); name = name[:em_.start()]
    # Older Turkish-drama uploads encode the episode number immediately before
    # a Telegram handle: "Esaret 1-@PlanetDizi.mkv" or
    # "Esaret 2 -@PlanetDizi.mkv". Preserve the number as an episode marker
    # before removing the handle, or the remaining bare number is ambiguous.
    handle_episode = re.match(
        r"(?is)^(?P<title>.+?)\s+(?P<episode>0*\d{1,4})\s*[-–—|:,]\s*@(?P<handle>[A-Za-z0-9_.]+)$",
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
    name = _PROMO_TOKEN.sub(" ", name)
    name = name.replace("_", " ")
    # Dots as word separators ("Show.Name.S01E01.720p") but not in "Mr. Robot S01E01".
    if len(re.findall(r"(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])", name)) >= 2:
        name = re.sub(r"(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])", " ", name)

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
    if mm: take(mm, int(mm.group(1)), _nums(mm.group(2), mm.group(3)))
    if not episodes:
        mm = _S_DASH_EP.search(text)
        if mm and int(mm.group(2)) not in _RES_SET and not (1900 <= int(mm.group(2)) <= 2099) \
                and not re.match(r"(?i)\s*S\s*\d{1,2}\s*[-–]\s*S", mm.group(0)):
            take(mm, int(mm.group(1)), _nums(mm.group(2), mm.group(3)))
    if not episodes:
        mm = _X_EP.search(text)
        if mm and int(mm.group(1)) < 100 and int(mm.group(2)) not in _RES_SET:
            take(mm, int(mm.group(1)), [int(mm.group(2))])
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
            special = True; special_label = f"{kw} {int(sp.group(2)):02d}"
            episodes = [int(sp.group(2))]; spans.append(sp.span())
    if not episodes:
        em = _EP_WORD.search(text)
        if em: episodes = _nums(em.group(1), em.group(2)); spans.append(em.span())
    if not episodes:
        hm = _HASH_EP.search(text)
        if hm: episodes = [int(hm.group(1))]; spans.append(hm.span())
    if not episodes and not _PACK_CUE.search(text) and not _SEASON_RANGE.search(text):
        # Anime releases commonly use a bare, zero-padded absolute number at
        # the end: "Bleach 048.mkv" / "One Piece 836".
        trailing=re.search(r"(?<![A-Za-z0-9])0*(\d{2,4})\s*$",text)
        generic_leftover={"video", "vid", "document", "file", "audio", "photo", "img", "dsc", "pxl", "screenshot", "recording", "untitled", "download", "new", "movie", "film"}
        leftover=re.sub(r"[ _-]+", " ", text[:trailing.start()].strip(" -–—:|,._")).strip().casefold() if trailing else ""
        if trailing and leftover not in generic_leftover and leftover and not 1900<=int(trailing.group(1))<=2099 and not re.search(r"(?i)\b(?:movie|film)\b",text[:trailing.start()]):
            episodes=[int(trailing.group(1))]; absolute=True; spans.append(trailing.span())
    if not episodes and not _PACK_CUE.search(text) and not _SEASON_RANGE.search(text):
        # Bare number: "Show - 112", "Show - 05 - Title", "Show 1145 720p".
        toks = [(t.group(0), t.start(), t.end()) for t in re.finditer(r"\S+", text)]
        for i, (tk, a, b) in enumerate(toks):
            if i == 0 or not re.fullmatch(r"0*\d{1,4}(?:v\d)?", tk): continue
            n = int(re.match(r"\d+", tk).group(0))
            if n == 0 or 1900 <= n <= 2099: continue
            prev = toks[i - 1][0]; rest = [t[0] for t in toks[i + 1:]]
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

    pm = _PART.search(text)
    if pm:
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
        s = _SEASON_RANGE.sub(" ", s); s = _PACK_CUE.sub(" ", s)
        s = re.sub(r"(?i)\bseasons?\b", " ", s)
        s = _PART.sub(" ", s)
        kept = []
        for w in s.split():
            lw = w.lower().strip("-–—:,.")
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
        if t and re.search(r"[A-Za-z0-9]", t):
            title = t; title_seg = sg; break
    ym = re.search(r"(?<![A-Za-z0-9])((?:19|20)\d{2})$", title)
    if ym and title[:ym.start()].strip(" -–—:|,."):
        year = year or int(ym.group(1)); title = title[:ym.start()].strip(" -–—:|,.")
    # A generic media label must never become a catalogue title, regardless
    # of which episode-number rule recognised the adjacent number.
    if title.casefold() in GENERIC_MEDIA_NAMES:
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
    return SeriesParsed(raw=raw, kind=kind, show=_smart_case(title), year=year, season=int(season or 0),
                        episodes=tuple(sorted(set(episodes))), absolute=absolute, ep_title=part_label,
                        special=special, special_label=special_label, resolution=res, source=source,
                        codec=codec, languages=langs, group=group, tags=tags, ext=ext)


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
