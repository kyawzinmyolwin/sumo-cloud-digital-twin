# SUMO producer image — runs run_traci.py --emit (the simulation + WebSocket emitter).
#
# SUMO isn't pip-installable at this project's version, so the image installs it
# from the official PPA. This is the SAME recipe infra/sim_host.tf uses on EC2,
# which is verified to load this repo's net/demand files — so the PPA's SUMO
# version is known-good for them.
#
# The code + data are NOT baked in (the 2D_simulation tree is ~0.6 GB); they are
# bind-mounted at /app by docker-compose.yml. The image carries only the runtime:
# SUMO, sumo-tools (sumolib + traci), and the emitter's Python deps.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV SUMO_HOME=/usr/share/sumo

# gnupg + dirmngr are needed for add-apt-repository to import the PPA signing key
# on a minimal base image (they are NOT in ubuntu:22.04); without them the PPA add
# or the following apt-get update fails. ppa:sumo/stable supports jammy (22.04).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      software-properties-common ca-certificates gnupg dirmngr python3 python3-pip \
 && add-apt-repository -y ppa:sumo/stable \
 && apt-get update \
 && apt-get install -y --no-install-recommends sumo sumo-tools \
 && rm -rf /var/lib/apt/lists/*

# Emitter runtime deps (websockets + pyproj; influxdb-client is unused here but
# harmless). Installed from the project's own requirements so versions match.
COPY smart_city_digital_twin/2D_simulation/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app

# Default run: the calibrated morning baseline, paced in real time so the dashboard
# animates. Override `command:` in compose (or on the CLI) to run a scenario.
# --emit-host 0.0.0.0 so both the host browser (localhost:8765) and the writer
# container (ws://sim:8765) can connect.
CMD ["python3", "scripts/run_traci.py", "--no-gui", \
     "--emit", "--emit-host", "0.0.0.0", "--emit-port", "8765", \
     "--real-time", "--speed", "5", \
     "--jump-to", "23400", "--end", "27000", \
     "--scenario-id", "baseline_am"]
