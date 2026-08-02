// Scout extension — popup logic.
//
// "Scrape Current Page" talks straight to the active tab's content script
// (it's already loaded there). Saved Searches and Custom Search instead ask
// background.js to open/navigate a tab and hand off once it's loaded — see
// SCOUT_RUN_SAVED_SEARCH/SCOUT_RUN_SINGLE_JOB in background.js. Both paths
// converge on the exact same content.js harvest, so progress rendering
// (renderProgress) is shared regardless of which button was clicked.

const contentEl = document.getElementById("content");
const offlineEl = document.getElementById("offline");
const connStatusEl = document.getElementById("conn-status");
const searchListEl = document.getElementById("saved-searches");
const reloadBtn = document.getElementById("btn-reload");
const reloadErrorEl = document.getElementById("reload-error");
const currentPageBtn = document.getElementById("btn-current");
const currentPageHint = document.getElementById("current-page-hint");
const customUrlInput = document.getElementById("custom-url");
const customBtn = document.getElementById("btn-custom");
const drawerEl = document.getElementById("drawer");
const themeToggleBtn = document.getElementById("theme-toggle");
const abortBtn = document.getElementById("btn-abort");

const LINKEDIN_JOBS_PATTERN = /^https:\/\/www\.linkedin\.com\/jobs\//;

// Ported from agent/runner.py's extract_single_job_id/resolve_scan_url and
// scanurl_c_smart.html's classify() — kept extension-side only, per design
// discussion: this is scrape-routing logic, not shared config, so it isn't
// worth a backend round-trip or a second Python implementation to stay in
// sync with.
const JOB_VIEW_RE = /\/jobs\/view\/(\d+)/;
const BARE_JOB_ID_RE = /^\d+$/;
const SEARCH_URL_RE = /\/jobs\/(search|search-results|collections)|\/comm\/jobs\/search/;

// Must match content.js's STORAGE_KEY — the single shared chrome.storage.local
// slot for the current/last run's halt state.
const STORAGE_KEY = "scout_run_state";

// How often to poll /api/extension/status for Pass 2/3 progress once
// ingest kicks it off server-side. Not the same cadence as the harvest's own
// live broadcasts (those are instant) — this is a plain HTTP poll.
const STATUS_POLL_MS = 1500;

let harvesting = false;
let statusPollTimer = null;
// The tab id the current harvest is running in, if any — needed so Abort can
// message the right tab (see abortRun()). null whenever there's no browser
// harvest to reach (Pass 2/3 only, or idle). Restored from chrome.storage.local
// on a popup reopen via checkStoredRunState(), same as the rest of run state.
let harvestingTabId = null;

init();

async function init() {
  try {
    const data = await sendMessage({ type: "SCOUT_GET_SEARCHES" });
    showConnected();
    renderSearches(data.searches || []);
    await refreshCurrentPageButton();

    // A run may still be crunching Pass 2/3 server-side from before this
    // popup was (re)opened — check live backend state before falling back
    // to the extension-local state (running Pass 1 / halted), since "Pass
    // 2/3 running" takes priority over either of those.
    let status = null;
    try {
      status = await sendMessage({ type: "SCOUT_GET_STATUS" });
    } catch (err) {
      // Non-fatal — just skip rehydration if the status call itself fails.
    }
    if (status && status.running) {
      rehydratePass23Running(status);
    } else {
      await checkStoredRunState();
    }
  } catch (err) {
    showOffline();
  }
}

/**
 * Popup reopened while Pass 2/3 is still running (e.g. after ingest from an
 * earlier popup session that's since closed). Resumes live polling from
 * scratch — startStatusPolling() renders the full accumulated log on its
 * first tick, which is exactly the "recover full detail on reopen" behavior
 * per the design: nothing needs to be persisted client-side for this.
 */
