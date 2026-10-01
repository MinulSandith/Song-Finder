// Safety net: without this, a bug anywhere in this script (a null element,
// a bad property access, a rejected promise nobody awaited) fails completely
// silently from the user's point of view — "I did everything right and
// nothing happened." Surface it instead.
window.addEventListener("error", (e) => {
  console.error("Uncaught error:", e.error || e.message);
  try {
    showToast({ level: "error", message: "UI error: " + (e.message || "something went wrong") });
  } catch (_) {
    // toastContainer not initialized yet (error happened very early) — console.error above still fired.
  }
});

window.addEventListener("unhandledrejection", (e) => {
  console.error("Unhandled promise rejection:", e.reason);
  try {
    const msg = (e.reason && e.reason.message) || String(e.reason);
    showToast({ level: "error", message: "UI error: " + msg });
  } catch (_) {
    // toastContainer not initialized yet.
  }
});

const searchForm = document.getElementById("search-form");
const queryInput = document.getElementById("query");
const resultsDiv = document.getElementById("results");
const statusEl = document.getElementById("status");

const ocrForm = document.getElementById("ocr-form");
const ocrFile = document.getElementById("ocr-file");
const ocrStatus = document.getElementById("ocr-status");
const ocrList = document.getElementById("ocr-list");
const ocrSearchAllBtn = document.getElementById("ocr-search-all");
const ocrRedoBtn = document.getElementById("ocr-redo");
const moreSongsBtn = document.getElementById("more-songs-btn");
const moreSongsDiv = document.getElementById("more-songs");

const queueBar = document.getElementById("queue-bar");
const queueText = document.getElementById("queue-text");
const queueSkipBtn = document.getElementById("queue-skip");
const queueStopBtn = document.getElementById("queue-stop");

// Songs from the last OCR scan — { scanned, title, artist, status, note, original }, where status is
// "pending" (still being checked), "verified", "corrected" (OCR misread it; title is the real
// song), "unverified" (no real song found) or "unchecked" (the check itself failed), and
// original is who first sang it — { state, singer, aliases, confidence, note } with state
// "pending", "found", "unknown" or "failed" (null when the song wasn't looked up) — plus the
// state of a "search all" run over them. While a run is active,
// saving a result for the current song (or skipping it) automatically searches the next song
// that hasn't been handled yet.
let ocrSongs = [];
let lastScanFile = null; // kept so "Re-check remaining" can resend the same photo
let queue = null; // { index, outcomes: [], savedTitles: [] } — outcomes[i] is "saved", "skipped" or undefined

// Incremented per search (and per scan) so a slow response can't overwrite newer results.
let searchSeq = 0;
let scanSeq = 0;

searchForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const q = queryInput.value.trim();
  if (!q) return;
  // Mid-run, a hand-typed query is a retry for the current song, so saving from it still advances.
  runSearch(q, queue ? queue.index : null);
});

// `filter` ({ name, singers, strict }) narrows the results to the original singer's own
// videos: the server tags each result original / other / cover and renderResults hides the
// ones that aren't the original behind a button. Plain searches pass no filter.
async function runSearch(q, queueIndex = null, filter = null) {
  const seq = ++searchSeq;
  queryInput.value = q;
  statusEl.textContent = "";
  resultsDiv.innerHTML = "<p>Searching YouTube…</p>";
  (queue ? queueBar : resultsDiv).scrollIntoView({ behavior: "smooth", block: "start" });

  const params = new URLSearchParams({ q });
  if (filter) filter.singers.forEach((s) => params.append("singer", s));

  try {
    const res = await fetch(`/api/search?${params}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    if (seq !== searchSeq) return;
    renderResults(data.results, queueIndex, filter);
  } catch (err) {
    if (seq !== searchSeq) return;
    resultsDiv.innerHTML = `<p class="error">Search failed: ${escapeHtml(String(err.message || err))}</p>`;
  }
}

// A scan is three calls: /api/ocr reads the names off the photo (shown straight away),
// /api/verify checks them all in one batch and corrects OCR misreads, then /api/originals asks
// who first sang each checked song. Searching starts after all three, so it uses the corrected
// names and can favour the original singer's own videos over covers.
ocrForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = ocrFile.files[0];
  if (!file) return;
  lastScanFile = file;

  const seq = ++scanSeq;
  ocrStatus.textContent = "Scanning photo…";
  ocrStatus.classList.remove("warn");
  ocrList.innerHTML = "";
  ocrSongs = [];
  queue = null;
  queueBar.hidden = true;
  ocrSearchAllBtn.hidden = true;
  ocrSearchAllBtn.disabled = false;
  ocrRedoBtn.hidden = true;
  moreSongsDiv.hidden = true;
  moreSongsDiv.innerHTML = "";

  let names;
  try {
    names = (await postScan("/api/ocr", file)).songs;
  } catch (err) {
    if (seq !== scanSeq) return;
    ocrStatus.textContent = "";
    ocrList.innerHTML = `<li class="error">Scan failed: ${escapeHtml(String(err.message || err))}</li>`;
    return;
  }
  if (seq !== scanSeq) return;

  if (!names || names.length === 0) {
    ocrStatus.textContent =
      "The model read the photo but found no song names in it. Try a clearer or more tightly-cropped photo.";
    ocrStatus.classList.add("warn");
    return;
  }

  ocrSongs = names.map((name) => scannedSong(name, "pending"));
  renderOcrList();
  ocrStatus.textContent = `Found ${names.length} song${names.length === 1 ? "" : "s"}. Checking the names for OCR mistakes…`;

  let summary;
  try {
    ocrSongs = (await postScan("/api/verify", file, names)).songs.map((s) => ({ ...s, flagged: false }));
    if (seq !== scanSeq) return;
    const corrected = ocrSongs.filter((s) => s.status === "corrected").length;
    const unverified = ocrSongs.filter((s) => s.status === "unverified").length;
    const checks = [corrected && `${corrected} corrected`, unverified && `${unverified} not found`].filter(Boolean);
    summary = `Checked ${ocrSongs.length} song${ocrSongs.length === 1 ? "" : "s"}${checks.length ? ` (${checks.join(", ")})` : ""}.`;
  } catch (err) {
    if (seq !== scanSeq) return;
    ocrSongs = names.map((name) => scannedSong(name, "unchecked"));
    summary = `Couldn't check the names (${err.message || err}).`;
    ocrStatus.classList.add("warn");
  }

  if (ocrSongs.some(canLookUpOriginal)) {
    ocrStatus.textContent = `${summary} Finding who first sang each one…`;
    const failure = await lookUpOriginals(ocrSongs);
    if (seq !== scanSeq) return;
    if (failure) {
      summary += ` Couldn't look up the original singers (${failure}).`;
      ocrStatus.classList.add("warn");
    }
  }

  const filtered = ocrSongs.filter((s) => originalFilter(s)).length;
  ocrStatus.textContent =
    `${summary} ` +
    (filtered
      ? "Searching them one by one — the original singer's version first, covers set aside."
      : "Searching them one by one — save the right video for each.");

  renderOcrList();
  ocrSearchAllBtn.hidden = false;
  ocrRedoBtn.hidden = false;
  startSearchAll();
});

