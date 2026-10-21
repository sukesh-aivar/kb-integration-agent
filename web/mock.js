// mock.js — the fake API (spec section 9). Implements the SAME function surface
// as api.js and returns fixtures with realistic timing, so the whole UI is
// reviewable/demonstrable with no AWS account, no credentials and no deployment.
//
// A virtual clock advances a run through the 13 steps; per-step durations are
// roughly proportional to the real ones (parsing and indexing are the long steps).

import { STEPS } from "./ui.js";

// Base per-step durations in SECONDS at 1x (proportional to observed real runs:
// normalize_docs/kb_ingest dominate; embed is long; others quick).
const BASE_DUR = {
  load_tickets: 1, adapt_tickets: 2, clean_tickets: 2, embed_texts: 18,
  intent_cluster: 6, name_clusters: 8, normalize_docs: 30, kb_create: 6,
  kb_ingest: 20, kb_retrieve: 3, coverage_check: 8, draft_article: 10,
  write_report: 1,
};

// ------------------------------------------------------------- fixtures -----
// Sample documents, including one obviously scanned PDF so the routing table
// shows BOTH parsers.
const SAMPLE_DOCS = [
  { name: "product-handbook.pdf", size: 2_900_000 },
  { name: "network-setup-guide.docx", size: 540_000 },
  { name: "faq.md", size: 18_000 },
  { name: "product-manual-scanned.pdf", size: 41_000_000 }, // big + "scanned" -> BDA
];
const SAMPLE_TICKETS = { name: "support-tickets-sample.csv", size: 820_000 };

// Routing decisions derived from the fixture file names/sizes so the table is
// consistent with what was "uploaded" (spec 9). Mirrors the backend rule table.
function routeFor(file) {
  const lower = file.name.toLowerCase();
  const ext = lower.split(".").pop();
  if (["txt", "md", "html", "csv"].includes(ext))
    return { route: "DEFAULT", rule_id: "R1", reason: "Plain text or markup needs no heavy parsing.", pages: 1 };
  if (lower.includes("scan"))
    return { route: "BDA", rule_id: "R6", reason: "Pages have no extractable text (scanned), so OCR-grade parsing is needed.", pages: 128 };
  if (ext === "pdf" && file.size > 10_000_000)
    return { route: "BDA", rule_id: "R8", reason: "Large, table-heavy PDF; the default parser would lose structure.", pages: 64 };
  if (ext === "docx")
    return { route: "DEFAULT", rule_id: "R4", reason: "Plain DOCX extracts cleanly with the default parser.", pages: 12 };
  return { route: "DEFAULT", rule_id: "R10", reason: "Default: cheap local parse.", pages: 8 };
}

// ------------------------------------------------------------- run state ----
// One in-memory run, advanced by a virtual clock.
const runs = new Map();

let scenario = "normal"; // normal | slow | fail
let speed = 3;           // 1 | 3 | 10
export function setScenario(s) { scenario = s; }
export function setSpeed(s) { speed = Number(s) || 1; }
export function getScenario() { return scenario; }
export function getSpeed() { return speed; }
export const IS_MOCK = true;

function newRunId() {
  const d = new Date();
  const stamp = `${d.getFullYear()}${String(d.getMonth()+1).padStart(2,"0")}${String(d.getDate()).padStart(2,"0")}`;
  return `demo-${stamp}-${Math.random().toString(36).slice(2, 7)}`;
}

function buildPlan(hasTickets) {
  // skip rules (spec section 4): documents-only skips ticket steps 1-6 + join 11,12.
  const skipTicketSteps = !hasTickets;
  return STEPS.map((s) => {
    const def = { id: s.id, state: "pending", started_at: null, ended_at: null, skip_reason: null };
    if (skipTicketSteps && ["load_tickets","adapt_tickets","clean_tickets","embed_texts","intent_cluster","name_clusters","coverage_check","draft_article"].includes(s.id)) {
      def.state = "skipped";
      def.skip_reason = "No ticket export uploaded";
    }
    // kb_retrieve only runs when a question set is supplied; otherwise the agent
    // skips it rather than claiming a check it did not do. The backend status
    // reports this skip (see agent-change note in OPEN_QUESTIONS.md).
    if (s.id === "kb_retrieve") {
      def.state = "skipped";
      def.skip_reason = "No question set supplied";
    }
    return def;
  });
}