function rehydratePass23Running(status) {
  drawerEl.innerHTML = "";
  const name = (status.searches || [])[0]?.name;
  if (name) upsertStep("s-target", "done", name);
  harvesting = true;
  // Pass 1 has already finished by this point (content.js clears its stored
  // state as soon as ingest hands off — see fetchAndIngest) — nothing left
  // to send SCOUT_ABORT to. Abort still works here via the backend kill
  // alone (see abortRun()).
  harvestingTabId = null;
  applyRunTriggerState();
  startStatusPolling();
}

/**
 * Rehydrate Pass 1 (content.js's harvest) or a halt from chrome.storage.local
 * on popup open — the popup itself carries no state across closes, so this
 * is the only way an in-flight or halted run from an earlier (now-closed)
 * popup session is still visible.
 *
 * A "running" state has no progress history to show (harvest lines are
 * broadcast-only, never persisted — see content.js's report()) — the
 * critical thing is disabling every Run button, not reconstructing exactly
 * which step it's on. If the harvest is still going and this popup stays
 * open, the very next SCOUT_HARVEST_PROGRESS broadcast will fill in real
 * detail via the onMessage listener below, same as if this popup had been
 * open the whole time.
 */
async function checkStoredRunState() {
  const stored = await new Promise((resolve) => chrome.storage.local.get(STORAGE_KEY, resolve));
  const state = stored[STORAGE_KEY];
  if (!state) return;

  drawerEl.innerHTML = "";
  upsertStep("s-target", "done", state.searchName);
  if (state.status === "running") {
    upsertStep("s-wait", "active", "Run in progress in the background…");
    harvesting = true;
    harvestingTabId = state.tabId || null;
    applyRunTriggerState();
  } else if (state.status === "halted") {
    appendHaltBanner(state);
  }
}

chrome.runtime.onMessage.addListener((message) => {
  if (message && message.type === "SCOUT_HARVEST_PROGRESS") {
    renderProgress(message.event);
  }
});

currentPageBtn.addEventListener("click", startCurrentPageHarvest);
customUrlInput.addEventListener("input", refreshCustomButton);
customBtn.addEventListener("click", startCustomSearchHarvest);
themeToggleBtn.addEventListener("click", toggleTheme);
reloadBtn.addEventListener("click", reloadSearches);
abortBtn.addEventListener("click", abortRun);

/**
 * Flip between light/dark, persisting the choice so it survives the next
 * popup open (popup.html's inline head script applies it before first
 * paint). Starts from whichever theme is currently rendered (system
 * preference, if the user hasn't chosen one yet) rather than assuming dark.
 */
function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme")
    || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("scout-theme", next);
}

/**
 * Send a typed message to background.js and resolve with its response,
 * rejecting if the backend/background relay reported an error.
 */
function sendMessage(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      if (response && response.error) {
        reject(new Error(response.error));
        return;
      }
      resolve(response);
    });
  });
}

/**
 * Render the popup's normal (connected) state.
 */
function showConnected() {
  connStatusEl.textContent = "Connected · localhost:8000";
  connStatusEl.className = "conn-status conn-status--ok";
  contentEl.classList.remove("hidden");
  offlineEl.classList.add("hidden");
}

/**
 * Render the "Scout isn't running" state and hide the rest of the popup —
 * there's nothing useful to show (or click) without a backend to talk to.
 */
function showOffline() {
  connStatusEl.textContent = "Not connected";
  connStatusEl.className = "conn-status conn-status--error";
  contentEl.classList.add("hidden");
  offlineEl.classList.remove("hidden");
}

/**
 * Populate the saved-search list from GET /api/extension/searches's response.
 */