// Posts the photo (plus, for /api/verify, the scanned names) and returns the parsed reply.
async function postScan(url, file, names = null) {
  const formData = new FormData();
  formData.append("file", file);
  if (names) formData.append("songs", JSON.stringify(names));

  return readReply(await fetch(url, { method: "POST", body: formData }));
}

// Posts a JSON body (the calls that don't need the photo) and returns the parsed reply.
async function postJson(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return readReply(res);
}

async function readReply(res) {
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

// flagged marks a song for the next "Re-check flagged" batch. A song that never got a real
// answer ("unchecked", because the check call itself failed) starts flagged since resending it
// isn't wasteful — it has no previous output to repeat. Everything else starts unflagged: once a
// song has a real answer, re-asking the same question tends to get the same answer, so it's only
// resent if you tick it yourself.
function scannedSong(name, status, note = "") {
  return { scanned: name, title: name, artist: "", status, note, flagged: status === "unchecked" };
}

function originalFrom(r) {
  if (!r || !r.singer) return { state: "unknown", note: (r && r.note) || "" };
  return { state: "found", singer: r.singer, aliases: r.aliases || [], confidence: r.confidence, note: r.note || "" };
}

// The distinct original singers worth expanding from: found by the third call, and not a
// low-confidence guess (a wrong singer would suggest the wrong songs).
function usableSingers() {
  const seen = new Map();
  ocrSongs.forEach((s) => {
    const o = s.original;
    if (o && o.state === "found" && o.confidence !== "low" && !seen.has(o.singer)) {
      seen.set(o.singer, { singer: o.singer, aliases: o.aliases });
    }
  });
  return [...seen.values()];
}

function updateMoreSongsButton() {
  moreSongsBtn.hidden = usableSingers().length === 0;
}

// Expands the list: one request asks Gemini for more songs by each singer, and the server keeps
// only the ones it can confirm on YouTube as a non-cover video by that singer (up to 4 each).
moreSongsBtn.addEventListener("click", async () => {
  const singers = usableSingers();
  if (!singers.length) return;

  const seq = scanSeq;
  moreSongsBtn.disabled = true;
  moreSongsDiv.hidden = false;
  moreSongsDiv.innerHTML = `<p class="hint">Asking for more songs by ${singers.length} singer${singers.length === 1 ? "" : "s"} and checking them on YouTube — this can take about a minute…</p>`;

  try {
    let haveIds = [];
    try {
      haveIds = ((await (await fetch("/api/saved")).json()).songs || []).map((s) => s.id);
    } catch (_) {
      // Only used to avoid suggesting videos you already saved.
    }
    const { singers: groups } = await postJson("/api/more-songs", {
      singers,
      have: ocrSongs.map((s) => s.title),
      have_ids: haveIds,
    });
    if (seq !== scanSeq) return;
    renderMoreSongs(groups);
  } catch (err) {
    if (seq !== scanSeq) return;
    moreSongsDiv.innerHTML = `<p class="error">Couldn't find more songs: ${escapeHtml(String(err.message || err))}</p>`;
  } finally {
    moreSongsBtn.disabled = false;
  }
});

function renderMoreSongs(groups) {
  moreSongsDiv.innerHTML = "";
  groups.forEach((g) => {
    const section = document.createElement("section");
    section.className = "more-singer";

    const note =
      g.videos.length >= 4
        ? ""
        : ` — only ${g.videos.length} of ${g.suggested} suggested song${g.suggested === 1 ? "" : "s"} could be confirmed on YouTube`;
    section.innerHTML = `<h3>More by ${escapeHtml(g.singer)}<span class="hint">${note}</span></h3>`;

    const grid = document.createElement("div");
    grid.className = "more-grid";
    g.videos.forEach((v) => grid.appendChild(buildResultCard(v, null)));
    section.appendChild(grid);
    moreSongsDiv.appendChild(section);
  });
}

// Only songs the check recognised are looked up: a name that matched nothing has no reliable
// title to ask about, and a hand-edited one is left alone to keep this to as few calls as possible.
function canLookUpOriginal(s) {
  return s.status === "verified" || s.status === "corrected";
}

// The third call: asks who first sang each of `songs` (those it can look up) in one request and
// stores the answer on each song. Returns "" on success or the error message on failure.
// Songs are updated in place, so a scan that was replaced meanwhile is harmlessly ignored.
async function lookUpOriginals(songs) {
  const targets = songs.filter(canLookUpOriginal);
  if (!targets.length) return "";

  targets.forEach((s) => (s.original = { state: "pending" }));
  renderOcrList();

  try {
    const { singers } = await postJson("/api/originals", {
      songs: targets.map((s) => ({ title: s.title, artist: s.artist })),
    });
    targets.forEach((s, n) => (s.original = originalFrom(singers[n])));
    return "";
  } catch (err) {
    targets.forEach((s) => (s.original = { state: "failed" }));
    return String(err.message || err);
  }
}

function renderOcrList() {
  updateMoreSongsButton();
  ocrList.innerHTML = "";
  ocrSongs.forEach((s, i) => ocrList.appendChild(buildOcrRow(s, i)));

  // Rebuilding the list (after an edit or a redo) wipes the DOM, so reapply
  // whatever a "search all" run had already recorded for each song.
  if (queue) {
    queue.outcomes.forEach((outcome, i) => {
      if (!outcome) return;
      const li = ocrList.children[i];
      li.classList.add(`queue-${outcome}`);
      const savedTitle = queue.savedTitles && queue.savedTitles[i];
      li.querySelector(".ocr-outcome").textContent = outcome === "saved" ? `✓ ${savedTitle || "Saved"}` : "Skipped";
    });
    highlightOcrItem(queue.index);
  }
}

function buildOcrRow(s, i) {
  const li = document.createElement("li");
  // An edited row is left exactly as you typed it, so there's no "Redo" checkbox for it —
  // ticking it would just send your manual fix back through the checker and risk overwriting it.
  const flagHtml =
    s.status === "edited"
      ? ""
      : `<label class="ocr-redo-flag" title="Flag this song to be sent again on the next re-check">
           <input type="checkbox" class="ocr-flag-checkbox" /> Redo
         </label>`;

  li.innerHTML = `
    <div class="ocr-text">${ocrTextHtml(s)}</div>
    <div class="ocr-row-actions">
      ${flagHtml}
      <button type="button" class="ocr-edit-btn">Edit</button>
      <button type="button" class="ocr-search-btn">Search</button>
    </div>
    <span class="ocr-outcome"></span>
    <div class="ocr-edit-form" hidden>
      <input type="text" class="ocr-edit-title" placeholder="Song title" />
      <input type="text" class="ocr-edit-artist" placeholder="Artist (optional)" />
      <button type="button" class="ocr-edit-save">Save</button>
      <button type="button" class="ocr-edit-cancel">Cancel</button>
    </div>
  `;

  const flagCb = li.querySelector(".ocr-flag-checkbox");
  if (flagCb) {
    flagCb.checked = !!s.flagged;
    flagCb.addEventListener("change", () => {
      s.flagged = flagCb.checked;
    });
  }

  // During a "search all" run, jump the run to this song instead of searching off to the side.
  const search = (q, filter = null) => (queue ? searchQueueItem(i, q, filter) : runSearch(q, null, filter));

  // The raw OCR name is searched as it is, with no singer filter.
  const alt = li.querySelector(".ocr-alt");
  if (alt) alt.addEventListener("click", () => search(s.scanned));

  li.querySelector(".ocr-search-btn").addEventListener("click", () => search(songQuery(s), originalFilter(s)));

  const editForm = li.querySelector(".ocr-edit-form");
  const titleInput = li.querySelector(".ocr-edit-title");
  const artistInput = li.querySelector(".ocr-edit-artist");

  li.querySelector(".ocr-edit-btn").addEventListener("click", () => {
    titleInput.value = s.title;
    artistInput.value = s.artist;
    editForm.hidden = false;
    titleInput.focus();
  });

  li.querySelector(".ocr-edit-cancel").addEventListener("click", () => {
    editForm.hidden = true;
  });

  li.querySelector(".ocr-edit-save").addEventListener("click", () => {
    const title = titleInput.value.trim();
    if (!title) return;
    ocrSongs[i] = {
      scanned: s.scanned,
      title,
      artist: artistInput.value.trim(),
      status: "edited",
      note: "Manually edited",
      flagged: false,
    };
    renderOcrList();
  });

  return li;
}

// Re-sends ONLY songs you've ticked "Redo" on (plus ones that never got a real answer at
// all), in ONE batched /api/verify call. A song that already has an answer and wasn't
// flagged is left exactly as it is — re-asking the same question tends to just get the same
// answer back, so that would waste a call rather than fix anything.
ocrRedoBtn.addEventListener("click", async () => {
  if (!lastScanFile) return;

  const targetIdx = [];
  const targetNames = [];
  ocrSongs.forEach((s, i) => {
    if (s.status !== "edited" && (s.flagged || s.status === "unchecked")) {
      targetIdx.push(i);
      targetNames.push(s.scanned);
    }
  });

  if (!targetNames.length) {
    ocrStatus.textContent = 'Nothing flagged to re-check. Tick "Redo" next to the songs you want re-checked.';
    ocrStatus.classList.add("warn");
    return;
  }

  ocrRedoBtn.disabled = true;
  ocrStatus.textContent = `Re-checking ${targetNames.length} song${targetNames.length === 1 ? "" : "s"} in one batched call…`;
  ocrStatus.classList.remove("warn");

  try {
    const data = await postScan("/api/verify", lastScanFile, targetNames);
    const results = data.songs || [];
    results.forEach((result, j) => {
      const idx = targetIdx[j];
      if (idx !== undefined) ocrSongs[idx] = { ...result, flagged: false };
    });
    renderOcrList();
    ocrStatus.textContent = `Re-checked ${results.length} song${results.length === 1 ? "" : "s"}.`;

    // The new results replaced the old song objects, which dropped their original singer.
    const failure = await lookUpOriginals(targetIdx.map((i) => ocrSongs[i]));
    renderOcrList();
    if (failure) {
      ocrStatus.textContent += ` Couldn't look up the original singers (${failure}).`;
      ocrStatus.classList.add("warn");
    }
  } catch (err) {
    ocrStatus.textContent = `Re-check failed: ${err.message || err}`;
    ocrStatus.classList.add("warn");
  } finally {
    ocrRedoBtn.disabled = false;
  }
});

// The original-singer filter for a song, or null when there isn't a trustworthy one. A "low"
// confidence singer is shown in the list but not used: a wrong guess would push the right
// video down. Only a "high" one lets the search set aside everything that isn't by them.
function originalFilter(s) {
  const o = s.original;
  if (!o || o.state !== "found" || o.confidence === "low") return null;
  return { name: o.singer, singers: [o.singer, ...o.aliases], strict: o.confidence === "high" };
}

// With a singer filter the server adds the singer's name to the search itself, and the
// lookup artist is left out of the query: it's often whoever covered the song.
function songQuery(s) {
  return (originalFilter(s) ? [s.title] : [s.title, s.artist]).filter(Boolean).join(" ");
}

// The OCR name, then what the check made of it. On a corrected row the OCR name is a
// link, so it can still be searched if the correction is wrong.
function ocrTextHtml(s) {
  const scanned = escapeHtml(s.scanned);
  const ocrName =
    s.status === "corrected"
      ? `<button type="button" class="ocr-alt" title="Search the OCR name instead">${scanned}</button>`
      : scanned;
  const song = escapeHtml(s.title) + (s.artist ? ` <span class="ocr-artist">— ${escapeHtml(s.artist)}</span>` : "");
  const [label, value] = {
    pending: ["Checking…", ""],
    verified: ["✓ Correct", song],
    corrected: ["Corrected", song],
    unverified: ["Not found", "searching the OCR name"],
    unchecked: ["Not checked", "searching the OCR name"],
    edited: ["✎ Edited", song],
  }[s.status] || ["", song];

  return `
    <span class="ocr-line"><span class="ocr-label">OCR</span><span>${ocrName}</span></span>
    <span class="ocr-line ocr-${s.status}"><span class="ocr-label">${label}</span><span>${value}</span></span>
    ${s.note ? `<span class="ocr-note">${escapeHtml(s.note)}</span>` : ""}
    ${originalLineHtml(s.original)}`;
}

// Who first sang the song. A "likely" or "unsure" tag shows when the model wasn't certain,
// since that decides whether the search trusts it (see originalFilter).
function originalLineHtml(o) {
  if (!o) return "";
  const text = {
    pending: "Finding…",
    unknown: "Not sure who first sang it",
    failed: "Couldn't look up",
    found: o.state === "found" ? escapeHtml(o.singer) + confidenceTag(o.confidence) : "",
  }[o.state];

  return `
    <span class="ocr-line ocr-original-${o.state}"><span class="ocr-label">Original</span><span>${text}</span></span>
    ${o.note && o.state !== "pending" ? `<span class="ocr-note">${escapeHtml(o.note)}</span>` : ""}`;
}

function confidenceTag(confidence) {
  if (confidence === "high") return "";
  return ` <span class="ocr-unsure">(${confidence === "low" ? "unsure — not used to filter" : "likely"})</span>`;
}

// Runs automatically once a scan finishes; the button restarts it after Stop or "All done".
ocrSearchAllBtn.addEventListener("click", startSearchAll);

function startSearchAll() {
  if (!ocrSongs.length) return;
  queue = { index: 0, outcomes: [], savedTitles: [] };
  ocrList.querySelectorAll("li").forEach((li) => {
    li.classList.remove("queue-saved", "queue-skipped");
    li.querySelector(".ocr-outcome").textContent = "";
  });
  ocrSearchAllBtn.disabled = true;
  queueSkipBtn.hidden = false;
  queueStopBtn.hidden = false;
  queueBar.hidden = false;
  searchQueueItem(0);
}

queueSkipBtn.addEventListener("click", () => {
  if (queue) advanceQueue("skipped");
});

queueStopBtn.addEventListener("click", () => {
  if (queue) finishQueue(true);
});

function searchQueueItem(index, q = songQuery(ocrSongs[index]), filter = originalFilter(ocrSongs[index])) {
  queue.index = index;
  const by = filter ? ` (original singer: ${filter.name})` : "";
  queueText.textContent = `Song ${index + 1} of ${ocrSongs.length}: ${q}${by} — save the right video to go to the next one.`;
  highlightOcrItem(index);
  runSearch(q, index, filter);
}

function advanceQueue(outcome, savedTitle = "") {
  const li = ocrList.children[queue.index];
  queue.outcomes[queue.index] = outcome;
  if (outcome === "saved") queue.savedTitles[queue.index] = savedTitle;
  li.classList.remove("queue-saved", "queue-skipped");
  li.classList.add(`queue-${outcome}`);
  li.querySelector(".ocr-outcome").textContent = outcome === "saved" ? `✓ ${savedTitle}` : "Skipped";

  const next = nextPendingIndex(queue.index);
  if (next === -1) {
    finishQueue(false);
  } else {
    searchQueueItem(next);
  }
}

// The next song after `from` with no outcome yet, wrapping around so songs passed
// over by jumping ahead still get visited. -1 when every song has been handled.
function nextPendingIndex(from) {
  for (let step = 1; step <= ocrSongs.length; step++) {
    const i = (from + step) % ocrSongs.length;
    if (!queue.outcomes[i]) return i;
  }
  return -1;
}

function finishQueue(stopped) {
  const saved = queue.outcomes.filter((o) => o === "saved").length;
  const skipped = queue.outcomes.filter((o) => o === "skipped").length;
  queueText.textContent = `${stopped ? "Stopped" : "All done"}! Saved ${saved}, skipped ${skipped}.`;
  queueSkipBtn.hidden = true;
  queueStopBtn.hidden = true;
  ocrSearchAllBtn.disabled = false;
  highlightOcrItem(-1);
  queue = null;
}

function highlightOcrItem(index) {
  Array.from(ocrList.children).forEach((li, i) => li.classList.toggle("queue-current", i === index));
}

// Small labels on result cards, set by the server's original/other/cover tagging.
const KIND_TAGS = {
  original: '<span class="kind-tag kind-original">Names the original singer</span>',
  cover: '<span class="kind-tag kind-cover">Looks like a cover</span>',
};

// Splits tagged results into what is shown and what sits behind the "show more" button.
// With a strict (high-confidence) singer only their own videos are shown; otherwise other
// non-cover videos stay visible below them. Covers are always set aside. When nothing is
// clearly by the singer, the non-covers are shown rather than an empty page.
function splitByOriginal(results, filter) {
  if (!filter) return { shown: results, hidden: [] };

  const of = (kind) => results.filter((r) => r.kind === kind);
  const originals = of("original");
  const others = of("other");
  const covers = of("cover");

  if (originals.length) {
    return filter.strict
      ? { shown: originals, hidden: [...others, ...covers] }
      : { shown: [...originals, ...others], hidden: covers };
  }
  return others.length ? { shown: others, hidden: covers } : { shown: covers, hidden: [] };
}

function renderResults(results, queueIndex = null, filter = null) {
  if (!results || results.length === 0) {
    if (queueIndex !== null) {
      resultsDiv.innerHTML = `
        <div class="card no-results-card">
          <p>No YouTube results for <strong>${escapeHtml(queryInput.value)}</strong>.</p>
          <p class="hint">OCR text is often a lyric fragment, not the exact video title — edit the search box above and hit Search, or skip this song.</p>
          <button type="button" id="inline-skip-btn">Skip this song</button>
        </div>
      `;
      document.getElementById("inline-skip-btn").addEventListener("click", () => {
        if (queue) advanceQueue("skipped");
      });
    } else {
      resultsDiv.innerHTML = "<p>No results found. Try a different search.</p>";
    }
    return;
  }

  resultsDiv.innerHTML = "";
  const { shown, hidden } = splitByOriginal(results, filter);

  if (filter) {
    const note = document.createElement("p");
    note.className = "filter-note";
    note.innerHTML = results.some((r) => r.kind === "original")
      ? `Showing videos that name <strong>${escapeHtml(filter.name)}</strong>, the original singer.`
      : `No video clearly by <strong>${escapeHtml(filter.name)}</strong>, the original singer, was found — showing the closest matches.`;
    resultsDiv.appendChild(note);
  }

  shown.forEach((r) => resultsDiv.appendChild(buildResultCard(r, queueIndex)));

  if (hidden.length) {
    const cards = hidden.map((r) => {
      const card = buildResultCard(r, queueIndex);
      card.hidden = true;
      resultsDiv.appendChild(card);
      return card;
    });

    const covers = hidden.filter((r) => r.kind === "cover").length;
    const parts = [covers && `${covers} cover${covers === 1 ? "" : "s"}`, hidden.length - covers && `${hidden.length - covers} by other singers`];
    const label = `${hidden.length} more (${parts.filter(Boolean).join(", ")})`;

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "show-more-btn";
    toggle.textContent = `Show ${label}`;
    toggle.addEventListener("click", () => {
      const show = cards[0].hidden;
      cards.forEach((c) => (c.hidden = !show));
      toggle.textContent = `${show ? "Hide" : "Show"} ${label}`;
    });
    // Right under the shown results, before the folded cards.
    resultsDiv.insertBefore(toggle, cards[0]);
  }
}

function buildResultCard(r, queueIndex) {
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `
    <img class="thumb" src="${r.thumbnail}" alt="${escapeHtml(r.title)}" loading="lazy" />
    <div class="info">
      ${KIND_TAGS[r.kind] || ""}
      <h3>${escapeHtml(r.title)}</h3>
      <p>${escapeHtml(r.channel || "")}${r.duration ? " · " + formatDuration(r.duration) : ""}</p>
      <div class="actions">
        <button class="preview-btn" type="button">Preview</button>
        <button class="save-btn" type="button">Save</button>
      </div>
      <div class="preview-slot"></div>
    </div>
  `;

  const previewBtn = card.querySelector(".preview-btn");
  const previewSlot = card.querySelector(".preview-slot");
  previewBtn.addEventListener("click", () => {
    if (previewSlot.innerHTML) {
      previewSlot.innerHTML = "";
      previewBtn.textContent = "Preview";
      return;
    }
    previewSlot.innerHTML = `<iframe src="https://www.youtube.com/embed/${r.id}" title="${escapeHtml(r.title)}" frameborder="0" allow="accelerometer; autoplay; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>`;
    previewBtn.textContent = "Hide preview";
  });

  const saveBtn = card.querySelector(".save-btn");
  if (queueIndex !== null) saveBtn.textContent = "Save & next";
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    try {
      const res = await fetch("/api/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(r),
      });
      const data = await res.json();
      // In a "search all" run, picking a video (even one saved earlier) moves on to the
      // next song. The index check ignores clicks on results for a song already moved past.
      // Advance first: the next search clears the status line, so set it afterwards.
      const picked = data.status === "ok" || data.status === "duplicate";
      if (picked && queue && queue.index === queueIndex) {
        advanceQueue("saved", r.title);
      }
      if (data.status === "ok") {
        statusEl.textContent = `Saved: ${r.title}`;
        saveBtn.textContent = "Saved ✓";
        loadSavedSongs();
      } else {
        statusEl.textContent = data.message || "Could not save.";
        saveBtn.disabled = false;
      }
    } catch (err) {
      statusEl.textContent = "Save failed: " + err.message;
      saveBtn.disabled = false;
    }
  });

  return card;
}

