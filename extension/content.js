// Scout extension — content script.
//
// Runs inside the user's own authenticated LinkedIn tab, so Voyager API
// calls are same-origin with real session cookies — mechanically identical
// to what a human browsing LinkedIn's own SPA does. Ported from
// agent/scrape_prompt.md's Step 1 (DOM harvest) and Step 2 (Voyager fetch),
// with two changes: the fetch loop is sequential+jittered instead of
// Promise.all (decision: a multi-minute loop needs to look like a human
// clicking through jobs, not a burst of parallel requests), and it halts
// hard on a block signal instead of the CDP prompt's "retry once and move
// on" (that fallback is for a single dead job id, not for LinkedIn actively
// pushing back on the whole run).

const RENDER_QUIET_MS = 500;
const RENDER_TIMEOUT_MS = 15000;
// The /jobs/search/ DOM variant (data-occludable-job-id) virtualizes the
// results list — LinkedIn only mounts ~7 cards' worth of DOM up front and
// mounts the rest in further bursts as it keeps hydrating, with gaps between
// bursts that can exceed RENDER_QUIET_MS. A single quiet-period wait can
// therefore settle after just the first burst. HARVEST_* drives a follow-up
// poll loop that nudges scroll (in case mounting is scroll-triggered) and
// keeps re-harvesting until the id count stops growing for two checks in a
// row, so a mid-render pause isn't mistaken for "done".
const HARVEST_POLL_MS = 700;
const HARVEST_STABLE_ROUNDS = 2;
const HARVEST_MAX_MS = 20000;
const WORKPLACE = { "1": "On-site", "2": "Remote", "3": "Hybrid" };

// Single overwritable chrome.storage.local slot tracking the current/last
// run's state — no persisted verbose log, no pruning logic (see design
// discussion). Holds one of: {status: "running", searchName, url, startedAt}
// (written the instant any harvest starts — this is what lets a reopened
// popup know a run is active and keep every button disabled instead of
// looking idle mid-scrape) or {status: "halted", ...} (as before). A fresh
// Run always overwrites it; Resume reads/updates it in place; any clean
// finish or unexpected error clears it. That bounds storage to "at most one
// run's worth of state" at any time.
const STORAGE_KEY = "scout_run_state";

// Set by SCOUT_ABORT (the popup's Abort button) and checked at every natural
// checkpoint in the harvest — see bailIfAborted()/HARVEST_* polling and
// fetchAndIngest's loop. Reset to false at the start of every fresh
// run/resume in markRunning(), so it can never leak from a previous run.
let abortRequested = false;

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === "SCOUT_HARVEST") {
    runHarvest(message.searchName || "Manual scrape", message.tabId);
    sendResponse({ started: true });
  } else if (message && message.type === "SCOUT_HARVEST_SINGLE_JOB") {
    runSingleJobHarvest(message.jobId, message.searchName || "Single job scan", message.tabId);
    sendResponse({ started: true });
  } else if (message && message.type === "SCOUT_RESUME_HARVEST") {
    runResumeHarvest(message.pendingIds, message.searchName,
                     message.completedCount || 0, message.totalCount || message.pendingIds.length,
                     message.tabId);
    sendResponse({ started: true });
  } else if (message && message.type === "SCOUT_ABORT") {
    abortRequested = true;
    sendResponse({ acknowledged: true });
  }
});

/**
 * If SCOUT_ABORT landed since the run started, tear down (clear stored
 * state, report the terminal "aborted" phase) and return true so the caller
 * can bail out immediately. A no-op returning false otherwise. Checked at
 * every natural pause point in runHarvest — the long jittered fetch loop
 * itself is covered separately inside fetchAndIngest, which has its own
 * abortRequested check at the top of the loop.
 */
async function bailIfAborted() {
  if (!abortRequested) return false;
  await clearRunState();
  report({ phase: "aborted" });
  return true;
}