function renderSearches(searches) {
  searchListEl.innerHTML = "";
  reloadBtn.disabled = harvesting;
  if (searches.length === 0) {
    const empty = document.createElement("div");
    empty.className = "hint";
    empty.textContent = "No saved searches configured in profiles/config.toml.";
    searchListEl.appendChild(empty);
    return;
  }
  for (const search of searches) {
    const row = document.createElement("div");
    row.className = "search-row";

    const name = document.createElement("span");
    name.className = "search-name";
    name.textContent = search.name;
    name.title = search.name;

    const runBtn = document.createElement("button");
    runBtn.className = "btn run-trigger";
    runBtn.textContent = "Run";
    runBtn.disabled = harvesting;
    runBtn.addEventListener("click", () => runSavedSearch(search));

    row.appendChild(name);
    row.appendChild(runBtn);
    searchListEl.appendChild(row);
  }
}

/**
 * Re-read profiles/config.toml server-side (SCOUT_RELOAD_SEARCHES →
 * POST /api/extension/reload-config, which clears app/config.py's
 * lru_cache'd Config) and re-render the saved-search list from the result.
 * On failure (e.g. the file was mid-edit and fails to parse), the currently
 * displayed list is left alone and the error is shown inline instead —
 * never wipe a working list over a bad edit.
 */
async function reloadSearches() {
  reloadBtn.disabled = true;
  reloadBtn.classList.add("is-loading");
  reloadErrorEl.classList.add("hidden");
  try {
    const data = await sendMessage({ type: "SCOUT_RELOAD_SEARCHES" });
    renderSearches(data.searches || []);
  } catch (err) {
    reloadErrorEl.textContent = err.message || "Couldn't reload saved searches.";
    reloadErrorEl.classList.remove("hidden");
  } finally {
    reloadBtn.classList.remove("is-loading");
    reloadBtn.disabled = harvesting;
  }
}

/**
 * Classify Custom Search's raw input the same way the CDP path's smart-paste
 * bar does: empty, a bare numeric job id, a /jobs/view/<id> URL, a search
 * URL, or unsupported/invalid.
 */
function classifyCustomInput(raw) {
  const v = (raw || "").trim();
  if (!v) return { kind: "empty" };
  if (BARE_JOB_ID_RE.test(v)) {
    return { kind: "single", jobId: v, url: `https://www.linkedin.com/jobs/view/${v}/` };
  }
  let u;
  try {
    u = new URL(v);
  } catch {
    return { kind: "invalid" };
  }
  if (!/(^|\.)linkedin\.com$/i.test(u.hostname)) return { kind: "invalid" };
  const viewMatch = u.pathname.match(JOB_VIEW_RE);
  if (viewMatch) return { kind: "single", jobId: viewMatch[1], url: v };
  if (SEARCH_URL_RE.test(u.pathname)) return { kind: "search", url: v };
  return { kind: "unsupported" };
}

function refreshCustomButton() {
  const c = classifyCustomInput(customUrlInput.value);
  customBtn.disabled = harvesting || (c.kind !== "search" && c.kind !== "single");
}

/**
 * Enable "Scrape Current Page" only when the active tab is a LinkedIn jobs
 * page (content.js is only injected there — clicking it anywhere else would
 * have nothing on the other end).
 */
async function refreshCurrentPageButton() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const onJobsPage = !!(tab && tab.url && LINKEDIN_JOBS_PATTERN.test(tab.url));
  currentPageBtn.disabled = !onJobsPage || harvesting;
  currentPageHint.textContent = onJobsPage
    ? "Scrapes every job on this page."
    : "Open a linkedin.com/jobs page to enable this.";
}

/**
 * Kick off content.js's harvest on the active tab and switch the drawer
 * into its "running" state. Progress arrives asynchronously via
 * SCOUT_HARVEST_PROGRESS broadcasts, handled by renderProgress().
 */
async function startCurrentPageHarvest() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !LINKEDIN_JOBS_PATTERN.test(tab.url || "")) return;

  beginRun("Current page");
  harvestingTabId = tab.id;
  try {
    await new Promise((resolve, reject) => {
      chrome.tabs.sendMessage(tab.id, { type: "SCOUT_HARVEST", searchName: "Manual scrape", tabId: tab.id }, (resp) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        resolve(resp);
      });
    });
  } catch (err) {
    failRun(`Couldn't reach this tab: ${err.message}`);
  }
}

