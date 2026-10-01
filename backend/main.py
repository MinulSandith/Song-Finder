import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Query, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.types import Scope


class NoCacheStaticFiles(StaticFiles):
    """Without this, browsers can keep serving a stale app.js/style.css after
    we change them — the page looks "broken" even though the server has the
    latest code, and a normal reload doesn't fix it (only a hard refresh does).
    """

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response

# The bulk-download tool prints emoji progress messages; Windows' default
# console/log encoding (cp1252) can't represent them and would crash the
# download thread, so force UTF-8 on this process's stdio.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Without this, our own module loggers (backend.ocr etc.) default to
# WARNING, so diagnostic .info() calls — like the raw OCR model response —
# are silently dropped instead of landing in the server console.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

from backend.activity_log import get_events, log_event
from backend.bulk_download import get_job_status, start_bulk_download
from backend.ocr import (
    extract_songs_from_image,
    expand_singers,
    find_original_singers,
    suggest_songs_by_singers,
    verify_songs,
)
from backend.search import _normalize, _search, rank_by_singer, search_youtube

SAVE_FILE = BASE_DIR / "saved_songs.json"
FRONTEND_DIR = BASE_DIR / "frontend"

app = FastAPI(title="Song Finder")


class SaveRequest(BaseModel):
    id: str
    title: str
    url: str
    thumbnail: str
    channel: Optional[str] = ""
    duration: Optional[int] = None


class SongRef(BaseModel):
    title: str
    artist: str = ""


class OriginalsRequest(BaseModel):
    songs: list[SongRef]


class SingerRef(BaseModel):
    singer: str
    aliases: list[str] = []


class MoreSongsRequest(BaseModel):
    singers: list[SingerRef]
    have: list[str] = []  # titles already on the list, so they aren't suggested again
    have_ids: list[str] = []  # video ids already shown or saved


class ExpandRequest(BaseModel):
    names: list[str]
    have_ids: list[str] = []  # video ids already saved, so they aren't offered again


class BulkDownloadRequest(BaseModel):
    ids: list[str]
    audio_only: bool = True
    max_resolution: Optional[int] = None


def _load_saved_songs() -> list[dict]:
    if not SAVE_FILE.exists():
        return []
    try:
        return json.loads(SAVE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


@app.get("/api/search")
def search(q: str, singer: list[str] = Query(default=[])):
    """`singer` (repeatable) is the original singer's name and other spellings; when
    given, results are tagged original/other/cover and ordered by that."""

    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="Query is required")
    singers = [s.strip() for s in singer if s.strip()]
    try:
        results = search_youtube(q.strip(), singers=singers)
    except Exception as exc:  # yt-dlp can raise various errors on network/extraction issues
        log_event("error", f"Search: '{q.strip()}' failed — {exc}")
        raise HTTPException(status_code=502, detail=f"Search failed: {exc}") from exc

    detail = ""
    if singers:
        by_singer = sum(1 for r in results if r["kind"] == "original")
        covers = sum(1 for r in results if r["kind"] == "cover")
        detail = f" ({by_singer} by {singers[0]}, {covers} cover(s) set aside)"
    log_event("info", f"Search: '{q.strip()}' → {len(results)} result(s){detail}")
    return {"results": results}


async def _read_image(file: UploadFile) -> bytes:
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Uploaded file must be an image")

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    return image_bytes


@app.post("/api/ocr")
async def ocr(file: UploadFile):
    image_bytes = await _read_image(file)

    try:
        songs = extract_songs_from_image(image_bytes, file.content_type)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"OCR failed: {exc}") from exc

    return {"songs": songs}


@app.post("/api/verify")
async def verify(file: UploadFile, songs: str = Form(...)):
    """Second step of a scan: check every name /api/ocr read against YouTube in
    one go, correcting the ones OCR misread. `songs` is a JSON array of the
    scanned names; the photo is sent again so Gemini can re-read unclear ones."""

    image_bytes = await _read_image(file)

    try:
        names = json.loads(songs)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="songs must be a JSON array of names") from exc
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise HTTPException(status_code=400, detail="songs must be a JSON array of names")
    names = [n.strip() for n in names if n.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="No song names to check")

    try:
        results = verify_songs(image_bytes, file.content_type, names)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Check failed: {exc}") from exc

    return {"songs": results}