/**
 * Run the full Pass 1 pipeline against the current page: wait for the
 * results to render, harvest job ids from the DOM (polling until the count
 * is stable — see harvestJobIdsUntilStable), dedupe against the backend,
 * then fetch+ingest every new job (see fetchAndIngest).
 */
async function runHarvest(searchName, tabId) {
  try {
    await markRunning(searchName, tabId);
    report({ phase: "waiting" });
    await waitForRender();
    if (await bailIfAborted()) return;

    const jobIds = await harvestJobIdsUntilStable();
    if (await bailIfAborted()) return;
    report({ phase: "harvested", count: jobIds.length });
    if (jobIds.length === 0) {
      await clearRunState();
      report({ phase: "done", accepted: 0, note: "no job cards found on this page" });
      return;
    }

    const dedupeResult = await relay({ type: "SCOUT_DEDUPE", jobIds });
    if (await bailIfAborted()) return;
    const newIds = dedupeResult.new_ids || [];
    report({ phase: "deduped", total: jobIds.length, new: newIds.length });
    if (newIds.length === 0) {
      await clearRunState();
      report({ phase: "done", accepted: 0, note: "nothing new" });
      return;
    }

    await fetchAndIngest(newIds, searchName, 0, newIds.length);
  } catch (err) {
    await clearRunState();
    report({ phase: "error", message: err.message || String(err) });
  }
}

/**
 * Run Pass 1 for a single known job id (the Custom Search "paste a job URL"
 * path) — no DOM harvest needed since the id came straight from the URL,
 * just a dedupe check and one Voyager fetch. Mirrors the CDP path's
 * scrape_single_prompt.md variant, minus the browser-agent overhead.
 */
async function runSingleJobHarvest(jobId, searchName, tabId) {
  try {
    await markRunning(searchName, tabId);
    const dedupeResult = await relay({ type: "SCOUT_DEDUPE", jobIds: [jobId] });
    if (await bailIfAborted()) return;
    if ((dedupeResult.new_ids || []).length === 0) {
      await clearRunState();
      report({ phase: "done", accepted: 0, note: "already in Scout" });
      return;
    }
    await fetchAndIngest([jobId], searchName, 0, 1);
  } catch (err) {
    await clearRunState();
    report({ phase: "error", message: err.message || String(err) });
  }
}

/**
 * Continue a previously-halted run against just its pending ids. Marks the
 * slot "running" again immediately (a reopened popup should see an active
 * resume the same way it'd see any other active run, not the stale halted
 * state it's resuming from) — fetchAndIngest overwrites it again with fresh
 * halt state if this halts a second time, or clears it on success.
 */
async function runResumeHarvest(pendingIds, searchName, completedCount, totalCount, tabId) {
  try {
    await markRunning(searchName, tabId);
    await fetchAndIngest(pendingIds, searchName, completedCount, totalCount);
  } catch (err) {
    await clearRunState();
    report({ phase: "error", message: err.message || String(err) });
  }
}

/**
 * Fetch each of ``pendingIds`` sequentially with jitter, halting hard on a
 * block signal (see fetchJob), then ingest whatever was collected — even a
 * partial batch, so a halt never throws away already-fetched work. On halt,
 * persists the remaining pending ids to storage for Resume; on a clean
 * finish, clears any stored state. ``completedSoFar``/``total`` are for
 * progress display continuity across a Resume (e.g. "6 of 9" rather than
 * restarting the counter at 1).
 *
 * Also checked here (not just via bailIfAborted's callers) is abortRequested
 * — this loop, with its multi-second jitter between fetches, is where an
 * Abort click is overwhelmingly likely to land. Unlike a halt, an abort
 * deliberately discards whatever's already been fetched rather than
 * ingesting it: ingesting would spawn a *new* Pass 2/3 subprocess after the
 * click, which the backend kill signal (fired at click time, before that
 * subprocess existed) can never reach — leaving the run looking stuck from
 * the popup's perspective long after it visibly "stopped." See
 * extension/popup.js's abortRun().
 */