const bulkRefreshBtn = document.getElementById("bulk-refresh");
const bulkSelectAll = document.getElementById("bulk-select-all");
const bulkList = document.getElementById("bulk-list");
const bulkStartBtn = document.getElementById("bulk-start");
const bulkStatus = document.getElementById("bulk-status");
const bulkResolution = document.getElementById("bulk-resolution");
const bulkModeRadios = document.querySelectorAll('input[name="bulk-mode"]');

bulkRefreshBtn.addEventListener("click", loadSavedSongs);
bulkModeRadios.forEach((radio) => {
  radio.addEventListener("change", () => {
    const isVideo = document.querySelector('input[name="bulk-mode"]:checked').value === "video";
    bulkResolution.disabled = !isVideo;
  });
});

bulkSelectAll.addEventListener("change", () => {
  bulkList.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.checked = bulkSelectAll.checked;
  });
});

async function loadSavedSongs() {
  bulkStatus.textContent = "Loading saved songs…";
  bulkList.innerHTML = "";
  try {
    const res = await fetch("/api/saved");
    const data = await res.json();
    renderBulkList(data.songs || []);
  } catch (err) {
    bulkStatus.textContent = "Failed to load saved songs: " + err.message;
  }
}

function renderBulkList(songs) {
  if (!songs.length) {
    bulkStatus.textContent = "No saved songs yet. Save some from a search above first.";
    return;
  }

  bulkStatus.textContent = `${songs.length} saved song${songs.length === 1 ? "" : "s"}.`;
  bulkList.innerHTML = "";
  songs.forEach((s) => {
    const li = document.createElement("li");
    li.innerHTML = `
      <input type="checkbox" value="${escapeHtml(s.id)}" />
      <img src="${s.thumbnail}" alt="" />
      <span>${escapeHtml(s.title)}</span>
      <button type="button" class="download-one-btn">Download audio</button>
      <span class="download-one-status"></span>
    `;

    const downloadBtn = li.querySelector(".download-one-btn");
    const downloadStatus = li.querySelector(".download-one-status");
    downloadBtn.addEventListener("click", () => downloadSingleSong(s.id, downloadBtn, downloadStatus));

    bulkList.appendChild(li);
  });
  bulkSelectAll.checked = false;
}