/**
 * Run a saved search: background.js opens (or reuses) a tab, navigates it,
 * waits for load, and hands off to content.js there.
 */
async function runSavedSearch(search) {
  beginRun(search.name);
  try {
    const resp = await sendMessage({ type: "SCOUT_RUN_SAVED_SEARCH", url: search.url, searchName: search.name });
    harvestingTabId = resp.tabId ?? null;
  } catch (err) {
    failRun(`Couldn't run "${search.name}": ${err.message}`);
  }
}

/**
 * Run Custom Search: classify the pasted URL/job id, then dispatch to
 * whichever background message type matches (search vs single job).
 */
async function startCustomSearchHarvest() {
  const c = classifyCustomInput(customUrlInput.value);
  if (c.kind !== "search" && c.kind !== "single") return;

  beginRun("Custom Search");
  try {
    const resp = c.kind === "search"
      ? await sendMessage({ type: "SCOUT_RUN_SAVED_SEARCH", url: c.url, searchName: "Custom Search" })
      : await sendMessage({ type: "SCOUT_RUN_SINGLE_JOB", url: c.url, jobId: c.jobId, searchName: "Custom Search" });
    harvestingTabId = resp.tabId ?? null;
  } catch (err) {
    failRun(`Couldn't start: ${err.message}`);
  }
}

/**
 * Render one progress event from content.js into the drawer as a step row
 * (icon + label + optional stat, matching mockups/extension_popup.html),
 * plus an indented per-job log line where relevant. Re-enables the Run
 * controls once the harvest reaches a terminal phase with nothing further
 * to wait on.
 */
function renderProgress(event) {
  switch (event.phase) {
    case "waiting":
      upsertStep("s-wait", "active", "Waiting for results to render…");
      return;
    case "harvested":
      upsertStep("s-wait", "done", "Page rendered");
      upsertStep("s-harvest", "done", "Harvested from DOM",
                `${event.count} job ID${event.count === 1 ? "" : "s"} found`);
      if (event.count === 0) finishWithoutIngest("no job cards found on this page");
      return;
    case "deduped":
      upsertStep("s-dedupe", "done", "Dedupe complete",
                `${event.new} new · ${event.total - event.new} already seen`);
      if (event.new === 0) finishWithoutIngest("nothing new");
      return;
    case "fetched": {
      upsertStep("s-voyager", "active", `Fetching jobs from LinkedIn — ${event.index} of ${event.total}`);
      const who = event.title && event.company
        ? `${truncate(event.title, 45)} @ ${truncate(event.company, 22)}`
        : `job ${event.jobId}`;
      appendLogLine(`→ fetched ${who}`, "good");
      return;
    }
    case "halted":
      upsertStep("s-voyager", "error", `Halted — HTTP ${event.state.haltedStatus} from LinkedIn`,
                `${event.state.completedCount} of ${event.state.totalCount} completed, ${event.state.pendingIds.length} pending`);
      appendLogLine(`${event.accepted ?? 0} job(s) ingested before halting.`, event.accepted ? "good" : null);
      appendHaltBanner(event.state);
      if (event.accepted) startStatusPolling(); else endRun();
      return;
    case "done":
      if (event.accepted) {
        upsertStep("s-voyager", "done", "Voyager loop complete");
        upsertStep("s-ingest", "done", "Ingested", `${event.accepted} job(s) accepted · run started`);
        startStatusPolling();
      } else {
        finishWithoutIngest(event.note || "no jobs to ingest");
      }
      return;
    case "error":
      appendLogLine(`Error: ${event.message}`, "bad");
      endRun();
      return;
    case "aborted":
      // abortRun() already showed the "Stopped by user" line and re-enabled
      // controls the instant the button was clicked — this delayed event is
      // just confirmation that the harvesting tab actually noticed the flag.
      // Only re-freeze any step icon that's still spinning (rare: the tab's
      // own report can race abortRun()'s immediate freeze if a step
      // transitioned in between) and no-op endRun() again.
      freezeSpinningSteps();
      endRun();
      return;
  }
}

