# Sample Pack Licences and Provenance

This file records the licence and source of **every open-source document** placed
in `sample_pack/documents/` for the knowledge-base build, and the provenance of
the synthetic tickets. Monday's deliverable names this file explicitly (spec 1.4).

> **Status:** real documents have been fetched into `documents/` from the
> Nextcloud user manual (CC BY 3.0). See the attribution and the per-file
> provenance table below. No document content was fabricated.

## Nextcloud documentation — CC BY 3.0 attribution

The files under `documents/` derived from Nextcloud are reproduced and adapted
under the **Creative Commons Attribution 3.0 Unported (CC BY 3.0)** licence:

- **Title:** Nextcloud User Manual
- **Author / attribution:** Nextcloud GmbH and the Nextcloud documentation
  contributors
- **Source:** <https://github.com/nextcloud/documentation> (directory
  `user_manual/`), published at <https://docs.nextcloud.com>
- **Licence:** CC BY 3.0 — full text at
  <https://creativecommons.org/licenses/by/3.0/legalcode>; summary at
  <https://creativecommons.org/licenses/by/3.0/>
- **Modifications:** the original reStructuredText was converted to Markdown /
  plain text / HTML, and two pages were rendered to image-only ("scanned") PDFs
  for test fixtures. These are modifications of the original work. Per-file source
  links are in the provenance table below.
- **No endorsement:** this attribution does not imply that Nextcloud endorses this
  project or its use of the material.

Licence checklist applied to each document before it went into the pack:

- [x] Licence permits redistribution within this demonstration pilot (CC BY 3.0).
- [x] Source URL recorded (per-file table below).
- [x] Any conversion (e.g. to scanned PDF) is noted.

## Tickets

- `tickets/tickets.csv`, `tickets/labels.csv`, `tickets/ticket_profile.json` are
  **synthetic**, produced by `generate_tickets.py` in this directory. They
  contain no real customer data. Reproducible from a fixed seed (spec 2.3).
- The hidden labels in `labels.csv` are ground truth for scoring only and are
  never read by `src/` (spec 2.4).

## Tooling / dependency licences of note

- **PyMuPDF** (a runtime dependency used for PDF feature extraction) is
  **AGPL-licensed**. Acceptability in a commercial deliverable is OPEN_QUESTIONS
  Q13 (owner: Engagement Lead / legal). The code keeps PDF extraction behind an
  interface so it can be swapped for `pypdf` + `pdfplumber` if AGPL is rejected.

## Fetched documents (added 2026-10-15)

| File | Source | Licence | Download date |
|------|--------|---------|---------------|
| `access_webgui.md` | https://github.com/nextcloud/documentation/blob/master/user_manual/files/access_webgui.rst | CC BY 3.0 | 2026-10-15 |
| `quota.md` | https://github.com/nextcloud/documentation/blob/master/user_manual/files/quota.rst | CC BY 3.0 | 2026-10-15 |
| `sharing.md` | https://github.com/nextcloud/documentation/blob/master/user_manual/files/sharing.rst | CC BY 3.0 | 2026-10-15 |
| `session_management.md` | https://github.com/nextcloud/documentation/blob/master/user_manual/session_management.rst | CC BY 3.0 | 2026-10-15 |
| `user_2fa.md` | https://github.com/nextcloud/documentation/blob/master/user_manual/user_2fa.rst | CC BY 3.0 | 2026-10-15 |
| `userpreferences.txt` | https://github.com/nextcloud/documentation/blob/master/user_manual/userpreferences.rst | CC BY 3.0 | 2026-10-15 |
| `installation.md` | https://github.com/nextcloud/documentation/blob/master/user_manual/desktop/installation.rst | CC BY 3.0 | 2026-10-15 |
| `usage.md` | https://github.com/nextcloud/documentation/blob/master/user_manual/desktop/usage.rst | CC BY 3.0 | 2026-10-15 |
| `conflicts.html` | https://github.com/nextcloud/documentation/blob/master/user_manual/desktop/conflicts.rst | CC BY 3.0 | 2026-10-15 |
| `faq.md` | https://github.com/nextcloud/documentation/blob/master/user_manual/desktop/faq.rst | CC BY 3.0 | 2026-10-15 |
| `sharing_scanned.pdf` | https://github.com/nextcloud/documentation/blob/master/user_manual/files/sharing.rst (rendered image-only PDF) | CC BY 3.0 | 2026-10-15 |
| `two_factor_scanned.pdf` | https://github.com/nextcloud/documentation/blob/master/user_manual/user_2fa.rst (rendered image-only PDF) | CC BY 3.0 | 2026-10-15 |
| `sharing_degraded_scan.pdf` | https://github.com/nextcloud/documentation/blob/master/user_manual/files/sharing.rst (rendered DEGRADED image-only PDF: low-res, noise, skew) | CC BY 3.0 | 2026-10-15 |

Rocket.Chat documentation was NOT bundled: its `docs` repo is archived with no declared licence, so redistribution rights are unclear. If a licensed Rocket.Chat source is confirmed, add it the same way.
