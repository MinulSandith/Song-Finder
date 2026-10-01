"""Extract a list of Sinhala song names from a photo using Gemini's vision model,
check each name against YouTube to correct OCR misreads, then work out who first
sang each song so YouTube results can be narrowed to the original recording."""

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

from google import genai
from google.genai import types

from backend import netfix  # noqa: F401  (patches socket.getaddrinfo on import)
from backend.activity_log import log_event
from backend.search import search_youtube

logger = logging.getLogger(__name__)

T = TypeVar("T")

PROMPT = (
    "This image contains a list of song names written in Sinhala "
    "(handwritten or printed). Read every line carefully and perform OCR.\n"
    "Return ONLY a JSON array of strings, one per song entry, exactly as "
    "written in the image (keep the Sinhala script, do not translate or "
    "transliterate). Do not include numbering, bullets, or any commentary. "
    "If a line has both an artist and a song name, keep them together as "
    "one string. Skip empty or unreadable lines."
)

VERIFY_PROMPT = (
    "This photo contains a handwritten or printed list of Sinhala song names. "
    "An OCR pass read the entries below from it, but OCR often misreads "
    "Sinhala (similar-looking letters, missing or extra vowel signs, merged "
    "or split words), so some of them may not be real song names. Under each "
    "entry are the top YouTube search results for the scanned text (titles "
    "may be in Sinhala or romanized Sinhala).\n\n"
    "{entries}\n\n"
    "For each entry, use the YouTube results and your own knowledge of "
    "Sinhala songs to decide whether it is a real song.\n"
    "- If it is, return its correct title.\n"
    "- If it is not, the OCR probably misread it. Look at the photo again "
    "and work out which real song the writer most likely meant: a result "
    "title that looks similar when handwritten, or a known song with a "
    "similar-looking name (by the artist, if one is written).\n"
    "- Only if no plausible real song can be found, keep the scanned text.\n\n"
    "Return ONLY a JSON array with exactly one object per entry, in the same "
    "order, each with these keys:\n"
    '  "scanned": the entry exactly as given above,\n'
    '  "title": the real song title in Sinhala script,\n'
    '  "artist": the singer\'s name in Sinhala script, or "" if unknown,\n'
    '  "status": "verified" if the scanned name is a real song, "corrected" '
    'if you replaced it with the song you believe was meant, or "unverified" '
    "if no matching song could be found,\n"
    '  "note": a short reason in English (e.g. "OCR read ක as න").\n'
    "No commentary outside the JSON array."
)

ORIGINAL_PROMPT = (
    "For each Sinhala song below, identify the ORIGINAL singer: the artist who "
    "first recorded and released it.\n"
    "- Do NOT give the singer of a later cover, remake, reprise or re-recorded "
    "version (for example a TV-show or viral cover), even if that version is "
    "the most popular one on YouTube.\n"
    "- The artist shown next to a title came from a YouTube lookup and may be "
    "someone who covered the song, so do not trust it.\n"
    "- For a duet or group, give the lead names joined with ' & '.\n"
    "- If you are not sure who first sang it, say so with a low confidence "
    "instead of guessing. Never invent a singer.\n\n"
    "{entries}\n\n"
    "Return ONLY a JSON array with exactly one object per song, in the same "
    "order, each with these keys:\n"
    '  "title": the song title exactly as given above,\n'
    '  "singer": the original singer\'s name in Sinhala script, or "" if you '
    "do not know,\n"
    '  "aliases": up to 4 other spellings of the same name as they would '
    'appear in a YouTube title or channel name, e.g. ["W. D. Amaradeva", '
    '"Amaradeva", "Amaradewa"],\n'
    '  "confidence": "high" only if you are sure this person first recorded '
    'the song, "medium" if fairly sure, otherwise "low",\n'
    '  "note": a short reason in English (e.g. "Original 1960s recording; '
    'later covered by Yohani").\n'
    "No commentary outside the JSON array."
)

STATUSES = {"verified", "corrected", "unverified"}
CONFIDENCES = {"high", "medium", "low"}

# YouTube titles shown to Gemini per scanned name when checking it.
VERIFY_RESULTS_PER_SONG = 5

# Ordered newest/best to most-available. gemini-3.8-flash is the current
# flagship free-tier flash model but has the tightest free daily quota
# (~20 requests/day as of 2026); the flash-lite models at the end have much
# higher free quotas (~500/day) so they make reliable last-resort fallbacks
# when the better models are rate-limited or temporarily overloaded.
FALLBACK_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]


def _model_chain() -> list[str]:
    """Preferred model first (from .env, if set), then the fallback chain, deduplicated."""

    preferred = os.environ.get("GEMINI_MODEL", "").strip()
    chain = ([preferred] if preferred else []) + FALLBACK_MODELS

    seen = set()
    ordered = []
    for model in chain:
        if model and model not in seen:
            seen.add(model)
            ordered.append(model)
    return ordered


