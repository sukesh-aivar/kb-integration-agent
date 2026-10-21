// upload.js — the upload screen: two separate drop zones, validation, selected
// lists, per-file progress, sample buttons. Rendering goes through ui.js; the
// network (createUploads / uploadToPresigned / startRun) goes through the api
// module passed in. This file owns only the upload screen's local wiring.

import * as ui from "./ui.js";

let ctx = null; // { state, api, ui, cfg, MOCK, onStart }

export function initUpload(context) {
  ctx = context;
  const { cfg } = ctx;
  const LIM = cfg.LIMITS || {
    DOC_MAX_FILES: 10, DOC_MAX_FILE_MB: 50, DOC_MAX_TOTAL_MB: 150, TICKET_MAX_FILE_MB: 50,
    DOC_ACCEPT: [".pdf", ".docx", ".txt", ".md", ".html"], TICKET_ACCEPT: [".csv", ".json", ".xlsx"],
  };
  ctx.LIM = LIM;

  wireDropzone("dz-docs", "input-docs", "doc");
  wireDropzone("dz-tickets", "input-tickets", "ticket");

  ui.el("btn-sample-docs").addEventListener("click", addSampleDocs);
  ui.el("btn-sample-tickets").addEventListener("click", addSampleTickets);
  ui.el("btn-start").addEventListener("click", startRun);

  renderLists();
}

export function resetUpload() {
  if (!ctx) return;
  ctx.state.docFiles = [];
  ctx.state.ticketFile = null;
  ui.el("err-docs").innerHTML = "";
  ui.el("err-tickets").innerHTML = "";
  renderLists();
}

// -------------------------------------------------------------- drop zones --
function wireDropzone(dzId, inputId, kind) {
  const dz = ui.el(dzId);
  const input = ui.el(inputId);

  const openPicker = () => input.click();
  dz.addEventListener("click", openPicker);
  dz.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openPicker(); }
  });
  input.addEventListener("change", () => { acceptFiles([...input.files], kind); input.value = ""; });

  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => {
    // IMPORTANT: a file dropped on the documents zone is ALWAYS a document, and a
    // file dropped on the ticket zone is ALWAYS the ticket export. The two zones
    // map to the agent contract's `document_uris` (list) vs `ticket_uri` (single);
    // we must never let a ticket CSV land in the documents list, so `kind` is
    // fixed by WHICH zone received the drop, not by the file's type.
    acceptFiles([...e.dataTransfer.files], kind);
  });
}

// -------------------------------------------------------------- validation --
function extOf(name) { return "." + name.toLowerCase().split(".").pop(); }

function acceptFiles(files, kind) {
  const { state, LIM } = ctx;
  const errBox = ui.el(kind === "doc" ? "err-docs" : "err-tickets");
  const errors = [];

  if (kind === "ticket") {
    // single ticket file only
    if (files.length > 1) errors.push("Only one ticket export is allowed; keeping the first valid file.");
    for (const f of files) {
      const e = validateOne(f, LIM.TICKET_ACCEPT, LIM.TICKET_MAX_FILE_MB);
      if (e) { errors.push(e); continue; }
      state.ticketFile = { name: f.name, size: f.size, file: f };
      break; // first valid wins
    }
  } else {
    for (const f of files) {
      // order of checks per spec: extension, per-file size, total size, count, duplicate
      const e = validateOne(f, LIM.DOC_ACCEPT, LIM.DOC_MAX_FILE_MB);
      if (e) { errors.push(e); continue; }
      if (state.docFiles.length >= LIM.DOC_MAX_FILES) {
        errors.push(`“${f.name}” rejected: at most ${LIM.DOC_MAX_FILES} documents per run.`); continue;
      }
      if (state.docFiles.some((x) => x.name === f.name)) {
        errors.push(`“${f.name}” rejected: a file with that name is already added.`); continue;
      }
      const totalMB = (state.docFiles.reduce((a, x) => a + x.size, 0) + f.size) / (1024 * 1024);
      if (totalMB > LIM.DOC_MAX_TOTAL_MB) {
        errors.push(`“${f.name}” rejected: run total would exceed ${LIM.DOC_MAX_TOTAL_MB} MB.`); continue;
      }
      state.docFiles.push({ name: f.name, size: f.size, file: f });
    }
  }

  errBox.innerHTML = errors.map(ui.errorCallout).join(""); // never silently drop a file
  renderLists();
}

