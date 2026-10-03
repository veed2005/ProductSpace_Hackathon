#!/usr/bin/env python3
"""Who owns what. Standard library only, so it runs before `uv sync`.

  python3 scripts/lane.py whoami          # print your lane
  python3 scripts/lane.py set B           # pin your lane on this machine (stored in .git/, not committed)
  python3 scripts/lane.py owner <path>    # which lane owns a file
  python3 scripts/lane.py check           # list files on this branch outside your lane (vs origin/main)
  python3 scripts/lane.py session-start   # context for Claude Code (used by .claude/settings.json)
  python3 scripts/lane.py pre-push        # git pre-push hook (used by .githooks/pre-push)
"""

import fnmatch
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LANES = {
    "A": {
        "name": "Conversation brain",
        "owner": "aryavsaigal",
        "plan": "docs/lanes/LANE_A.md",
        "paths": [
            "app/core/turn.py", "app/core/router.py", "app/core/style.py", "app/core/referrals*",
            "app/engines/form_engine.py", "app/engines/conversation*.py",
            "app/llm/prompts/conversation*", "scripts/simulate.py", "tests/test_brain_*",
        ],
    },
    "B": {
        "name": "Channels + identity",
        "owner": "Edoubek1024",
        "plan": "docs/lanes/LANE_B.md",
        "paths": [
            "app/channels/*", "app/core/identity.py", "app/reminders.py", "tests/test_channels_*",
            "tests/test_identity_*", "tests/test_reminders_*",
        ],
    },
    "C": {
        "name": "Forms, PDFs, documents",
        "owner": "luisNava111",
        "plan": "docs/lanes/LANE_C.md",
        "paths": [
            "app/engines/form_library.py", "app/engines/document_engine.py", "app/engines/ingest.py",
            "app/pdf/*", "forms/*", "scripts/ingest_form.py", "scripts/inspect_pdf.py",
            "scripts/make_sample_form.py", "app/llm/prompts/document*", "app/llm/prompts/ingest*",
            "tests/test_forms_*", "tests/test_docs_*", "tests/test_pdf_*", "tests/fixtures/documents/*",
        ],
    },
    "D": {
        "name": "Memory, dashboard, metrics, demo",
        "owner": "veed2005",
        "plan": "docs/lanes/LANE_D.md",
        "paths": [
            "app/memory/*", "app/engines/status_engine.py", "app/dashboard/*", "app/metrics.py",
            "scripts/seed_demo.py", "scripts/reset_demo.py", "docs/PRIVACY.md", "docs/DEMO_SCRIPT.md",
            "tests/test_memory_*", "tests/test_dashboard_*", "tests/test_metrics_*",
        ],
    },
}

# Announce in the team chat before editing; keep changes small and additive.
SHARED = [
    "app/contracts.py", "app/models.py", "app/events.py", "app/llm/client.py", "app/config.py",
    "app/main.py", "app/db.py", "pyproject.toml", "uv.lock", ".env.example", "tests/conftest.py",
    "tests/test_integration.py", "CLAUDE.md", "docs/TEAM.md", "docs/PLAN.md", "scripts/lane.py",
]

LANE_FILE = ROOT / ".git" / "formline-lane"


