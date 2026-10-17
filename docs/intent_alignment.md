# Intent / document alignment

status: approved — Option A, 2026-10-15 (reviewer-approved; conditions applied)

This document maps the 20 planted ticket intents to the Nextcloud documents that
answer them, with the **specific document and section** as evidence, and
designates exactly **3 deliberately undocumented intents** (gaps) — each proven
undocumented by grep over the fetched docs. The coverage check must flag these 3
and only these 3 (the "3 of 3" target, spec 1.5), and the false-positive rate is
measured over the 17 documented intents.

The fetched documents (CC BY 3.0, see `sample_pack/LICENSES.md`) are the Nextcloud
user-manual pages: `access_webgui`, `quota`, `sharing`, `session_management`,
`user_2fa`, `userpreferences`, and the desktop-client pages `installation`,
`usage`, `conflicts`, `faq`.

## 17 documented intents — evidence (doc + section)

| # | Intent | Document | Section / evidence |
|---|--------|----------|--------------------|
| 1 | access_web_interface | `access_webgui.md` | "Accessing your files using the Nextcloud web interface"; "Navigating your files" |
| 2 | file_details_sidebar | `access_webgui.md` | "Activity and comments" / details sidebar (Activity, sharing, version tabs) |
| 3 | file_versions_restore | `access_webgui.md`, `faq.md` | access_webgui "version history"; faq "Activity" pane references prior versions |
| 4 | storage_quota | `quota.md` | "Storage quota"; "how your quota is calculated" |
| 5 | share_public_link | `sharing.md` | "Public link shares"; "options … for public folder shares" |
| 6 | share_permissions | `sharing.md` | "File sharing" targets; per-share permission options |
| 7 | federated_sharing | `sharing.md` | "users or groups on federated Nextcloud servers" |
| 8 | share_via_files_page | `sharing.md` | "Go to your Files page and click the Share icon on the file or directory" |
| 9 | two_factor_setup | `user_2fa.md` | "Using two-factor authentication"; enabling a 2FA app |
| 10 | two_factor_methods | `user_2fa.md` | second-factor options (text message, generated code on device) |
| 11 | session_management | `session_management.md` | "Manage connected browsers and devices"; revoke access |
| 12 | user_preferences | `userpreferences.txt` | "Setting your preferences"; "access your personal settings" |
| 13 | notification_settings | `userpreferences.txt` | "Manage your Activity stream and notifications" |
| 14 | desktop_client_install | `installation.md` | "Installation"; download client; "Supported server versions" |
| 15 | desktop_add_account | `usage.md` | "Add account"; proxy settings; client certificate (PKCS#12) |
| 16 | sync_conflict | `conflicts.html` | "Conflicts"; "conflicted copy" file naming |
| 17 | edit_locally_protocol | `faq.md` | "How the Edit locally functionality works"; nc:// protocol handler |

## 3 deliberately undocumented intents (gaps) — grep evidence

Each was chosen as a real Nextcloud capability (provided via server apps) that is
**absent from every fetched document**. Commands run in `sample_pack/documents/`:

| # | Gap intent | grep (case-insensitive) | Result |
|---|-----------|--------------------------|--------|
| 18 | ldap_directory_integration | `grep -ric "ldap\|active directory"` | **0 hits** (the word "directory" appears only as file-system directory, not LDAP) |
| 19 | antivirus_scanning | `grep -ric "antivirus\|anti-virus\|malware\|virus\|clamav"` | **0 hits** |
| 20 | deck_kanban_boards | `grep -ric "deck\|kanban\|card board"` | **0 hits** |

> Note: the originally-proposed gaps (server-side encryption, external storage,
> activity feed) were REJECTED after grep showed they ARE mentioned in the fetched
> docs (`quota.md` encryption keys; `usage.md` external storage; `access_webgui.md`
> / `faq.md` / `userpreferences.txt` activity). A gap must have no answering
> passage, so they were replaced with the three above. This is condition 2 of the
> approval, applied.

Rationale for these three: each is a genuine Nextcloud feature with its own manual
page we did not fetch. A reviewer can later add that one page and the coverage
check should flip the intent from GAP to COVERED — demonstrating the KB_CHANGED
re-evaluation seam.

## Conditions applied (from the Option A approval)

1. **Evidence per intent** — the two tables above.
2. **Gaps proven undocumented** — grep evidence above; original proposed gaps
   rejected because they were mentioned.
3. **Gap ticket volume** — the generator plants >= `drafting.min_source_tickets`
   RESOLVED tickets for each of the 3 gap intents, so drafting can trigger for
   them. Verified by `tests/unit/test_sample_pack.py`.
4. **Customer-language wording** — ticket problem text is written as customer
   complaints, deliberately NOT copied from doc headings, so retrieval/coverage
   are not flattered. (The documented-intent tickets avoid the doc section titles.)
5. **Both packs** — `generate_tickets.py` and `generate_tickets_llm.py` use this
   intent set (the latter imports `INTENTS`).

## How `make eval` uses this file

`make eval` reads this file; the planted-gaps (3 of 3) and coverage-FP scorers run
only when `status: approved` appears here. The three GAP intents are identified by
the `GAP_INTENTS` set in `sample_pack/generate_tickets.py` (kept in sync with
table rows 18-20).