async function fetchAndIngest(pendingIds, searchName, completedSoFar, total) {
  const pacing = await relay({ type: "SCOUT_GET_SEARCHES" });
  const minDelay = pacing.min_delay_ms || 3000;
  const maxDelay = pacing.max_delay_ms || 8000;

  const jobs = {};
  let haltState = null;
  for (let i = 0; i < pendingIds.length; i++) {
    if (abortRequested) {
      await clearRunState();
      report({ phase: "aborted" });
      return;
    }
    const jobId = pendingIds[i];
    const outcome = await fetchJob(jobId);
    if (outcome.halted) {
      haltState = {
        status: "halted",
        searchName,
        url: location.href,
        pendingIds: pendingIds.slice(i),
        haltedStatus: outcome.status,
        completedCount: completedSoFar + i,
        totalCount: total,
        updatedAt: Date.now(),
      };
      break;
    }
    jobs[jobId] = outcome.record;
    report({
      phase: "fetched", index: completedSoFar + i + 1, total, jobId,
      title: outcome.record.title, company: outcome.record.company,
    });
    if (i < pendingIds.length - 1) {
      await sleep(minDelay + Math.random() * (maxDelay - minDelay));
    }
  }

  // Ingest whatever was collected, even on a halt — partial progress
  // shouldn't be thrown away just because the run stopped early.
  let ingestResult = null;
  if (Object.keys(jobs).length > 0) {
    ingestResult = await relay({
      type: "SCOUT_INGEST",
      payload: { search_name: searchName, url: location.href, jobs },
    });
  }

  if (haltState) {
    await setRunState(haltState);
    report({ phase: "halted", accepted: ingestResult?.accepted ?? 0,
             runId: ingestResult?.run_id, state: haltState });
  } else {
    await clearRunState();
    report({ phase: "done", accepted: ingestResult?.accepted ?? 0, runId: ingestResult?.run_id });
  }
}

/**
 * Mark the storage slot "running" — the single source of truth a reopened
 * popup checks (see popup.js's init()) to know a harvest is active in some
 * tab and keep every Run button disabled, even though it has no live
 * progress history to show for it (harvest lines are broadcast-only, not
 * persisted — see report() below). Also resets abortRequested (a fresh
 * run/resume must never inherit a previous one's abort flag) and persists
 * ``tabId`` so the popup's Abort button can message this tab even after a
 * close/reopen — this content script has no chrome.tabs access of its own
 * to look its id up, so the harvest's initiator (popup.js or background.js,
 * both of which already know it synchronously) passes it in.
 */
function markRunning(searchName, tabId) {
  abortRequested = false;
  return setRunState({ status: "running", searchName, url: location.href, startedAt: Date.now(), tabId });
}

/**
 * Write the storage slot — the single source of truth Resume/popup-reopen
 * reads from, whether the popup stayed open (live SCOUT_HARVEST_PROGRESS
 * event) or was closed and reopened later (popup.js reads this on init).
 */
function setRunState(state) {
  return new Promise((resolve) => chrome.storage.local.set({ [STORAGE_KEY]: state }, resolve));
}

/**
 * Clear the storage slot — called at the start of every fresh (non-resume)
 * run, silently discarding any unresolved halted/running state per the
 * "always wipe on Run" design decision, and on any clean finish or
 * unexpected error since there's nothing left to resume (an error clearing
 * it, rather than leaving "running" stuck forever, matters — otherwise a
 * single crash would permanently disable every Run button until manually
 * cleared).
 */
function clearRunState() {
  return new Promise((resolve) => chrome.storage.local.remove(STORAGE_KEY, resolve));
}

