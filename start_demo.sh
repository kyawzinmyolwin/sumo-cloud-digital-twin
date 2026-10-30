#!/usr/bin/env bash
#
# start_demo.sh — bring up the LOCAL demo stack with one command, so a non-IT
# user never types a Python command line.
#
# It starts the four long-running background services the dashboard needs:
#   1. InfluxDB 2.7      :8086   time-series store (Docker container cdt-influxdb)
#   2. dashboard web     :8000   serves intersection_map.html (python http.server)
#   3. replay server     :8788   read endpoint for History/Compare (replay_server.py)
#   4. control server    :8799   Start/Stop scenarios from the browser (control_server.py)
#
# It does NOT start the simulation or metrics_writer — those are launched per
# scenario from the dashboard's "Scenario control" panel (control_server now
# auto-starts metrics_writer with each run). So the flow for the user is:
#   ./start_demo.sh   →   open the printed URL   →   click a Start button.
#
# Logs + pidfiles live in .demo/ (gitignored). Stop everything with ./stop_demo.sh.
#
# Re-running is safe: already-running services are left alone.
set -u

# --- paths -------------------------------------------------------------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIM="$ROOT/smart_city_digital_twin/2D_simulation"
SCRIPTS="$SIM/scripts"
RUN="$ROOT/.demo"
mkdir -p "$RUN"

PY="${PYTHON:-python3}"
WEB_PORT=8000
REPLAY_PORT=8788
CONTROL_PORT=8799
INFLUX_PORT=8086
# Bind host for the Python servers: 0.0.0.0 so a VM's port-forwarding reaches
# them (set BIND=127.0.0.1 to keep them on this machine only).
BIND="${BIND:-0.0.0.0}"

green() { printf '\033[32m%s\033[0m\n' "$1"; }
yellow() { printf '\033[33m%s\033[0m\n' "$1"; }
red() { printf '\033[31m%s\033[0m\n' "$1"; }

# port_busy PORT -> 0 if something is already listening
port_busy() {
  local p="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -q ":$p "
  elif command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1
  else
    # no tool to check — assume free
    return 1
  fi
}

# start_bg NAME PORT CWD CMD...  : spawn a backgrounded server with a pidfile + log
start_bg() {
  local name="$1" port="$2" cwd="$3"; shift 3
  local pidf="$RUN/$name.pid" logf="$RUN/$name.log"
  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf" 2>/dev/null)" 2>/dev/null; then
    yellow "• $name already running (pid $(cat "$pidf")) — leaving it"
    return 0
  fi
  if port_busy "$port"; then
    yellow "• $name: port $port already in use — assuming it's up, not starting another"
    return 0
  fi
  ( cd "$cwd" && exec "$@" ) >"$logf" 2>&1 &
  echo $! >"$pidf"
  sleep 1
  if kill -0 "$(cat "$pidf")" 2>/dev/null; then
    green "✓ $name on :$port (pid $(cat "$pidf"), log .demo/$name.log)"
  else
    red "✗ $name failed to start — see .demo/$name.log"
    tail -n 5 "$logf" 2>/dev/null | sed 's/^/    /'
  fi
}

echo "Starting the local digital-twin demo stack…"
echo

# --- 0. .env (InfluxDB creds) ------------------------------------------------
if [ ! -f "$ROOT/.env" ]; then
  if [ -f "$ROOT/.env.example" ]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    yellow "• created .env from .env.example — using the default local-dev token."
    yellow "  (fine for a local demo; change the password/token for anything shared)"
  else
    red "• no .env and no .env.example — InfluxDB may fail to initialise"
  fi
fi
# shellcheck disable=SC1090
set -a; [ -f "$ROOT/.env" ] && . "$ROOT/.env"; set +a

# --- 1. InfluxDB (Docker) ----------------------------------------------------
# Prefer compose; fall back to a plain `docker run` (the VM often has neither
# docker-compose v1 nor the v2 plugin).
# influx_reachable: true if something already answers on the InfluxDB port. This
# is a NETWORK check (curl → wget → bash /dev/tcp), independent of the Docker
# socket — so a container started by root/sudo, or when this shell isn't in the
# docker group, is still detected without any docker permission.
influx_reachable() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 2 "http://localhost:${INFLUX_PORT}/health" >/dev/null 2>&1 && return 0
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 2 -O /dev/null "http://localhost:${INFLUX_PORT}/health" 2>/dev/null && return 0
  fi
  # Dependency-free fallback: can we open a TCP connection to the port?
  (exec 3<>"/dev/tcp/127.0.0.1/${INFLUX_PORT}") 2>/dev/null && { exec 3>&- 3<&-; return 0; }
  return 1
}

