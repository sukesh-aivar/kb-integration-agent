// app.js — owns state and sequencing: view switching, the upload flow, the
// polling loop and the local tick. It calls the API module (mock or real) and
// the UI module; it never renders markup or fetches directly itself.

import * as ui from "./ui.js";

const cfg = window.APP_CONFIG || {};
const params = new URLSearchParams(window.location.search);
const MOCK = params.get("mock") === "1" || cfg.MOCK === true || !cfg.API_BASE_URL;
const STALE_AFTER = Number(cfg.STALE_AFTER) || 120;

// Dynamically choose the backend so the real api.js (which imports auth.js and
// touches window.crypto) is never evaluated in a pure mock review.
const api = MOCK ? await import("./mock.js") : await import("./api.js");
const auth = MOCK ? null : await import("./auth.js");

// ------------------------------------------------------------- app state ----
const state = {
  screen: "signin",
  // upload
  docFiles: [],      // [{name,size,file}]
  ticketFile: null,  // {name,size,file} | null
  // run
  runId: null,
  status: null,      // last status payload from the API
  // polling
  pollTimer: null,
  pollIntervalMs: 3000,
  pollStartedAt: 0,
  lastChangeAt: 0,   // wall time of last stage change (for the 10-min stall note)
  lastSignature: "", // to detect stage changes
  // local 1s tick (keeps elapsed + still-working moving between polls)
  tickTimer: null,
  localElapsedBase: 0, // elapsed_seconds from last poll
  lastPollWall: 0,
};

// ============================================================ bootstrapping =
init();

async function init() {
  probeLogo();
  wireChrome();
  wireSignIn();
  wireUpload();
  wireOutputsNav();
  wireProgressChrome();

  if (MOCK) {
    setupMockBar();
  } else {
    // real: finish a returning Cognito code exchange, then reflect auth state
    try { await auth.completeSignInIfReturning(); } catch (e) { console.warn(e); }
    reflectAuth();
  }

  // Resume from the URL hash if a run id is present (#run=<id>).
  const hashRun = runIdFromHash();
  if (hashRun) {
    state.runId = hashRun;
    ui.showScreen("progress");
    state.screen = "progress";
    startPolling();
  } else if (MOCK) {
    ui.showScreen("signin");
  } else if (auth.isSignedIn()) {
    goUpload();
  } else {
    ui.showScreen("signin");
  }
}

// ----------------------------------------------------------------- chrome ---
function probeLogo() {
  // Show the PNG if it loads; otherwise keep the text wordmark. No layout shift:
  // both occupy the same slot and only one is visible.
  const img = ui.el("logo-img");
  const word = ui.el("logo-wordmark");
  const test = new Image();
  test.onload = () => { img.hidden = false; word.hidden = true; };
  test.onerror = () => { img.hidden = true; word.hidden = false; };
  test.src = "assets/logo.png?probe=" + Date.now();
}

function wireChrome() {
  const so = ui.el("sign-out");
  if (so) so.addEventListener("click", () => {
    if (MOCK) { window.location.hash = ""; window.location.search = ""; location.reload(); }
    else auth.signOut();
  });
}

function reflectAuth() {
  if (MOCK) return;
  const uc = ui.el("user-controls");
  const email = auth.currentEmail();
  if (email) { uc.hidden = false; ui.el("user-email").textContent = email; }
  else uc.hidden = true;
}

// --------------------------------------------------------------- sign-in ----
function wireSignIn() {
  ui.el("btn-signin").addEventListener("click", () => {
    if (MOCK) goUpload();
    else auth.beginSignIn();
  });
  ui.el("btn-example").addEventListener("click", () => {
    // Pre-seeded completed run straight to outputs (reliable demo path).
    const id = MOCK ? api.seedExampleRun() : "example";
    openOutputs(id);
  });
}

function goUpload() {
  reflectAuth();
  state.screen = "upload";
  ui.showScreen("upload");
}

// --------------------------------------------------------- mock controls ----
function setupMockBar() {
  const bar = ui.el("mockbar");
  bar.hidden = false;
  const sc = ui.el("mock-scenario");
  const sp = ui.el("mock-speed");
  sc.value = api.getScenario();
  sp.value = String(api.getSpeed());
  sc.addEventListener("change", () => api.setScenario(sc.value));
  sp.addEventListener("change", () => api.setSpeed(sp.value));
}

// ===================================================== upload screen ========
function wireUpload() {
  if (wireUpload._done) return; wireUpload._done = true;
  // real wiring lives in upload.js-style handlers added in task 6 via app.
  import("./upload.js").then((m) => m.initUpload({ state, api, ui, cfg, MOCK, onStart: beginRun }));
}

