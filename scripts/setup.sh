#!/bin/sh
# One-time setup for a fresh clone. Safe to re-run.
#   sh scripts/setup.sh
set -e
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed. Install it, then re-run this script:"
  echo "  Mac/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "  Windows:   powershell -ExecutionPolicy ByPass -c \"irm https://astral.sh/uv/install.ps1 | iex\""
  exit 1
fi

echo "==> Installing dependencies"
uv sync

echo "==> Enabling git hooks (blocks pushes to main, warns on cross-lane edits)"
git config core.hooksPath .githooks

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env from .env.example. Fill in OPENAI_API_KEY (or ANTHROPIC_API_KEY); get Twilio values from Lane B."
fi

echo "==> Running tests"
uv run pytest -q

echo "==> Your lane"
PY="$(command -v python3 || command -v python)"
if "$PY" scripts/lane.py whoami | grep -q "^Lane "; then
  "$PY" scripts/lane.py whoami
else
  echo "Couldn't tell which lane you own. Pin it with:  $PY scripts/lane.py set <A|B|C|D>"
fi

echo
echo "Done. Next: open Claude Code here and say 'Start Phase <your lane>1', or read docs/lanes/."