/**
 * Wait for LinkedIn's results to finish rendering: resolve once the DOM has
 * gone quiet for RENDER_QUIET_MS, or reject after RENDER_TIMEOUT_MS. A fixed
 * sleep isn't safe here — LinkedIn renders results progressively and a flat
 * delay risks harvesting a partial list.
 */
function waitForRender() {
  return new Promise((resolve, reject) => {
    let quietTimer = null;
    const deadline = setTimeout(() => {
      observer.disconnect();
      reject(new Error("timed out waiting for results to render"));
    }, RENDER_TIMEOUT_MS);

    const settle = () => {
      clearTimeout(deadline);
      observer.disconnect();
      resolve();
    };

    const observer = new MutationObserver(() => {
      if (quietTimer) clearTimeout(quietTimer);
      quietTimer = setTimeout(settle, RENDER_QUIET_MS);
    });
    observer.observe(document.body, { childList: true, subtree: true });
    // Start the quiet timer immediately too, in case the page is already
    // fully rendered by the time the content script runs.
    quietTimer = setTimeout(settle, RENDER_QUIET_MS);
  });
}

/**
 * Pull every job id off the current page. LinkedIn uses two different
 * attributes depending on the URL variant; this handles both and
 * deduplicates. Ported verbatim from scrape_prompt.md's Step 1.
 */
function harvestJobIds() {
  return [...new Set([
    ...[...document.querySelectorAll('[componentkey^="job-card-component-ref-"]')]
      .map((el) => el.getAttribute("componentkey").replace("job-card-component-ref-", "")),
    ...[...document.querySelectorAll("[data-occludable-job-id]")]
      .map((el) => el.getAttribute("data-occludable-job-id")),
  ])];
}

/**
 * Find the nearest scrollable ancestor of ``el`` (an element whose overflow-y
 * allows scrolling and that actually has overflow content), falling back to
 * the document's own scrolling element. Deliberately selector-free — not
 * tied to any of LinkedIn's own CSS class names, which change often — so it
 * works whichever panel actually owns the results list's scroll.
 */
function findScrollableAncestor(el) {
  let node = el && el.parentElement;
  while (node && node !== document.body) {
    const style = getComputedStyle(node);
    if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight) {
      return node;
    }
    node = node.parentElement;
  }
  return document.scrollingElement || document.documentElement;
}

/**
 * Nudge whichever panel holds the results list to its current bottom (plus
 * the window itself, as a fallback) — in case a virtualized list only mounts
 * further cards once they're scrolled into view.
 */
function scrollResultsPanel() {
  const cards = document.querySelectorAll(
    '[componentkey^="job-card-component-ref-"], [data-occludable-job-id]'
  );
  const last = cards[cards.length - 1];
  const container = findScrollableAncestor(last);
  container.scrollTo({ top: container.scrollHeight });
  window.scrollTo({ top: document.body.scrollHeight });
}

/**
 * Harvest job ids, then keep re-harvesting (with a scroll nudge each round)
 * until the count holds steady for HARVEST_STABLE_ROUNDS consecutive checks
 * or HARVEST_MAX_MS elapses — see the HARVEST_* constants for why a single
 * quiet-period wait isn't sufficient on the virtualized DOM variant.
 */
async function harvestJobIdsUntilStable() {
  const deadline = Date.now() + HARVEST_MAX_MS;
  let ids = harvestJobIds();
  let stableStreak = 0;

  while (Date.now() < deadline && stableStreak < HARVEST_STABLE_ROUNDS && !abortRequested) {
    scrollResultsPanel();
    await sleep(HARVEST_POLL_MS);
    const next = harvestJobIds();
    stableStreak = next.length === ids.length ? stableStreak + 1 : 0;
    ids = next;
  }

  return ids;
}

/**
 * Fetch one job from the Voyager API and map it to the Scout job-record
 * shape. Ported from scrape_prompt.md's Step 2, with the fallback rules it
 * documents: a 404 means the job was removed between page-load and fetch
 * (permanent, not a run-level problem — skip it and move on); any other
 * failure is retried once before being left as an error entry; a block
 * signal (999/401/403) halts the whole run rather than being retried —
 * retrying into a block only escalates throttling into flagging.
 */
