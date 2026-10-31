# infra/ — AWS cloud pipeline (Terraform)

Infrastructure-as-code for the AWS side of the digital twin: a WebSocket API Gateway
that ingests emitter ticks and fans them out to browsers, Lambdas that aggregate
metrics and serve replays, InfluxDB Cloud for time-series storage, an authenticated
control API that drives scenarios on an on-demand EC2 sim host, the counts ETL, and a
CloudFront + S3 front end for the dashboard.

**Status: deployed and verified end-to-end** in `ap-southeast-2` — live SUMO traffic →
API Gateway → Lambdas → InfluxDB Cloud → hosted dashboard. Region `ap-southeast-2`
throughout, except the billing alarm (`us-east-1`, which is where AWS publishes billing
metrics).

## Architecture

```
 SUMO + run_traci.py --emit-target wss://…/prod      (your machine, or the EC2 sim host)
        │  {"action":"sendmessage","data":{snapshot}} every N ticks
        ▼
 API Gateway (WebSocket)  $connect · $disconnect · sendmessage · $default
        │
        ▼
 traffic-ingest λ ──┬── fan-out to connected browsers (DynamoDB connection registry)
                    └── async ──► traffic-metrics λ ──► InfluxDB Cloud (per-tick metrics)

 Dashboard (S3 + CloudFront) ─┬─ History/Compare ─► HTTP API /metrics ─► traffic-replay λ ─► InfluxDB Cloud
                              ├─ Scenario control ─► HTTP API (Bearer token) ─► control-authorizer λ
                              │                        ─► traffic-control λ ─► SSM ─► EC2 sim host
                              └─ Counts dropdown ──► HTTP API /traffic-counts ─► query-traffic-counts λ ─► S3

 etl-traffic-counts λ (scheduled) ─► public CCC Google Drive ─► S3 (raw / processed / rejected)
```

The metrics Lambda stores **aggregated metrics, not vehicle positions**, so replay
drives the charts, not the map markers. For a fuller, port-by-port picture, see
[`../docs/SYSTEM_FLOW.md`](../docs/SYSTEM_FLOW.md).

To experiment without risking this deployment, stand up an isolated copy in a separate AWS
account: see [`../docs/RESEARCH_ENV.md`](../docs/RESEARCH_ENV.md).

## What's in here

| File | What it creates |
|---|---|
| `versions.tf`, `main.tf`, `variables.tf`, `outputs.tf` | Providers (incl. the `us-east-1` alias), S3 backend, default tags, inputs, outputs |
| `api_gateway.tf` | WebSocket API, its four routes, `prod` stage |
| `lambda.tf` + `functions/traffic_*` | `traffic-ingest`, `traffic-metrics`, `traffic-replay`, shared IAM role, DynamoDB connections table |
| `replay_api.tf` | HTTP API in front of `traffic-replay` (`/metrics`) — **what the dashboard uses** |
| `control_api.tf` + `functions/control_authorizer`, `functions/traffic_control` | Authenticated HTTP control API (shared token) → SSM restart of the sim host with a preset scenario. See [`../docs/CLOUD_CONTROL.md`](../docs/CLOUD_CONTROL.md) |
| `sim_host.tf` + `sim_host_userdata.sh.tftpl` | On-demand EC2 running SUMO + the emitter as a `sumo-emitter` systemd service; gated by `sim_host_enabled` (default off); SSM access, no open ports |
| `etl.tf` + `functions/build_etl.py` | Counts ETL: scheduled ingest Lambda, query Lambda + HTTP API, private data bucket, Drive API-key secret. See [`ETL.md`](ETL.md) |
| `storage.tf` | Dashboard S3 bucket, InfluxDB-token secret |
| `cdn.tf` | CloudFront distribution + OAC bucket policy |
| `observability.tf` | Log groups (14-day retention) + billing alarm (default US$5) with SNS email |
| `deploy_dashboard.sh` | Uploads the dashboard + data files to S3, invalidates CloudFront, prints the wired URL |
| `dashboard_url.sh` | Prints the wired dashboard URL from `terraform output` (no upload) |