def _client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or api_key == "your-gemini-api-key-here":
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add your key to the .env file."
        )
    return genai.Client(api_key=api_key)


def _parse_json_array(text: str) -> list:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()

    parsed = json.loads(cleaned)
    if not isinstance(parsed, list):
        raise ValueError("Model did not return a JSON array")
    return parsed


def _parse_song_list(text: str) -> list[str]:
    parsed = _parse_json_array(text)
    return [str(item).strip() for item in parsed if str(item).strip()]


def _generate_with_fallback(
    client: genai.Client,
    label: str,
    contents: list,
    config: types.GenerateContentConfig,
    parse: Callable[[str], T],
) -> tuple[T, str]:
    """Try each model in the chain until one answers with a reply `parse` accepts."""

    chain = _model_chain()
    errors: list[str] = []

    for i, model in enumerate(chain):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            logger.info("%s raw response from %s: %r", label, model, (response.text or "")[:1000])
            result = parse(response.text)
        except Exception as exc:
            logger.warning("%s failed on model %s: %s", label, model, exc)
            errors.append(f"{model}: {exc}")

            next_model = chain[i + 1] if i + 1 < len(chain) else None
            if next_model:
                log_event(
                    "warning",
                    f"{label}: {model} failed ({_short(exc)}), trying {next_model}…",
                )
            continue

        if i > 0:
            log_event(
                "warning",
                f"{label}: switched to fallback model {model} after {i} failure(s)",
            )
        return result, model

    log_event("error", f"{label}: all Gemini models failed (rate limits/traffic)")
    raise RuntimeError(
        "All Gemini models failed (likely rate limits/traffic on the free "
        "tier). Tried: " + "; ".join(errors)
    )


def extract_songs_from_image(image_bytes: bytes, mime_type: str) -> list[str]:
    client = _client()

    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    config = types.GenerateContentConfig(response_mime_type="application/json")

    log_event("info", f"OCR: scanning photo (starting with {_model_chain()[0]})")

    songs, model = _generate_with_fallback(
        client, "OCR", [image_part, PROMPT], config, _parse_song_list
    )

    if songs:
        log_event("success", f"OCR: found {len(songs)} song(s) using {model}")
    else:
        log_event(
            "warning",
            f"OCR: {model} read the photo but found no song names "
            "— try a clearer or more tightly cropped photo",
        )
    return songs


def _parse_verify_reply(text: str) -> list[dict]:
    items = [item for item in _parse_json_array(text) if isinstance(item, dict)]
    if not items:
        raise ValueError("Model returned no song objects")
    return items


def _unverified(scanned: str, note: str = "") -> dict:
    return {"scanned": scanned, "title": scanned, "artist": "", "status": "unverified", "note": note}


def _match_verified(scanned_songs: list[str], items: list[dict]) -> list[dict]:
    """Line the model's answers up with the scanned names, one result per name."""

    by_scanned = {str(item.get("scanned", "")).strip(): item for item in items}
    same_order = len(items) == len(scanned_songs)

    results = []
    for i, scanned in enumerate(scanned_songs):
        item = items[i] if same_order else by_scanned.get(scanned)
        title = str(item.get("title") or "").strip() if item else ""
        if not title:
            results.append(_unverified(scanned))
            continue

        status = str(item.get("status") or "").strip().lower()
        if status not in STATUSES:
            status = "verified" if title == scanned else "corrected"
        results.append(
            {
                "scanned": scanned,
                "title": title,
                "artist": str(item.get("artist") or "").strip(),
                "status": status,
                "note": str(item.get("note") or "").strip(),
            }
        )
    return results


def _youtube_titles(query: str) -> list[str]:
    try:
        return [
            f'"{r["title"]}"' + (f' ({r["channel"]})' if r["channel"] else "")
            for r in search_youtube(query, max_results=VERIFY_RESULTS_PER_SONG)
        ]
    except Exception as exc:
        logger.warning("Verify: YouTube lookup failed for %r: %s", query, exc)
        return []