@app.post("/api/originals")
def originals(req: OriginalsRequest):
    """Third step of a scan: for each checked song, who first sang it. Runs on titles
    alone (no photo); the answer comes back in the same order as `songs`."""

    songs = [{"title": s.title.strip(), "artist": s.artist.strip()} for s in req.songs if s.title.strip()]
    if not songs:
        raise HTTPException(status_code=400, detail="No songs to look up")

    try:
        results = find_original_singers(songs)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Original singer lookup failed: {exc}") from exc

    return {"singers": results}


MORE_SONGS_PER_SINGER = 4


def _names_song(video_title: str, song: dict) -> bool:
    """True if the video's title contains the suggested song's title (Sinhala as written,
    or every significant word of the romanized one). Without this, any video by the
    singer would "confirm" a made-up title."""

    text = _normalize(video_title)
    if song["title"] and _normalize(song["title"]) in text:
        return True
    tokens = [t for t in _normalize(song["title_en"]).split() if len(t) >= 3]
    padded = f" {text}"
    return bool(tokens) and all(f" {t}" in padded for t in tokens)


def _confirm_song(song: dict, singer_names: list[str]) -> Optional[dict]:
    """The best YouTube video that is this song, by the singer and not a cover, or None.
    This is what filters out titles Gemini made up."""

    for query in dict.fromkeys(q for q in (song["title_en"], song["title"]) if q):
        try:
            results = search_youtube(query, singers=singer_names)
        except Exception:
            continue
        original = next(
            (r for r in results if r["kind"] == "original" and _names_song(r["title"], song) and _is_single_song(r)),
            None,
        )
        if original:
            return {**original, "suggested_title": song["title"]}
    return None


@app.post("/api/more-songs")
def more_songs(req: MoreSongsRequest):
    """Expand the list: for each singer, Gemini suggests songs they first sang and each
    one is confirmed with a YouTube search that must find a video naming that singer
    (not marked as a cover). Returns up to 4 confirmed videos per singer."""

    singers = [
        {"singer": s.singer.strip(), "aliases": [a.strip() for a in s.aliases if a.strip()]}
        for s in req.singers
        if s.singer.strip()
    ]
    if not singers:
        raise HTTPException(status_code=400, detail="No singers to look up")

    try:
        candidates = suggest_songs_by_singers(singers, req.have)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Song suggestions failed: {exc}") from exc

    already = set(req.have_ids)
    out = []
    for s, songs in zip(singers, candidates):
        names = [s["singer"], *s["aliases"]]
        with ThreadPoolExecutor(max_workers=4) as pool:
            confirmed = list(pool.map(lambda song: _confirm_song(song, names), songs))

        videos = []
        for video in confirmed:
            if video and video["id"] not in already and len(videos) < MORE_SONGS_PER_SINGER:
                already.add(video["id"])
                videos.append(video)
        log_event(
            "info" if len(videos) >= MORE_SONGS_PER_SINGER else "warning",
            f"More songs: {len(videos)} of {len(songs)} suggested song(s) confirmed for {s['singer']}",
        )
        out.append({"singer": s["singer"], "suggested": len(songs), "videos": videos})

    return {"singers": out}


EXPAND_PER_SINGER = 5
EXPAND_MIN_PER_SINGER = 4
EXPAND_CANDIDATES = 10  # asked for per singer: some won't be found on YouTube
TOP_UP_SEARCH_RESULTS = 25
SONG_MAX_SECONDS = 10 * 60  # longer uploads are albums, nonstops or compilations, not one song
COMPILATION_WORDS = re.compile(
    r"\b(albums?|full\s+album|non-?\s?stops?|collections?|best\s+of|jukebox|mashup|medley|"
    r"playlist|top\s+\d+|\d+\s+songs|hits)\b",
    re.IGNORECASE,
)
COMPILATION_WORDS_SINHALA = ("ඇල්බම්", "එකතුව", "නොනවතින", "නන්ස්ටොප්")


def _is_single_song(video: dict) -> bool:
    """False for albums, nonstops, medleys and compilations: the Expand tab offers one
    song per video. Judged from the title and, when known, the length."""

    if video.get("duration") and video["duration"] > SONG_MAX_SECONDS:
        return False
    title = video["title"]
    return not COMPILATION_WORDS.search(title) and not any(w in title for w in COMPILATION_WORDS_SINHALA)


