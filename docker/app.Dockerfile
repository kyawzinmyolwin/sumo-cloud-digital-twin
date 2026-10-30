# Lightweight Python image shared by the three pure-Python services:
#   - writer  (metrics_writer.py: emitter WS -> InfluxDB)
#   - replay  (replay_server.py: HTTP read endpoint for the dashboard)
#   - web     (http.server: serves intersection_map.html + data)
#
# No SUMO here. The code + data are bind-mounted at /app by docker-compose.yml;
# each service sets its own `command:`. The image carries only the two runtime
# deps the writer needs (replay + web are stdlib-only).
FROM python:3.11-slim

RUN pip install --no-cache-dir "websockets>=13.0" "influxdb-client>=1.40.0"

WORKDIR /app

# Overridden per-service in docker-compose.yml.
CMD ["python3", "-c", "print('set a command: in docker-compose.yml')"]