async function downloadSingleSong(id, btn, statusSpan) {
  btn.disabled = true;
  statusSpan.textContent = "Starting…";

  try {
    const res = await fetch("/api/bulk-download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: [id], audio_only: true, max_resolution: null }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || `HTTP ${res.status}`);
    }
    pollJobStatus(data.job_id, (job) => {
      if (job.status === "running") {
        statusSpan.textContent = "Downloading…";
      } else if (job.status === "done") {
        statusSpan.textContent = `Saved to ${job.output_path}`;
        btn.disabled = false;
      } else {
        statusSpan.textContent = `Failed: ${job.error || "unknown error"}`;
        btn.disabled = false;
      }
    });
  } catch (err) {
    statusSpan.textContent = "Could not start: " + err.message;
    btn.disabled = false;
  }
}

bulkStartBtn.addEventListener("click", async () => {
  const ids = Array.from(bulkList.querySelectorAll('input[type="checkbox"]:checked')).map((cb) => cb.value);
  if (!ids.length) {
    bulkStatus.textContent = "Select at least one song first.";
    return;
  }

  const audioOnly = document.querySelector('input[name="bulk-mode"]:checked').value === "audio";
  const maxResolution = bulkResolution.value ? parseInt(bulkResolution.value, 10) : null;

  bulkStartBtn.disabled = true;
  bulkStatus.textContent = `Starting download of ${ids.length} song${ids.length === 1 ? "" : "s"}…`;

  try {
    const res = await fetch("/api/bulk-download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, audio_only: audioOnly, max_resolution: maxResolution }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || `HTTP ${res.status}`);
    }
    pollJobStatus(data.job_id, (job) => {
      if (job.status === "running") {
        bulkStatus.textContent = `Downloading ${job.total} item(s)… (see the server console for live progress)`;
      } else if (job.status === "done") {
        bulkStatus.textContent = `Done! Files saved to: ${job.output_path}`;
        bulkStartBtn.disabled = false;
      } else {
        bulkStatus.textContent = `Download failed: ${job.error || "unknown error"}`;
        bulkStartBtn.disabled = false;
      }
    });
  } catch (err) {
    bulkStatus.textContent = "Could not start download: " + err.message;
    bulkStartBtn.disabled = false;
  }
});