function stepDuration(id) {
  let d = BASE_DUR[id] ?? 5;
  // Slow parse: make normalize_docs clearly outlast STALE_AFTER (default 120s)
  // so the "still working" callout is demonstrable with margin, not right at the
  // moment the step ends. Base 30s * 6 = 180s virtual.
  if (scenario === "slow" && id === "normalize_docs") d *= 6;
  return d;
}

// Precompute each active step's virtual start/end offsets (in virtual seconds).
function schedule(stages) {
  let t = 0;
  const times = {};
  for (const st of stages) {
    if (st.state === "skipped") { times[st.id] = { start: null, end: null }; continue; }
    const dur = stepDuration(st.id);
    times[st.id] = { start: t, end: t + dur };
    t += dur;
  }
  return { times, totalVirtual: t };
}

// --------------------------------------------------------- API surface ------
export async function createUploads(files) {
  const run_id = newRunId();
  runs.set(run_id, {
    run_id,
    hasTickets: files.some((f) => f.kind === "ticket"),
    docFiles: files.filter((f) => f.kind === "doc").map((f) => ({ name: f.name, size: f.size })),
    ticketFile: files.find((f) => f.kind === "ticket") || null,
    startedWall: null,
    stages: null,
    sched: null,
    events: [],
    terminal: null, // "complete" | "failed"
    resumedFrom: null,
  });
  // presigned-POST shape (mock values; no network)
  return {
    run_id,
    uploads: files.map((f) => ({ file_name: f.name, url: "mock://upload", fields: {} })),
  };
}

export async function startRun(run_id) {
  const r = runs.get(run_id);
  if (!r) throw notFound();
  r.stages = buildPlan(r.hasTickets);
  r.sched = schedule(r.stages);
  r.startedWall = performance.now();
  r.events = [{ t: 0, text: `Run created with ${r.docFiles.length} document${r.docFiles.length === 1 ? "" : "s"}${r.hasTickets ? " and a ticket export" : ""}.` }];
  r._emitted = new Set();
  return { run_id, status: "running" };
}

function virtualNow(r) {
  if (r.startedWall == null) return 0;
  return ((performance.now() - r.startedWall) / 1000) * speed;
}

// Advance the stages to match the virtual clock, emitting events as we go.
function advance(r) {
  if (!r.stages) return;
  const vnow = virtualNow(r);
  const failAt = scenario === "fail" ? "kb_ingest" : null;

  for (const st of r.stages) {
    if (st.state === "skipped") continue;
    const sc = r.sched.times[st.id];
    if (sc.start == null) continue;

    // start — emit a start line only for the long steps (parsing/embedding/
    // indexing) so the feed is decisions and counts, not paired Started/Finished.
    if (st.state === "pending" && vnow >= sc.start) {
      st.state = "running";
      st.started_at = Math.round(sc.start);
      if (["normalize_docs", "embed_texts", "kb_ingest"].includes(st.id)) {
        emit(r, sc.start, `${labelOf(st.id)}…`);
      }
      routingEvents(r, st, sc.start);
    }
    // fail midway through kb_ingest in the fail scenario
    if (failAt && st.id === failAt && st.state === "running") {
      const failPoint = sc.start + (sc.end - sc.start) * 0.5;
      if (vnow >= failPoint && r.resumedFrom !== st.id) {
        st.state = "failed";
        st.ended_at = Math.round(failPoint);
        emit(r, failPoint, `Indexing failed: the knowledge base rejected a batch of vectors.`);
        r.terminal = "failed";
        r.error = "ServiceException: ingestion job FAILED — vector batch 3 rejected (simulated failure for the demo).";
        return;
      }
    }
    // finish
    if (st.state === "running" && vnow >= sc.end) {
      st.state = "done";
      st.ended_at = Math.round(sc.end);
      finishEvents(r, st, sc.end);
    }
  }

  // terminal complete?
  const active = r.stages.filter((s) => s.state !== "skipped");
  if (active.every((s) => s.state === "done") && r.terminal !== "complete") {
    r.terminal = "complete";
    r.finalElapsed = Math.round(r.sched.totalVirtual);
    emit(r, r.sched.totalVirtual, "Run complete — report and manifest written.");
  }
}