function finishWithoutIngest(note) {
  appendLogLine(`Done — nothing to save (${note}).`, "good");
  endRun();
}

/**
 * Poll GET /api/extension/status (relayed through background.js) for Pass
 * 2/3 progress once ingest has kicked it off server-side, streaming the
 * backend's own log lines (clean/enrich/save head, per-job, and summary
 * messages — already in true chronological order) straight into the drawer.
 * Starting shownLogCount at 0 on every call means a fresh popup (first run,
 * or reopened mid-run) always renders the full accumulated log, not just
 * what's new since some earlier point it never actually saw.
 */
function startStatusPolling() {
  stopStatusPolling();
  let shownLogCount = 0;
  const tick = async () => {
    let status;
    try {
      status = await sendMessage({ type: "SCOUT_GET_STATUS" });
    } catch (err) {
      return; // transient poll failure — try again next tick
    }

    // Deliberately no separate step-row rendering for clean/enrich/save here
    // (unlike Pass 1's s-wait/s-harvest/etc. above, which come from content.js's
    // live broadcasts). A step row and this tick's log lines were two separate
    // rendering paths updated in a fixed order every tick, which could put a
    // freshly-created "Pass 3" header before that same tick's tail-end Pass 2
    // log lines when a poll straddled the transition. The backend's log
    // already carries equivalent head/summary lines ("Cleaning N
    // descriptions…", "Cleaning done · N/N (Xs)", "Enriching N jobs…", …) in
    // true chronological order, so there's nothing lost by relying on it alone.
    const newLines = (status.log || []).slice(shownLogCount);
    shownLogCount = (status.log || []).length;
    newLines.forEach((line) =>
      appendLogLine(truncateJobLogMsg(line.msg), line.level === "good" ? "good" : line.level === "warn" ? "bad" : null));

    if (!status.running) {
      if (status.killed) appendLogLine("■ Stopped by user.", "muted");
      else if (status.error) appendLogLine(`Scout server error: ${status.error}`, "bad");
      stopStatusPolling();
      endRun();
    }
  };
  tick();
  statusPollTimer = setInterval(tick, STATUS_POLL_MS);
}

function stopStatusPolling() {
  if (statusPollTimer) {
    clearInterval(statusPollTimer);
    statusPollTimer = null;
  }
}

/**
 * Build (or update in place) one step row — icon + label + optional
 * mono-font stat, matching mockups/extension_popup.html's step display.
 */
function upsertStep(id, status, label, sub) {
  const row = document.createElement("div");
  row.id = id;
  row.className = "step-row";
  row.appendChild(buildStepIcon(status));

  const body = document.createElement("div");
  body.className = "step-body";
  const labelEl = document.createElement("div");
  labelEl.className = "step-label step-label--" + status;
  labelEl.textContent = label;
  body.appendChild(labelEl);
  if (sub) {
    const subEl = document.createElement("div");
    subEl.className = "step-sub";
    subEl.textContent = sub;
    body.appendChild(subEl);
  }
  row.appendChild(body);

  const existing = document.getElementById(id);
  if (existing) existing.replaceWith(row);
  else drawerEl.appendChild(row);
  drawerEl.scrollTop = drawerEl.scrollHeight;
}

function buildStepIcon(status) {
  const span = document.createElement("span");
  span.className = "step-icon";
  if (status === "active") {
    const spinner = document.createElement("span");
    spinner.className = "step-spinner";
    span.appendChild(spinner);
  } else if (status === "done") {
    span.classList.add("step-icon--done");
    span.innerHTML = '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M3 8.5l3.2 3.2L13 5"/></svg>';
  } else if (status === "error") {
    span.classList.add("step-icon--error");
    span.innerHTML = '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M4 4l8 8M12 4l-8 8"/></svg>';
  } else if (status === "stopped") {
    span.classList.add("step-icon--stopped");
    span.innerHTML = '<svg viewBox="0 0 16 16" width="10" height="10" fill="currentColor"><rect x="3" y="3" width="10" height="10" rx="1.5"/></svg>';
  } else {
    const dot = document.createElement("span");
    dot.className = "step-dot";
    span.appendChild(dot);
  }
  return span;
}

