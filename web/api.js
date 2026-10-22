// api.js — all fetch calls, token attachment, error normalisation.
// FETCHES and never renders (separation per spec 7). Exposes the SAME function
// surface as mock.js so app.js can swap one for the other.
//
// IMPORTANT: the live backend (6 Lambdas behind an HTTP API) serves the agent's
// RAW self-reported run-state under runs/{run_id}/. The UI (app.js/ui.js) was
// written against the richer mock shape (stages[] with per-step state/timestamps,
// status in queued|running|complete|failed, events as {t,text}). All of that
// translation lives HERE so app.js/ui.js/upload.js stay untouched.
//
// Route map (matches API Gateway):
//   POST /uploads                      -> { run_id, key, upload:{url,fields} }  (one file per call)
//   POST /start                        -> { run_id, status }
//   GET  /status/{run_id}              -> raw run-state (_started/_events/_finished merged)
//   GET  /outputs/{run_id}             -> { report, state, artifacts }
//   GET  /vectors/{run_id}             -> vectors_sample.json
//   GET  /preview/{run_id}/{artifact}  -> { url }  (presigned GET)

import { getToken } from "./auth.js";
import { STEPS } from "./ui.js";

const cfg = (typeof window !== "undefined" && window.APP_CONFIG) || {};
const BASE = (cfg.API_BASE_URL || "").replace(/\/$/, "");

export const IS_MOCK = false;

// Ticket-pipeline steps that are skipped on a documents-only run (mirrors the
// agent's skip set; used when the agent has not yet written _started.json).
const TICKET_STEPS = [
  "load_tickets", "adapt_tickets", "clean_tickets", "embed_texts",
  "intent_cluster", "name_clusters", "coverage_check", "draft_article",
];

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; this.name = "ApiError"; }
}

async function request(path, { method = "GET", body } = {}) {
  const token = await getToken(); // ID token; may trigger a silent refresh
  let res;
  try {
    res = await fetch(`${BASE}${path}`, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: body != null ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "Could not reach the service. Check the connection and try again.");
  }

  if (res.status === 401) throw new ApiError(401, "Your session has expired. Please sign in again.");
  if (res.status === 403) throw new ApiError(403, "That run was not found, or is not yours.");
  if (res.status === 404) throw new ApiError(404, "That run was not found, or is not yours.");
  if (res.status >= 500) throw new ApiError(res.status, "The service hit a temporary problem. Retrying.");
  if (!res.ok && res.status !== 409) throw new ApiError(res.status, `Request failed (${res.status}).`);

  const text = await res.text();
  const json = text ? JSON.parse(text) : {};
  if (res.status === 409) json.__conflict = true; // not-ready; handled by caller
  return json;
}

// ---- uploads ---------------------------------------------------------------
// Upload mode: "presigned" (browser POSTs directly to S3) or "proxy" (file goes
// as base64 through API Gateway/Lambda). Proxy avoids the browser -> *.s3.amazonaws.com
// hop that some corporate DLP agents (e.g. Trellix) block. Default presigned.
const UPLOAD_MODE = (cfg.UPLOAD_MODE === "proxy") ? "proxy" : "presigned";
// API Gateway hard-caps the request at 10 MB; base64 inflates ~33%, so keep the
// proxy-upload ceiling well under that.
const PROXY_MAX_BYTES = 8 * 1024 * 1024;

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => {
      // result is a data: URL; strip the "data:...;base64," prefix.
      const s = String(r.result || "");
      resolve(s.slice(s.indexOf(",") + 1));
    };
    r.onerror = () => reject(new ApiError(0, "Could not read the file."));
    r.readAsDataURL(file);
  });
}