function validateOne(f, accept, maxMB) {
  if (!accept.includes(extOf(f.name)))
    return `“${f.name}” rejected: type not accepted here (allowed: ${accept.join(", ")}).`;
  if (f.size > maxMB * 1024 * 1024)
    return `“${f.name}” rejected: larger than ${maxMB} MB.`;
  return null;
}

// --------------------------------------------------------------- rendering --
function renderLists() {
  const { state } = ctx;
  ui.el("list-docs").innerHTML = state.docFiles.map((f) => ui.renderFileRow(f)).join("");
  ui.el("list-tickets").innerHTML = state.ticketFile ? ui.renderFileRow(state.ticketFile) : "";

  // wire remove buttons
  ui.el("list-docs").querySelectorAll("[data-remove]").forEach((b) =>
    b.addEventListener("click", () => { removeDoc(b.dataset.remove); }));
  const tr = ui.el("list-tickets").querySelector("[data-remove]");
  if (tr) tr.addEventListener("click", () => { ctx.state.ticketFile = null; renderLists(); });

  // documents-only warning (docs present, no ticket)
  ui.el("docs-only-warning").hidden = !(state.docFiles.length > 0 && !state.ticketFile);

  // summary + start enablement
  const n = state.docFiles.length;
  const totalMB = (state.docFiles.reduce((a, x) => a + x.size, 0) + (state.ticketFile?.size || 0)) / (1024 * 1024);
  const parts = [];
  if (n) parts.push(`${n} document${n === 1 ? "" : "s"}`);
  if (state.ticketFile) parts.push("a ticket export");
  let summary = parts.length ? `${parts.join(" and ")} · ${totalMB.toFixed(1)} MB` : "";
  // Rough, clearly-labelled run estimate before Start (item 13).
  if (n) summary += ` · estimated run ${runEstimate(state)}`;
  ui.el("upload-summary").textContent = summary;
  ui.el("btn-start").disabled = n === 0;
}

// Very rough estimate from the step duration hints: documents path always runs;
// ticket path adds its steps when a ticket export is present. Labelled "~" and
// "estimate" so it is never read as a promise.
function runEstimate(state) {
  const docSteps = ["normalize_docs", "kb_create", "kb_ingest", "write_report"];
  const ticketSteps = ["load_tickets", "adapt_tickets", "clean_tickets", "embed_texts", "intent_cluster", "name_clusters", "coverage_check", "draft_article"];
  const ids = state.ticketFile ? [...ticketSteps, ...docSteps] : docSteps;
  let lo = 0, hi = 0;
  for (const id of ids) {
    const d = ui.STEP_BY_ID[id]?.durHint;
    if (d) { lo += d[0]; hi += d[1]; }
  }
  const m = (s) => (s >= 60 ? `${Math.round(s / 60)} min` : `${s}s`);
  return `~${m(lo)}–${m(hi)} (estimate)`;
}

function removeDoc(name) {
  ctx.state.docFiles = ctx.state.docFiles.filter((f) => f.name !== name);
  renderLists();
}

// ---------------------------------------------------------------- samples ---
function addSampleDocs() {
  const samples = ctx.MOCK ? ctx.api.sampleDocs() : fallbackSampleDocs();
  const existing = new Set(ctx.state.docFiles.map((f) => f.name));
  for (const s of samples) if (!existing.has(s.name)) ctx.state.docFiles.push({ name: s.name, size: s.size, file: null });
  ui.el("err-docs").innerHTML = "";
  renderLists();
}
function addSampleTickets() {
  const t = ctx.MOCK ? ctx.api.sampleTickets() : { name: "support-tickets-sample.csv", size: 820_000 };
  ctx.state.ticketFile = { name: t.name, size: t.size, file: null };
  ui.el("err-tickets").innerHTML = "";
  renderLists();
}
function fallbackSampleDocs() {
  return [
    { name: "product-handbook.pdf", size: 2_900_000 },
    { name: "network-setup-guide.docx", size: 540_000 },
    { name: "faq.md", size: 18_000 },
    { name: "product-manual-scanned.pdf", size: 41_000_000 },
  ];
}