def _format_entries(songs: list[str], lookups: list[list[str]]) -> str:
    blocks = []
    for i, (name, titles) in enumerate(zip(songs, lookups), start=1):
        lines = [f'{i}. Scanned: "{name}"', "   YouTube results:"]
        lines += [f"   - {t}" for t in titles] or ["   (none found)"]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def verify_songs(image_bytes: bytes, mime_type: str, songs: list[str]) -> list[dict]:
    """Look each scanned name up on YouTube, then have Gemini compare the photo,
    the scanned names and the real titles found, replacing OCR misreads with the
    song the writer most likely meant. Returns one result per scanned name, in order.
    """

    if not songs:
        return []

    client = _client()

    log_event("info", f"Verify: looking up {len(songs)} song name(s) on YouTube…")
    with ThreadPoolExecutor(max_workers=4) as pool:
        lookups = list(pool.map(_youtube_titles, songs))

    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    prompt = VERIFY_PROMPT.format(entries=_format_entries(songs, lookups))
    config = types.GenerateContentConfig(response_mime_type="application/json")

    items, model = _generate_with_fallback(
        client, "Verify", [image_part, prompt], config, _parse_verify_reply
    )
    results = _match_verified(songs, items)

    corrected = sum(r["status"] == "corrected" for r in results)
    unverified = sum(r["status"] == "unverified" for r in results)
    log_event(
        "success" if not unverified else "warning",
        f"Verify: {len(results) - corrected - unverified} confirmed, "
        f"{corrected} corrected, {unverified} not found (using {model})",
    )
    return results


def _parse_originals_reply(text: str) -> list[dict]:
    items = [item for item in _parse_json_array(text) if isinstance(item, dict)]
    if not items:
        raise ValueError("Model returned no singer objects")
    return items


def _unknown_singer(note: str = "") -> dict:
    return {"singer": "", "aliases": [], "confidence": "low", "note": note}


def _match_originals(songs: list[dict], items: list[dict]) -> list[dict]:
    """Line the model's answers up with the songs asked about, one result per song."""

    by_title = {str(item.get("title", "")).strip(): item for item in items}
    same_order = len(items) == len(songs)

    results = []
    for i, song in enumerate(songs):
        item = items[i] if same_order else by_title.get(song["title"])
        singer = str(item.get("singer") or "").strip() if item else ""
        if not singer:
            results.append(_unknown_singer(str(item.get("note") or "").strip() if item else ""))
            continue

        aliases = item.get("aliases")
        confidence = str(item.get("confidence") or "").strip().lower()
        results.append(
            {
                "singer": singer,
                "aliases": [str(a).strip() for a in aliases if str(a).strip()][:6]
                if isinstance(aliases, list)
                else [],
                "confidence": confidence if confidence in CONFIDENCES else "low",
                "note": str(item.get("note") or "").strip(),
            }
        )
    return results


def find_original_singers(songs: list[dict]) -> list[dict]:
    """Ask Gemini who first recorded each song. `songs` are {"title", "artist"} dicts;
    returns one {"singer", "aliases", "confidence", "note"} per song, in order.

    This relies on the model's own knowledge (no web lookup), so confidence is part
    of the answer: callers should treat anything below "high" as a hint, not a fact.
    """

    if not songs:
        return []

    client = _client()

    entries = "\n".join(
        f'{i}. "{s["title"]}"' + (f' (YouTube lookup artist: {s["artist"]})' if s["artist"] else "")
        for i, s in enumerate(songs, start=1)
    )
    config = types.GenerateContentConfig(response_mime_type="application/json")

    log_event("info", f"Original singer: looking up {len(songs)} song(s)…")
    items, model = _generate_with_fallback(
        client,
        "Original singer",
        [ORIGINAL_PROMPT.format(entries=entries)],
        config,
        _parse_originals_reply,
    )

    results = _match_originals(songs, items)

    found = sum(1 for r in results if r["singer"])
    log_event(
        "success" if found == len(results) else "warning",
        f"Original singer: found {found} of {len(results)} (using {model})",
    )
    return results


MORE_SONGS_PROMPT = (
    "For each Sinhala singer below, list {count} of their best-known songs "
    "that THEY themselves originally sang (not songs they only covered).\n"
    "- Only name songs you are confident really exist. Fewer is better than "
    "an invented title.\n"
    "- Skip any song in the 'already have' list.\n\n"
    "{entries}\n\n"
    "Already have: {have}\n\n"
    "Return ONLY a JSON array with exactly one object per singer, in the same "
    "order, each with these keys:\n"
    '  "singer": the singer exactly as given above,\n'
    '  "songs": an array of objects {{"title": the song title in Sinhala '
    'script, "title_en": the same title romanized in English letters as it '
    'would appear on YouTube}}.\n'
    "No commentary outside the JSON array."
)

# Candidates asked for per singer; more than the 4 wanted, since some won't be found on YouTube.
MORE_SONGS_CANDIDATES = 8


def _parse_more_songs_reply(text: str) -> list[dict]:
    items = [item for item in _parse_json_array(text) if isinstance(item, dict)]
    if not items:
        raise ValueError("Model returned no singer objects")
    return items


