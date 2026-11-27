"""Classify media the series parser did not turn into an episode.

Classification is a *label on a skip record*. It never creates, renames or
modifies a catalogue show/file: a "likely film" is not a film entry and an
"unrecognised series" is not a show. When evidence is weak the answer is
"ambiguous", never a confident guess.

Categories (skip categories):
  film                 likely standalone film (reviewable list, no film catalogue)
  unrecognised_series  looks like a series episode in a format the parser lacks
  ambiguous            documentaries, specials, sports events, uncertain material
  unreadable           not enough usable filename information
  lookalike            suspected duplicate copy (written by the indexer, not here)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from registries import strip_uploader_prefix
from series_parse import (NON_INDEXABLE_EXTS, _EP_NAMES, _SEASON_NAMES, _meta, _PROMO_WORDS, _strip_promo_tokens, _RES_RE, _YEAR,
                          is_generic_media_name, parse_series)

SERIES = "series"
FILM = "film"
UNRECOGNISED = "unrecognised_series"
AMBIGUOUS = "ambiguous"
UNREADABLE = "unreadable"
LOOKALIKE = "lookalike"
SKIP_CATEGORIES = (FILM, UNRECOGNISED, AMBIGUOUS, UNREADABLE, LOOKALIKE)

CATEGORY_LABELS = {
    FILM: "Likely films",
    UNRECOGNISED: "Unrecognised series",
    AMBIGUOUS: "Other / ambiguous",
    UNREADABLE: "Unreadable",
    LOOKALIKE: "Look-alikes",
}
CATEGORY_BLURBS = {
    FILM: "Standalone title with a release year and release metadata, and no episode marker. Review list only: nothing is added to a catalogue.",
    UNRECOGNISED: "Looks like a series episode but uses a format the parser does not support. Use /adopt to place them.",
    AMBIGUOUS: "Documentaries, specials, sports events and anything uncertain.",
    UNREADABLE: "Not enough usable filename information to tell what this is.",
    LOOKALIKE: "Same show, episode, resolution, codec and exactly the same size as a copy already saved. The first copy is kept.",
}

# Reason codes -> human text
REASONS = {
    "unsupported_episode_format": "Looks like a series episode, but the numbering format is not supported",
    "manual_adoption_candidate": "Known legacy numbering (e.g. 412 = S04E12): place it with /adopt ... legacy",
    "ambiguous_dual_number": "Multiple episode numbers (e.g. 330 [132]): configure a numbering rule in /parserrules",
    "no_episode_marker": "Readable title but no episode marker",
    "likely_standalone_film": "Title + year + release metadata and no episode marker",
    "insufficient_title_information": "Not enough usable title information",
    "documentary": "Documentary",
    "ambiguous_special": "Special, sports event or one-off without enough identifying information",
    "possible_duplicate": "Look-alike (same size)",
    "non_video_file": "Subtitle, archive, audio or other non-video file",
    "extras_or_featurette": "Extra, featurette, bloopers, highlights or interview",
    "franchise_movie": "A movie of a series (Movie N): review list only, no episode",
    "legacy_unclassified": "Stored before classification existed: run /reclassify",
}
LOOKALIKE_REASON = "Look-alike (same size)"          # unchanged text: existing records use it

_YEAR_TOKEN = re.compile(r"(?<![A-Za-z0-9])(19\d{2}|20[0-3]\d)(?![A-Za-z0-9])")
_NON_VIDEO_EXT = frozenset("srt ass ssa sub vtt idx zip rar 7z tar gz txt nfo flac mp3 aac m4a wav ogg opus jpg jpeg png gif pdf epub 001 002 exe apk".split())
_EXTRAS = re.compile(
    r"(?i)(?<![A-Za-z0-9])(highlights?|bloopers?|interviews?|behind[ ._-]the[ ._-]scenes|previews?|a[ ._-]look[ ._-]back|"
    r"memories|alternate[ ._-]ending|featurette|making[ ._-]of|deleted[ ._-]scenes|trailers?|teasers?|soundtrack|"
    r"in[ ._-]\d+[ ._-]minutes|talk[ ._-]with)(?![A-Za-z0-9])")
_DEVICE_NAME = re.compile(r"(?i)^(?:video|vid|img|mov|clip|pxl|photo|audio|voice|rec|screen[ _-]?recording|screenshot)[ _-]*[\d\-_ .:]+$")
_MOVIE_N = re.compile(r"(?i)(?<![A-Za-z0-9])(?:movie|film|gekijouban)\s*0*\d{1,2}(?![A-Za-z0-9])")
_DOCUMENTARY = re.compile(r"(?i)(?<![A-Za-z0-9])(documentary|documentaries|docuseries|docu[ ._-]?series)(?![A-Za-z0-9])")
_SPECIAL_CUE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(special|specials|ppv|pay[ ._-]?per[ ._-]?view|title[ ._-]match|fight[ ._-]night|ufc|"
    r"wrestlemania|summerslam|royal[ ._-]rumble|highlights|awards?|ceremony|live[ ._-]event|concert|tribute|"
    r"nxt|wwe|aew|boxing)(?![A-Za-z0-9])")
# "This is an episode of something" cues. Codes (S01, E05, 1x05) are strong; marker WORDS (season,
# episode, folge, bölüm ...) are only trusted when the name carries no release year, because
# films such as "Season of the Witch (2011)" also contain those words.
_SERIES_CODE_CUE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:S\d{1,2}|E\d{1,3}|\d{1,2}x\d{1,3})(?![A-Za-z0-9])")
_SERIES_WORD_CUE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:%s|%s)(?![A-Za-z0-9])" % (_SEASON_NAMES, _EP_NAMES))


def _clean_for_guess(name: str) -> str:
    text = (name or "").strip()
    text = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", text)
    text, _ = strip_uploader_prefix(text)
    text = _PROMO_WORDS.sub(" ", text)
    text = _strip_promo_tokens(text)
    text = re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", text)
    if len(re.findall(r"(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])", text)) >= 2:
        text = re.sub(r"(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_guess(name: str) -> tuple[str, int | None]:
    """Best-effort display title and year for a non-series file (used only for labelling/review)."""
    text = _clean_for_guess(name)
    year = None
    ym = _YEAR_TOKEN.search(text)
    head = text
    if ym and text[:ym.start()].strip():
        year = int(ym.group(1)); head = text[:ym.start()]
    else:
        cut = len(head)
        for w in re.finditer(r"\S+", head):
            tok = w.group(0)
            if _RES_RE.fullmatch(tok) or re.fullmatch(r"(?i)web[-.]?dl|webrip|bluray|hdrip|dvdrip|bdrip|hdtv|x26[45]|hevc|h26[45]", tok):
                cut = w.start(); break
        head = head[:cut]
    return re.sub(r"\s+", " ", head).strip(" -–—:|,._+~()"), year


@dataclass(frozen=True)
class Classification:
    category: str            # one of SERIES + SKIP_CATEGORIES
    code: str                # reason code
    title: str = ""          # title guess, for review only
    year: int | None = None

    @property
    def reason(self) -> str:
        return LOOKALIKE_REASON if self.category == LOOKALIKE else REASONS.get(self.code, self.code)


def classify_text(name: str, caption: str = "", parsed=None, known_show: bool = False) -> Classification:
    """Classify a file that did not become a catalogue episode.

    ``parsed`` is the SeriesParsed the indexer already produced (only its ``hint`` is used here);
    ``known_show`` says the guessed title matches an existing catalogue show, which rules out "film".
    """
    hint = getattr(parsed, "hint", "") if parsed is not None else ""
    base = (name or "").strip()
    if not base or is_generic_media_name(base):           # a generic filename falls back to the caption
        base = (caption or "").strip() or base
    if hint == "manual_adoption_candidate":
        return Classification(UNRECOGNISED, "manual_adoption_candidate", getattr(parsed, "show", ""), None)
    if hint == "ambiguous_dual_number":
        return Classification(AMBIGUOUS, "ambiguous_dual_number", getattr(parsed, "show", ""), None)
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if ext in _NON_VIDEO_EXT:                             # subtitles/archives/audio are never episodes or films
        return Classification(AMBIGUOUS, "non_video_file", title_guess(base)[0])
    if not base or base == "(no filename)" or is_generic_media_name(base):
        return Classification(UNREADABLE, "insufficient_title_information")
    if _DEVICE_NAME.match(re.sub(r"\.[A-Za-z0-9]{2,4}$", "", base)):      # video_2026-11-11_00-11-23: a phone/recorder name
        return Classification(UNREADABLE, "insufficient_title_information")
    title, year = title_guess(base)
    letters = re.findall(r"[^\W\d_]", title)
    if len(letters) < 3:
        return Classification(UNREADABLE, "insufficient_title_information", title, year)
    low = _clean_for_guess(base)
    # 1. documentaries are never silently turned into a series or a film
    if _DOCUMENTARY.search(low):
        return Classification(AMBIGUOUS, "documentary", title, year)
    # 1b. bonus material: "Season Two Bloopers", "Episode Previews", "Season One - A Look Back"
    res0, src0, cod0, _l0, _t0 = _meta(base)
    film_like = bool(year and (res0 or src0 or cod0) and 1888 <= year <= 2035 and not known_show)
    if _EXTRAS.search(low) and not film_like:             # "The Interview (2014) 1080p BluRay" is a film, not an extra
        return Classification(AMBIGUOUS, "extras_or_featurette", title, year)
    # 2. looks like a series episode in an unsupported format
    if _SERIES_CODE_CUE.search(low) or (not year and _SERIES_WORD_CUE.search(low)):
        return Classification(UNRECOGNISED, "unsupported_episode_format", title, year)
    # 2b. "Show - Movie 14": a movie of a franchise. Goes to the film review list, never to an episode.
    if _MOVIE_N.search(low):
        return Classification(FILM, "franchise_movie", title, year)
    # 3. specials / sports / one-offs lacking a date or number
    if _SPECIAL_CUE.search(low):
        return Classification(AMBIGUOUS, "ambiguous_special", title, year)
    # 4. conservative film detection: title + year + release metadata, no episode marker, not a known show
    res, source, codec, _langs, _tags = _meta(base)
    has_release_meta = bool(res or source or codec)
    if year and has_release_meta and not known_show and 1888 <= year <= 2035:
        return Classification(FILM, "likely_standalone_film", title, year)
    # 5. readable but unproven
    return Classification(AMBIGUOUS, "no_episode_marker", title, year)


def classify_name(name: str, caption: str = "", promos=(), known_show: bool = False, rules=None) -> Classification:
    """Convenience: parse with the series parser first; recognised episodes are category 'series'."""
    from series_catalog import _resolve_parsed
    parsed, _generic = _resolve_parsed(name or "", caption or "", promos, rules=rules)
    if parsed.kind in ("episode", "pack") and parsed.show:
        return Classification(SERIES, "recognised", parsed.show, parsed.year)
    return classify_text(name, caption, parsed, known_show)