def _run(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def owner_of(path: str) -> str:
    """'A'..'D', 'shared', or 'unowned'."""
    if any(fnmatch.fnmatch(path, p) for p in SHARED):
        return "shared"
    for lane, info in LANES.items():
        if any(fnmatch.fnmatch(path, p) for p in info["paths"]):
            return lane
    return "unowned"


def whoami() -> tuple[str | None, str]:
    """(lane, how we know). Order: env var, pinned file, GitHub login, git config."""
    env = os.environ.get("FORMLINE_LANE", "").strip().upper()
    if env in LANES:
        return env, "FORMLINE_LANE env var"
    if LANE_FILE.exists():
        pinned = LANE_FILE.read_text().strip().upper()
        if pinned in LANES:
            return pinned, "pinned with scripts/lane.py set"
    candidates = [
        (_run("gh", "api", "user", "--jq", ".login"), "GitHub login"),
        (_run("git", "config", "github.user"), "git config github.user"),
        (_run("git", "config", "user.name"), "git config user.name"),
        (_run("git", "config", "user.email").split("@")[0], "git config user.email"),
    ]
    for value, how in candidates:
        for lane, info in LANES.items():
            if value and value.lower() == info["owner"].lower():
                return lane, f"{how} = {value}"
    return None, "could not match your GitHub login or git config to a lane owner"


def changed_files() -> list[str]:
    _run("git", "fetch", "-q", "origin", "main")
    base = _run("git", "merge-base", "HEAD", "origin/main")
    out = _run("git", "diff", "--name-only", f"{base}..HEAD") if base else ""
    return [f for f in out.splitlines() if f]


def outside_lane(lane: str, files: list[str]) -> tuple[list[str], list[str]]:
    """(files owned by other lanes, shared files)."""
    other, shared = [], []
    for f in files:
        o = owner_of(f)
        if o == "shared":
            shared.append(f)
        elif o in LANES and o != lane:
            other.append(f"{f} (lane {o})")
    return other, shared


def cmd_session_start() -> None:
    # Make sure the repo's git hooks are active for this clone.
    if _run("git", "config", "core.hooksPath") != ".githooks":
        _run("git", "config", "core.hooksPath", ".githooks")
    lane, how = whoami()
    if lane is None:
        print(
            "FORMLINE LANE: unknown (" + how + "). Before changing any code, ask the user which lane "
            "they own (A brain / B channels+identity / C forms+PDFs+documents / D memory+dashboard+demo), "
            "then run `python3 scripts/lane.py set <LANE>`."
        )
        return
    info = LANES[lane]
    branch = _run("git", "rev-parse", "--abbrev-ref", "HEAD")
    print(
        f"FORMLINE LANE: this user is {info['owner']}, owner of Lane {lane} ({info['name']}); detected via {how}.\n"
        f"Read {info['plan']} and work through its phases in order. Edit only Lane {lane} files: "
        f"{', '.join(info['paths'])}. Shared files need a heads-up to the team (see docs/TEAM.md). "
        f"Files owned by other lanes: don't edit them; tell the user what the other lane needs to change.\n"
        f"Current branch: {branch}. Never commit directly to main; work on a branch named "
        f"{lane.lower()}/<task>."
    )


def cmd_pre_push(stdin: str) -> int:
    for line in stdin.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[2] == "refs/heads/main" and not os.environ.get("ALLOW_MAIN_PUSH"):
            print("\n✋ Don't push straight to main. Push your branch and open a PR:\n"
                  "   git push -u origin HEAD && gh pr create --fill\n", file=sys.stderr)
            return 1
    lane, _ = whoami()
    if lane:
        other, shared = outside_lane(lane, changed_files())
        if other:
            print(f"\n⚠️  This branch edits files owned by other lanes (you're Lane {lane}):", file=sys.stderr)
            for f in other:
                print(f"   {f}", file=sys.stderr)
            print("   Get that lane owner's OK on the PR.\n", file=sys.stderr)
        if shared:
            print("\nℹ️  Shared files changed; post a heads-up in the team chat: " + ", ".join(shared) + "\n",
                  file=sys.stderr)
    return 0


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "whoami"
    if cmd == "whoami":
        lane, how = whoami()
        print(f"Lane {lane}: {LANES[lane]['name']} ({how}). Plan: {LANES[lane]['plan']}" if lane else how)
    elif cmd == "set" and len(args) == 2 and args[1].upper() in LANES:
        LANE_FILE.write_text(args[1].upper() + "\n")
        print(f"Pinned this clone to Lane {args[1].upper()}.")
    elif cmd == "owner" and len(args) == 2:
        print(owner_of(args[1]))
    elif cmd == "check":
        lane, how = whoami()
        if not lane:
            print(how)
            return 1
        other, shared = outside_lane(lane, changed_files())
        print("Other lanes' files:", *(other or ["none"]), sep="\n  ")
        print("Shared files:", *(shared or ["none"]), sep="\n  ")
    elif cmd == "session-start":
        cmd_session_start()
    elif cmd == "pre-push":
        return cmd_pre_push(sys.stdin.read())
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