def suggest_songs_by_singers(singers: list[dict], have: list[str]) -> list[list[dict]]:
    """Ask Gemini for well-known songs each singer originally sang. `singers` are
    {"singer", "aliases"} dicts; returns one list of {"title", "title_en"} per singer,
    in order. These come from the model's memory, so callers should confirm them."""

    if not singers:
        return []

    client = _client()
    entries = "\n".join(
        f'{i}. {s["singer"]}' + (f' ({", ".join(s["aliases"][:3])})' if s["aliases"] else "")
        for i, s in enumerate(singers, start=1)
    )
    prompt = MORE_SONGS_PROMPT.format(
        count=MORE_SONGS_CANDIDATES,
        entries=entries,
        have="; ".join(have) if have else "(none)",
    )
    config = types.GenerateContentConfig(response_mime_type="application/json")

    log_event("info", f"More songs: asking for songs by {len(singers)} singer(s)…")
    items, model = _generate_with_fallback(
        client, "More songs", [prompt], config, _parse_more_songs_reply
    )

    by_name = {str(i.get("singer", "")).strip(): i for i in items}
    same_order = len(items) == len(singers)

    results = []
    for n, s in enumerate(singers):
        item = items[n] if same_order else by_name.get(s["singer"])
        raw = item.get("songs") if item else None
        songs = []
        for song in raw if isinstance(raw, list) else []:
            if isinstance(song, str):
                song = {"title": song}
            if isinstance(song, dict) and str(song.get("title") or "").strip():
                songs.append(
                    {
                        "title": str(song["title"]).strip(),
                        "title_en": str(song.get("title_en") or "").strip(),
                    }
                )
        results.append(songs)

    log_event("success", f"More songs: {sum(map(len, results))} candidate(s) (using {model})")
    return results


EXPAND_PROMPT = (
    "A user typed the names of Sinhala singers (in English or Sinhala letters, "
    "possibly misspelled). For each one:\n"
    "- Work out which Sinhala singer they mean.\n"
    "- List {count} of that singer's best-known songs that THEY themselves "
    "originally sang (not songs they only covered).\n"
    "- Only name songs you are confident really exist. Fewer is better than "
    "an invented title. If you don't recognise the singer, return no songs.\n\n"
    "{entries}\n\n"
    "Return ONLY a JSON array with exactly one object per typed name, in the "
    "same order, each with these keys:\n"
    '  "typed": the name exactly as given above,\n'
    '  "singer": the singer\'s name in Sinhala script ("" if not recognised),\n'
    '  "aliases": up to 4 other spellings as they would appear in a YouTube '
    'title or channel name, e.g. ["W. D. Amaradeva", "Amaradeva"],\n'
    '  "songs": an array of objects {{"title": the song title in Sinhala '
    'script, "title_en": the same title romanized in English letters as it '
    'would appear on YouTube}}.\n'
    "No commentary outside the JSON array."
)


def expand_singers(names: list[str], count: int) -> list[dict]:
    """Ask Gemini who each typed singer name is and for songs they originally sang.
    Returns one {"singer", "aliases", "songs": [{"title", "title_en"}]} per name, in
    order. The songs come from the model's memory, so callers should confirm them."""

    if not names:
        return []

    client = _client()
    entries = "\n".join(f'{i}. "{n}"' for i, n in enumerate(names, start=1))
    config = types.GenerateContentConfig(response_mime_type="application/json")

    log_event("info", f"Expand: looking up {len(names)} singer(s)…")
    items, model = _generate_with_fallback(
        client,
        "Expand",
        [EXPAND_PROMPT.format(count=count, entries=entries)],
        config,
        _parse_more_songs_reply,
    )

    by_typed = {str(i.get("typed", "")).strip(): i for i in items}
    same_order = len(items) == len(names)

    results = []
    for n, typed in enumerate(names):
        item = items[n] if same_order else by_typed.get(typed)
        item = item or {}
        aliases = item.get("aliases")
        raw = item.get("songs")
        songs = []
        for song in raw if isinstance(raw, list) else []:
            if isinstance(song, str):
                song = {"title": song}
            if isinstance(song, dict) and str(song.get("title") or "").strip():
                songs.append(
                    {
                        "title": str(song["title"]).strip(),
                        "title_en": str(song.get("title_en") or "").strip(),
                    }
                )
        results.append(
            {
                "singer": str(item.get("singer") or "").strip(),
                "aliases": [str(a).strip() for a in aliases if str(a).strip()][:6]
                if isinstance(aliases, list)
                else [],
                "songs": songs,
            }
        )

    log_event("success", f"Expand: {sum(len(r['songs']) for r in results)} candidate(s) (using {model})")
    return results


def _short(exc: Exception, limit: int = 120) -> str:
    text = str(exc)
    return text if len(text) <= limit else text[:limit] + "…"