function labelOf(id) { return STEPS.find((s) => s.id === id)?.label || id; }

function emit(r, t, text) {
  const key = `${t}|${text}`;
  if (r._emitted.has(key)) return;
  r._emitted.add(key);
  r.events.push({ t: Math.round(t), text });
}

function routingEvents(r, st, t) {
  if (st.id !== "normalize_docs") return;
  for (const f of r.docFiles) {
    const d = routeFor(f);
    const dest = d.route === "BDA" ? "Bedrock Data Automation" : "the default parser";
    emit(r, t, `Routing ${f.name} to ${dest} (rule ${d.rule_id}: ${d.reason})`);
  }
}

function finishEvents(r, st, t) {
  // Prefer an informative, decision/count line over a bare "Finished" (item 14).
  const dur = fmtDur(st.ended_at - st.started_at);
  const bdaPages = r.docFiles.reduce((a, f) => a + (routeFor(f).route === "BDA" ? routeFor(f).pages : 0), 0);
  const nDocs = r.docFiles.length;
  const specific = {
    load_tickets: `Read the ticket export.`,
    adapt_tickets: `Mapped ticket fields to the common shape.`,
    clean_tickets: `Cleaned and de-duplicated the tickets.`,
    embed_texts: `Embedded ticket text into vectors (${dur}).`,
    intent_cluster: `Grouped tickets into intents.`,
    name_clusters: `Named the discovered intents.`,
    normalize_docs: bdaPages
      ? `Parsed ${nDocs} document${nDocs === 1 ? "" : "s"}, ${bdaPages} pages with Bedrock Data Automation (${dur}).`
      : `Parsed ${nDocs} document${nDocs === 1 ? "" : "s"} with the default parser (${dur}).`,
    kb_create: `Knowledge base created and active.`,
    kb_ingest: `Indexing complete — 1,736 chunks created (${dur}).`,
    coverage_check: `Checked coverage for the top intents.`,
    draft_article: `Drafted articles for the coverage gaps.`,
    write_report: `Report and run manifest written.`,
  }[st.id];
  emit(r, t, specific || `Completed ${labelOf(st.id)} (${dur}).`);
}

function fmtDur(s) { const m = Math.floor(s / 60); return `${m}:${String(Math.round(s % 60)).padStart(2, "0")}`; }

export async function getStatus(run_id) {
  const r = runs.get(run_id);
  if (!r) throw notFound();
  if (!r.stages) return { run_id, status: "queued", stages: [], events: [], elapsed_seconds: 0, error: null };
  advance(r);
  const status = r.terminal === "failed" ? "failed" : r.terminal === "complete" ? "complete" : "running";
  return {
    run_id,
    status,
    stages: r.stages.map((s) => ({ ...s })),
    events: r.events.slice(),
    elapsed_seconds: Math.round(Math.min(virtualNow(r), r.sched.totalVirtual)),
    error: r.error || null,
  };
}

export async function resumeRun(run_id) {
  const r = runs.get(run_id);
  if (!r) throw notFound();
  // completed steps stay complete; retry the failed step and continue
  const failed = r.stages.find((s) => s.state === "failed");
  if (failed) {
    r.resumedFrom = failed.id;
    failed.state = "running";
    failed.ended_at = null;
    r.terminal = null;
    r.error = null;
    // re-base the clock so remaining steps play out from now
    const resumeVirtual = r.sched.times[failed.id].start;
    r.startedWall = performance.now() - (resumeVirtual * 1000) / speed;
    emit(r, resumeVirtual, "Run resumed from the failed step.");
  }
  return { run_id, status: "running" };
}

