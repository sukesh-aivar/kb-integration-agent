# POC Frontend — Implementation Review

**Branch:** `kbagent-poc`
**Scope:** Browser UI only for the Knowledge Base Integration Agent POC. No AWS code, no Lambda handlers, no infrastructure (that is the companion deployment spec, out of scope here).
**Status:** All four screens work end to end in mock mode. Verified in headless Chrome (CDP), not just static review.
**Reviewer note:** This document describes what was built against the Kiro Build Specification (POC Frontend, UI only). Section numbers below refer to that spec.

---

## 1. How to review it

- Open `web/index.html?mock=1` on a static server (`python3 -m http.server` in `web/`). The `?mock=1` activates mock mode — no AWS account, no credentials, no network.
- In mock mode the amber bar exposes a **Scenario** dropdown (Normal / Slow parse / Fails while indexing) and a **Speed** control (1× / 3× / 10×). At 10× a ~2-minute run completes in ~12 seconds.
- Fastest path: "Sign in" → "Add sample files" + "Add sample ticket export" → "Start run". Or "View an example run" from the sign-in screen for an instant completed outputs view.

---

## 2. Architecture and file layout

Plain HTML/CSS/vanilla JS ES modules. No framework, no bundler, no build step (spec 7). The only external dependency is the Inter web font.

```
web/
├── index.html      (385 lines)  all four screens, persistent chrome, inline SVG icon sprite
├── styles.css      (389 lines)  design tokens, dark mode, component inventory, layout
├── ui.js           (462 lines)  render helpers + the 13-step definitions. RENDERS, never fetches.
├── mock.js         (372 lines)  the fake API: virtual clock, 3 scenarios, fixtures
├── api.js          (97 lines)   real fetch calls, token attach, error normalisation. FETCHES, never renders.
├── auth.js         (119 lines)  Cognito authorization-code + PKCE, token refresh
├── app.js          (385 lines)  owns state + sequencing: view switching, polling loop, hash resume
├── upload.js       (264 lines)  upload screen: two drop zones, validation, per-file progress
├── config.example.js (34 lines) committed template with REPLACE_ME_* placeholders
└── assets/
    ├── logo.png                 active logo (= Ingram_micro_logo.png, transparent wordmark)
    ├── Ingram_micro_logo.png    source: 791×157 transparent wordmark
    ├── Ingram Micro Logo.png    original square icon (unused; kept for reference)
    └── logo-2.png               alternative blue-badge logo (unused)
```

**Separation of concerns (spec 7, enforced):** `ui.js` renders and never calls the network; `api.js`/`mock.js` fetch and never render; `app.js` owns state and sequencing. `app.js` dynamically imports **either** `mock.js` **or** `api.js`+`auth.js` based on the `MOCK` flag, so the real-API and auth code is never even evaluated during a pure mock review.

`config.js` (the deploy-time, git-ignored file) is **not** present; it is generated from stack outputs and holds only non-secret values. `web/config.js` has been added to `.gitignore`. In its absence the UI defaults to mock mode.

---

## 3. Screens

One HTML page, four `<section>`s, only one visible at a time. No client-side router: screens are shown/hidden and the active run id lives in the URL hash (`#run=<id>`), so a refresh resumes the same run.

### 3.1 Persistent chrome (every screen)
- **Mock-mode bar** (mock only): amber, carries the scenario + speed controls.
- **Top bar**: logo + divider + "KB Integration Agent" on the left; signed-in email chip + "Sign out" on the right.
- **Step indicator**: Upload → Progress → Outputs, current step `aria-current="step"`.
- **Data banner**: the verbatim demonstration-pilot copy.
- **Footer**: "Aivar Innovations · demonstration pilot" / "Sample or masked data only".

### 3.2 Sign-in
- Two-column hero: title + lead + "Sign in" / "View an example run" buttons on the left; the diagonal blue `clip-path` panel on the right with two floating decorative cards (a 4-step pipeline preview and a CSS bar chart). Both decorative cards are `aria-hidden`.
- "Sign in" → Cognito Hosted UI in the real build; → upload screen in mock. No username/password form is built in the page (spec 3.2).
- "View an example run" → a pre-seeded completed run straight to outputs (reliable demo path).

