# infra/ — Phase 2 cloud pipeline (Terraform)

Infrastructure-as-code for the AWS side of the digital twin: a WebSocket API
Gateway that ingests emitter ticks, Lambda functions that aggregate metrics and
serve replays, InfluxDB Cloud for time-series storage, and a CloudFront + S3
front end for the dashboard.

## Architecture (target)

```
 SUMO + run_traci.py --emit                (your machine / a small VM)
        │  vehicle-state JSON per tick
        ▼
 API Gateway (WebSocket API)               $connect · $disconnect · sendmessage · $default
        │
        ▼
 Lambda: traffic-ingest ──► Lambda: traffic-metrics ──► InfluxDB Cloud (tick metrics)
                                                              ▲
 Browser dashboard ◄── CloudFront ◄── S3   Lambda: traffic-replay ──┘ (query time range)
```

## Scaffold status

| File | State |
|---|---|
| `versions.tf`, `main.tf`, `variables.tf`, `outputs.tf` | ✅ foundation |
| `storage.tf` (S3 dashboard bucket, Secrets Manager) | ✅ done |
| `api_gateway.tf` (WebSocket API + routes + stage) | ✅ done |
| `lambda.tf` + `functions/` (ingest / metrics / replay + IAM + connections table) | ✅ done |
| `cdn.tf` (CloudFront + OAC bucket policy) | ✅ done |
| `observability.tf` (log groups + billing alarm) | ✅ done |
| InfluxDB bucket as a TF resource | ⏳ (created in UI for now) |

**Nothing here has been `terraform validate`d yet** — Terraform isn't installed on
the dev machine. Run `terraform fmt -check` and `terraform validate` once you've
installed the tools (below) and expect to fix small issues.

---

## One-time bootstrap (do this while the scaffold is being finished)

### 1. Install the tools (macOS)

```bash
brew tap hashicorp/tap
brew install hashicorp/tap/terraform awscli
terraform version && aws --version
```
(If Homebrew asks you to `brew trust hashicorp/tap`, that's the official HashiCorp
tap — same prompt you saw for the SUMO tap.)

### 2. AWS account + credentials

1. Create/sign in to an AWS account.
2. Create an IAM user with programmatic access and least-privilege policies for the
   services used here (API Gateway, Lambda, S3, DynamoDB, CloudFront, CloudWatch,
   Secrets Manager, IAM, SNS). Start scoped; widen only if a `plan` says it needs more.
3. `aws configure` — paste the access key/secret, set region `ap-southeast-2`.
4. Enable billing alerts in the Billing console (required before the CloudWatch
   estimated-charges alarm can fire).

### 3. Create the Terraform backend by hand (chicken-and-egg — Terraform can't make its own backend)

```bash
# Globally-unique bucket name:
aws s3 mb s3://YOUR-TF-STATE-BUCKET --region ap-southeast-2
aws s3api put-bucket-versioning --bucket YOUR-TF-STATE-BUCKET \
  --versioning-configuration Status=Enabled

# Lock table so two applies can't race:
aws dynamodb create-table --table-name tf-state-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST --region ap-southeast-2
```

### 4. InfluxDB Cloud

1. Sign up for the InfluxDB Cloud free tier.
2. Create an organization and a bucket (name it `traffic-metrics`, or set
   `influxdb_bucket_name` to match).
3. Create an all-access API token and copy it — you'll store it in Secrets Manager
   *after* the first apply (step 7).
4. Note your regional URL and org name for `terraform.tfvars`.

---

## Apply

### 5. Configure

```bash
cd infra
cp backend.hcl.example backend.hcl          # fill in your state bucket + lock table
cp terraform.tfvars.example terraform.tfvars # fill in bucket name, email, influx values
```

### 6. Init + plan + apply

```bash
terraform init -backend-config=backend.hcl
terraform fmt -check
terraform validate
terraform plan      # READ THIS. Confirm it only creates what you expect.
terraform apply
```

### 7. Post-apply (one time)

```bash
# Put the real InfluxDB token into the secret Terraform created (see the
# influxdb_token_secret_name output). The token never goes into Terraform.
aws secretsmanager put-secret-value \
  --secret-id "$(terraform output -raw influxdb_token_secret_name)" \
  --secret-string 'YOUR_INFLUX_TOKEN'
```

Then confirm the billing-alarm SNS subscription from the email AWS sends you.

### 8. Make it live (after the infra exists)

`terraform apply` builds the plumbing; these steps put data and a page through it.
Grab the URLs first:

```bash
terraform output      # websocket_url, replay_url, dashboard_url, ...
```

**a. Upload the dashboard to S3** (CloudFront serves it):
```bash
aws s3 cp \
  ../smart_city_digital_twin/2D_simulation/scripts/intersection_map.html \
  "s3://$(terraform output -raw dashboard_bucket_name)/intersection_map.html"
# then browse dashboard_url (HTTPS via CloudFront)
```

**b. Point the emitter at the cloud** — see the caveat below; this needs a small
emitter change that is not built yet.

**c. Smoke-test the read path** once metrics exist:
```bash
curl "$(terraform output -raw replay_url)?start=-1h&field=avgSpeed"
# → {"field":"avgSpeed","points":[{"time":"...","value":6.6}, ...]}
```

## ⚠️ Connecting the local emitter to the cloud (not built yet)

The current emitter (`run_traci.py --emit`) **hosts** a WebSocket server — browsers
connect *to it*. The cloud flips that: the emitter must become a **client** that
connects to `websocket_url` and sends each snapshot wrapped for the `sendmessage`
route:

```json
{ "action": "sendmessage", "data": { ...the emitter snapshot... } }
```

That's a small additive feature (an `--emit-target wss://…` mode that dials out and
posts, instead of listening). It is **out of scope for this Terraform** — the infra
is ready and waiting; wiring the producer to it is the next code task. Until then you
can exercise the pipeline by sending test messages to `websocket_url` with a
WebSocket client (e.g. `wscat`).

## Architecture recap

```
emitter ─push─►┐
                API Gateway (WebSocket)  ── traffic-ingest ─┬─ fan-out to browsers
browsers ─────►┘   $connect/$disconnect/sendmessage/$default │
                                                             └─ async ─► traffic-metrics ─► InfluxDB Cloud
dashboard (S3 + CloudFront)  ── replay reads ─► traffic-replay (Function URL) ─► InfluxDB Cloud
```

Note the metrics Lambda stores **aggregated metrics, not vehicle positions** (same
cardinality decision as local) — so `traffic-replay` can drive the charts, not the
map markers.

## Tear down

```bash
terraform destroy
```
Then, if you're fully done, delete the state bucket and lock table by hand.

## Cost

Target ~US$1–2/month: Secrets Manager (~$0.40/secret), everything else within the
AWS free tier. The billing alarm (default $5) is your safety net — do not ignore it.