The InfluxDB bucket itself is created in the InfluxDB Cloud UI, not by Terraform.

---

## One-time bootstrap

### 1. Install the tools

Terraform and the AWS CLI. The project's cloud work is run from the Ubuntu VM;
on macOS use Homebrew:

```bash
# macOS
brew tap hashicorp/tap && brew install hashicorp/tap/terraform awscli
# Ubuntu: install terraform from HashiCorp's apt repo, and awscli v2 from AWS
terraform version && aws --version
```

To shell into the sim host you also need the **Session Manager plugin** — see
`running_guide.md` §E. Don't commit the downloaded `.deb` (it's git-ignored).

### 2. AWS account + credentials

1. Create/sign in to an AWS account.
2. Create an IAM user with programmatic access and least-privilege policies for the
   services used here (API Gateway, Lambda, S3, DynamoDB, CloudFront, CloudWatch,
   Secrets Manager, IAM, SNS, EC2, SSM, EventBridge). Start scoped; widen only if a
   `plan` says it needs more.
3. `aws configure` — paste the access key/secret, set region `ap-southeast-2`.
4. Enable billing alerts in the Billing console (required before the estimated-charges
   alarm can fire).

### 3. Create the Terraform backend by hand (Terraform can't make its own backend)

```bash
aws s3 mb s3://YOUR-TF-STATE-BUCKET --region ap-southeast-2
aws s3api put-bucket-versioning --bucket YOUR-TF-STATE-BUCKET \
  --versioning-configuration Status=Enabled

aws dynamodb create-table --table-name tf-state-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST --region ap-southeast-2
```

### 4. InfluxDB Cloud

