#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
QUEUE_PYTHON="${QUEUE_PYTHON:-python3}"
SESSION="$("$QUEUE_PYTHON" -c 'from settings import load;print(load()["tmux_session"])')"
if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "tmux session already exists: $SESSION"
    exit 0
fi
printf -v COMMAND '%q -u %q run' "$QUEUE_PYTHON" "$PWD/server_agent.py"
if [[ "${1:-}" == '--execute' ]]; then
    COMMAND+=' --execute'
elif [[ $# -gt 0 ]]; then
    echo 'Usage: bash start-server.sh [--execute]' >&2
    exit 2
fi
tmux new-session -d -s "$SESSION" -n queue "$COMMAND"
echo "Started: $SESSION (Ctrl+B, D to detach)"