start_influx() {
  # If InfluxDB is already up (any way it was started), use it — no docker needed.
  if influx_reachable; then
    green "✓ InfluxDB already up on :$INFLUX_PORT — using it"
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    red "• docker not found — skipping InfluxDB. History/Compare won't have data."
    red "  Install Docker, or point the replay server at InfluxDB Cloud via .env."
    return 1
  fi
  # The daemon must be reachable before any docker subcommand that talks to it
  # (`docker compose version` succeeds without it and would leak a daemon error).
  if ! docker info >/dev/null 2>&1; then
    red "• Can't reach the Docker daemon from this shell — skipping InfluxDB start."
    red "  If the daemon is DOWN: start it ('sudo systemctl start docker', or 'colima start' on macOS)."
    red "  If it's a PERMISSION error (container running under sudo, shell not in the 'docker'"
    red "  group yet): run 'newgrp docker' or re-open the shell. Nothing to do if InfluxDB is"
    red "  already up — this check is only skipped because :$INFLUX_PORT wasn't answering."
    red "  The web/replay/control servers still start regardless."
    return 1
  fi
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx cdt-influxdb; then
    yellow "• InfluxDB container cdt-influxdb already running — leaving it"
    return 0
  fi
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx cdt-influxdb; then
    green "✓ InfluxDB: starting existing cdt-influxdb container"
    docker start cdt-influxdb >/dev/null 2>&1 && return 0
  fi
  if docker compose version >/dev/null 2>&1; then
    ( cd "$ROOT" && docker compose up -d influxdb ) && { green "✓ InfluxDB via docker compose (:$INFLUX_PORT)"; return 0; }
  elif command -v docker-compose >/dev/null 2>&1; then
    ( cd "$ROOT" && docker-compose up -d influxdb ) && { green "✓ InfluxDB via docker-compose (:$INFLUX_PORT)"; return 0; }
  fi
  # Fallback: docker run, mirroring docker-compose.yml's setup env.
  if docker run -d --name cdt-influxdb -p "${INFLUX_PORT}:8086" \
    -e DOCKER_INFLUXDB_INIT_MODE=setup \
    -e DOCKER_INFLUXDB_INIT_USERNAME="${INFLUXDB_USERNAME:-admin}" \
    -e DOCKER_INFLUXDB_INIT_PASSWORD="${INFLUXDB_PASSWORD:-change-me-8plus}" \
    -e DOCKER_INFLUXDB_INIT_ORG="${INFLUXDB_ORG:-christchurch}" \
    -e DOCKER_INFLUXDB_INIT_BUCKET="${INFLUXDB_BUCKET:-traffic-metrics}" \
    -e DOCKER_INFLUXDB_INIT_ADMIN_TOKEN="${INFLUXDB_TOKEN:-local-dev-token-change-me}" \
    -e DOCKER_INFLUXDB_INIT_RETENTION="${INFLUXDB_RETENTION:-30d}" \
    -v cdt-influxdb-data:/var/lib/influxdb2 \
    -v cdt-influxdb-config:/etc/influxdb2 \
    influxdb:2.7 >/dev/null 2>&1; then
    green "✓ InfluxDB via docker run (:$INFLUX_PORT)"
    return 0
  fi
  red "✗ InfluxDB failed to start (is the Docker daemon running?). Check 'docker logs cdt-influxdb'."
  red "  The web/replay/control servers still start; History/Compare just won't have data."
  return 1
}
start_influx

# --- 2. dashboard web server -------------------------------------------------
# Serve 2D_simulation/ so intersection_map.html's ../data/output/* fetches resolve.
start_bg web "$WEB_PORT" "$SIM" "$PY" -m http.server "$WEB_PORT" --bind "$BIND"

# --- 3. replay server (read endpoint) ----------------------------------------
# Needs INFLUXDB_TOKEN/ORG (from .env, exported above). Run from 2D_simulation
# so its .env walk-up finds the repo-root .env.
start_bg replay "$REPLAY_PORT" "$SIM" "$PY" "$SCRIPTS/replay_server.py" --host "$BIND" --port "$REPLAY_PORT"

# --- 4. control server (start/stop scenarios from the browser) ---------------
start_bg control "$CONTROL_PORT" "$SIM" "$PY" "$SCRIPTS/control_server.py" --host "$BIND" --port "$CONTROL_PORT"

# --- summary -----------------------------------------------------------------
echo
URL="http://localhost:${WEB_PORT}/scripts/intersection_map.html"
green "Demo stack is up."
echo
echo "  Open the dashboard:"
echo "    $URL"
echo
echo "  Then in the 'Scenario control' panel click a Start button —"
echo "  that launches the sim + metrics writer for you (no command line)."
echo
echo "  Services:  web :$WEB_PORT · replay :$REPLAY_PORT · control :$CONTROL_PORT · InfluxDB :$INFLUX_PORT"
echo "  (the sim's live feed is on :8765 once a scenario is started)"
if [ "$BIND" = "0.0.0.0" ]; then
  echo
  echo "  On a VM, forward these host ports to reach it from your browser:"
  echo "    $WEB_PORT  8765  $REPLAY_PORT  $CONTROL_PORT"
fi
echo
echo "  Stop everything:  ./stop_demo.sh"