async function fetchJob(jobId, isRetry = false) {
  const csrfToken = document.cookie
    .split("; ")
    .find((c) => c.startsWith("JSESSIONID="))
    ?.split("=")[1]
    ?.replace(/"/g, "");

  let resp;
  try {
    resp = await fetch(
      `/voyager/api/jobs/jobPostings/${jobId}?decorationId=com.linkedin.voyager.deco.jobs.web.shared.WebFullJobPosting-65`,
      {
        headers: {
          "csrf-token": csrfToken,
          "x-restli-protocol-version": "2.0.0",
          accept: "application/vnd.linkedin.normalized+json+2.1",
        },
        credentials: "include",
      }
    );
  } catch (err) {
    if (isRetry) return { record: { error: String(err) } };
    return fetchJob(jobId, true);
  }

  if (resp.status === 999 || resp.status === 401 || resp.status === 403) {
    return { halted: true, status: resp.status };
  }
  if (resp.status === 404) {
    return { record: { error: "not_found (404)" } };
  }
  if (!resp.ok) {
    if (!isRetry) return fetchJob(jobId, true);
    return { record: { error: `not_found (${resp.status})` } };
  }

  const j = await resp.json();
  const d = j.data;
  const companyEntity = j.included?.find(
    (e) => e.$type === "com.linkedin.voyager.entities.shared.MiniCompany" || e.$type?.endsWith(".Company")
  );
  const applyInfo = j.included?.find((e) => e.$type?.endsWith("JobApplyingInfo"));
  const wpUrn = (d.workplaceTypes || [])[0] || "";
  const wp = WORKPLACE[wpUrn.split(":").pop()] || null;
  const location_ = d.formattedLocation ? (wp ? `${d.formattedLocation} (${wp})` : d.formattedLocation) : null;
  const easy = d.applyMethod?.easyApplyUrl || null;
  const companyApply = d.applyMethod?.companyApplyUrl || null;

  return {
    record: {
      title: d.title ?? null,
      company: companyEntity?.name ?? null,
      location: location_,
      applied: applyInfo?.applied ?? false,
      jobState: d.jobState ?? null,
      apply_platform: easy ? "easy_apply" : platformFor(companyApply),
      apply_url: companyApply || easy || null,
      salary_range: salaryFromText(d.description?.text),
      description_raw: d.description?.text ?? null,
    },
  };
}

function platformFor(url) {
  if (!url) return "other";
  if (/greenhouse\.io|grnh\.se/.test(url)) return "greenhouse";
  if (/ashbyhq\.com/.test(url)) return "ashby";
  if (/myworkdayjobs\.com/.test(url)) return "workday";
  return "other";
}

// Salary is NOT in the Voyager API — parse it from the description text.
function salaryFromText(t) {
  if (!t) return null;
  const m = t.match(
    /\$\s?[\d.,]+\s?[KkMm]?(?:\/\s?(?:yr|year|hour|hr))?\s*(?:-|–|—|to)\s*\$?\s?[\d.,]+\s?[KkMm]?(?:\/\s?(?:yr|year|hour|hr))?/
  );
  return m ? m[0].replace(/\s+/g, " ").trim() : null;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Relay a message to background.js (for backend calls) and unwrap its
 * response, throwing on error the same way background.js's own callers do.
 */
function relay(message) {
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
 * Broadcast a progress event. Live-only, by design — the popup listens for
 * these while open, but nothing here is persisted; a reopened popup
 * recovers aggregate state from the backend's own run state instead (see
 * app/main.py's _run["log"]), not a replayed harvest scrollback.
 */
function report(event) {
  chrome.runtime.sendMessage({ type: "SCOUT_HARVEST_PROGRESS", event });
}
