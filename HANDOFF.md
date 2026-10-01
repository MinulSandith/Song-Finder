# Handoff for the cloud session — read this first

You are continuing development of **Song Finder**, a project the user built locally on Windows and has now moved to GitHub: **https://github.com/MinulSandith/Song-Finder** (private, branch `main`). From now on all work happens in a cloud session; there are no local copies. The user writes short, informal messages with typos — read for intent, and ask one clarifying question only when a real decision is theirs.

## What the app does
A local web app for building a music list from a photo of Sinhala song names:
1. **Scan tab:** upload a photo → Gemini reads the song names (OCR) → a second Gemini call corrects OCR misreads using real YouTube titles → a third call finds who **first** sang each song → YouTube is searched for the original singer's own version, with covers set aside → the user saves the right video per song.
2. **Expand tab:** the user types singer names → Gemini suggests songs → each is confirmed on YouTube → up to 5 videos per singer, saved like any result.
3. **Bulk download** of saved songs as MP3/MP4.

## FIRST THING TO FIX: the repo does not start as pushed
`backend/bulk_download.py` does `from download import download_youtube_content` from a folder `Download-Simply-Videos-From-YouTube/` next to `backend/`. That folder is a clone of the third-party project **https://github.com/pH-7/Download-Simply-Videos-From-YouTube** and was **deliberately not pushed** (it has its own `.git`). `backend/main.py` imports `bulk_download` at startup, so without that folder the whole server fails with an ImportError.

Options (ask the user which they prefer, or pick the least invasive):
- Clone it into the repo root: `git clone https://github.com/pH-7/Download-Simply-Videos-From-YouTube.git` (keep it out of git via `.gitignore` or `.git/info/exclude`, or add as a submodule).
- Make the import lazy (inside `start_bulk_download`) so the app still runs and only downloading is unavailable until the folder exists.
The user's old local edit to that folder's `download.py` only removed interactive prompts under `if __name__ == "__main__"`; the app doesn't use that part, so an unmodified upstream clone works.