// --------------------------------------------------- outputs + vectors ------
export async function getOutputs(run_id) {
  const r = runs.get(run_id) || seededRun();
  const routing = r.docFiles.map((f) => {
    const d = routeFor(f);
    return { file: f.name, size: fmtBytes(f.size), pages: d.pages, route: d.route, rule_id: d.rule_id, reason: d.reason };
  });
  const bdaCount = routing.filter((x) => x.route === "BDA").length;
  const defCount = routing.length - bdaCount;

  // Derive step counts and total time from the run's own plan/schedule so the
  // Outputs screen agrees with the activity feed and the progress clock.
  const plan = r.stages || buildPlan(r.hasTickets);
  const sched = r.sched || schedule(plan);
  const completed = plan.filter((s) => s.state !== "skipped").length;
  const skipped = plan.filter((s) => s.state === "skipped").length;
  const totalSecs = r.finalElapsed != null ? r.finalElapsed : Math.round(sched.totalVirtual);

  const objects = buildObjects(r);
  return {
    objects,
    routing,
    summary: {
      run_id: r.run_id,
      status: r.terminal === "failed" ? "failed" : "complete",
      documents_processed: r.docFiles.length,
      parser_split: `${defCount} default parser · ${bdaCount} Bedrock Data Automation`,
      ticket_export_included: r.hasTickets,
      steps_completed: completed,
      steps_skipped: skipped,
      total_time: fmtDur(totalSecs),
      total_seconds: totalSecs,
      kb_id: "demo-kb-7f3qz2ax",
    },
  };
}