// The backend enforces a strict S3-key-safe filename (letters, digits, dot,
// dash, underscore; 1-128 chars). Real filenames have spaces, &, parentheses,
// etc., which would 400. Sanitize to a safe S3 key here while the UI keeps the
// user's original display name. Collapse runs of disallowed chars to one "_",
// trim the length, and keep a leading alphanumeric.
function safeFilename(name) {
  let s = String(name || "file")
    .replace(/[^A-Za-z0-9._-]+/g, "_") // disallowed -> underscore
    .replace(/_{2,}/g, "_")            // collapse repeats
    .replace(/^[._-]+/, "");           // must start with alphanumeric
  if (!s) s = "file";
  if (s.length > 128) {
    // preserve the extension when trimming
    const dot = s.lastIndexOf(".");
    const ext = dot > 0 ? s.slice(dot) : "";
    s = s.slice(0, 128 - ext.length) + ext;
  }
  return s;
}

// app.js/upload.js expect createUploads(files) -> { run_id, uploads:[{file_name,...}] },
// then call uploadToPresigned(upload, file) per file.
//
// presigned mode: /uploads returns a presigned POST per file; the browser POSTs
//   the bytes to S3 (via uploadToPresigned).
// proxy mode: the bytes are sent (base64) to /upload-direct HERE, so the upload
//   is already done by the time createUploads returns; uploadToPresigned then
//   just reports completion.
export async function createUploads(files) {
  let runId = null;
  const uploads = [];
  for (const f of files) {
    const kind = f.kind === "ticket" ? "tickets" : "docs";

    if (UPLOAD_MODE === "proxy") {
      if (!f.file) { uploads.push({ file_name: f.name, done: true }); continue; }
      if (typeof f.size === "number" && f.size > PROXY_MAX_BYTES) {
        throw new ApiError(413,
          `“${f.name}” is too large for this environment’s upload path ` +
          `(limit ~8 MB). Please use a smaller file for the demo.`);
      }
      const content_b64 = await readAsBase64(f.file);
      const payload = { filename: safeFilename(f.name), kind, content_b64 };
      if (runId) payload.run_id = runId;
      if (f.file.type) payload.content_type = f.file.type;
      const r = await request("/upload-direct", { method: "POST", body: payload });
      runId = r.run_id;
      uploads.push({ file_name: f.name, done: true });
      continue;
    }

    // presigned mode (production default)
    const payload = { filename: safeFilename(f.name), kind };
    if (runId) payload.run_id = runId;
    if (f.file && f.file.type) payload.content_type = f.file.type;
    if (typeof f.size === "number") payload.size = f.size;
    const r = await request("/uploads", { method: "POST", body: payload });
    runId = r.run_id;
    uploads.push({ file_name: f.name, url: r.upload.url, fields: r.upload.fields });
  }
  return { run_id: runId, uploads };
}

// Upload one file. In presigned mode this POSTs to the S3 presigned target (XHR
// for progress). In proxy mode the bytes were already sent in createUploads, so
// this just reports 100%.
export function uploadToPresigned(upload, file, onProgress) {
  if (upload && upload.done) {
    if (onProgress) onProgress(1);
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const [k, v] of Object.entries(upload.fields || {})) form.append(k, v);
    form.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", upload.url, true);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new ApiError(xhr.status, "Upload failed.")));
    xhr.onerror = () => reject(new ApiError(0, "Upload failed — network error."));
    xhr.send(form);
  });
}

export async function startRun(runId) {
  return request("/start", { method: "POST", body: { run_id: runId } });
}

// The backend has no separate resume endpoint in Phase 1; re-invoking /start is
// idempotent on the agent side (already_running) and resumes from markers.
export async function resumeRun(runId) {
  return request("/start", { method: "POST", body: { run_id: runId } });
}

// ---- status: transform raw run-state -> the UI's stages[] shape ------------
export async function getStatus(runId) {
  const raw = await request(`/status/${encodeURIComponent(runId)}`);
  return adaptStatus(runId, raw);
}

