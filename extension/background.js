// Scout extension — background service worker.
//
// Thin relay only: talks to the local Scout backend and manages tabs for
// saved-search runs. No scraping or long-running work lives here — MV3
// service workers are killed after ~30s idle, and a multi-minute jittered
// Voyager loop needs a tab-lifetime context (that's content.js, added in a
// later slice). Content-script fetch() to localhost from the linkedin.com
// origin is cross-origin and subject to CORS; the extension's own origin
// with host_permissions is not, which is why every localhost call is
// relayed through here rather than issued directly from the content script.

const SCOUT_BASE_URL = "http://127.0.0.1:8000";

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  handleMessage(message)
    .then(sendResponse)
    .catch((err) => sendResponse({ error: err.message || String(err) }));
  return true; // keep the message channel open for the async sendResponse
});

/**
 * Dispatch one runtime message to its handler and return its result/error.
 */
async function handleMessage(message) {
  switch (message && message.type) {
    case "SCOUT_GET_SEARCHES":
      return scoutFetch("/api/extension/searches");
    case "SCOUT_DEDUPE":
      return scoutFetch("/api/extension/dedupe", {
        method: "POST",
        body: { job_ids: message.jobIds },
      });
    case "SCOUT_INGEST":
      return scoutFetch("/api/extension/ingest", {
        method: "POST",
        body: message.payload,
      });
    case "SCOUT_GET_STATUS":
      return scoutFetch("/api/extension/status");
    case "SCOUT_OPEN_TAB":
      return openTab(message.url);
    case "SCOUT_RUN_SAVED_SEARCH":
      return runTabHarvest(message.url, {
        type: "SCOUT_HARVEST",
        searchName: message.searchName,
      });
    case "SCOUT_RUN_SINGLE_JOB":
      return runTabHarvest(message.url, {
        type: "SCOUT_HARVEST_SINGLE_JOB",
        jobId: message.jobId,
        searchName: message.searchName,
      });
    case "SCOUT_RESUME_RUN":
      // Always opens a fresh tab rather than trying to reuse whichever tab
      // originally halted — that tab may have been closed since, and
      // chrome.storage.local's persisted state (url + pendingIds) is enough
      // to resume from scratch regardless.
      return runTabHarvest(message.url, {
        type: "SCOUT_RESUME_HARVEST",
        pendingIds: message.pendingIds,
        searchName: message.searchName,
        completedCount: message.completedCount,
        totalCount: message.totalCount,
      });
    case "SCOUT_HARVEST_PROGRESS":
      // Broadcast from content.js for the popup to render live; background
      // has no state of its own to update. Nothing to do here — the
      // sendResponse this triggers is harmless and unread by the sender.
      return null;
    default:
      throw new Error(`Unknown message type: ${message && message.type}`);
  }
}

/**
 * Fetch a Scout backend endpoint and return its parsed JSON body.
 *
 * Throws with the backend's own error message when the response isn't ok
 * (or a generic status-code message if the body isn't JSON), so callers see
 * a real reason rather than a bare network failure.
 */
async function scoutFetch(path, { method = "GET", body } = {}) {
  const resp = await fetch(`${SCOUT_BASE_URL}${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    throw new Error((data && data.error) || `Scout server returned ${resp.status}`);
  }
  return data;
}

/**
 * Open a LinkedIn URL in a new background tab (used for saved-search runs).
 */
function openTab(url) {
  return new Promise((resolve) => {
    chrome.tabs.create({ url, active: false }, (tab) => resolve({ tabId: tab.id }));
  });
}

/**
 * Open ``url`` in a background tab, wait for it to finish loading, then hand
 * off to content.js with ``harvestMessage``. This is the saved-search /
 * custom-search path: the extension navigates the tab itself instead of the
 * user already being on the page (Pass 1's manual-page path), but from here
 * on it's the exact same content-script harvest — see content.js.
 */
async function runTabHarvest(url, harvestMessage) {
  const tab = await new Promise((resolve) => chrome.tabs.create({ url, active: false }, resolve));
  await waitForTabComplete(tab.id);
  return sendToTab(tab.id, harvestMessage);
}

const LINKEDIN_JOBS_URL = /^https:\/\/www\.linkedin\.com\/jobs\//;
const TAB_LOAD_TIMEOUT_MS = 20000;

/**
 * Resolve once ``tabId`` finishes loading a URL content.js is actually
 * injected into (https://www.linkedin.com/jobs/*). Some saved/pasted URLs
 * redirect through an intermediate hop (e.g. a /comm/jobs/... email-link
 * wrapper) before landing there — waiting for the *first* "complete" event
 * would fire while the tab is still on that intermediate, non-matching page,
 * which has no content script to receive the harvest message. Rejects after
 * TAB_LOAD_TIMEOUT_MS so a stuck navigation (e.g. LinkedIn bouncing to a
 * login page) fails loudly instead of hanging.
 */
function waitForTabComplete(tabId) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("Tab didn't finish loading a LinkedIn jobs page in time"));
    }, TAB_LOAD_TIMEOUT_MS);

    function listener(updatedTabId, changeInfo, tab) {
      if (updatedTabId === tabId && changeInfo.status === "complete"
          && tab.url && LINKEDIN_JOBS_URL.test(tab.url)) {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

/**
 * Send a message to a specific tab's content script and resolve with its
 * response (or reject if nothing was there to receive it).
 */
function sendToTab(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, (resp) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      resolve(resp);
    });
  });
}
