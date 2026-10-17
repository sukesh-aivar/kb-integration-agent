"""Sample-pack ticket generator (spec 1.4 Monday deliverable, build-order step 2).

Produces a reproducible synthetic ticket export for the knowledge-base
integration pipeline to run against. The pack validates the *pipeline*, not
real-world quality - every number this feeds is a synthetic-data measurement.

What it generates:

* ~1,500 tickets across **20 intents** (configurable), ~75 per intent.
* A customer-facing ``tickets.csv`` that contains **no intent labels**.
* A separate ``labels.csv`` keyed by ``ticket_id`` holding the hidden
  ``seeded_intent`` - ground truth used ONLY to score clustering (spec 2.4).
  Keeping it in a separate file is the safe design: nothing in ``src/`` reads it,
  and it can never ride into a prompt or embedding by accident.
* Seeded edge cases, each tagged in ``labels.csv`` (not in ``tickets.csv``) so a
  test can assert the pipeline caught them:
    - PII rows (email / phone / card-like / national-id in the body)
    - non-English rows
    - exact duplicate rows
    - no-resolution rows (unresolved tickets)
* A 70/30 **time split**: 70% of tickets fall in an "older" window and 30% in a
  "newer" window. TODO(confirm) Q4 - the manager's intended meaning of the split
  (older/newer to simulate coverage gaps vs a KB change) is unconfirmed; this
  generator implements a simple older/newer timestamp split and records it in
  ``ticket_profile.json`` so the interpretation is explicit and swappable.

Reproducibility (spec 2.3): all randomness is driven by a single seed (default
from ``config/default.yaml``). Same seed + same parameters -> byte-identical pack.

NOTE: Ticket *text* here is templated synthetic prose, not Claude-generated. The
spec's Monday plan mentions Claude-generated tickets; generating them offline
with templates keeps ``make run``/tests free of an LLM call and credentials while
still planting 20 separable intents with known labels. This deviation is recorded
in OPEN_QUESTIONS / the stop-point summary.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

# --- Intent catalogue --------------------------------------------------------
# 20 Nextcloud-aligned intents (see docs/intent_alignment.md, reviewer-approved
# Option A). 17 are answered by the fetched Nextcloud docs; 3 are DELIBERATE GAPS
# (LDAP, antivirus, Deck) proven undocumented by grep.
#
# Ticket wording is written as a CUSTOMER would phrase a complaint - deliberately
# NOT copied from the doc section headings - so retrieval/coverage are not
# flattered (approval condition 4). Each intent still has distinctive vocabulary
# so density clustering can rediscover it. These labels are ground truth.


@dataclass(frozen=True)
class IntentSpec:
    """A planted intent: its hidden label plus phrase fragments to vary text."""

    label: str
    subjects: tuple[str, ...]
    problems: tuple[str, ...]
    resolutions: tuple[str, ...]


# The three intents with NO answering document (coverage must flag exactly these).
GAP_INTENTS: frozenset[str] = frozenset(
    {"ldap_directory_integration", "antivirus_scanning", "deck_kanban_boards"}
)


INTENTS: tuple[IntentSpec, ...] = (
    # ---- documented intents (17) ----
    IntentSpec(
        "access_web_interface",
        ("Can't open the web page", "Website won't load my files", "Browser login issue"),
        (
            "I type the address but the page just spins and I never see my files.",
            "When I log in on the browser none of my folders show up on screen.",
            "The web page loads but I cannot find where my uploaded files went.",
        ),
        (
            "Walked the customer to the Files view in the web interface and confirmed access.",
            "Cleared the browser cache which restored the file list on the web page.",
            "Confirmed the correct server address and the customer could browse files again.",
        ),
    ),
    IntentSpec(
        "file_details_sidebar",
        ("Where are comments", "Can't see file details", "Missing the side panel"),
        (
            "I want to leave a comment on a file but I cannot find where to do that.",
            "There used to be a panel on the right showing details and I lost it.",
            "How do I see who did what to a file and add a note for my team?",
        ),
        (
            "Showed the customer the details sidebar and its Activity and comments tab.",
            "Re-enabled the details panel and the comment box reappeared for the customer.",
            "Explained the per-file activity log and how to add comments.",
        ),
    ),
    IntentSpec(
        "file_versions_restore",
        ("Need an old version", "Overwrote my file", "Recover previous file"),
        (
            "I saved over my document by mistake and need yesterday's version back.",
            "Is there any way to get an earlier copy of a file I changed?",
            "My spreadsheet changed and I want to roll it back to last week.",
        ),
        (
            "Restored the earlier file version from the version history for the customer.",
            "Pointed the customer to version history and they recovered the older copy.",
            "Rolled the file back to the requested prior version.",
        ),
    ),
    IntentSpec(
        "storage_quota",
        ("Running out of space", "Quota almost full", "No room to upload"),
        (
            "It says I am almost out of space but I am not sure what is using it.",
            "I keep getting a warning that my storage is nearly full.",
            "I cannot upload anything new because my account says it is full.",
        ),
        (
            "Explained how the quota is calculated and which data counts against it.",
            "Asked the admin to raise the storage quota for the customer's account.",
            "Showed the customer the quota indicator on their personal settings page.",
        ),
    ),
    IntentSpec(
        "share_public_link",
        ("Public link not working", "Send a link to someone", "Link share options"),
        (
            "I want to send a folder to someone outside the company with a link.",
            "The link I shared asks my colleague to log in and it should not.",
            "Can I put a password and an expiry on a link I send to a customer?",
        ),
        (
            "Created a public link share with a password and expiry and sent it over.",
            "Enabled public link sharing options for the folder and tested the link.",
            "Set the public folder share permissions the customer wanted.",
        ),
    ),
    IntentSpec(
        "share_permissions",
        ("Wrong share rights", "Can't give edit access", "Share is read only"),
        (
            "I shared a folder but my teammate can only view, not edit it.",
            "I need to let one person edit and everyone else just read.",
            "A colleague says they cannot upload into the folder I shared.",
        ),
        (
            "Adjusted the per-share permissions so the collaborator could edit.",
            "Granted edit and upload rights on the shared folder for the named user.",
            "Corrected the share's permission level for the customer's team.",
        ),
    ),
    IntentSpec(
        "federated_sharing",
        ("Share to another server", "Cross-server share fails", "Remote Nextcloud share"),
        (
            "I want to share a folder with someone on a different Nextcloud server.",
            "My share to a partner's own server never reaches them.",
            "How do I send files to a user whose account is on another server?",
        ),
        (
            "Set up a federated share to the remote Nextcloud server for the customer.",
            "Confirmed the remote server address and the federated share went through.",
            "Explained sharing with users on federated Nextcloud servers.",
        ),
    ),
    IntentSpec(
        "share_via_files_page",
        ("How do I share", "Where is the share button", "Start a share"),
        (
            "I have a file open and I just want to know how to share it.",
            "Where is the icon I press to share a single document?",
            "I cannot work out how to begin sharing a folder from my files.",
        ),
        (
            "Showed the customer the Share icon on the file in their Files page.",
            "Walked through starting a share from the file's share icon.",
            "Pointed out the share control next to the file in the Files view.",
        ),
    ),
    IntentSpec(
        "two_factor_setup",
        ("Turn on 2FA", "Set up two-factor", "Extra login security"),
        (
            "I want to add a second step to my login for more security.",
            "How do I switch on two-factor on my account?",
            "My company asked me to enable two-factor and I do not know how.",
        ),
        (
            "Guided the customer through enabling the two-factor authentication app.",
            "Confirmed the admin had enabled 2FA, then set it up on the account.",
            "Enabled two-factor authentication and verified the second step worked.",
        ),
    ),
    IntentSpec(
        "two_factor_methods",
        ("2FA code options", "Which second factor", "Text or app code"),
        (
            "Can I get my second-step code by text instead of an app?",
            "What are my options for the second factor at login?",
            "I would rather generate a code on my phone than receive a message.",
        ),
        (
            "Explained the available second factors: a texted code or a device-generated code.",
            "Switched the customer to a device-generated code as their second factor.",
            "Set up the text-message second factor the customer preferred.",
        ),
    ),
    IntentSpec(
        "session_management",
        ("Log out other devices", "Unknown device logged in", "Revoke a session"),
        (
            "I think an old laptop is still signed in and I want to kick it off.",
            "I see a device I do not recognise connected to my account.",
            "How do I sign out everywhere except the computer I am on now?",
        ),
        (
            "Showed the customer the connected devices list and revoked the stale session.",
            "Revoked the unrecognised device's access from the security settings.",
            "Walked the customer through revoking individual device sessions.",
        ),
    ),
    IntentSpec(
        "user_preferences",
        ("Change my settings", "Where are my settings", "Update my profile"),
        (
            "Where do I go to change my personal account settings?",
            "I want to update my display name and profile details.",
            "I cannot find the page with all my personal options.",
        ),
        (
            "Directed the customer to their personal settings page.",
            "Showed where to edit profile details in personal settings.",
            "Walked the customer to the personal settings to update their preferences.",
        ),
    ),
    IntentSpec(
        "notification_settings",
        ("Too many emails", "Turn off alerts", "Change notifications"),
        (
            "I am getting far too many email alerts and want to dial them down.",
            "How do I stop being notified every time someone touches a file?",
            "I want to change which activities send me a notification.",
        ),
        (
            "Showed the customer how to manage their activity stream and notifications.",
            "Adjusted the notification preferences to reduce the email volume.",
            "Walked through the notification settings in personal preferences.",
        ),
    ),
    IntentSpec(
        "desktop_client_install",
        ("Install the desktop app", "Where to download client", "Set up sync app"),
        (
            "I need the desktop syncing program but I am not sure where to get it.",
            "How do I install the client on my Windows machine?",
            "I want the app that keeps my folders in sync on my laptop.",
        ),
        (
            "Pointed the customer to the desktop client download and confirmed install.",
            "Confirmed their server version was supported and installed the client.",
            "Walked the customer through downloading and installing the sync client.",
        ),
    ),
    IntentSpec(
        "desktop_add_account",
        ("Connect my account", "Client won't connect", "Add account to app"),
        (
            "I installed the app but it will not connect to my account.",
            "The desktop program keeps asking for a certificate I do not have.",
            "How do I add my work account to the sync client behind our proxy?",
        ),
        (
            "Added the account in the client and set the proxy settings for the customer.",
            "Supplied the client certificate the customer needed to connect.",
            "Walked through adding the account and completing the connection wizard.",
        ),
    ),
    IntentSpec(
        "sync_conflict",
        ("Conflicted copy files", "Two versions of my file", "Sync conflict"),
        (
            "I keep seeing files named 'conflicted copy' and do not know which to keep.",
            "The same document edited in two places made two copies on my computer.",
            "My client says there is a conflict and left me with two files.",
        ),
        (
            "Explained how conflict files are created and which copy to keep.",
            "Helped the customer merge the conflicted copy back into the original.",
            "Resolved the sync conflict and removed the duplicate copy.",
        ),
    ),
    IntentSpec(
        "edit_locally_protocol",
        ("Edit locally broken", "nc:// link does nothing", "Open in desktop app"),
        (
            "When I click 'edit locally' nothing opens on my computer.",
            "The button that should open a file in my desktop app does not work.",
            "Clicking edit-locally gives an error about a protocol handler.",
        ),
        (
            "Registered the nc:// protocol handler so edit-locally opened the app.",
            "Reinstalled with the MSI so the edit-locally handler worked.",
            "Walked the customer through enabling the edit-locally functionality.",
        ),
    ),
    # ---- deliberate GAPS (3) - NO answering document (see intent_alignment.md) ----
    IntentSpec(
        "ldap_directory_integration",
        ("Connect to LDAP", "Active Directory login", "Corporate directory accounts"),
        (
            "We want our staff to sign in with their existing company directory accounts.",
            "Can the system pull user accounts from our Active Directory?",
            "How do I connect our LDAP server so people do not need new logins?",
        ),
        (
            "Configured the directory integration so staff sign in with company accounts.",
            "Connected the corporate directory and mapped the user attributes.",
            "Enabled directory-based login for the customer's organisation.",
        ),
    ),
    IntentSpec(
        "antivirus_scanning",
        ("Scan uploads for viruses", "Malware check on files", "Antivirus for storage"),
        (
            "Can uploaded files be scanned for viruses before they are stored?",
            "We need malware checking on everything our users upload.",
            "Is there a way to block infected files from being saved?",
        ),
        (
            "Enabled virus scanning on uploads for the customer's deployment.",
            "Configured the malware scanner to check files on upload.",
            "Set up antivirus scanning so infected uploads are blocked.",
        ),
    ),
    IntentSpec(
        "deck_kanban_boards",
        ("Set up a task board", "Kanban boards", "Project cards"),
        (
            "I want to organise my team's tasks on a board with cards and columns.",
            "Is there a kanban-style board for tracking our project work?",
            "How do we create task cards and move them across columns?",
        ),
        (
            "Set up the board app so the team could create cards and columns.",
            "Enabled the kanban boards feature and created the first board.",
            "Walked the customer through adding cards to their task board.",
        ),
    ),
)

# --- Edge-case content -------------------------------------------------------
# PII strings are synthetic and clearly fake (reserved ranges / example domains).

PII_SNIPPETS: tuple[str, ...] = (
    "You can reach me at jane.doe@example.com for a faster reply.",
    "My direct number is +1-202-555-0147 if you need to call.",
    "I paid with card 4111 1111 1111 1111, please check that transaction.",
    "My national ID is AB-123456-C in case you need to verify me.",
)

NON_ENGLISH_BODIES: tuple[str, ...] = (
    "Bonjour, je ne peux pas me connecter a mon compte depuis ce matin.",
    "Hola, la aplicacion se cierra sola cada vez que la abro.",
    "Guten Tag, meine Dateien werden nicht mehr synchronisiert.",
    "Buongiorno, non riesco a reimpostare la mia password.",
)

# Lexical-variety fillers. These add surface variation to each ticket (so the
# pack is ~1,500 UNIQUE tickets, not a handful of repeated strings) WITHOUT adding
# cross-intent vocabulary - the intent-specific subject/problem phrases still
# dominate, so density clustering can still recover the planted intents.
_OPENERS: tuple[str, ...] = (
    "Hi team,",
    "Hello,",
    "Good morning,",
    "Hi support,",
    "Hey,",
    "To whom it may concern,",
    "",
)
_CONTEXT: tuple[str, ...] = (
    "This started earlier today.",
    "It has been happening since yesterday.",
    "This is urgent for our team.",
    "I am on the latest version.",
    "I have already tried restarting.",
    "A colleague sees the same thing.",
    "This worked fine last week.",
    "I am not sure what changed.",
    "",
)
_CLOSERS: tuple[str, ...] = (
    "Please help as soon as you can.",
    "Any guidance would be appreciated.",
    "Let me know what you need from me.",
    "Thanks in advance.",
    "Could you advise?",
    "What should I do next?",
    "",
)


# --- Generation --------------------------------------------------------------


def _load_seed_from_config() -> int:
    """Read the reproducibility seed from config/default.yaml, defaulting to 42.

    Imported lazily and defensively so the generator still runs if executed in an
    environment where the package import path is not set up.
    """
    try:
        import sys

        repo_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(repo_root / "src"))
        from kb_agent.config import load_config

        return load_config().seed
    except Exception:  # pragma: no cover - defensive fallback
        return 42


@dataclass
class GeneratedTicket:
    ticket_id: str
    subject: str
    description: str
    resolution: str | None
    status: str
    created_at: datetime
    resolved_at: datetime | None
    tags: list[str]
    source_platform: str
    # ground truth + edge-case flags (go to labels.csv ONLY)
    seeded_intent: str
    edge_case: str  # "" | "pii" | "non_english" | "duplicate" | "no_resolution"


def generate(
    per_intent: int = 75,
    seed: int | None = None,
    now: datetime | None = None,
) -> list[GeneratedTicket]:
    """Generate the full synthetic ticket list (not yet written to disk).

    Args:
        per_intent: Target tickets per intent (20 x 75 = 1500 by default).
        seed: Reproducibility seed; falls back to config, then 42.
        now: Reference "current" time; defaults to a fixed instant so timestamps
            are reproducible across machines.

    Returns:
        A list of :class:`GeneratedTicket`, order shuffled deterministically.
    """
    rng = random.Random(seed if seed is not None else _load_seed_from_config())
    # Fixed reference time so the pack is byte-reproducible regardless of wall clock.
    now = now or datetime(2026, 1, 1, tzinfo=UTC)

    # 70/30 time split (spec 1.4; interpretation TODO(confirm) Q4).
    # "older" window: 365-120 days ago; "newer" window: last 90 days.
    older_start, older_end = now - timedelta(days=365), now - timedelta(days=120)
    newer_start, newer_end = now - timedelta(days=90), now - timedelta(days=1)

    tickets: list[GeneratedTicket] = []
    counter = 0

    def _rand_dt(start: datetime, end: datetime) -> datetime:
        span = int((end - start).total_seconds())
        return start + timedelta(seconds=rng.randint(0, span))

    for intent in INTENTS:
        for _ in range(per_intent):
            counter += 1
            tid = f"T{counter:05d}"

            subject = rng.choice(intent.subjects)
            # Compose the body with varied filler so tickets are distinct while
            # the intent-specific problem phrase still carries the signal.
            core = rng.choice(intent.problems)
            parts = [rng.choice(_OPENERS), core, rng.choice(_CONTEXT), rng.choice(_CLOSERS)]
            description = " ".join(p for p in parts if p).strip()
            edge_case = ""

            # ~70% older, ~30% newer.
            if rng.random() < 0.70:
                created = _rand_dt(older_start, older_end)
            else:
                created = _rand_dt(newer_start, newer_end)

            resolved = True
            # Seed no-resolution rows (~6%): unresolved tickets.
            if rng.random() < 0.06:
                edge_case = "no_resolution"
                resolution = None
                status = rng.choice(["open", "pending"])
                resolved_at = None
                resolved = False
            else:
                resolution = rng.choice(intent.resolutions)
                status = rng.choice(["solved", "closed"])
                resolved_at = created + timedelta(hours=rng.randint(1, 72))

            # Seed PII rows (~4%): inject a fake-PII sentence into the body.
            if not edge_case and rng.random() < 0.04:
                edge_case = "pii"
                description = f"{description} {rng.choice(PII_SNIPPETS)}"

            # Seed non-English rows (~3%): replace the body with non-English text.
            if not edge_case and rng.random() < 0.03:
                edge_case = "non_english"
                description = rng.choice(NON_ENGLISH_BODIES)

            tickets.append(
                GeneratedTicket(
                    ticket_id=tid,
                    subject=subject,
                    description=description,
                    resolution=resolution,
                    status=status,
                    created_at=created,
                    resolved_at=resolved_at,
                    tags=[intent.label.split("_")[0]],
                    source_platform="sample",
                    seeded_intent=intent.label,
                    edge_case=edge_case,
                )
            )
            _ = resolved  # readability only

    # Seed exact-duplicate rows (~1.5%): copy existing tickets with new IDs.
    n_dupes = max(1, int(len(tickets) * 0.015))
    for _ in range(n_dupes):
        original = rng.choice(tickets)
        counter += 1
        dup = GeneratedTicket(
            ticket_id=f"T{counter:05d}",
            subject=original.subject,
            description=original.description,
            resolution=original.resolution,
            status=original.status,
            created_at=original.created_at,
            resolved_at=original.resolved_at,
            tags=list(original.tags),
            source_platform="sample",
            seeded_intent=original.seeded_intent,
            edge_case="duplicate",
        )
        tickets.append(dup)

    rng.shuffle(tickets)
    return tickets


# --- Writers -----------------------------------------------------------------

# Customer-facing columns ONLY. Deliberately excludes seeded_intent/edge_case so
# ground truth cannot leak into the pipeline via the data file (spec 2.4).
TICKET_COLUMNS = (
    "ticket_id",
    "subject",
    "description",
    "resolution",
    "status",
    "created_at",
    "resolved_at",
    "tags",
    "source_platform",
)


def write_tickets_csv(tickets: list[GeneratedTicket], path: Path) -> None:
    """Write the customer-facing ticket export (no labels)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(TICKET_COLUMNS)
        for t in tickets:
            writer.writerow(
                [
                    t.ticket_id,
                    t.subject,
                    t.description,
                    t.resolution if t.resolution is not None else "",
                    t.status,
                    t.created_at.isoformat(),
                    t.resolved_at.isoformat() if t.resolved_at else "",
                    "|".join(t.tags),
                    t.source_platform,
                ]
            )