async function pollJobStatus(jobId, onUpdate) {
  try {
    const res = await fetch(`/api/bulk-download/${jobId}`);
    const job = await res.json();
    onUpdate(job);

    if (job.status === "running") {
      setTimeout(() => pollJobStatus(jobId, onUpdate), 2000);
    }
  } catch (err) {
    onUpdate({ status: "error", error: "Lost track of the download job: " + err.message });
  }
}

loadSavedSongs();

const notifBell = document.getElementById("notif-bell");
const notifBadge = document.getElementById("notif-badge");
const notifPanel = document.getElementById("notif-panel");
const notifList = document.getElementById("notif-list");
const notifClearBtn = document.getElementById("notif-clear");
const toastContainer = document.getElementById("toast-container");

let lastSeenLogId = 0;
let unreadCount = 0;
let panelOpen = false;

notifBell.addEventListener("click", () => {
  panelOpen = !panelOpen;
  notifPanel.hidden = !panelOpen;
  if (panelOpen) {
    unreadCount = 0;
    updateNotifBadge();
  }
});

document.addEventListener("click", (e) => {
  if (panelOpen && !notifPanel.contains(e.target) && e.target !== notifBell) {
    panelOpen = false;
    notifPanel.hidden = true;
  }
});

notifClearBtn.addEventListener("click", () => {
  notifList.innerHTML = '<li class="notif-empty">No activity yet.</li>';
});