def _top_up_videos(singer_names: list[str], already: set, needed: int) -> list[dict]:
    """Non-cover videos naming the singer from a plain search for their name, for when
    too few of Gemini's suggested songs could be confirmed. These are by the singer
    but not checked against a song list, so they're marked "topped_up"."""

    latin = next((n for n in singer_names if n.isascii()), None)
    sinhala = next((n for n in singer_names if not n.isascii()), None)
    picked = []
    for query in (q for q in (latin, sinhala) if q):
        if len(picked) >= needed:
            break
        try:
            results = rank_by_singer(_search(f"{query} songs", TOP_UP_SEARCH_RESULTS), singer_names)
        except Exception:
            continue
        for r in results:
            if len(picked) >= needed:
                break
            if r["kind"] != "original" or r["id"] in already or not _is_single_song(r):
                continue
            already.add(r["id"])
            picked.append({**r, "topped_up": True})
    return picked


@app.post("/api/expand")
def expand(req: ExpandRequest):
    """The Expand tab: for each singer name typed in, Gemini works out who they are and
    suggests songs they originally sang; each is confirmed on YouTube (a non-cover video
    by that singer with the song's title) and up to 5 videos per singer are returned."""

    names = list(dict.fromkeys(n.strip() for n in req.names if n.strip()))
    if not names:
        raise HTTPException(status_code=400, detail="Type at least one singer name")
    if len(names) > 20:
        raise HTTPException(status_code=400, detail="Please keep it to 20 singers at a time")

    try:
        found = expand_singers(names, EXPAND_CANDIDATES)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Singer lookup failed: {exc}") from exc

    already = set(req.have_ids)
    out = []
    for typed, info in zip(names, found):
        # The typed spelling is kept as an alias: it may be how the uploads spell the name.
        singer_names = [n for n in (info["singer"], *info["aliases"], typed) if n]
        with ThreadPoolExecutor(max_workers=4) as pool:
            confirmed = list(pool.map(lambda song: _confirm_song(song, singer_names), info["songs"]))

        videos = []
        for video in confirmed:
            if video and video["id"] not in already and len(videos) < EXPAND_PER_SINGER:
                already.add(video["id"])
                videos.append(video)
        confirmed_count = len(videos)
        if confirmed_count < EXPAND_MIN_PER_SINGER:
            videos += _top_up_videos(singer_names, already, EXPAND_MIN_PER_SINGER - confirmed_count)
        log_event(
            "info" if len(videos) >= EXPAND_MIN_PER_SINGER else "warning",
            f"Expand: {confirmed_count} video(s) confirmed for {info['singer'] or typed}"
            + (f", {len(videos) - confirmed_count} added from a search for the singer" if len(videos) > confirmed_count else ""),
        )
        out.append(
            {
                "typed": typed,
                "singer": info["singer"],
                "recognised": bool(info["singer"] and info["songs"]),
                "suggested": len(info["songs"]),
                "videos": videos,
            }
        )

    return {"singers": out, "total": sum(len(s["videos"]) for s in out)}


@app.post("/api/save")
def save(item: SaveRequest):
    data = _load_saved_songs()

    if any(d.get("id") == item.id for d in data):
        return {"status": "duplicate", "message": "This song is already saved."}

    data.append(item.model_dump())
    SAVE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log_event("success", f"Saved: {item.title}")
    return {"status": "ok"}


@app.get("/api/logs")
def logs(since: int = 0):
    return {"events": get_events(since)}


@app.get("/api/saved")
def saved():
    return {"songs": _load_saved_songs()}


@app.post("/api/bulk-download")
def bulk_download(req: BulkDownloadRequest):
    songs = _load_saved_songs()
    urls = [s["url"] for s in songs if s.get("id") in req.ids and s.get("url")]

    if not urls:
        raise HTTPException(status_code=400, detail="No matching saved songs found")

    try:
        job_id = start_bulk_download(
            urls,
            audio_only=req.audio_only,
            max_resolution=req.max_resolution,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"job_id": job_id, "count": len(urls)}


@app.get("/api/bulk-download/{job_id}")
def bulk_download_status(job_id: str):
    job = get_job_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return job


# Serve the frontend last so it doesn't shadow the /api routes above.
app.mount("/", NoCacheStaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