def write_labels_csv(tickets: list[GeneratedTicket], path: Path) -> None:
    """Write the hidden ground-truth labels, keyed by ticket_id (spec 2.4).

    This file is read ONLY by evals/tests, never by ``src/``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ticket_id", "seeded_intent", "edge_case"])
        for t in tickets:
            writer.writerow([t.ticket_id, t.seeded_intent, t.edge_case])


def write_ticket_profile(tickets: list[GeneratedTicket], path: Path, seed: int) -> None:
    """Write ``ticket_profile.json`` describing the pack (Monday deliverable)."""
    edge_counts: dict[str, int] = {}
    intent_counts: dict[str, int] = {}
    older = newer = 0
    cutoff = datetime(2026, 1, 1, tzinfo=UTC) - timedelta(days=100)
    for t in tickets:
        edge_counts[t.edge_case or "none"] = edge_counts.get(t.edge_case or "none", 0) + 1
        intent_counts[t.seeded_intent] = intent_counts.get(t.seeded_intent, 0) + 1
        if t.created_at < cutoff:
            older += 1
        else:
            newer += 1
    profile = {
        "schema": "ticket_profile v0.1",
        "total_tickets": len(tickets),
        "intent_count": len(intent_counts),
        "per_intent_counts": intent_counts,
        "edge_case_counts": edge_counts,
        "time_split": {
            "note": "70/30 older/newer. Interpretation is TODO(confirm) Q4.",
            "older": older,
            "newer": newer,
        },
        "seed": seed,
        "columns": list(TICKET_COLUMNS),
        "labels_file": "labels.csv (hidden ground truth, never read by src/)",
        "synthetic": True,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic sample pack.")
    parser.add_argument("--per-intent", type=int, default=75)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "tickets",
        help="Output directory for tickets.csv / labels.csv / ticket_profile.json",
    )
    args = parser.parse_args()

    seed = args.seed if args.seed is not None else _load_seed_from_config()
    tickets = generate(per_intent=args.per_intent, seed=seed)

    write_tickets_csv(tickets, args.out / "tickets.csv")
    write_labels_csv(tickets, args.out / "labels.csv")
    write_ticket_profile(tickets, args.out / "ticket_profile.json", seed)

    print(
        f"Generated {len(tickets)} tickets across {len(INTENTS)} intents "
        f"(seed={seed}) -> {args.out}"
    )


if __name__ == "__main__":
    main()