function adaptStatus(runId, raw) {
  // Queued: accepted but the agent has not written _started.json yet.
  if (!raw || raw.status === "queued" || !raw.stage_order || raw.stage_order.length === 0) {
    return { run_id: runId, status: "queued", stages: [], events: [], elapsed_seconds: 0, error: null };
  }

  const skipped = raw.skipped || {};
  const events = raw.events || [];

  // Index the latest event per stage to derive per-stage state + timings.
  const startedAt = {}; const endedAt = {}; const failedAt = {};
  for (const ev of events) {
    const st = ev.stage;
    if (!st) continue;
    if (ev.type === "stage_started" && startedAt[st] == null) startedAt[st] = ev.t;
    if (ev.type === "stage_done") endedAt[st] = ev.t;
    if (ev.type === "stage_failed") failedAt[st] = ev.t;
  }

  const finished = raw.finished || null;
  const failingStage = finished && finished.failing_stage;
  // A successfully finished run has NO running/pending steps. The final poll can
  // arrive between a stage's stage_started and stage_done events, which left the
  // last step (e.g. draft_article) spinning forever even though the run is done.
  // When finished-ok, force every non-skipped, non-failed stage to "done".
  const finishedOk = !!finished && finished.status !== "failed";

  const stages = STEPS.map((s) => {
    const id = s.id;
    const def = { id, state: "pending", started_at: null, ended_at: null, skip_reason: null };
    if (id in skipped) { def.state = "skipped"; def.skip_reason = skipped[id]; return def; }
    if (failedAt[id] != null || failingStage === id) {
      def.state = "failed"; def.started_at = startedAt[id] ?? null; def.ended_at = failedAt[id] ?? null; return def;
    }
    if (endedAt[id] != null) { def.state = "done"; def.started_at = startedAt[id] ?? 0; def.ended_at = endedAt[id]; return def; }
    if (finishedOk) {
      // Run is done and this stage neither skipped nor failed -> it completed,
      // even if its stage_done event wasn't in this (final) poll's event list.
      def.state = "done"; def.started_at = startedAt[id] ?? 0; def.ended_at = endedAt[id] ?? null; return def;
    }
    if (startedAt[id] != null) { def.state = "running"; def.started_at = startedAt[id]; return def; }
    return def;
  });

  // Overall status in the UI's vocabulary.
  let status;
  if (finished) status = finished.status === "failed" ? "failed" : "complete";
  else status = "running";

  // Elapsed seconds. When finished, use the agent's authoritative value. While
  // running, derive it from the WALL CLOCK since started_at — NOT the last
  // event's `t`. The event `t` only advances when an event fires, so using it
  // made the on-screen timer jump backward every poll (the local 1s tick counts
  // real seconds, then a poll snapped the base back to the last event's t).
  let elapsed;
  if (finished) {
    elapsed = Math.round(finished.elapsed_seconds || 0);
  } else if (raw.started_at) {
    const startedMs = Date.parse(raw.started_at);
    elapsed = Number.isFinite(startedMs)
      ? Math.max(0, Math.round((Date.now() - startedMs) / 1000))
      : (events.length ? events[events.length - 1].t : 0);
  } else {
    elapsed = events.length ? events[events.length - 1].t : 0;
  }

  return {
    run_id: runId,
    status,
    stages,
    // UI activity feed wants {t, text}; pass the agent's human-readable text.
    events: events
      .filter((e) => e.type !== "stage_started" || ["normalize_docs", "embed_texts", "kb_ingest"].includes(e.stage))
      .map((e) => ({ t: e.t, text: e.text })),
    elapsed_seconds: elapsed,
    error: finished && finished.error ? (finished.error.message || "The run failed.") : null,
  };
}