function updateNotifBadge() {
  if (unreadCount > 0) {
    notifBadge.textContent = unreadCount > 99 ? "99+" : String(unreadCount);
    notifBadge.hidden = false;
  } else {
    notifBadge.hidden = true;
  }
}

function formatLogTime(iso) {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

function addNotifEntry(event) {
  const empty = notifList.querySelector(".notif-empty");
  if (empty) empty.remove();

  const li = document.createElement("li");
  li.className = `notif-${event.level}`;
  li.innerHTML = `${escapeHtml(event.message)}<span class="notif-time">${formatLogTime(event.time)}</span>`;
  notifList.insertBefore(li, notifList.firstChild);

  while (notifList.children.length > 50) {
    notifList.removeChild(notifList.lastChild);
  }
}

function showToast(event) {
  const toast = document.createElement("div");
  toast.className = `toast notif-${event.level}`;
  toast.textContent = event.message;
  toastContainer.appendChild(toast);
  setTimeout(() => toast.remove(), 6000);
}

async function pollActivityLog() {
  try {
    const res = await fetch(`/api/logs?since=${lastSeenLogId}`);
    const data = await res.json();
    const events = data.events || [];

    events.forEach((event) => {
      lastSeenLogId = Math.max(lastSeenLogId, event.id);
      addNotifEntry(event);
      showToast(event);
      if (!panelOpen) {
        unreadCount += 1;
      }
    });

    if (events.length) updateNotifBadge();
  } catch {
    // Network hiccup polling logs; just retry on the next tick.
  }

  setTimeout(pollActivityLog, 3000);
}

pollActivityLog();

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str || "";
  return div.innerHTML;
}

