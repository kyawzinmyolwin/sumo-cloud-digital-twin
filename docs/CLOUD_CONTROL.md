# Cloud scenario control

Drive the simulation from the **hosted** dashboard: start/stop named-preset runs, start a
road-block on a clicked edge, and **close or reopen a road live in the running simulation**,
over the cloud, with an authenticated API.

This is the cloud counterpart of the local `control_server.py` (:8799). It mirrors the
same HTTP contract (`/scenarios`, `/status`, `/start`, `/stop`, `/start_roadblock`), plus the
live routes `/inject` and `/reopen`, so
the **same dashboard "Scenario control" panel** drives either one — you just point it at
the cloud endpoint and give it a token.

---

## How it works

```
Hosted dashboard ──POST /start (Authorization: Bearer <token>)──▶ HTTP API Gateway
                                                                       │
                                                        control-authorizer (token check)
                                                                       │
                                                             traffic-control Lambda
                                                                       │ ssm:SendCommand (tag:Name)
                                                                       ▼
                                                   EC2 sim host: rewrite /etc/sumo-emitter.env
                                                   with the chosen scenario args, then
                                                   `systemctl restart sumo-emitter`
                                                                       │
                                                   emitter forwards to the WebSocket API
                                                   → live vehicles on the dashboard + metrics
```

- **Safety:** the Lambda only composes the run command from a **fixed preset allowlist**
  (plus a regex-validated edge id); it never runs arbitrary input. The SSM command just
  rewrites a scenario-args env file and restarts the service.
- **Auth:** every control call is gated by a **shared token** (a Lambda authorizer). The
  public dashboard URL is read-only without the token — starting a run triggers
  compute/cost, so it must be authenticated.

Terraform: `infra/control_api.tf` (+ `infra/functions/traffic_control/`,
`infra/functions/control_authorizer/`). Sim-host service change:
`infra/sim_host_userdata.sh.tftpl` (an `EnvironmentFile` the Lambda rewrites).

---

## Deploy

1. **Bring up the sim host** (it must be running during the demo) and apply:
   ```bash
   cd infra
   terraform apply -var 'sim_host_enabled=true'
   ```
   Wait ~2–3 min after apply for the box to boot, install SUMO and start the service.

   > **Important:** the EC2 sim host clones the repo and checks out `sim_host_git_ref`
   > (default `main`). To demo code that isn't merged to `main` yet, add
   > `-var 'sim_host_git_ref=<branch>'`; otherwise the box runs `main`'s code.

2. **Get the endpoint + token:**
   ```bash
   terraform output control_api_url
   terraform output -raw control_api_token
   ```

3. **Point the hosted dashboard at it.** Build the full URL from the Terraform outputs
   (run in `infra/`):
   ```bash
   echo "$(./dashboard_url.sh)&control=$(terraform output -raw control_api_url)&token=$(terraform output -raw control_api_token)"
   ```
   which produces:
   ```
   <dashboard_url>/intersection_map.html?ws=<websocket_url>&replay=<replay_api_url>&counts=<counts_api_url>&edges=/edges.geojson&control=<control_api_url>&token=<control_api_token>
   ```
   `dashboard_url` already starts with `https://`, the page sits at the site root, and
   `replay_api_url` already ends in `/metrics`. Keep this link private (it carries the
   token); `./dashboard_url.sh` alone gives the shareable view-only URL.

4. In the **Scenario control** panel, click a Start button (or click a road → **Start road
   block**). The live feed connects automatically; History/Compare fill from InfluxDB.

---

## Live Close / Reopen (no restart)

Click a road on the hosted dashboard → **Close live** closes it in the **running**
simulation; **Reopen road** lifts it and traffic recovers as the queue drains. Nothing is
restarted — the cloud equivalent of the local Close/Reopen.

```
Hosted dashboard ──POST /inject?edge=<id>  (Bearer token)──▶ control API ─▶ traffic-control λ
                   POST /reopen                                              │ scan connections table
                                                                             │ for role = producer
                                                                             ▼
                                               post_to_connection over the WebSocket API
                                                                             ▼
             emitter CloudForwarder (connected as ?role=producer) ─▶ ControlInbox
                                                                             ▼
                              run_traci applies it on the next tick (IncidentController)
```