// ---- outputs: transform report.json + _state.json -> UI outputs shape ------
export async function getOutputs(runId) {
  const o = await request(`/outputs/${encodeURIComponent(runId)}`);
  if (o.__conflict) {
    // Not finished yet — return a minimal shape so the screen degrades gracefully.
    return { objects: [], routing: [], summary: { run_id: runId, status: "running" } };
  }
  const report = o.report || {};
  const state = o.state || {};
  const routing = (state.routing || []).map((r) => ({
    file: r.file,
    size: typeof r.size === "number" ? `${(r.size / (1024 * 1024)).toFixed(1)} MB` : (r.size || "—"),
    pages: r.pages,
    route: (r.route || "").toUpperCase(),
    rule_id: r.rule_id,
    reason: r.reason,
  }));
  const bda = routing.filter((x) => x.route === "BDA").length;
  const def = routing.length - bda;

  const objects = (o.artifacts || []).map((a) => ({
    key: `runs/${runId}/${a.artifact.replace(/^draft\//, "")}`.replace(/^runs\/[^/]+\/(report|manifest)/, `runs/${runId}/$1`),
    artifact: a.artifact,
    name: a.name,
    previewable: a.artifact.endsWith(".md") || a.artifact.endsWith(".json"),
  }));

  const ing = state.ingestion || {};
  return {
    objects,
    routing,
    summary: {
      run_id: runId,
      status: o.status || report.status || "complete",
      documents_processed: state.document_count ?? (report.metrics ? report.metrics.document_count : null) ?? 0,
      parser_split: `${def} default parser · ${bda} Bedrock Data Automation`,
      ticket_export_included: !!state.ticket_count,
      kb_id: state.knowledge_base_id || (report.knowledge_base && report.knowledge_base.kb_id) || null,
      ingestion: ing,
    },
  };
}

// Preview an artifact's content by following the presigned GET URL. app.js only
// calls this lazily when the user clicks "Preview", so the fetch lives here.
export async function previewArtifact(runId, artifact) {
  const r = await request(`/preview/${encodeURIComponent(runId)}/${encodeURIComponent(artifact)}`);
  if (!r.url) return "";
  try {
    const res = await fetch(r.url);
    return await res.text();
  } catch {
    return "(could not load preview)";
  }
}

// ---- vectors: transform vectors_sample.json -> UI vectors shape ------------
export async function getVectors(runId) {
  const v = await request(`/vectors/${encodeURIComponent(runId)}`);
  // Also fold in index/ingestion facts from _state via outputs when present.
  let state = {};
  try { const o = await request(`/outputs/${encodeURIComponent(runId)}`); if (!o.__conflict) state = o.state || {}; } catch { /* ignore */ }
  const ing = state.ingestion || {};
  const chunks = (v.chunks || []).map((c) => ({
    text: c.text || "",
    source_file: c.source_id || c.source_file || "",
    page: c.page ?? null,
    metadata: c.metadata || { run_id: runId, data_source_id: state.data_source_id || null },
    embedding_preview: c.embedding_preview || [],
  }));
  return {
    index: {
      bucket: state.vector_bucket || null,
      name: state.index_name || null,
      dimension: 1024,
      metric: "cosine",
    },
    ingestion: {
      scanned: ing.scanned ?? null, indexed: ing.indexed ?? null,
      failed: ing.failed ?? null, chunks_written: ing.chunks_written ?? null,
    },
    embedding_model: state.embedding_model || "amazon.titan-embed-text-v2:0",
    chunking_strategy: state.chunking
      ? `${state.chunking.strategy} (${state.chunking.parent_tokens} / ${state.chunking.child_tokens} tokens, ${state.chunking.overlap_tokens} overlap)`
      : "hierarchical",
    chunks,
    // Phase 1: the agent defers the live chunk sample, so this is usually empty.
    note: v.error === "unavailable" ? "Chunk preview is not available for this run yet." : null,
  };
}

// Ticket outputs are embedded in report.json (intent_view + coverage).
export async function getTicketOutputs(runId) {
  let report = {};
  try { const o = await request(`/outputs/${encodeURIComponent(runId)}`); if (!o.__conflict) report = o.report || {}; } catch { /* ignore */ }
  const intents = (report.intent_view || []).map((i) => ({
    intent: i.intent || i.name, volume: i.volume ?? i.size ?? 0, coherence_flag: !!i.coherence_flag,
  }));
  const coverage = (report.coverage || []).map((c) => ({ intent: c.intent, status: c.status }));
  return { intents, coverage };
}

export { ApiError };