function formatDuration(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

// ---- Tabs ----
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.id !== `tab-${btn.dataset.tab}`));
  });
});

// ---- Expand tab ----
// Typed singer names -> /api/expand: Gemini identifies each singer and suggests songs, the server
// confirms them on YouTube and returns up to 5 videos per singer, which are saved like any result.
const expandForm = document.getElementById("expand-form");
const expandNames = document.getElementById("expand-names");
const expandBtn = document.getElementById("expand-btn");
const expandStatus = document.getElementById("expand-status");
const expandSummary = document.getElementById("expand-summary");
const expandResults = document.getElementById("expand-results");
const EXPAND_BATCH = 10;

expandForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const names = expandNames.value.split(/[\n,;]+/).map((n) => n.trim()).filter(Boolean);
  if (!names.length) return;

  expandBtn.disabled = true;
  expandStatus.classList.remove("warn");
  expandSummary.hidden = true;
  expandResults.innerHTML = "";

  // The server takes at most 20 singers per request and each one needs several YouTube
  // lookups, so long lists go in small batches and each batch shows up as soon as it's done.
  const unique = [...new Set(names)];
  const batches = [];
  for (let i = 0; i < unique.length; i += EXPAND_BATCH) batches.push(unique.slice(i, i + EXPAND_BATCH));

  const singers = [];
  try {
    const haveIds = [];
    try {
      haveIds.push(...((await (await fetch("/api/saved")).json()).songs || []).map((s) => s.id));
    } catch (_) {
      // Only used to avoid offering videos you already saved.
    }

    for (let b = 0; b < batches.length; b++) {
      expandStatus.textContent =
        batches.length === 1
          ? `Finding songs for ${unique.length} singer${unique.length === 1 ? "" : "s"} — this can take up to a minute…`
          : `Batch ${b + 1} of ${batches.length} (${singers.length} of ${unique.length} singers done) — this can take a few minutes…`;
      try {
        const data = await postJson("/api/expand", { names: batches[b], have_ids: haveIds });
        data.singers.forEach((s) => s.videos.forEach((v) => haveIds.push(v.id)));
        singers.push(...data.singers);
        renderExpand({ singers, total: singers.reduce((n, s) => n + s.videos.length, 0) }, b + 1 === batches.length);
      } catch (err) {
        const left = unique.length - singers.length;
        expandStatus.textContent = `Stopped at batch ${b + 1} of ${batches.length}: ${err.message || err}${
          singers.length ? ` — kept the ${singers.length} singer${singers.length === 1 ? "" : "s"} done so far; ${left} left to run again.` : ""
        }`;
        expandStatus.classList.add("warn");
        return;
      }
    }
  } finally {
    expandBtn.disabled = false;
  }
});