- **How the producer is found:** the emitter's `CloudForwarder` connects with
  `?role=producer`; `traffic-ingest` stores that role on `$connect` and no longer echoes
  snapshots back to producers. `traffic-control` pushes the command to every producer.
- **Using it:** the dashboard picks the path from the feed — on a cloud feed (`wss://`) it
  calls `/inject` / `/reopen` with the token; on a local feed (`ws://`) it sends the command
  up the local WebSocket as before. So the hosted page needs `&control=…&token=…`.
- **The running sim must have this code.** The sim host runs `sim_host_git_ref`; if that
  code predates live control, `/inject` still returns ok but the sim ignores the command.
  The host's log shows `cloud control received: incident` when it works.
- **IAM added** (least privilege): `dynamodb:Scan` on the connections table and
  `execute-api:ManageConnections` on this WebSocket stage's `@connections` only.
- **Responses:** `200` delivered (`"producers": n`), `400` invalid edge or duration,
  `409` no live simulation connected.

**Tear down after the demo** (stops EC2 cost):
```bash
terraform apply -var 'sim_host_enabled=false'
```

---

## Security & cost notes

- **Keep the token secret.** Anyone with the URL **and** the token can start runs (and
  thus spend). Don't commit it or put it in a public link; share it out-of-band for the
  demo and re-apply to rotate (`terraform apply -replace=random_password.control_token`).
- CORS is open (`*`) for convenience; tighten `allow_origins` to your CloudFront domain
  for anything beyond a demo.
- The control Lambda's IAM is least-privilege: `ssm:SendCommand` is restricted to the
  sim-host instance **by tag**, plus read/write of one SSM parameter for status.
- The sim host is still gated by `sim_host_enabled` and the account has a \$5 billing
  alarm; EC2 runs only while you have it enabled.

---

## Limitations (v1)

- **The sim host must already be running.** `/start` does not boot EC2 — it switches the
  scenario on a running box (fast). If the box is down, `/start` returns a clear "sim host
  not running" error. (Auto-boot on Start is a possible later enhancement.)
- **Presets + click-a-road closure**, not a full free-form scenario builder (by design —
  arbitrary args on a process-spawning path would be unsafe).
- **Status** reflects the last start/stop the control Lambda recorded (an SSM parameter),
  not live service health; the dashboard's live feed is the real "is data flowing?" signal.
- **Producer role isn't authenticated.** Anyone can connect to the WebSocket API with
  `?role=producer`. That only lets them *receive* live commands (and stop receiving
  snapshots); they still can't *send* commands to the real sim, because only the control
  Lambda — behind the token — can push to a connection. An authorizer on `$connect` would
  close this gap.
- `/stop` stops the service; because the service is `Restart=always`, a *completed* run
  (end of the window) is restarted by systemd — use `/stop` to end it, or `/start` to
  switch scenarios.

---

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| `401 Unauthorized` | missing/wrong token | confirm `&token=` matches `terraform output -raw control_api_token` |
| `"sim host not running"` | EC2 down | `terraform output sim_host_instance_id`; ensure `sim_host_enabled=true` and booted |
| Start returns ok but no vehicles | service didn't restart / emit | `aws ssm start-session --target <id>` then `journalctl -u sumo-emitter -n 50` |
| CORS error in the browser console | origin not allowed | CORS is `*` by default; if you tightened it, add your CloudFront domain |
| Close live: "no live simulation connected" (409) | no producer on the WebSocket API | start a run first; check the sim host is forwarding (`journalctl -u sumo-emitter`) |
| Close live: "add ?control=…&token=…" | hosted page opened without the control params | append `&control=<control_api_url>&token=<control_api_token>` |
| Close live returns ok but the road stays open | sim host running code without live control | set `sim_host_git_ref` to a ref with this feature, re-apply; look for `cloud control received` in the log |
