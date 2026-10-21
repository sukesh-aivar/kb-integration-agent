// ui.js — rendering helpers ONLY. This module renders and NEVER fetches.
// (Separation per spec 7: ui renders, api fetches, app owns state/sequencing.
// A render function that calls the network is what makes the page untestable.)

// ------------------------------------------------------ step definitions ----
// The 13 stages (spec section 4). `id` matches the backend stage name; `label`
// is reader-facing; `description` is the sub-line and the "right now" sentence;
// `insight` shows in the "right now" panel only for steps that have one.
// `durHint` is a rough typical range in seconds [low, high], based on observed
// run times, shown as a labelled estimate in the "right now" panel (item 13).
export const STEPS = [
  { id: "load_tickets",   group: "Tickets", label: "Reading the ticket export",
    description: "Loads the CSV, JSON or Excel file.", durHint: [1, 5] },
  { id: "adapt_tickets",  group: "Tickets", label: "Mapping ticket fields",
    description: "Maps each platform’s columns to one common shape and drops unusable rows.", durHint: [1, 5] },
  { id: "clean_tickets",  group: "Tickets", label: "Cleaning and de-duplicating",
    description: "Removes signatures, quoted replies and duplicate tickets.", durHint: [1, 5] },
  { id: "embed_texts",    group: "Tickets", label: "Embedding ticket text",
    description: "Turns each customer’s problem statement into a vector.",
    insight: "Only the subject and description are embedded, never the resolution or any label.", durHint: [60, 300] },
  { id: "intent_cluster", group: "Tickets", label: "Discovering intents",
    description: "Groups similar tickets to find what customers keep asking about.", durHint: [5, 15] },
  { id: "name_clusters",  group: "Tickets", label: "Naming intents",
    description: "A language model names each group from a sample of its tickets.",
    insight: "One model call per group, not one per ticket.", durHint: [20, 60] },
  { id: "normalize_docs", group: "Documents", label: "Reading and parsing documents",
    description: "Picks a parser for each file and extracts clean text.",
    insight: "Files with no extractable text, such as scans, go to Bedrock Data Automation. Plain text and clean documents use the default parser, which costs nothing extra.", durHint: [45, 180] },
  { id: "kb_create",      group: "Documents", label: "Creating the knowledge base",
    description: "Creates a knowledge base for this run and waits until it is ready.", durHint: [5, 20] },
  { id: "kb_ingest",      group: "Documents", label: "Indexing content",
    description: "Splits text into chunks, embeds them and writes the vectors to S3 Vectors.",
    insight: "Each chunk becomes a vector stored with its source file and metadata, so it can be found by meaning rather than by keyword.", durHint: [15, 90] },
  { id: "kb_retrieve",    group: "Documents", label: "Checking retrieval",
    description: "Asks test questions to confirm the right passages come back.", durHint: [2, 10] },
  { id: "coverage_check", group: "Joining the results", label: "Checking coverage",
    description: "Asks the knowledge base about frequent intents and judges whether it can answer them.", durHint: [10, 40] },
  { id: "draft_article",  group: "Joining the results", label: "Drafting articles",
    description: "Drafts articles for gaps, citing the tickets they came from.",
    insight: "Every drafted article cites the tickets it was built from, so a person can check it.", durHint: [10, 60] },
  { id: "write_report",   group: "Joining the results", label: "Writing the report",
    description: "Assembles the report and the run manifest.", durHint: [1, 5] },
];

export const STEP_BY_ID = Object.fromEntries(STEPS.map((s) => [s.id, s]));

// ------------------------------------------------------------- utilities ----
export function $(sel, root = document) { return root.querySelector(sel); }
export function el(id) { return document.getElementById(id); }

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

export function mmss(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds || 0));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