function appendLogLine(text, tone) {
  const div = document.createElement("div");
  div.className = "log-line" + (tone ? ` log-line--${tone}` : "");
  div.textContent = text;
  drawerEl.appendChild(div);
  drawerEl.scrollTop = drawerEl.scrollHeight;
}

function truncate(str, maxLen) {
  if (!str || str.length <= maxLen) return str;
  return str.slice(0, maxLen).trimEnd() + "…";
}

// Backend Pass 2/3 log lines (from /api/extension/status's run["log"], see
// agent/runner.py's emit_log calls) arrive as fully-formatted strings, e.g.
// "✓ cleaned {title} @ {company} (4/7)" or
// "✓ {title} @ {company} — 78.0/100 · 8s" or "✗ {title} @ {company} — dropped (Other)".
// Long real-world titles/companies push these past one line — this shortens
// just the title/company parts so the trailing score/timing info (the part
// that actually changes per job) never gets pushed out of view. Falls back
// to the message untouched if it doesn't match one of those known shapes.
function truncateJobLogMsg(msg) {
  const atIdx = msg.indexOf(" @ ");
  if (atIdx === -1) return msg;
  const prefixMatch = msg.slice(0, atIdx).match(/^(✓ cleaned |✓ |✗ )(.*)$/);
  if (!prefixMatch) return msg;
  const [, lead, title] = prefixMatch;
  const rest = msg.slice(atIdx + 3);
  const tailMatch = rest.match(/\s(?=\(|—)/);
  const splitIdx = tailMatch ? tailMatch.index : rest.length;
  const company = rest.slice(0, splitIdx);
  const tail = rest.slice(splitIdx);
  return `${lead}${truncate(title, 45)} @ ${truncate(company, 22)}${tail}`;
}

/**
 * Render a halt banner with a Resume button, from either a live
 * SCOUT_HARVEST_PROGRESS event or storage read on popup open — both use the
 * exact same state shape (see content.js's fetchAndIngest), so this is the
 * one place that knows how to display it.
 */
function appendHaltBanner(state) {
  const wrap = document.createElement("div");
  wrap.className = "halt-banner";

  const text = document.createElement("div");
  text.className = "halt-text";
  text.textContent = `Halted — LinkedIn returned HTTP ${state.haltedStatus}. `
    + `${state.completedCount} of ${state.totalCount} completed, ${state.pendingIds.length} pending.`;

  const resumeBtn = document.createElement("button");
  resumeBtn.className = "btn";
  resumeBtn.textContent = "Resume";
  resumeBtn.addEventListener("click", () => resumeRun(state));

  wrap.appendChild(text);
  wrap.appendChild(resumeBtn);
  drawerEl.appendChild(wrap);
  drawerEl.scrollTop = drawerEl.scrollHeight;
}

/**
 * Resume a halted run from its persisted state. Always opens a fresh tab
 * (see background.js's SCOUT_RESUME_RUN) rather than assuming the original
 * tab is still open — the user may have closed it since halting.
 */
async function resumeRun(state) {
  beginRun(state.searchName);
  upsertStep("s-voyager", "active", `Resuming — ${state.completedCount} of ${state.totalCount}`);
  try {
    const resp = await sendMessage({
      type: "SCOUT_RESUME_RUN",
      url: state.url,
      pendingIds: state.pendingIds,
      searchName: state.searchName,
      completedCount: state.completedCount,
      totalCount: state.totalCount,
    });
    harvestingTabId = resp.tabId ?? null;
  } catch (err) {
    failRun(`Couldn't resume: ${err.message}`);
  }
}

/**
 * Disable every Run control and reset the drawer — called the instant any
 * Run button is clicked, before its background/content-script round trip
 * even starts, so a second click can't race in. Renders a "done" step row
 * naming the target (search name, "Current page", "Custom Search", …),
 * matching mockups/extension_popup.html's s-target row.
 */
function beginRun(targetLabel) {
  stopStatusPolling(); // discard any leftover poll from a run superseded by this one
  harvesting = true;
  applyRunTriggerState();
  drawerEl.innerHTML = "";
  upsertStep("s-target", "done", targetLabel);
}

/**
 * Re-enable every Run control once a harvest reaches a terminal phase.
 */
function endRun() {
  harvesting = false;
  harvestingTabId = null;
  applyRunTriggerState();
}

/**
 * A run failed before content.js ever got to report progress on its own
 * (e.g. the tab couldn't be reached) — show it and re-enable controls.
 */
function failRun(message) {
  appendLogLine(message, "bad");
  endRun();
}

function applyRunTriggerState() {
  document.querySelectorAll(".run-trigger").forEach((b) => { b.disabled = harvesting; });
  reloadBtn.disabled = harvesting;
  // Abort is the mirror image of every other control here: enabled only
  // while a run is actually active, disabled the rest of the time.
  abortBtn.disabled = !harvesting;
  refreshCurrentPageButton();
  refreshCustomButton();
}

/**
 * Stop the run right where it is, whichever pass it's in. Fires both a
 * browser-side SCOUT_ABORT (to whichever tab is harvesting, if any) and the
 * backend kill (POST /api/extension/kill, for a Pass 2/3 subprocess) rather
 * than trying to track precisely which pass is live — whichever one is
 * actually running absorbs it, the other is a harmless no-op. No
 * confirmation dialog (explicit design decision) and no Resume offered
 * afterward, unlike a block-halt — this is deliberate, not something to
 * recover from.
 *
 * Re-enables every control immediately rather than waiting for the actual
 * "aborted"/killed confirmation to round-trip back — the harvesting tab's
 * own loop only notices SCOUT_ABORT at its next checkpoint, which can lag by
 * up to one fetch-jitter cycle, and there's no reason to hold the popup
 * hostage to that. Both signals are fire-and-forget; a real confirmation
 * (the "■ Stopped by user" log line) still arrives shortly after and is
 * harmless to render on top of an already-idle popup.
 */
function abortRun() {
  if (!harvesting) return;

  if (harvestingTabId) {
    chrome.tabs.sendMessage(harvestingTabId, { type: "SCOUT_ABORT" }, () => {
      void chrome.runtime.lastError; // best-effort — tab may already be gone
    });
  }
  sendMessage({ type: "SCOUT_KILL" }).catch(() => null);

  // Show the confirmation and freeze the drawer *before* re-enabling
  // controls, not after — otherwise the buttons visibly come back to life
  // a moment before the user sees any acknowledgment that Abort did
  // anything at all.
  freezeSpinningSteps();
  appendLogLine("■ Stopped by user — nothing further was fetched or saved.", "muted");
  stopStatusPolling();
  endRun();
}

/**
 * Freeze whichever step is still showing a spinner into a neutral "stopped"
 * icon — otherwise it'd keep animating forever with nothing left to update
 * it. Only steps that actually got shown (existing in the DOM) are touched,
 * so this never spuriously creates a row for a phase the run never reached.
 */
function freezeSpinningSteps() {
  ["s-wait", "s-harvest", "s-dedupe", "s-voyager", "s-ingest"].forEach((id) => {
    const el = document.getElementById(id);
    if (el && el.querySelector(".step-spinner")) {
      const label = el.querySelector(".step-label")?.textContent || "";
      upsertStep(id, "stopped", `${label} — stopped`);
    }
  });
}
