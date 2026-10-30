#!/usr/bin/env bash
#
# stop_demo.sh — tear down the local demo stack started by start_demo.sh.
#
# Stops the backgrounded Python servers (web / replay / control) by their
# pidfiles in .demo/, then stops the InfluxDB container. InfluxDB DATA is kept
# (named volumes survive) so History/Compare still has it next time; pass
# --wipe to also delete the container (keeps volumes) — use plain `docker
# compose down -v` if you want to drop the data too.
#
# If start_demo.sh auto-started nothing (ports were already busy), there may be
# no pidfile to stop — that's reported, not an error.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="$ROOT/.demo"
WIPE=0
[ "${1:-}" = "--wipe" ] && WIPE=1

green() { printf '\033[32m%s\033[0m\n' "$1"; }
yellow() { printf '\033[33m%s\033[0m\n' "$1"; }

stop_pid() {
  local name="$1"
  local pidf="$RUN/$name.pid"
  if [ ! -f "$pidf" ]; then
    yellow "• $name: no pidfile (not started by this script, or already stopped)"
    return 0
  fi
  local pid; pid="$(cat "$pidf" 2>/dev/null)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null
    # give it a moment, then force if still alive
    for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 0.3; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    green "✓ stopped $name (pid $pid)"
  else
    yellow "• $name: not running (stale pidfile)"
  fi
  rm -f "$pidf"
}

echo "Stopping the local digital-twin demo stack…"
stop_pid control
stop_pid replay
stop_pid web

# InfluxDB container
if command -v docker >/dev/null 2>&1; then
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx cdt-influxdb; then
    docker stop cdt-influxdb >/dev/null 2>&1 && green "✓ stopped InfluxDB container (data kept)"
  else
    yellow "• InfluxDB container not running"
  fi
  if [ "$WIPE" = 1 ] && docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx cdt-influxdb; then
    docker rm cdt-influxdb >/dev/null 2>&1 && green "✓ removed InfluxDB container (volumes kept)"
  fi
fi

# Any sim/metrics_writer started via the control server are child processes of
# control_server and are terminated when it stops; nothing more to do here.
green "Done."
