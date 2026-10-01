"""YouTube search using yt-dlp (no API key required)."""

import re
import unicodedata

import yt_dlp

from backend import netfix  # noqa: F401  (patches socket.getaddrinfo on import)

# Words that mark a video as someone else's take on a song (or a derivative of it).
COVER_WORDS = re.compile(
    r"\b(covers?|kara?o?ke|karaoka|instrumental|remix|reverb|slowed|lo-?fi|tribute|remake|reprise)\b",
    re.IGNORECASE,
)
COVER_WORDS_SINHALA = ("කවර්", "කැරෝකේ", "කරාඔකේ", "රීමික්ස්", "රිමික්ස්")

# When filtering by singer, look further down the results than a plain search
# does: the original is often not in the first few hits next to popular covers.
SINGER_SEARCH_RESULTS = 15

# If a search with the singer's name added turns up fewer originals than this,
# the plain title is searched too and the two result sets are merged.
ENOUGH_ORIGINALS = 3


def search_youtube(query: str, max_results: int = 8, singers: list[str] | None = None) -> list[dict]:
    """Search YouTube. With `singers` (names/spellings of the original singer), each
    result also gets a "kind" — "original", "other" or "cover" — and results are
    ordered original first, covers last.

    The singer's Latin spelling is added to the query because most uploads are
    titled and credited that way, but a very specific query can come back empty
    (e.g. for old songs whose original singer has little on YouTube), so a thin
    result falls back to the plain title as well.
    """

    if not singers:
        return _search(query, max_results)

    count = max(max_results, SINGER_SEARCH_RESULTS)
    latin_name = next((s for s in singers if s.isascii()), "")

    results = _search(f"{query} {latin_name}", count) if latin_name else []
    ranked = rank_by_singer(results, singers)
    if sum(r["kind"] == "original" for r in ranked) < ENOUGH_ORIGINALS:
        seen = {r["id"] for r in results}
        results += [r for r in _search(query, count) if r["id"] not in seen]
        ranked = rank_by_singer(results, singers)
    return ranked


def _search(query: str, max_results: int) -> list[dict]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "default_search": "ytsearch",
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)

    results = []
    for entry in info.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        video_id = entry["id"]
        results.append(
            {
                "id": video_id,
                "title": entry.get("title") or "Unknown title",
                "channel": entry.get("channel") or entry.get("uploader") or "",
                "duration": entry.get("duration"),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                # hqdefault.jpg always exists for public videos, so this is a
                # reliable fallback regardless of what extract_flat returns.
                "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            }
        )
    return results


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation, keeping Sinhala letters *and* their vowel
    signs (marks), which a plain \\w split would cut words apart on."""

    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.replace("‍", "").replace("‌", "")  # Sinhala conjunct joiners
    kept = (c if c.isalnum() or unicodedata.category(c)[0] == "M" else " " for c in text)
    return " ".join("".join(kept).split())


def _alias_tokens(alias: str) -> list[str]:
    # Initials ("W. D.") and tiny words can't identify anyone, so only longer tokens count.
    return [t for t in _normalize(alias).split() if len(t) >= 3]


def _names_singer(text: str, singers: list[str]) -> bool:
    """True if `text` contains every significant word of at least one singer spelling.
    Words match from their start, so Sinhala suffixes ("අමරදේවයන්") still count."""

    padded = f" {text}"
    for alias in singers:
        tokens = _alias_tokens(alias)
        if tokens and all(f" {t}" in padded for t in tokens):
            return True
    return False


def _is_cover(title: str, channel: str) -> bool:
    haystack = f"{title} {channel}"
    return bool(COVER_WORDS.search(haystack)) or any(w in haystack for w in COVER_WORDS_SINHALA)


def rank_by_singer(results: list[dict], singers: list[str]) -> list[dict]:
    """Tag results as the original singer's ("original": the title or channel names
    them and it isn't marked as a cover), a "cover" (marked as one), or "other",
    then order them in that preference. YouTube's own order is kept within a group."""

    order = {"original": 0, "other": 1, "cover": 2}
    tagged = []
    for r in results:
        if _is_cover(r["title"], r["channel"]):
            kind = "cover"
        elif _names_singer(_normalize(f"{r['title']} {r['channel']}"), singers):
            kind = "original"
        else:
            kind = "other"
        tagged.append({**r, "kind": kind})

    return sorted(tagged, key=lambda r: order[r["kind"]])