function buildObjects(r) {
  const id = r.run_id;
  const B = "demo-bucket"; // obviously-fake name; never the real bucket
  // Absolute ISO timestamps relative to a fixed demo "now" so rows read like a
  // real console listing rather than "just now".
  const base = Date.now();
  const ts = (offsetSec) => new Date(base - offsetSec * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC";
  const o = (key, size, agoSec, previewable = false) =>
    ({ key, uri: `s3://${B}/${key}`, size, last_modified: ts(agoSec), prefix_group: key.split("/")[0], previewable });
  const list = [];
  // uploads/
  for (const f of r.docFiles) list.push(o(`uploads/${id}/docs/${f.name}`, fmtBytes(f.size), 180));
  if (r.ticketFile) list.push(o(`uploads/${id}/${r.ticketFile.name}`, fmtBytes(r.ticketFile.size), 180));
  // runs/
  list.push(o(`runs/${id}/report.md`, "7 KB", 5, true));
  list.push(o(`runs/${id}/report.json`, "14 KB", 5, true));
  list.push(o(`runs/${id}/manifest.json`, "6 KB", 4, true));
  for (const f of r.docFiles) list.push(o(`runs/${id}/kb-docs/${f.name}.md`, "12 KB", 30, true));
  // stage markers — ONE per step that actually ran (not the skipped ones), so
  // the count matches steps_completed (e.g. 5 on a documents-only run, not 13).
  const ranSteps = (r.stages || buildPlan(r.hasTickets)).filter((s) => s.state !== "skipped");
  for (const s of ranSteps) list.push(o(`runs/${id}/_markers/${s.id}.done`, "4 B", 10));
  // drafts/
  if (r.hasTickets) {
    list.push(o(`drafts/${id}/account_access_help.json`, "3 KB", 6, true));
    list.push(o(`drafts/${id}/billing_questions.json`, "3 KB", 6, true));
  }
  return list;
}

export async function getVectors(run_id) {
  const r = runs.get(run_id) || seededRun();
  const docFiles = r.docFiles.length ? r.docFiles : SAMPLE_DOCS;
  // A chunk's parser MUST come from its source file's routing decision, so the
  // vectors card never contradicts the routing table.
  const parserOf = (name) => (routeFor({ name, size: fileSize(name, docFiles) }).route === "BDA"
    ? "Bedrock Data Automation" : "default parser");

  // Generic, obviously-sample chunk text. Each is labelled as sample content and
  // carries no real product details.
  const samples = [
    "Sample content: this passage describes how to reset an account password using the self-service flow. Chunk text is illustrative only.",
    "Sample content: this passage explains how to configure a network connection and switch transport settings. Chunk text is illustrative only.",
    "Sample content: this passage covers how to raise a support request and what information to include. Chunk text is illustrative only.",
    "Sample content: this passage summarises service availability and expected response times. Chunk text is illustrative only.",
  ];
  const previews = [
    [0.0142, -0.0310, 0.0558, 0.0021, -0.0677, 0.0193, 0.0402, -0.0088],
    [-0.0209, 0.0471, 0.0133, -0.0505, 0.0088, 0.0260, -0.0317, 0.0119],
    [0.0355, 0.0102, -0.0440, 0.0298, 0.0017, -0.0611, 0.0224, 0.0076],
    [-0.0122, -0.0288, 0.0491, 0.0150, -0.0203, 0.0366, -0.0054, 0.0411],
  ];
  const pages = [5, 12, 23, 41];
  const chunks = samples.map((text, i) => {
    const src = docFiles[i % docFiles.length].name;
    return {
      text,
      source_file: src,
      page: pages[i],
      metadata: { run_id: r.run_id, data_source_id: "demo-ds-8842", parser: parserOf(src) },
      embedding_preview: previews[i],
    };
  });

  return {
    index: { bucket: "demo-vector-bucket", name: "demo-vector-index", dimension: 1024, metric: "cosine" },
    ingestion: { scanned: docFiles.length, indexed: docFiles.length, failed: 0, chunks_written: 1736 },
    embedding_model: "amazon.titan-embed-text-v2 (1024-dim)",
    chunking_strategy: "hierarchical (1500 / 300 tokens, 60 overlap)",
    chunks,
  };
}

function fileSize(name, docFiles) {
  const f = docFiles.find((d) => d.name === name);
  return f ? f.size : 1_000_000;
}

export async function getTicketOutputs(run_id) {
  const r = runs.get(run_id) || seededRun();
  if (!r.hasTickets) return { intents: [], coverage: [] };
  return {
    intents: [
      { intent: "vpn_connection_issues", volume: 142, coherence_flag: false },
      { intent: "password_reset_help", volume: 98, coherence_flag: false },
      { intent: "printer_and_peripherals", volume: 67, coherence_flag: true },
      { intent: "billing_and_licensing", volume: 44, coherence_flag: false },
      { intent: "account_access_mixed", volume: 31, coherence_flag: true },
    ],
    coverage: [
      { intent: "vpn_connection_issues", status: "covered" },
      { intent: "password_reset_help", status: "covered" },
      { intent: "printer_and_peripherals", status: "partial" },
      { intent: "billing_and_licensing", status: "gap" },
      { intent: "account_access_mixed", status: "gap" },
    ],
  };
}

// A pre-seeded COMPLETED run for "View an example run" (spec 3.2) — instant,
// reliable demo path that does not depend on a live run.
export function seedExampleRun() {
  const run_id = "demo-example-run";
  if (!runs.has(run_id)) {
    const r = {
      run_id, hasTickets: true,
      docFiles: SAMPLE_DOCS.map((f) => ({ ...f })),
      ticketFile: { ...SAMPLE_TICKETS },
      startedWall: 0, stages: buildPlan(true), sched: null, events: [], terminal: "complete", error: null, resumedFrom: null,
    };
    r.sched = schedule(r.stages);
    for (const st of r.stages) { if (st.state !== "skipped") { st.state = "done"; st.started_at = r.sched.times[st.id].start; st.ended_at = r.sched.times[st.id].end; } }
    runs.set(run_id, r);
  }
  return run_id;
}
function seededRun() { return runs.get(seedExampleRun()); }

// sample-file helpers for the upload screen
export function sampleDocs() { return SAMPLE_DOCS.map((f) => ({ ...f, kind: "doc" })); }
export function sampleTickets() { return { ...SAMPLE_TICKETS, kind: "ticket" }; }

// --------------------------------------------------------------- helpers ----
function fmtBytes(b) {
  const mb = b / (1024 * 1024);
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`;
}
function notFound() { const e = new Error("not found or not yours"); e.status = 404; return e; }
