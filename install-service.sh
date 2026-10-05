#!/bin/bash
# Install, restart or remove the launchd agent that keeps the bot running.
#
#   ./install-service.sh            install and start
#   ./install-service.sh restart    reload after a code change
#   ./install-service.sh stop       stop but keep it installed
#   ./install-service.sh uninstall  remove it entirely
#   ./install-service.sh status     is it running?

set -euo pipefail
cd "$(dirname "$0")"

LABEL="com.deccan.slack-intake"
AGENTS="$HOME/Library/LaunchAgents"
TARGET="$AGENTS/$LABEL.plist"
ACTION="${1:-install}"

case "$ACTION" in
  install)
    [ -f .env ] || { echo "No .env — copy .env.example and fill in the tokens first." >&2; exit 1; }
    chmod +x run.sh
    mkdir -p logs "$AGENTS"

    # The plist hard-codes this directory; rewrite it if the project has moved.
    sed "s|/Users/Dishagra/Downloads/slack-project-intake|$(pwd)|g" \
      "$LABEL.plist" > "$TARGET"

    launchctl unload "$TARGET" 2>/dev/null || true
    launchctl load "$TARGET"
    sleep 3
    echo "Installed. Logs: $(pwd)/logs/bot.log"
    tail -n 3 logs/bot.log 2>/dev/null || echo "(no output yet — check again in a moment)"
    ;;

  restart)
    launchctl unload "$TARGET" 2>/dev/null || true
    launchctl load "$TARGET"
    sleep 3
    tail -n 3 logs/bot.log 2>/dev/null
    ;;

  stop)
    launchctl unload "$TARGET" 2>/dev/null || true
    echo "Stopped. Start it again with: ./install-service.sh restart"
    ;;

  uninstall)
    launchctl unload "$TARGET" 2>/dev/null || true
    rm -f "$TARGET"
    echo "Removed. The bot will not start again on its own."
    ;;

  status)
    if launchctl list | grep -q "$LABEL"; then
      echo "launchd agent: loaded"
      launchctl list | grep "$LABEL" | awk '{print "  pid " $1 "   last exit " $2}'
    else
      echo "launchd agent: not loaded"
    fi
    if pgrep -f "app\.py" > /dev/null; then
      echo "bot process:   running (pid $(pgrep -f 'app\.py' | tr '\n' ' '))"
    else
      echo "bot process:   not running"
    fi
    tail -n 3 logs/bot.log 2>/dev/null || true
    ;;

  *)
    echo "Usage: $0 [install|restart|stop|uninstall|status]" >&2
    exit 1
    ;;
esac
