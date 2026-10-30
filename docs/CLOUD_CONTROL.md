# Cloud scenario control

Drive the simulation from the **hosted** dashboard: start/stop named-preset runs and
start a road-block on a clicked edge, over the cloud, with an authenticated API.

This is the cloud counterpart of the local `control_server.py` (:8799). It mirrors the
same HTTP contract (`/scenarios`, `/status`, `/start`, `/stop`, `/start_roadblock`), so
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
   terraform apply -var 'sim_host_enabled=true' \
                   -var 'sim_host_git_ref=claude/next-step-nun1nw'
   ```
   Wait ~2–3 min after apply for the box to boot, install SUMO and start the service.

   > **Important:** the EC2 sim host clones the repo and checks out `sim_host_git_ref`
   > (default `main`). The scenario flags (`--scenario-id`, `--close-edge`, `--real-time`)
   > live on the feature branch, so until that branch is merged to `main` you **must**
   > pass `-var 'sim_host_git_ref=<branch>'` — otherwise the box runs old code and every
   > scenario Start fails with "unrecognized arguments". Once merged to `main`, drop the var.

2. **Get the endpoint + token:**
   ```bash
   terraform output control_api_url
   terraform output -raw control_api_token
   ```

3. **Point the hosted dashboard at it** by adding URL parameters:
   ```
   https://<cloudfront-domain>/scripts/intersection_map.html?ws=<wss API>&replay=<replay API>/metrics&control=<control_api_url>&token=<control_api_token>
   ```
   (`deploy_dashboard.sh` already wires `ws`/`replay`; append `&control=…&token=…`.)

4. In the **Scenario control** panel, click a Start button (or click a road → **Start road
   block**). The live feed connects automatically; History/Compare fill from InfluxDB.

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