// --------------------------------------------------- progress chrome -------
function wireProgressChrome() {
  // "Show all activity" toggle (item 14)
  const btn = ui.el("btn-show-all-activity");
  if (btn) btn.addEventListener("click", () => {
    showAllActivity = !showAllActivity;
    if (state.status) ui.renderActivity(state.status.events || [], showAllActivity);
  });

  // Sticky mini strip (item 12): reveal once the run header card scrolls out of
  // view. Uses IntersectionObserver on the run header, falling back to scroll.
  const strip = ui.el("mini-strip");
  const headerCard = document.querySelector("#screen-progress .card");
  if (strip && headerCard && "IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      const visible = entries[0].isIntersecting;
      const onProgress = !ui.el("screen-progress").hidden;
      strip.hidden = visible || !onProgress;
    }, { threshold: 0 });
    io.observe(headerCard);
  }
}

// ===================================================== run lifecycle ========
async function beginRun(runId) {
  state.runId = runId;
  window.location.hash = `run=${encodeURIComponent(runId)}`;
  state.screen = "progress";
  ui.showScreen("progress");
  // reset step groups to their initial (skip-aware) state comes from first poll
  startPolling(true);
}

function runIdFromHash() {
  const m = /(?:^|[#&])run=([^&]+)/.exec(window.location.hash || "");
  return m ? decodeURIComponent(m[1]) : null;
}

// ---------------------------------------------------------- polling loop ----
function startPolling(fresh = false) {
  stopPolling();
  state.pollStartedAt = Date.now();
  state.pollIntervalMs = 3000;
  state.lastChangeAt = Date.now();
  state.lastSignature = "";
  startLocalTick();
  pollOnce();
}

function stopPolling() {
  if (state.pollTimer) clearTimeout(state.pollTimer);
  state.pollTimer = null;
  if (state.tickTimer) clearInterval(state.tickTimer);
  state.tickTimer = null;
}

function scheduleNextPoll() {
  // 3s while running; back off to 10s after 2 minutes.
  const elapsed = Date.now() - state.pollStartedAt;
  state.pollIntervalMs = elapsed > 120_000 ? 10_000 : 3_000;
  state.pollTimer = setTimeout(pollOnce, state.pollIntervalMs);
}

async function pollOnce() {
  try {
    const s = await api.getStatus(state.runId);
    clearReconnectChip();
    state.status = s;
    state.localElapsedBase = s.elapsed_seconds || 0;
    state.lastPollWall = Date.now();

    const sig = s.stages.map((x) => `${x.id}:${x.state}`).join("|");
    if (sig !== state.lastSignature) { state.lastChangeAt = Date.now(); announceTransition(s); state.lastSignature = sig; }

    render(s, s.elapsed_seconds || 0);

    if (s.status === "complete" || s.status === "failed") {
      stopPolling();
      renderTerminal(s);
      return;
    }
    // stalled backend: >10 min with no change -> show warn, keep polling (not failed)
    if (Date.now() - state.lastChangeAt > 10 * 60_000) showStalledNote();

    scheduleNextPoll();
  } catch (err) {
    handlePollError(err);
  }
}

function handlePollError(err) {
  if (err.status === 401 && !MOCK) {
    // session expired mid-run: prompt re-sign-in WITHOUT losing the run id.
    showReconnectChip("Session expired — sign in again");
    // keep the hash; redirect to Cognito, we return to the same progress screen
    setTimeout(() => auth.beginSignIn(), 400);
    return;
  }
  if (err.status === 404) {
    renderStartError("That run was not found, or is not yours.");
    stopPolling();
    return;
  }
  // transient (network / 5xx): keep last known state, show quiet chip, retry.
  showReconnectChip("Reconnecting…");
  scheduleNextPoll();
}

// -------------------------------------------------- local 1s liveness tick --
function startLocalTick() {
  if (state.tickTimer) clearInterval(state.tickTimer);
  state.tickTimer = setInterval(() => {
    if (!state.status || state.status.status === "complete" || state.status.status === "failed") return;
    // advance elapsed locally between polls so the counter never freezes
    const extra = Math.floor((Date.now() - state.lastPollWall) / 1000);
    const now = state.localElapsedBase + Math.max(0, extra);
    ui.renderElapsed(now);
    ui.renderStillWorking(state.status, state.status.stages, now, STALE_AFTER);
    ui.renderStepGroups(state.status.stages, now); // live per-step count-up
    ui.renderMiniStrip(state.status, state.status.stages, now);
  }, 1000);
}

// --------------------------------------------------------------- rendering --
let showAllActivity = false;
function render(s, nowSeconds) {
  ui.renderRunHeader(s, s.stages);
  ui.renderElapsed(nowSeconds);
  ui.renderStepGroups(s.stages, nowSeconds);
  ui.renderNowPanel(s, s.stages, nowSeconds, handlers());
  ui.renderStillWorking(s, s.stages, nowSeconds, STALE_AFTER);
  ui.renderActivity(s.events || [], showAllActivity);
  ui.renderMiniStrip(s, s.stages, nowSeconds);
}

function renderTerminal(s) {
  render(s, s.elapsed_seconds || 0);
  ui.renderNowPanel(s, s.stages, s.elapsed_seconds || 0, handlers());
  if (s.status === "complete") ui.announce("Run complete. View outputs is available.");
  else ui.announce("Run failed. A resume is offered.");
}

function handlers() {
  return {
    viewOutputs: () => openOutputs(state.runId),
    resume: async () => {
      await api.resumeRun(state.runId);
      pushActivityNote();
      startPolling();
    },
    another: () => { window.location.hash = ""; state.screen = "upload"; resetUpload(); ui.showScreen("upload"); },
  };
}

function announceTransition(s) {
  const running = s.stages.find((x) => x.state === "running");
  if (running) {
    const def = ui.STEP_BY_ID[running.id];
    if (def) ui.announce(`Step started: ${def.label}`);
  }
}

function pushActivityNote() { /* the backend/mock logs "Run resumed…"; nothing to do here */ }

// ---------------------------------------------------- stalled / reconnect ---
function showReconnectChip(text) {
  let chip = ui.el("reconnect-chip");
  if (!chip) {
    chip = document.createElement("div");
    chip.id = "reconnect-chip";
    chip.className = "callout warn reconnect";
    chip.innerHTML = `<svg class="ico" width="16" height="16" aria-hidden="true"><use href="#i-clock"/></svg><span></span>`;
    const header = document.querySelector("#screen-progress .wrap");
    if (header) header.prepend(chip);
  }
  chip.querySelector("span").textContent = text;
}
function clearReconnectChip() { const c = ui.el("reconnect-chip"); if (c) c.remove(); }
function showStalledNote() {
  const box = ui.el("still-working");
  box.hidden = false;
  ui.el("still-working-text").innerHTML =
    `<strong>Still working.</strong> No step has changed for a while. A long Bedrock Data Automation ` +
    `job is slow, not broken — the run is still being polled. Nothing has failed.`;
}

function renderStartError(message) {
  ui.showScreen("progress");
  const panel = ui.el("now-panel");
  panel.className = "nowpanel danger";
  ui.el("now-stepno").textContent = "Problem";
  ui.el("now-label").textContent = "Couldn’t reach the run";
  ui.el("now-desc").textContent = message;
  ui.el("now-insight").hidden = true;
  ui.el("now-next").textContent = "";
  ui.el("now-actions").innerHTML = `<button class="btn btn-primary" id="btn-try-again">Try again</button>`;
  ui.el("btn-try-again").addEventListener("click", () => startPolling());
}

// ===================================================== outputs screen =======
async function openOutputs(runId) {
  state.runId = runId;
  window.location.hash = `run=${encodeURIComponent(runId)}`;
  stopPolling();
  ui.showScreen("outputs");
  state.screen = "outputs";
  try {
    const [outputs, vectors, tickets] = await Promise.all([
      api.getOutputs(runId), api.getVectors(runId), api.getTicketOutputs(runId),
    ]);
    ui.renderOutputs(outputs, { runId });
    ui.renderVectors(vectors);
    ui.renderTicketOutputs(tickets);
    wireOutputsInteractions(outputs);
  } catch (err) {
    console.error(err);
  }
}

function wireOutputsNav() {
  ui.el("btn-back-progress").addEventListener("click", () => {
    if (!state.runId) return;
    window.location.hash = `run=${encodeURIComponent(state.runId)}`;
    ui.showScreen("progress");
    startPolling();
  });
  ui.el("btn-another").addEventListener("click", () => {
    window.location.hash = ""; resetUpload(); ui.showScreen("upload"); state.screen = "upload";
  });
}

// Preview + copy buttons on the S3 card (download disabled in mock).
function wireOutputsInteractions(outputs) {
  const s3 = ui.el("out-s3");
  s3.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(btn.dataset.copy); flash(btn); } catch {}
    });
  });
  s3.querySelectorAll("[data-preview]").forEach((btn) => {
    btn.addEventListener("click", () => togglePreview(btn, outputs));
  });
}