function renderExpand({ singers, total }, finished = true) {
  const unknown = singers.filter((s) => !s.recognised).length;
  if (finished) {
    expandStatus.textContent = `Done: ${total} video${total === 1 ? "" : "s"} from ${singers.length - unknown} singer${singers.length - unknown === 1 ? "" : "s"}. Save the ones you want below.`;
  }

  // The totals: one row per singer, then the overall count.
  expandSummary.hidden = false;
  expandSummary.innerHTML = `
    <table>
      <thead><tr><th>Typed</th><th>Singer</th><th>Videos</th></tr></thead>
      <tbody>
        ${singers
          .map(
            (s) => `<tr>
              <td>${escapeHtml(s.typed)}</td>
              <td>${s.recognised ? escapeHtml(s.singer) : '<span class="ocr-unsure">not recognised</span>'}</td>
              <td>${s.videos.length}${s.videos.length < 5 && s.recognised ? ` <span class="hint">of ${s.suggested} suggested</span>` : ""}</td>
            </tr>`
          )
          .join("")}
      </tbody>
      <tfoot><tr><td colspan="2">Total</td><td>${total}</td></tr></tfoot>
    </table>`;

  expandResults.innerHTML = "";
  singers.forEach((s) => {
    if (!s.videos.length) return;
    const section = document.createElement("section");
    section.className = "more-singer";
    const toppedUp = s.videos.filter((v) => v.topped_up).length;
    section.innerHTML = `<h3>${escapeHtml(s.singer || s.typed)} <span class="hint">${s.videos.length} video${s.videos.length === 1 ? "" : "s"}${
      toppedUp ? ` · ${toppedUp} from a general search for the singer` : ""
    }</span></h3>`;
    const grid = document.createElement("div");
    grid.className = "more-grid";
    s.videos.forEach((v) => grid.appendChild(buildResultCard(v, null)));
    section.appendChild(grid);
    expandResults.appendChild(section);
  });
}