### 3.3 Upload
- Two **separate** drop zones — "Support documents" (list) and "Ticket export" (single, "Optional" chip). The separation is deliberate and commented: the agent contract has a list of `document_uris` and a single `ticket_uri`, so a file's kind is fixed by *which zone* received it, never inferred from the file.
- Validation order per spec: extension → per-file size → total size → file count → duplicate name. Every rejected file gets its own named error in a `role="alert"` container; nothing is silently dropped.
- Drop zones are keyboard operable (`role="button"`, `tabindex="0"`, Enter/Space opens the picker).
- "Documents only" warning appears (verbatim copy) when documents are selected and no ticket export is.
- Sample buttons populate fixtures. During upload: per-file percentage chip + per-file progress bar, then an "Uploaded" chip; failed uploads get a Retry with backoff (up to 3). "Start run" is disabled until at least one document is present.

### 3.4 Progress (the heart of the spec)
Built as the five stacked blocks the spec requires:
1. **Run header** — run id + status chip; elapsed `m:ss` in tabular numerals; overall progress bar (`role="progressbar"`, live `aria-valuenow`) whose width is the share of **active** (non-skipped) steps, so a documents-only run reaches 100%. Moving diagonal stripes while running.
2. **"Still working" notice** — appears when the current step exceeds `STALE_AFTER` (default 120s), with the verbatim reassurance copy; clears when the step completes.
3. **"Right now" panel** — the largest text; step number, plain-English label, description, an optional insight line (from the spec's `tip` column), and "Next: …". Turns green with "View outputs" on completion; red with a failing-step name, a collapsed technical detail, and "Resume run" / "Start another run" on failure. Square corners on the single-sided blue border.
4. **Step list** — three cards (Documents + Tickets side by side; Joining the results full width). Each row has a status dot (waiting/running/done/failed/skipped), label, sub-line (or skip reason), live/final duration, a shimmer on the running row, and a visually hidden state word so status is never colour-only.
5. **Activity feed** — the eight most recent events, newest first, including routing decisions with rule ids and reasons.

**Liveness (spec 3.4):** polls every 3s, backs off to 10s after 2 minutes, stops on a terminal status. A **local 1-second tick** advances the elapsed counter, the still-working timer and the running step's count-up *between* polls, so the screen is never frozen. Step transitions are announced via `aria-live="polite"`. A 10-minute no-change stall shows the warn note but does not mark the run failed.

### 3.5 Outputs (read-only)
Ordered most-convincing-first: run summary → "How each file was routed" (parser chip + rule id + reason, with the per-page-billing note) → "Landed in S3" (objects grouped by `uploads/ runs/ drafts/`, the 13 stage markers collapsed into one row, copy buttons, in-place previews, downloads disabled in mock) → "Landed in S3 Vectors" (index definition list, ingestion count chips, the authoritative-counts caveat, and chunk cards each with a collapsible "Show embedding" revealing 1024-dim + first 8 values) → "Ticket outputs" (intent table with volume bars + "Mixed topics" chips; coverage table with verdict chips), shown only when a ticket export was uploaded.

---

## 4. The 13 steps (spec section 4)

`ui.js` holds the step definitions keyed by the backend stage id, with the reader-facing label, the description, and the insight line for the six steps that have one. Groups: Tickets (`load_tickets`, `adapt_tickets`, `clean_tickets`, `embed_texts`, `intent_cluster`, `name_clusters`), Documents (`normalize_docs`, `kb_create`, `kb_ingest`, `kb_retrieve`), Joining the results (`coverage_check`, `draft_article`, `write_report`).

**Skip rules:** a documents-only run marks the six ticket steps plus `coverage_check` and `draft_article` as skipped with "No ticket export uploaded", from the moment the run starts (8 steps). The UI takes the skip set from the status response when it provides one, and falls back to these derived rules otherwise.

---

## 5. Mock mode (spec section 9)

`mock.js` implements the same function surface as `api.js` (`createUploads`, `startRun`, `getStatus`, `resumeRun`, `getOutputs`, `getVectors`, `getTicketOutputs`) plus sample-fixture and scenario/speed setters. A virtual clock (`performance.now()` × speed) advances a run through the 13 steps with per-step durations roughly proportional to the real ones (parsing and indexing are the long steps). Three scenarios: **Normal**, **Slow parse** (`normalize_docs` ×6 so the "still working" callout shows with margin), **Fails while indexing** (`kb_ingest` fails at 50%, resume continues to completion). Fixtures include a scanned PDF (`product-manual-scanned.pdf`) so the routing table shows both parsers; routing decisions are derived from fixture file names/sizes.

---

## 6. Accessibility (spec section 6)

Verified live under emulated dark mode + `prefers-reduced-motion: reduce`:
- Body-text contrast: muted-on-page 7.32:1 (light) / 8.51:1 (dark); ink 17.6 / 15.6; blue-on-white 4.64 — all ≥ 4.5:1.
- Drop zones keyboard-operable (`role=button`, `tabindex=0`, aria-label).
- Progress bar `role="progressbar"` + live `aria-valuenow`; `aria-live="polite"` region for transitions/terminal state; error containers `role="alert"`.
- Status never colour-only — every step row carries a visually hidden state word (13 present).
- Under reduced motion the spin, stripe and shimmer animations are disabled (animation-name resolves to `none`), while the elapsed counter and activity feed keep updating.
- One `h1` per screen; dark mode fully applied (body background is the dark `--page` token, not a half-inverted page).

---

## 7. Compliance with the build rules (spec section 0 + Definition of Done)

- **UI only** — no AWS code, no Lambda, no infrastructure.
- **Mock-first** — the entire UI is reviewable/demonstrable with no account, credentials or deployment.
- **No approval UI of any kind** — no approve/reject/rerun buttons, no "pending review" badges, no review-status columns. (The only `reject` in the code is a Promise reject callback; the only `role="alert"` is for upload errors.)
- **"customer", never "client"** in reader-facing copy. "client" appears only in approved technical senses: the Cognito "app client" (commented) and "the VPN client" inside a mock knowledge-chunk body.
- **No secrets** — no ARNs, account ids or credentials anywhere in `web/`. `REPLACE_ME_*` placeholders live only in `config.example.js`.
- **Verbatim copy strings** (spec section 8) used exactly where given.
- **Logo** — renders the supplied PNG when present, falls back to a text wordmark with no layout shift when absent.
- Every `TODO(confirm)` is logged in `OPEN_QUESTIONS.md` (entries P1–P7).

---

## 8. Logo

Using `Ingram_micro_logo.png` — a 791×157 **transparent** wide wordmark — copied to `web/assets/logo.png`. Displayed height-constrained (`height:26px; width:auto; max-width:180px`) so it reads as the blue "INGRAM micro" wordmark beside the divider and product name, matching the customer's site. Verified clean on both the white (light) and dark top bars — no white box. (The earlier square 840×859 white-background icon rendered as a tiny dot and showed a white box in dark mode; replaced. `logo-2.png` was not used because it is a solid-blue-background badge.)

---

## 9. Open questions for the reviewer (full detail in OPEN_QUESTIONS.md, P1–P7)

- **P1** — keep the screenshot-sampled palette, or swap in an official brand palette/font? (one swap point in `:root`.)
- **P2** — RESOLVED: transparent wordmark logo now used; clean in both themes.
- **P3** — ANSWERED from the agent code: `kb_retrieve` is **not** skipped in a normal run; it no-ops unless a golden question set is supplied. The UI treats it as a fast `done` by default and only shows it skipped if the status response says so.
- **P4** — per-run limits are the spec defaults (10 files / 50 MB each / 150 MB total; ticket 1 file / 50 MB) in `config.example.js`; needs confirmation from the cost owner.
- **P5** — "View an example run" is built as available **before** sign-in (fastest demo path); trivially gated later if required.
- **P6** — ANSWERED from observed run times: 60s `STALE_AFTER` is too low (real `normalize_docs` ran ~48–77s), set to 120s.
- **P7** — "Open in AWS console" deep-links beside S3 objects are **not** built; needs confirmation whether reviewers have console access.

---

## 10. Known caveats / things a reviewer should weigh in on

1. **Sign-in preview card shows two steps "complete."** This is per spec 3.2 (the decorative card previews the progress screen with two complete / one running / one waiting) and is `aria-hidden`. It can read as if a run is already underway before sign-in. Open decision: keep as-spec, or relabel the card (e.g. "Preview" / "Example run") so it is unmistakably an illustration. Not yet changed.
2. **`app.js` uses top-level `await import(...)`** to keep the real API/auth modules out of a pure mock review. Requires a modern, module-capable browser — fine for the demo, worth noting.
3. **No backend is wired.** `api.js`/`auth.js` target `REPLACE_ME_*` placeholders; the companion deployment spec (Lambda handlers, the API) was not part of this task. The AWS keys in `.env` are irrelevant to this UI.
4. **Nothing is committed yet** — all work is in the `kbagent-poc` working tree.
