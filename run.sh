#!/bin/bash
# Start the intake bot with its environment loaded.
#
# Used both for running it by hand and by the launchd agent. Everything the bot
# needs comes from .env; nothing is baked in here.

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "No .env in $(pwd). Copy .env.example and fill in the tokens." >&2
  exit 1
fi

# shellcheck disable=SC1091
set -a && source .env && set +a

exec .venv/bin/python app.py