## Setup in a cloud environment
- Python 3.10+ (developed on 3.14). `pip install -r requirements.txt` (fastapi, uvicorn[standard], yt-dlp, google-genai, python-dotenv, python-multipart).
- Secret: **`GEMINI_API_KEY`** (never commit it; locally it lives in `.env`, see `.env.example`). Optional `GEMINI_MODEL` = preferred model name.
- `ffmpeg` on PATH is needed for MP3 downloads only.
- Run: `python -m uvicorn backend.main:app --port 8000`, open `http://127.0.0.1:8000`. There is no `--reload` by default; restart after backend changes. Frontend files are served with `Cache-Control: no-store`.
- **Warning:** YouTube often blocks datacenter/cloud IPs. yt-dlp searches and downloads that worked locally may fail or demand cookies in the cloud. If so, tell the user plainly; don't silently work around it.
- `saved_songs.json` (the user's saved list) and `.env` are git-ignored, so a fresh clone starts with an empty saved list.

## Architecture
```
backend/main.py           FastAPI app + all routes; serves frontend/ at "/"
backend/ocr.py            EVERY Gemini call, with a model-fallback chain
backend/search.py         yt-dlp YouTube search + original/cover ranking
backend/bulk_download.py  bridge to the third-party downloader (see above)
backend/activity_log.py   in-memory event feed (bell icon / toasts)
backend/netfix.py         forces IPv4 name lookups (fixes slow/hanging IPv6 on the user's network)
frontend/index.html, app.js, style.css   plain HTML/JS/CSS, no build step
```

### API routes
| Route | Purpose |
|---|---|
| `POST /api/ocr` (photo) | Gemini reads song names → `{songs: [str]}` |
| `POST /api/verify` (photo + `songs` JSON field) | batch-correct names using YouTube top-5 titles → per song `{scanned, title, artist, status: verified/corrected/unverified, note}` |
| `POST /api/originals` (JSON `{songs:[{title,artist}]}`) | who first sang each song → `{singers:[{singer, aliases, confidence: high/medium/low, note}]}` (Gemini memory only, no web check) |
| `GET /api/search?q=&singer=…` (repeatable `singer`) | YouTube search; with `singer` each result gets `kind` = original/other/cover and is ordered by it |
| `POST /api/more-songs` | "Find more songs by these singers" button on the scan tab |
| `POST /api/expand` (JSON `{names, have_ids}`) | Expand tab: identify singers, suggest songs, confirm on YouTube, ≤5 videos each + `total` |
| `POST /api/save`, `GET /api/saved` | save/list videos in `saved_songs.json` |
| `POST /api/bulk-download`, `GET /api/bulk-download/{job_id}` | download saved songs |
| `GET /api/logs?since=` | activity feed |

### Key design decisions (keep these unless the user says otherwise)
- **Gemini fallback chain** (`ocr.py`): `GEMINI_MODEL` then `gemini-3.8-flash → 3.7 → 3.6 → 3.5-flash → 3.5-flash-lite → 3.1-flash-lite`. Quota (429) and overload (503) errors fall through to the next model. All Gemini calls go through `_generate_with_fallback`. The free key's **Google Search grounding tool is rejected (429)**, so it is not used.
- **Gemini is told never to invent.** Anything from its memory is cross-checked on YouTube: corrections use real titles; suggested songs in Expand/More-songs only count if a non-cover video **by that singer whose title contains the song title** exists (`_confirm_song` in `main.py`).
- **Search query** = song title + the singer's *Latin* spelling (Sinhala-script singer names return almost nothing on YouTube); if fewer than 3 results name the singer, the plain title is searched too and results merged (`search.py`).
- **Cover detection is a text heuristic** (cover/karaoke+misspellings/instrumental/remix/reverb/slowed/lofi/tribute/remake/reprise + Sinhala words). "Original" means title or channel names the singer. Imperfect; say so rather than overclaim.
- **Confidence gating:** high → show only the singer's own videos (rest behind "Show N more"); medium → also show other non-covers; low → shown but never used to filter.
- Sinhala text handling: `search._normalize` keeps Sinhala vowel signs (marks) and strips ZWJ so word matching works.
- Failures degrade gracefully: if verify or originals fails, the scan continues with the OCR names / no filter.
- Frontend sends scan steps as separate requests so each result shows as soon as it arrives; `scanSeq`/`searchSeq` guard against stale responses.

## Known limits (be upfront with the user)
- Original-singer answers come from model memory and varied between runs for old songs; only a matching YouTube upload gives independent confirmation.
- A video that merely names the singer (e.g. a duet re-upload) can pass as "original".
- A scan = 3 Gemini requests; free-tier quota on the newest model is small and 503s can make one call take up to a minute.
- Hand-edited and "Not found" songs get no original-singer lookup.
- `/api/ocr` and `/api/verify` are `async def` but call blocking code, so other requests can stall during a long Gemini call (a plain `def` would fix it).

## User preferences observed
- Wants things done, with short plain explanations; likes seeing real output/totals.
- Explicitly asked for **no README/.md files in the repo** when publishing. Don't add docs to the repo unless asked (this handoff file is meant to be pasted to you, not committed).
- Asks before publishing; the repo was created **private** on their GitHub account (`MinulSandith`).

## Windows quirks from local development (only matter if the user returns to local)
- Passing Sinhala text as a command-line argument to curl in Git Bash turns into `?????`; send it from a file or Python when testing.
- Test pages were screenshotted with headless Edge; network calls were mocked for UI checks.

## Ideas the user may ask for next
- Re-run the original-singer lookup after a manual Edit.
- Rank true originals higher using upload date or "Topic"/official-channel signals; fall back to the singer's Topic channel when Expand finds fewer than 5.
- Make long Gemini endpoints plain `def`; show model used/elapsed time per step.