export function humanSize(bytes) {
  if (bytes == null) return "—";
  const mb = bytes / (1024 * 1024);
  if (mb >= 1) return `${mb.toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

export function icon(name, size = 20) {
  return `<svg class="ico" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

// ------------------------------------------------------- screen switching ---
const SCREENS = ["signin", "upload", "progress", "outputs"];
const INDICATOR_STEP = { upload: "upload", progress: "progress", outputs: "outputs" };

let _firstScreen = true;
export function showScreen(name) {
  for (const s of SCREENS) {
    const node = el(`screen-${s}`);
    if (node) node.hidden = s !== name;
  }
  // chrome that depends on screen
  el("step-indicator").hidden = name === "signin";
  updateStepIndicator(INDICATOR_STEP[name] || null);
  // Move focus to the screen heading for screen-reader context on a VIEW CHANGE
  // only — not on the initial load, where a focus ring around the sign-in
  // headline looks like a stray box. The outline is suppressed for these
  // programmatically-focused non-interactive headings via .no-outline (CSS).
  if (!_firstScreen) {
    const h = $(`#screen-${name} h1`);
    if (h) { h.setAttribute("tabindex", "-1"); h.classList.add("no-outline"); h.focus({ preventScroll: false }); }
  }
  _firstScreen = false;
  window.scrollTo({ top: 0, behavior: "auto" });
}

const INDICATOR_ORDER = ["upload", "progress", "outputs"];
function updateStepIndicator(current) {
  const curIdx = INDICATOR_ORDER.indexOf(current);
  document.querySelectorAll(".stepindicator li[data-step]").forEach((li) => {
    const idx = INDICATOR_ORDER.indexOf(li.dataset.step);
    const num = li.querySelector(".num");
    li.classList.remove("done");
    if (li.dataset.step === current) {
      li.setAttribute("aria-current", "step");
      if (num) num.textContent = String(idx + 1);
    } else {
      li.removeAttribute("aria-current");
      // steps before the current one are complete -> show a check (item 22)
      if (curIdx > -1 && idx > -1 && idx < curIdx) {
        li.classList.add("done");
        if (num) num.innerHTML = `<svg class="ico" width="14" height="14" aria-hidden="true"><use href="#i-check"/></svg>`;
      } else if (num) {
        num.textContent = String(idx + 1);
      }
    }
  });
}

export function announce(text) {
  const r = el("live-region");
  if (r) { r.textContent = ""; requestAnimationFrame(() => (r.textContent = text)); }
}

// --------------------------------------------------------- status glyphs ----
const STATE_WORD = { pending: "Waiting", running: "In progress", done: "Done",
                     failed: "Failed", skipped: "Skipped" };

function dot(state) {
  const cls = { pending: "waiting", running: "running", done: "done",
                failed: "failed", skipped: "skipped" }[state] || "waiting";
  let inner = "";
  if (state === "done") inner = icon("check", 14);
  else if (state === "failed") inner = icon("cross", 14);
  else if (state === "skipped") inner = icon("minus", 14);
  return `<span class="dot ${cls}" aria-hidden="true">${inner}</span>`;
}

// ---------------------------------------------- progress: the step rows -----
// stage: {id, state, started_at, ended_at, skip_reason}; nowSeconds: run clock.
export function renderStepGroups(stages, nowSeconds) {
  const byGroup = { Documents: [], Tickets: [], "Joining the results": [] };
  const skippedByGroup = { Documents: [], Tickets: [], "Joining the results": [] };
  for (const st of stages) {
    const def = STEP_BY_ID[st.id];
    if (!def) continue;
    if (st.state === "skipped") { skippedByGroup[def.group]?.push({ st, def }); continue; }
    byGroup[def.group]?.push(stepRow(st, def, nowSeconds));
  }
  // Collapse skipped steps into ONE expandable line per group (item 11), so a
  // documents-only run does not show six identical "No ticket export" rows.
  for (const group of Object.keys(byGroup)) {
    const ul = el(`group-${group}`);
    if (!ul) continue;
    let html = byGroup[group].join("");
    const skipped = skippedByGroup[group];
    if (skipped.length) html += collapsedSkips(skipped);
    ul.innerHTML = html;
  }
}

function collapsedSkips(items) {
  // Group by reason so the summary reads naturally.
  const reason = items[0].st.skip_reason || "Skipped";
  const n = items.length;
  const rows = items.map(({ st, def }) => `
    <li class="steprow skipped">
      ${dot("skipped")}
      <div><div class="lab">${esc(def.label)}<span class="sr-only"> — Skipped</span></div>
      <div class="sub">${esc(st.skip_reason || reason)}</div></div>
      <div class="time"></div>
    </li>`).join("");
  return `
    <details class="skip-collapse">
      <summary>${dot("skipped")} <span>${n} step${n === 1 ? "" : "s"} skipped: ${esc(reason.toLowerCase())}</span>
        <svg class="ico chev" width="16" height="16" aria-hidden="true"><use href="#i-chevron"/></svg></summary>
      <ul class="steplist" style="margin:0">${rows}</ul>
    </details>`;
}

function stepRow(st, def, nowSeconds) {
  const subline = st.state === "skipped" && st.skip_reason
    ? esc(st.skip_reason) : esc(def.description);

  let time = "";
  if (st.state === "done" && st.started_at != null && st.ended_at != null) {
    time = mmss(st.ended_at - st.started_at);
  } else if (st.state === "running" && st.started_at != null) {
    time = mmss(nowSeconds - st.started_at);
  } else if (st.state === "failed" && st.started_at != null) {
    time = `after ${mmss((st.ended_at ?? nowSeconds) - st.started_at)}`;
  }

  const shimmer = st.state === "running" ? `<div class="shimmer" aria-hidden="true"></div>` : "";

  return `
    <li class="steprow ${st.state === "running" ? "running" : ""} ${st.state === "skipped" ? "skipped" : ""}">
      ${dot(st.state)}
      <div>
        <div class="lab">${esc(def.label)}<span class="sr-only"> — ${STATE_WORD[st.state] || "Waiting"}</span></div>
        <div class="sub">${subline}</div>
        ${shimmer}
      </div>
      <div class="time tnum">${time}</div>
    </li>`;
}

// ------------------------------------------- progress: the "right now" panel -
export function renderNowPanel(state, stages, nowSeconds, handlers) {
  const panel = el("now-panel");
  const actions = el("now-actions");
  const tech = el("now-techdetail");
  panel.className = "nowpanel";
  actions.innerHTML = "";
  tech.hidden = true;

  const active = stages.filter((s) => s.state !== "skipped");
  const doneCount = stages.filter((s) => s.state === "done").length;
  const total = active.length;

  el("now-eta").hidden = true;
  if (state.status === "complete") {
    panel.classList.add("ok");
    el("now-stepno").textContent = "Complete";
    el("now-label").textContent = `Run complete in ${mmss(state.elapsed_seconds)}`;
    el("now-desc").textContent = "Everything the run produced has landed in S3 and S3 Vectors.";
    el("now-insight").hidden = true;
    el("now-next").textContent = "";
    actions.innerHTML = `<button class="btn btn-primary" id="btn-view-outputs">View outputs</button>`;
    el("btn-view-outputs").addEventListener("click", handlers.viewOutputs);
    return;
  }

  if (state.status === "failed") {
    panel.classList.add("danger");
    const failed = stages.find((s) => s.state === "failed");
    const def = failed ? STEP_BY_ID[failed.id] : null;
    el("now-stepno").textContent = "Failed";
    el("now-label").textContent = def ? `Couldn’t finish “${def.label}”` : "Run failed";
    el("now-desc").textContent =
      "Earlier steps finished and are kept, so a resume picks up from this step.";
    el("now-insight").hidden = true;
    el("now-next").textContent = "";
    if (state.error) {
      tech.hidden = false;
      el("now-error").textContent = state.error;
    }
    actions.innerHTML =
      `<button class="btn btn-primary" id="btn-resume">Resume run</button>
       <button class="btn btn-secondary" id="btn-another2">Start another run</button>`;
    el("btn-resume").addEventListener("click", handlers.resume);
    el("btn-another2").addEventListener("click", handlers.another);
    return;
  }

  // running (or queued)
  const current = stages.find((s) => s.state === "running");
  if (!current) {
    el("now-stepno").textContent = "Getting ready";
    el("now-label").textContent = "Getting ready";
    el("now-desc").textContent = "The run is being prepared.";
    el("now-insight").hidden = true;
    el("now-next").textContent = "";
    return;
  }
  const def = STEP_BY_ID[current.id];
  // Number among ACTIVE (non-skipped) steps so the panel agrees with the header
  // ("Step 1 of 5", not "Step 7 of 13") on a documents-only run.
  const activeOrder = stages.filter((s) => s.state !== "skipped").map((s) => s.id);
  const activeIdx = activeOrder.indexOf(current.id) + 1;
  el("now-stepno").textContent = `Step ${activeIdx} of ${activeOrder.length}`;
  el("now-label").textContent = def.label;
  el("now-desc").textContent = def.description;
  if (def.insight) {
    el("now-insight").hidden = false;
    el("now-insight-text").textContent = def.insight;
  } else {
    el("now-insight").hidden = true;
  }
  // typical duration estimate (item 13), clearly labelled
  const eta = el("now-eta");
  if (def.durHint) {
    eta.hidden = false;
    eta.textContent = `Usually ${def.durHint[0]}–${def.durHint[1]} seconds (estimate)`;
  } else {
    eta.hidden = true;
  }
  // next non-skipped step
  const order = STEPS.map((s) => s.id);
  const stageById = Object.fromEntries(stages.map((s) => [s.id, s]));
  let nextLabel = "";
  for (let i = order.indexOf(current.id) + 1; i < order.length; i++) {
    const s = stageById[order[i]];
    if (s && s.state !== "skipped") { nextLabel = STEP_BY_ID[order[i]].label; break; }
  }
  el("now-next").textContent = nextLabel ? `Next: ${nextLabel}` : "";
  return { doneCount, total };
}

// --------------------------------------------- progress: header + bar -------
export function renderRunHeader(state, stages) {
  el("run-id").textContent = state.run_id;
  const chip = el("run-status");
  const map = { running: ["info", "Running"], queued: ["info", "Running"],
                complete: ["ok", "Complete"], failed: ["danger", "Failed"] };
  const [cls, label] = map[state.status] || ["info", "Running"];
  chip.className = `chip ${cls}`;
  chip.textContent = label;

  const active = stages.filter((s) => s.state !== "skipped");
  const done = stages.filter((s) => s.state === "done").length;
  const total = active.length || 1;
  const pct = Math.round((done / total) * 100);

  const bar = el("overall-fill");
  bar.style.width = `${pct}%`;
  bar.classList.toggle("running", state.status === "running" || state.status === "queued");
  bar.classList.toggle("ok", state.status === "complete");
  bar.classList.toggle("danger", state.status === "failed");

  const pb = bar.parentElement;
  pb.setAttribute("aria-valuenow", String(pct));

  el("steps-caption").textContent = `${done} of ${active.length} steps complete`;
}

export function renderElapsed(seconds) { el("elapsed").textContent = mmss(seconds); }

// --------------------------------------------- progress: still-working ------
export function renderStillWorking(state, stages, nowSeconds, staleAfter) {
  const box = el("still-working");
  const current = stages.find((s) => s.state === "running");
  if (state.status !== "running" || !current || current.started_at == null) {
    box.hidden = true; return;
  }
  const running = nowSeconds - current.started_at;
  if (running < staleAfter) { box.hidden = true; return; }
  const def = STEP_BY_ID[current.id];
  box.hidden = false;
  el("still-working-text").innerHTML =
    `<strong>Still working.</strong> “${esc(def.label)}” has been running for ` +
    `${mmss(running)}. Large scanned documents are slow to parse, so this is expected. ` +
    `Nothing has failed.`;
}

// --------------------------------------------- progress: activity feed ------
// showAll=false shows the 8 most recent; true shows the full log (item 14).
export function renderActivity(events, showAll = false) {
  const ul = el("activity");
  const list = showAll ? events.slice() : events.slice(-8);
  const rows = list.reverse(); // newest first
  ul.innerHTML = rows.map((e) =>
    `<li><span class="t">${mmss(e.t)}</span><span class="msg">${esc(e.text)}</span></li>`
  ).join("");
  // toggle the "Show all activity" button only when there is more to show
  const btn = el("btn-show-all-activity");
  if (btn) {
    btn.hidden = events.length <= 8;
    btn.textContent = showAll ? "Show recent only" : "Show all activity";
  }
}

// Update the sticky mini status strip (item 12).
export function renderMiniStrip(state, stages, nowSeconds) {
  const strip = el("mini-strip");
  if (!strip) return;
  const current = stages.find((s) => s.state === "running");
  const active = stages.filter((s) => s.state !== "skipped");
  const done = stages.filter((s) => s.state === "done").length;
  const pct = Math.round((done / (active.length || 1)) * 100);
  let label = "Getting ready";
  if (state.status === "complete") label = "Complete";
  else if (state.status === "failed") label = "Failed";
  else if (current) label = STEP_BY_ID[current.id]?.label || "Working";
  el("mini-step").textContent = label;
  el("mini-elapsed").textContent = mmss(nowSeconds);
  const fill = el("mini-fill");
  fill.style.width = `${pct}%`;
  fill.classList.toggle("running", state.status === "running" || state.status === "queued");
  fill.classList.toggle("ok", state.status === "complete");
  fill.classList.toggle("danger", state.status === "failed");
}

// ====================================================== OUTPUTS rendering ====
export function renderOutputs(data, opts) {
  el("out-runid").textContent = data.summary?.run_id || opts.runId || "";
  const sc = el("out-status");
  sc.className = `chip ${data.summary?.status === "failed" ? "danger" : "ok"}`;
  sc.textContent = data.summary?.status === "failed" ? "Failed" : "Complete";
  el("out-time").textContent = data.summary?.total_time || "—";

  renderSummary(data.summary || {});
  renderRouting(data.routing || []);
  renderS3(data.objects || []);
}

function renderSummary(s) {
  const dl = el("out-summary");
  const rows = [
    ["Documents processed", s.documents_processed ?? "—"],
    ["Parsers used", s.parser_split || "—"],
    ["Ticket export", s.ticket_export_included ? "Included" : "Not included"],
    ["Steps completed", s.steps_completed ?? "—"],
    ["Steps skipped", s.steps_skipped ?? "—"],
    ["Total time", s.total_time || "—"],
    ["Knowledge base id", `<span class="mono">${esc(s.kb_id || "—")}</span>`],
  ];
  dl.innerHTML = rows.map(([t, v]) => `<dt>${esc(t)}</dt><dd>${v}</dd>`).join("");
}

function routeChip(route) {
  // BDA is info-blue (a deliberate routing choice), not a warning. The hint
  // reminds the reader why it is used sparingly (item 21).
  if (route === "BDA") return `<span class="chip info" title="Billed per page">Bedrock Data Automation</span> <span class="sub" style="font-size:12px;color:var(--muted)">billed per page</span>`;
  return `<span class="chip ok">Default parser</span>`;
}

function renderRouting(rows) {
  el("out-routing").innerHTML = rows.map((r) => `
    <tr>
      <td class="mono">${esc(r.file)}</td>
      <td>${esc(r.size)}</td>
      <td>${esc(r.pages ?? "—")}</td>
      <td>${routeChip(r.route)}</td>
      <td class="mono">${esc(r.rule_id)}</td>
      <td>${esc(r.reason)}</td>
    </tr>`).join("");
}

// Group S3 objects by prefix group; collapse the stage markers into a real
// expandable disclosure with per-marker timestamps (item 18). The count comes
// from the API's own object list, so it matches the steps that actually ran.
function renderS3(objects) {
  const groups = { "uploads": [], "runs": [], "drafts": [] };
  const markerRe = /_markers\/.+\.done$/;
  const markers = [];
  for (const o of objects) {
    if (markerRe.test(o.key)) { markers.push(o); continue; }
    const g = o.prefix_group || (o.key.split("/")[0]);
    (groups[g] || (groups[g] = [])).push(o);
  }
  const container = el("out-s3");
  container.innerHTML = "";
  for (const [g, items] of Object.entries(groups)) {
    if (!items.length && !(g === "runs" && markers.length)) continue;
    const wrap = document.createElement("div");
    wrap.className = "s3group";
    wrap.innerHTML = `<h4>${esc(g)}/</h4>` + items.map(s3row).join("");
    if (g === "runs" && markers.length) {
      const markerRows = markers.map((m) => `
        <div class="s3row marker-row">
          <span class="uri">${esc(m.uri)}</span>
          <span class="sub">${esc(m.last_modified)}</span>
        </div>`).join("");
      wrap.insertAdjacentHTML("beforeend", `
        <details class="marker-disclosure">
          <summary>${icon("chevron", 16)} <span>${markers.length} stage marker${markers.length === 1 ? "" : "s"} (one per completed step)</span></summary>
          <div class="marker-list">${markerRows}</div>
        </details>`);
    }
    container.appendChild(wrap);
  }
}

function s3row(o) {
  const keyChip = /(?:report\.md|manifest\.json)$/.test(o.key)
    ? `<span class="chip info">Key output</span>` : "";
  const preview = o.previewable
    ? `<button class="btn btn-ghost btn-small" data-preview="${esc(o.key)}">Preview</button>` : "";
  return `
    <div class="s3row" data-key="${esc(o.key)}">
      <span class="uri">${esc(o.uri)}</span> ${keyChip}
      <span class="sub">${esc(o.size)} · ${esc(o.last_modified)}</span>
      <span class="acts">
        ${preview}
        <button class="iconbtn" data-copy="${esc(o.uri)}" aria-label="Copy S3 URI for ${esc(o.key)}">${icon("copy", 18)}</button>
        <button class="btn btn-ghost btn-small" disabled title="Downloads are disabled in mock mode">Download</button>
      </span>
    </div>`;
}

// vectors card
export function renderVectors(v) {
  const dl = el("out-vec-index");
  const i = v.index || {};
  const rows = [
    ["Vector bucket", `<span class="mono">${esc(i.bucket || "—")}</span>`],
    ["Index name", `<span class="mono">${esc(i.name || "—")}</span>`],
    ["Dimension", esc(i.dimension ?? "—")],
    ["Distance metric", esc(i.metric || "—")],
  ];
  // Surface the embedding model and chunking strategy when the manifest has them
  // (item 20), so the vectors card explains HOW the vectors were produced.
  if (v.embedding_model) rows.push(["Embedding model", `<span class="mono">${esc(v.embedding_model)}</span>`]);
  if (v.chunking_strategy) rows.push(["Chunking strategy", esc(v.chunking_strategy)]);
  dl.innerHTML = rows.map(([t, val]) => `<dt>${esc(t)}</dt><dd>${val}</dd>`).join("");

  const ing = v.ingestion || {};
  const counts = [
    `<span class="chip">Documents scanned · ${esc(ing.scanned ?? 0)}</span>`,
    `<span class="chip ok">Indexed · ${esc(ing.indexed ?? 0)}</span>`,
  ];
  // "Chunks written" chip (item 20) — the headline figure that was previously
  // only visible in the activity feed.
  if (ing.chunks_written != null)
    counts.push(`<span class="chip info">Chunks written · ${esc(Number(ing.chunks_written).toLocaleString())}</span>`);
  counts.push(`<span class="chip ${ing.failed ? "danger" : ""}">Failed · ${esc(ing.failed ?? 0)}</span>`);
  el("out-vec-counts").innerHTML = counts.join("");

  el("out-vec-chunks").innerHTML = (v.chunks || []).map(chunkCard).join("");
}

function chunkCard(c) {
  const meta = c.metadata || {};
  const metaRows = Object.entries(meta)
    .map(([k, val]) => `<dt>${esc(k)}</dt><dd class="mono">${esc(val)}</dd>`).join("");
  return `
    <div class="chunkcard">
      <div class="head">
        <span class="chip info">${esc(c.source_file)}</span>
        <span class="sub" style="font-size:13px;color:var(--muted)">page ${esc(c.page ?? "—")}</span>
      </div>
      <div class="body">${esc(c.text)}</div>
      <dl class="dl">${metaRows}</dl>
      <details>
        <summary>Show embedding</summary>
        <div class="pre">1024 dimensions · first 8 values\n[${esc((c.embedding_preview || []).join(", "))}, …]</div>
      </details>
    </div>`;
}

// ticket outputs (only when a ticket export was uploaded)
export function renderTicketOutputs(data) {
  const card = el("out-tickets-card");
  if (!data || !data.intents || !data.intents.length) { card.hidden = true; return; }
  card.hidden = false;
  const maxVol = Math.max(...data.intents.map((i) => i.volume), 1);
  el("out-intents").innerHTML = data.intents.map((i) => {
    const w = Math.max(6, Math.round((i.volume / maxVol) * 120));
    const mixed = i.coherence_flag ? ` <span class="chip warn">Mixed topics</span>` : "";
    return `<tr><td>${esc(i.intent)}${mixed}</td>
      <td><span class="volbar" style="width:${w}px"></span><span class="tnum">${esc(i.volume)}</span></td>
      <td></td></tr>`;
  }).join("");

  const verdictChip = (v) => {
    const cls = v === "covered" ? "ok" : v === "gap" ? "danger" : "warn";
    return `<span class="chip ${cls}">${esc(v)}</span>`;
  };
  el("out-coverage").innerHTML = (data.coverage || []).map((c) =>
    `<tr><td>${esc(c.intent)}</td><td>${verdictChip(c.status)}</td></tr>`).join("");
}

// --------------------------------------------------------- upload rendering -
export function renderFileRow(file, { removable = true } = {}) {
  return `
    <li class="filerow" data-file="${esc(file.name)}">
      <span class="icon-circle compact">${icon("file", 18)}</span>
      <div class="meta">
        <div class="name">${esc(file.name)}</div>
        <div class="size">${humanSize(file.size)}</div>
        <div class="progress fileprog" hidden><div class="fill"></div></div>
      </div>
      <div class="right">
        ${removable
          ? `<button class="iconbtn" data-remove="${esc(file.name)}" aria-label="Remove ${esc(file.name)}">${icon("cross", 18)}</button>`
          : ""}
      </div>
    </li>`;
}

export function errorCallout(message) {
  return `<div class="callout danger"><svg class="ico" width="18" height="18" aria-hidden="true"><use href="#i-alert"/></svg><span>${esc(message)}</span></div>`;
}