1. Sign up for the InfluxDB Cloud free tier (a **separate** account from the local
   Docker InfluxDB — the Lambdas can't reach `localhost:8086`).
2. Create an organization and a bucket named `traffic-metrics` (or set
   `influxdb_bucket_name`).
3. Create an all-access API token — it goes into Secrets Manager after the first apply.
4. Note the regional URL and org name for `terraform.tfvars`.

---

## Apply

### 5. Configure

```bash
cd infra
cp backend.hcl.example backend.hcl            # state bucket + lock table
cp terraform.tfvars.example terraform.tfvars  # see below
```

Required in `terraform.tfvars`: `dashboard_bucket_name`, `etl_data_bucket_name`
(both globally unique, lowercase, hyphens only), `alarm_email`, plus `influxdb_url`
and `influxdb_org`. Everything else has a default (`sim_host_enabled = false`,
`sim_host_instance_type = "t3.medium"`, `sim_host_git_ref = "main"`,
`etl_schedule_expression = "rate(7 days)"`, `billing_alarm_threshold_usd = 5`, …).

### 6. Build the ETL bundles, then init + plan + apply

```bash
python functions/build_etl.py      # stages the ETL Lambda bundles (archive_file can't pip-install)
terraform init -backend-config=backend.hcl
terraform fmt -check
terraform validate
terraform plan                     # read it; confirm it only creates what you expect
terraform apply
```

Re-run `build_etl.py` before any apply that should pick up ETL code changes.

### 7. Post-apply secrets (one time)

Tokens never go into Terraform variables or state — set them by hand:

```bash
# InfluxDB Cloud token (the CLOUD token, not the local .env one)
aws secretsmanager put-secret-value \
  --secret-id "$(terraform output -raw influxdb_token_secret_name)" \
  --secret-string 'YOUR_INFLUX_CLOUD_TOKEN'

# Google Drive API key for the counts ETL
aws secretsmanager put-secret-value \
  --secret-id "$(terraform output -raw drive_api_key_secret_name)" \
  --secret-string 'YOUR_DRIVE_API_KEY'
```

Then confirm the billing-alarm SNS subscription from the email AWS sends you.

---

## Make it live

### 8. Deploy the dashboard

```bash
./infra/deploy_dashboard.sh                  # from anywhere
./infra/deploy_dashboard.sh --rebuild-names  # occasionally: refresh the counts dropdown names
```

This uploads `intersection_map.html`, `edges.geojson` and
`data/output/intersection_geo.csv` to the dashboard bucket, invalidates CloudFront,
and prints the ready-to-open URL with `ws`, `replay`, `counts` and `edges` wired in.
`./infra/dashboard_url.sh` prints the same URL without uploading.

The printed URL deliberately **omits the control token**, so it's safe to share
(view-only). To drive scenarios from the hosted page, append the control API yourself
and keep that link private:

```bash
echo "$(./infra/dashboard_url.sh)&control=$(terraform output -raw control_api_url)&token=$(terraform output -raw control_api_token)"
```

### 9. Feed it data

**Option A — emitter on your machine:** point the emitter at the WebSocket API. It
dials out as a client and wraps each snapshot for the `sendmessage` route:

```bash
cd ../smart_city_digital_twin/2D_simulation
python scripts/run_traci.py --no-gui --emit-target "$(terraform -chdir=../../infra output -raw websocket_url)" \
  --emit-interval 10 --jump-to 23400 --end 27000
```

Throttle with `--emit-interval` — every tick would flood API Gateway, and a
WebSocket message is capped at 128 KB (a ~600-vehicle snapshot is ~50 KB). The
forwarder reconnects on drops and never crashes the sim.

**Option B — the EC2 sim host (no laptop):**

```bash
terraform apply -var 'sim_host_enabled=true'    # boots SUMO + emitter as a systemd service
aws ssm start-session --target "$(terraform output -raw sim_host_instance_id)"   # optional shell
terraform apply -var 'sim_host_enabled=false'   # ALWAYS tear down after the demo (hourly-billed)
```

The host clones this repo at `sim_host_git_ref` and runs the baseline scenario.
`/start` on the control API switches scenario on a **running** host; it does not boot
the instance.

### 10. Smoke-test the read path

```bash
curl "$(terraform output -raw replay_api_url)?start=-1h&field=avgSpeed"
# → {"field":"avgSpeed","points":[{"time":"...","value":6.6}, ...]}
```

Use `replay_api_url` (the HTTP API). The older `replay_url` output is the Lambda
Function URL, which returns **403 for anonymous access on this account** — it's kept
only for signed/internal calls.

---

## Gotchas

- **Secrets Manager blocks `destroy` → `apply`.** The InfluxDB secret has a 7-day
  recovery window, so a re-apply can't recreate a same-named secret and the Lambda
  chain fails half-created. Before destroying, set `recovery_window_in_days = 0` in
  `storage.tf`, or run
  `aws secretsmanager delete-secret --secret-id <name> --force-delete-without-recovery`.
  (The Drive-key secret in `etl.tf` already uses 0.)
- **Sim host user data must start with `#!`**, or cloud-init silently skips it.
- **Changing `sim_host_userdata.sh.tftpl` replaces the instance** on the next apply.
  Harmless — it's off by default — but expect it.
- **Pin `--region`** on CLI calls (or `aws configure set region ap-southeast-2`).
- **S3 bucket names:** lowercase, hyphens, globally unique.

## Tear down

Just the hourly-billed piece:

```bash
terraform apply -var 'sim_host_enabled=false'
```

Everything: use **`teardown.sh`**, not a bare `terraform destroy`. A bare destroy stops
half-way here (neither S3 bucket has `force_destroy`, the dashboard bucket is versioned,
and the InfluxDB secret's 7-day recovery window blocks a re-apply). The script checks
you're in the account you named, empties both buckets (every version), destroys, then
force-deletes the secrets:

```bash
export TEARDOWN_PROTECTED_ACCOUNTS=<demo-account-id>     # optional: never allow this one
./infra/teardown.sh --expect-account <account-id> --dry-run              # see the plan
./infra/teardown.sh --expect-account <account-id> --backup ~/twin-backup # do it
```

It leaves the state bucket and lock table (delete them by hand if you're fully done) and
the InfluxDB bucket (managed in InfluxDB Cloud).

## Cost

About US$1–2/month idle: Secrets Manager (~US$0.40/secret/month), with the rest in
the AWS free tier. The EC2 sim host is the only hourly-billed piece — keep
`sim_host_enabled=false` except during demos. The billing alarm (default US$5) is the
safety net; don't ignore it.