function togglePreview(btn, outputs) {
  const row = btn.closest(".s3row");
  let pre = row.nextElementSibling;
  if (pre && pre.classList.contains("preview-pre")) {
    pre.remove(); btn.textContent = "Preview"; return;
  }
  const key = btn.dataset.preview;
  const obj = (outputs.objects || []).find((o) => o.key === key);
  const sample = obj?.preview || samplePreview(key);
  pre = document.createElement("div");
  pre.className = "pre preview-pre";
  pre.style.marginTop = "6px";
  pre.textContent = sample;
  row.after(pre);
  btn.textContent = "Hide preview";
}

function samplePreview(key) {
  if (key.endsWith("report.md")) return "# Run report\n\nStatus: needs_review\nDocuments processed: 4\nParsers: 2 default · 2 Bedrock Data Automation\n…";
  if (key.endsWith("manifest.json")) return '{\n  "run_id": "…",\n  "stages": [ … ],\n  "library_versions": { "boto3": "1.43.114" }\n}';
  return "(preview of this object)";
}

function flash(btn) { const old = btn.getAttribute("aria-label"); btn.setAttribute("aria-label", "Copied"); setTimeout(() => btn.setAttribute("aria-label", old), 1200); }

// ----------------------------------------------------------------- resets ---
function resetUpload() {
  state.docFiles = []; state.ticketFile = null; state.runId = null; state.status = null;
  import("./upload.js").then((m) => m.resetUpload && m.resetUpload());
}