// ------------------------------------------------------------- start a run --
async function startRun() {
  const { state, api } = ctx;
  setBusy(true);

  const files = [
    ...state.docFiles.map((f) => ({ name: f.name, size: f.size, kind: "doc", file: f.file })),
    ...(state.ticketFile ? [{ ...state.ticketFile, kind: "ticket" }] : []),
  ];

  try {
    const { run_id, uploads } = await api.createUploads(files);

    // Per-file progress. In mock mode there is no real transfer, so simulate a
    // quick fill so the per-file bars are demonstrable; in real mode use the XHR
    // progress callback from api.uploadToPresigned.
    showFileBars();
    if (ctx.MOCK) {
      await Promise.all(files.map((f) => simulateUpload(f.name)));
    } else {
      await Promise.all(uploads.map((u) => {
        const f = files.find((x) => x.name === u.file_name);
        if (!f || !f.file) return Promise.resolve();
        return retry(() => api.uploadToPresigned(u, f.file, (p) => setFileProgress(f.name, p)), 3)
          .then(() => markUploaded(f.name))
          .catch(() => markUploadFailed(f.name, u, f));
      }));
    }

    await api.startRun(run_id);
    ctx.onStart(run_id);
  } catch (err) {
    console.error(err);
    showStartFailure(err);
    setBusy(false);
  }
}

function setBusy(busy) {
  ui.el("btn-start").disabled = busy || ctx.state.docFiles.length === 0;
  ui.el("dz-docs").style.pointerEvents = busy ? "none" : "";
  ui.el("dz-tickets").style.pointerEvents = busy ? "none" : "";
}

function showFileBars() {
  document.querySelectorAll("#list-docs .filerow, #list-tickets .filerow").forEach((row) => {
    const prog = row.querySelector(".fileprog"); if (prog) prog.hidden = false;
    const rm = row.querySelector("[data-remove]"); if (rm) rm.remove();
    const right = row.querySelector(".right");
    if (right && !right.querySelector(".pct")) {
      const chip = document.createElement("span"); chip.className = "chip pct"; chip.textContent = "0%";
      right.appendChild(chip);
    }
  });
}
function setFileProgress(name, p) {
  const row = rowFor(name); if (!row) return;
  const fill = row.querySelector(".fileprog .fill"); if (fill) fill.style.width = `${Math.round(p * 100)}%`;
  const chip = row.querySelector(".pct"); if (chip) chip.textContent = `${Math.round(p * 100)}%`;
}
function markUploaded(name) {
  const row = rowFor(name); if (!row) return;
  setFileProgress(name, 1);
  const chip = row.querySelector(".pct"); if (chip) { chip.className = "chip ok"; chip.textContent = "Uploaded"; }
}
function markUploadFailed(name, upload, file) {
  const row = rowFor(name); if (!row) return;
  const right = row.querySelector(".right");
  right.innerHTML = `<span class="chip danger">Upload failed</span>
    <button class="btn btn-ghost btn-small" data-retry>Retry</button>`;
  right.querySelector("[data-retry]").addEventListener("click", async () => {
    right.innerHTML = `<span class="chip pct">0%</span>`;
    try { await retry(() => ctx.api.uploadToPresigned(upload, file.file, (p) => setFileProgress(name, p)), 3); markUploaded(name); }
    catch { markUploadFailed(name, upload, file); }
  });
}
function rowFor(name) {
  return [...document.querySelectorAll(".filerow")].find((r) => r.dataset.file === name);
}
async function simulateUpload(name) {
  for (let p = 0; p <= 1.0001; p += 0.2) { setFileProgress(name, Math.min(1, p)); await sleep(90); }
  markUploaded(name);
}
function showStartFailure(err) {
  const box = ui.el("err-docs");
  box.innerHTML = ui.errorCallout(
    err?.status === 0 || err?.status >= 500
      ? "Couldn’t reach the service to start the run. Please try again."
      : (err?.message || "Couldn’t start the run."));
}

// ----------------------------------------------------------------- helpers --
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
async function retry(fn, attempts) {
  let last;
  for (let i = 0; i < attempts; i++) {
    try { return await fn(); } catch (e) { last = e; await sleep(400 * 2 ** i); }
  }
  throw last;
}
