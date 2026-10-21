// api.js — all fetch calls, token attachment, error normalisation.
// This module FETCHES and never renders (separation per spec 7). It exposes the
// SAME function surface as mock.js so app.js can swap one for the other.

import { getToken } from "./auth.js";

const cfg = (typeof window !== "undefined" && window.APP_CONFIG) || {};
const BASE = (cfg.API_BASE_URL || "").replace(/\/$/, "");

export const IS_MOCK = false;

// Normalised error: carries a `.status` and a plain-language `.message`.
class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; this.name = "ApiError"; }
}

async function request(path, { method = "GET", body } = {}) {
  const token = await getToken(); // may trigger a silent refresh
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
  } catch (networkErr) {
    // transient network failure — app.js decides whether to retry/keep state
    throw new ApiError(0, "Could not reach the service. Check the connection and try again.");
  }

  if (res.status === 401) throw new ApiError(401, "Your session has expired. Please sign in again.");
  if (res.status === 404) throw new ApiError(404, "That run was not found, or is not yours.");
  if (res.status >= 500) throw new ApiError(res.status, "The service hit a temporary problem. Retrying.");
  if (!res.ok) throw new ApiError(res.status, `Request failed (${res.status}).`);

  // 204 or empty body tolerance
  const text = await res.text();
  return text ? JSON.parse(text) : {};
}

// ---- the surface mock.js also implements -----------------------------------

// files: [{name, size, kind:'doc'|'ticket', file: File}]
export async function createUploads(files) {
  return request("/uploads", {
    method: "POST",
    body: { files: files.map((f) => ({ file_name: f.name, size: f.size, kind: f.kind })) },
  });
}

// Upload one file to its presigned POST target (used by app.js with progress).
// Returns a Promise resolving when the PUT/POST completes. Uses XHR for progress.
export function uploadToPresigned(upload, file, onProgress) {
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
  return request(`/runs/${encodeURIComponent(runId)}/start`, { method: "POST" });
}

export async function getStatus(runId) {
  return request(`/runs/${encodeURIComponent(runId)}/status`);
}

export async function resumeRun(runId) {
  return request(`/runs/${encodeURIComponent(runId)}/start`, { method: "POST", body: { resume: true } });
}

export async function getOutputs(runId) {
  return request(`/runs/${encodeURIComponent(runId)}/outputs`);
}

export async function getVectors(runId) {
  return request(`/runs/${encodeURIComponent(runId)}/vectors`);
}

// Ticket outputs are part of the outputs payload in the real API; the UI reads
// them from there. Kept as a function for surface-parity with mock.js.
export async function getTicketOutputs(runId) {
  const out = await getOutputs(runId);
  return out.ticket_outputs || { intents: [], coverage: [] };
}

export { ApiError };
