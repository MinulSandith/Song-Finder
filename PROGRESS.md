# Song Finder — progress and feature notes

A local web app: photograph a handwritten/printed list of Sinhala song names, have Gemini read and correct them, find each song's original singer, search YouTube for the original recording (not covers), save the right videos, and bulk-download them.

_Last updated: 2026-10-01_

## Stack

| Part | Tech |
|---|---|
| Backend | FastAPI (`backend/main.py`), run with `venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000` |
| Frontend | Plain HTML/CSS/JS in `frontend/`, served by the backend (no-cache static files) |
| AI | Google Gemini via `google-genai`, key in `.env` (`GEMINI_API_KEY`, optional `GEMINI_MODEL`) |
| Search / download | `yt-dlp` (no YouTube API key); ffmpeg needed for MP3 |
| Storage | `saved_songs.json` in the project root |

Backend modules: `ocr.py` (all Gemini calls), `search.py` (YouTube search + cover filtering), `bulk_download.py` (downloads), `activity_log.py` (notification feed), `netfix.py` (forces IPv4 name lookups).

## Features

### 1. Scan a photo — three API calls
A scan runs three requests in order. Each result appears in the page as soon as it arrives.

| # | Endpoint | Input | Does |
|---|---|---|---|
| 1 | `POST /api/ocr` | photo | Gemini reads the song names off the photo (Sinhala script kept as written). |
| 2 | `POST /api/verify` | photo + all scanned names (one batched call) | Looks each name up on YouTube (top 5 titles each), then Gemini compares photo + names + real titles and marks each **verified**, **corrected** (OCR misread, replaced with the real song) or **unverified**. |
| 3 | `POST /api/originals` | corrected titles only (no photo, one batched call) | Gemini says who **first** recorded each song, with aliases/spellings, a confidence (high/medium/low) and a short note. |

Each row shows: `OCR` (raw text) → status line (Correct / Corrected / Not found / Not checked / Edited) → `Original` singer. On corrected rows the OCR text is a link that searches the raw OCR name instead.

Failure handling: if call 2 fails, rows are marked "Not checked" and searched as scanned. If call 3 fails, the page warns and searches without a singer filter. A scan never breaks because a later step failed.

### 2. Original-singer search (covers set aside)
`GET /api/search?q=<title>&singer=<name>&singer=<alias>…`

- Query = title + the singer's Latin spelling (most YouTube uploads use English spelling; a Sinhala-script name returned almost nothing). If fewer than 3 results name the singer, the plain title is searched too and the results merged. Up to 15 results.
- Each result is tagged `original` (title/channel names the singer and isn't marked as a cover), `cover` (words like cover, karaoke, instrumental, remix, reverb, slowed, lofi, tribute, remake, reprise, plus a few Sinhala equivalents) or `other`. Name matching handles Sinhala vowel signs, alternate spellings and suffixes.
- Display: high-confidence singer → only originals shown, everything else behind a "Show N more (X covers, Y by other singers)" button. Medium → other non-covers stay visible under the originals. Low → shown on the row but not used to filter. If nothing names the singer, the closest non-covers are shown with a note.
- Cards carry small tags ("Names the original singer", "Looks like a cover"). A plain search with no `singer` params behaves as it always did.

### 2b. Expand the list: "Find more songs by these singers"
Button under the scan list (shown when at least one original singer was found with high/medium confidence). One request, `POST /api/more-songs`:
1. One batched Gemini call asks for up to 8 well-known songs per singer that they originally sang (skipping titles already on the list; it is told to name fewer rather than invent).
2. Each suggestion is confirmed by a YouTube search (no Gemini quota): it needs a **non-cover video that names the singer AND whose title contains the suggested song's title**. Made-up titles don't pass.
3. Up to 4 confirmed videos per singer are shown under "More by <singer>" with Preview/Save; the header says how many of the suggested songs were confirmed. Videos already saved are skipped. Nothing is saved automatically.

Test: Amaradeva → 4 of 8 suggested confirmed (real songs); Satheeshan → Gemini suggested only 1 song, not found, so 0. Takes ~20–40 s. Lesser-known or newer singers will often return fewer than 4.

### 3. "Search all" queue
After a scan, the app walks the songs one by one: search → **Save & next** (or **Skip** / **Stop**). Songs passed over are revisited; clicking a row's Search jumps the run there. Saved/skipped state and saved titles are shown per row and survive list re-renders.

### 4. Manual correction tools
- **Edit** per row: change title/artist by hand (status becomes "Edited"; the original-singer lookup is dropped since the title may be a different song).
- **Re-check remaining (1 batched call)**: re-sends only songs that aren't verified/edited in one `/api/verify` call, then re-runs the original-singer lookup for the songs it replaced.

### 5. Save + bulk download
- `POST /api/save` stores a video (id, title, url, thumbnail, channel, duration) in `saved_songs.json`; duplicates are detected.
- `GET /api/saved` lists them. `POST /api/bulk-download` + `GET /api/bulk-download/{job_id}` download selected songs as audio (MP3) or video (selectable max resolution). Individual "Download audio" buttons too.

### 6. Activity feed
`GET /api/logs?since=` powers a notification bell and toasts (OCR/verify/search/save/download events, model fallbacks, errors). The page also surfaces uncaught JS errors as toasts.

## Model handling
`ocr.py` tries models in order: `GEMINI_MODEL` from `.env`, then `gemini-3.8-flash → 3.7-flash → 3.6-flash → 3.5-flash → 3.5-flash-lite → 3.1-flash-lite`. A model that errors (429 quota, 503 overload) or returns an unparseable reply falls through to the next. All three Gemini calls share this chain.

## Known limits
- **Original singer comes from Gemini's memory** (no web check). The free key rejects Gemini's Google Search tool (429), so it wasn't used. For old songs the answer varied between runs ("Danno Budunge": two different singers, high then medium confidence) — hence only high confidence filters strictly. A matching real upload on YouTube is a useful cross-check.
- **Cover detection is a text heuristic.** A video that names the singer but isn't their recording can still count as "original" (e.g. a "Yohani & Satheeshan" short). A cover without any cover keyword and without the singer's name lands in "other".
- **Speed/quota:** a scan is 3 Gemini requests. During testing Google returned 503s on the newest models, so one call took up to a minute while falling back. `gemini-3.8-flash` has a small free daily quota.
- Hand-edited and "Not found" songs get no original-singer lookup.
- Endpoints `/api/ocr` and `/api/verify` are `async` but call blocking code, so other requests (like log polling) can stall during a long Gemini call.

## Testing notes
- Tested with a rendered Sinhala list image (headless Edge) through all three endpoints, with live Gemini and YouTube calls; UI states checked with mocked network replies in a browser (pending, checked, original-found, covers folded/expanded, redo, third-call failure).
- On this Windows shell, passing Sinhala text as a **command-line argument** to curl becomes `?????`. Send it from a file or from Python when testing.
- Restart the server after backend changes (it runs without `--reload`); frontend files are served uncached.

## Possible next steps
- Re-run the original-singer lookup after a manual Edit (costs one extra call per edit).
- Use video upload dates or "Topic"/official-channel signals to rank true originals higher.
- Make the long Gemini endpoints plain `def` so they run in a thread pool.
- Show the model used and elapsed time per step in the status line.
