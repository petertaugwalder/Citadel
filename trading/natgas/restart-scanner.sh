#!/bin/bash
# restart-scanner.sh - find and restart the desk's commodity scanner.
#
# Paste-safe: no placeholders to fill in, so nothing here can be eaten by the
# shell as a redirection. Works under bash and zsh.
#
#   ./restart-scanner.sh              # restart the scanner
#   ./restart-scanner.sh --status     # say what is running, change nothing
#   ./restart-scanner.sh --stop       # stop it and leave it stopped
#
# It prefers launchd when a LaunchAgent references the script, because a
# hand-started process dies with its terminal and launchd's does not.
#
# Override the defaults with environment variables:
#   SCANNER_JS=other-scanner.js SCANNER_ARGS="--quiet --verbose" ./restart-scanner.sh

set -uo pipefail

SCANNER_JS="${SCANNER_JS:-commodity-scanner.js}"
SCANNER_ARGS="${SCANNER_ARGS:---quiet}"
LOG_DIR="${SCANNER_LOG_DIR:-$HOME/Library/Logs}"
AGENT_DIR="$HOME/Library/LaunchAgents"
SEARCH_ROOTS=(
  "$HOME/Energy&Commodity Desk"
  "$HOME/Energy&Commodity Desk/pipeline"
  "$HOME/Desktop"
  "$HOME/Documents"
  "$HOME/src"
  "$HOME/code"
)

say()  { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- what is running right now ------------------------------------------------
scanner_pids() {
  # Two conditions, both required: the command line mentions the scanner file AND
  # the executable really is node. Matching the command line alone would also catch
  # this script, an editor with the file open, or a tail on its log.
  local pid comm out=""
  for pid in $(pgrep -f "$SCANNER_JS" 2>/dev/null); do
    [ "$pid" = "$$" ] && continue
    [ "$pid" = "$PPID" ] && continue
    comm=$(ps -o comm= -p "$pid" 2>/dev/null)
    case "$comm" in
      node|*/node) out="$out $pid" ;;
    esac
  done
  printf '%s' "${out# }"
}

describe_pids() {
  local pids="$1"
  [ -z "$pids" ] && { say "  not running"; return; }
  # shellcheck disable=SC2086
  ps -o pid=,lstart=,command= -p $pids 2>/dev/null | sed 's/^/  /'
}

# --- where the script lives ---------------------------------------------------
find_scanner() {
  local root hit
  for root in "${SEARCH_ROOTS[@]}"; do
    [ -d "$root" ] || continue
    hit=$(find "$root" -maxdepth 4 -name "$SCANNER_JS" -not -path '*/node_modules/*' -print -quit 2>/dev/null)
    [ -n "$hit" ] && { printf '%s\n' "$hit"; return 0; }
  done
  # last resort: the whole home directory, still skipping node_modules and caches
  hit=$(find "$HOME" -maxdepth 6 -name "$SCANNER_JS" \
          -not -path '*/node_modules/*' -not -path '*/Library/Caches/*' \
          -print -quit 2>/dev/null)
  [ -n "$hit" ] && { printf '%s\n' "$hit"; return 0; }
  return 1
}

# --- does launchd own it ------------------------------------------------------
find_agent() {
  local plist
  [ -d "$AGENT_DIR" ] || return 1
  for plist in "$AGENT_DIR"/*.plist; do
    [ -f "$plist" ] || continue
    if grep -q "$SCANNER_JS" "$plist" 2>/dev/null; then
      basename "$plist" .plist
      return 0
    fi
  done
  return 1
}

agent_loaded() {
  launchctl print "gui/$(id -u)/$1" >/dev/null 2>&1
}

# --- actions ------------------------------------------------------------------
stop_scanner() {
  local pids
  pids=$(scanner_pids)
  [ -z "$pids" ] && { say "nothing to stop"; return 0; }
  say "stopping: $pids"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null
  local waited=0
  while [ "$waited" -lt 10 ]; do
    sleep 1
    waited=$((waited + 1))
    [ -z "$(scanner_pids)" ] && { say "stopped after ${waited}s"; return 0; }
  done
  pids=$(scanner_pids)
  warn "still alive after 10s, sending SIGKILL to: $pids"
  # shellcheck disable=SC2086
  kill -9 $pids 2>/dev/null
  sleep 1
  [ -z "$(scanner_pids)" ] || die "could not stop $(scanner_pids)"
  say "killed"
}

start_via_launchd() {
  local label="$1"
  if agent_loaded "$label"; then
    say "restarting LaunchAgent $label"
    launchctl kickstart -k "gui/$(id -u)/$label" || die "kickstart failed for $label"
  else
    say "loading LaunchAgent $label (it was not loaded)"
    launchctl bootstrap "gui/$(id -u)" "$AGENT_DIR/$label.plist" || die "bootstrap failed for $label"
    launchctl kickstart "gui/$(id -u)/$label" 2>/dev/null
  fi
}

start_by_hand() {
  local js="$1" dir log
  dir=$(dirname "$js")
  mkdir -p "$LOG_DIR"
  log="$LOG_DIR/$(basename "$SCANNER_JS" .js).log"
  say "starting by hand in $dir"
  say "log: $log"
  ( cd "$dir" && nohup node "$SCANNER_JS" $SCANNER_ARGS >>"$log" 2>&1 & )
  warn "note: a hand-started scanner dies when this terminal closes."
  warn "      to survive logout, add a LaunchAgent that references $SCANNER_JS."
}

verify() {
  local waited=0 pids
  while [ "$waited" -lt 8 ]; do
    sleep 1
    waited=$((waited + 1))
    pids=$(scanner_pids)
    [ -n "$pids" ] && { say "running: $pids"; describe_pids "$pids"; return 0; }
  done
  return 1
}

# --- main ---------------------------------------------------------------------
mode="restart"
case "${1:-}" in
  --status) mode="status" ;;
  --stop)   mode="stop" ;;
  --help|-h)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 0 ;;
  "") ;;
  *) die "unknown option: $1 (try --help)" ;;
esac

say "scanner: $SCANNER_JS"
say "current:"
describe_pids "$(scanner_pids)"

agent_label=$(find_agent) || agent_label=""
[ -n "$agent_label" ] && say "LaunchAgent: $agent_label" || say "LaunchAgent: none references $SCANNER_JS"

if [ "$mode" = "status" ]; then
  js=$(find_scanner) && say "script: $js" || say "script: not found under the usual roots"
  exit 0
fi

stop_scanner
[ "$mode" = "stop" ] && exit 0

if [ -n "$agent_label" ]; then
  start_via_launchd "$agent_label"
else
  js=$(find_scanner) || die "cannot find $SCANNER_JS. Set SCANNER_JS or start it by hand from its directory."
  say "script: $js"
  start_by_hand "$js"
fi

if verify; then
  say "OK"
else
  warn "the scanner did not appear within 8s."
  if [ -n "$agent_label" ]; then
    warn "check: launchctl print gui/$(id -u)/$agent_label"
  else
    warn "check the log named above."
  fi
  exit 1
fi
