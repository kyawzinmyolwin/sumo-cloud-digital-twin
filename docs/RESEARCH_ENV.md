# Research environment — a separate AWS copy that can't touch the demo

A guide to standing up a **second, fully isolated copy** of the cloud stack for research and
experiments (for example, trying ECS Fargate), while the demo deployment stays untouched.

The demo is the stack described in [`../infra/README.md`](../infra/README.md). Nothing in this
guide changes it.

## Why a separate AWS account (not just a second environment)

The Terraform already names every account-level resource from `project` + `environment`, so a
`environment = "research"` copy *in the same account* would deploy without name clashes. It
still can't fully protect the demo:

| Risk to the demo | Same account | Separate account |
|---|---|---|
| Running `apply`/`destroy` with the demo's state or credentials by mistake | Possible | Impossible: research credentials can't see the demo |
| Lambda concurrency and API Gateway throttling (account-wide, per region) | Shared | Separate |
| One set of admin credentials that can delete anything | Yes | No |
| Account-wide settings (networking, quotas) changed while experimenting | Shared | Separate |

The first row is the real risk: most "I broke prod" incidents are the right command run against
the wrong environment. A separate account removes it entirely.

## What stays shared, and why that's safe

- **The GitHub repo** — research happens on a **branch** (e.g. `research/fargate`). `main` is only
  changed deliberately. The Pages workflow (`deploy-dashboard.yml`) only runs on pushes to `main`,
  so research commits never redeploy anything.
- **The InfluxDB Cloud account** — research gets its **own bucket** and a **token scoped to only that
  bucket**, so it can't read, write or delete the demo's data. See step 5 for when to use a separate
  InfluxDB account instead.
- **The Google Drive API key** — the CCC folder is public and the key is read-only, so reusing it is
  fine. Create a separate key if you want to revoke research access independently.

---

## Setup

### 1. Create the research AWS account

**Option A — AWS Organizations member account (recommended).** In the current (demo) account:
*AWS Organizations → Add an AWS account → Create an AWS account*. It takes minutes and costs
nothing.
- One combined bill; you reach the new account by switching role from your existing login, so
  there's no extra password.
- Enabling Organizations makes the demo account the **management account**. That's an account
  setting only; it doesn't touch any demo resources.
- The new account gets a role named `OrganizationAccountAccessRole` with **administrator access**,
  which your demo-account user assumes.

**Option B — a standalone account** with a different email address. Fully independent, with a
separate bill. Enable MFA on its root user and create an IAM user for day-to-day work.

Note the new account's **12-digit account ID**; you'll need it below.

### 2. Add a `research` AWS CLI profile

For Option A, add this to `~/.aws/config` on the machine you'll work from:

```ini
[profile research]
role_arn       = arn:aws:iam::<RESEARCH_ACCOUNT_ID>:role/OrganizationAccountAccessRole
source_profile = default          # the profile you use for the demo account
region         = ap-southeast-2
# mfa_serial   = arn:aws:iam::<DEMO_ACCOUNT_ID>:mfa/<your-user>   # if your user requires MFA
```

For Option B, run `aws configure --profile research` with that account's IAM user keys.

Check you're in the right place — this is the habit that prevents mistakes:

```bash
AWS_PROFILE=research aws sts get-caller-identity --query Account --output text
# must print the RESEARCH account ID, never the demo one
```

### 3. Bootstrap Terraform state in the research account

Terraform can't create its own backend, so make a state bucket and lock table **in the research
account** (bucket names are globally unique):

```bash
export AWS_PROFILE=research
aws s3 mb s3://<research-tfstate-bucket> --region ap-southeast-2
aws s3api put-bucket-versioning --bucket <research-tfstate-bucket> \
  --versioning-configuration Status=Enabled
aws dynamodb create-table --table-name tf-state-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST --region ap-southeast-2
```

### 4. Separate folder, separate branch

Keep the research clone in its own folder so its config files can never be mixed up with the
demo's:

```bash
mkdir -p ~/research && cd ~/research
git clone https://github.com/kyawzinmyolwin/sumo-cloud-digital-twin.git
cd sumo-cloud-digital-twin
git checkout -b research/fargate        # any name; push it if the sim host must clone it
```

### 5. Create the research InfluxDB bucket and token

In the InfluxDB Cloud UI:
1. **Load Data → Buckets → Create Bucket** named `traffic-metrics-research`.
2. **Load Data → API Tokens → Generate API Token → Custom API Token** with **read and write on
   `traffic-metrics-research` only**. Copy it; it goes into Secrets Manager in step 8.

Use a **separate free InfluxDB account** instead only if you plan heavy write or load testing: the
free plan's rate limits apply to the whole account, so a flood of research writes could slow the
demo's. Also check how many buckets your plan allows.

### 6. Write fresh config files — never copy the demo's

`infra/backend.hcl`:

```hcl
bucket         = "<research-tfstate-bucket>"
key            = "christchurch-digital-twin/research/terraform.tfstate"
region         = "ap-southeast-2"
dynamodb_table = "tf-state-lock"
encrypt        = true
```

`infra/terraform.tfvars`:

```hcl
environment          = "research"   # resources become christchurch-digital-twin-research-*

# Globally unique across all of S3 — new names, not the demo's
dashboard_bucket_name = "christchurch-digital-twin-research-dashboard-<suffix>"
etl_data_bucket_name  = "christchurch-digital-twin-research-traffic-data-<suffix>"

alarm_email          = "you@example.com"
billing_alarm_threshold_usd = 5

influxdb_url         = "https://REGION.aws.cloud2.influxdata.com"
influxdb_org         = "your-influx-org"
influxdb_bucket_name = "traffic-metrics-research"

# The research sim host clones this branch instead of main (push it first)
sim_host_git_ref     = "research/fargate"
# sim_host_enabled   = false        # default; turn on only while testing

# Optional: cap the first ETL backfill in the research copy
# etl_max_files = "50"
```

Both files are gitignored. Keep them only in the research clone.

### 7. Build and deploy

```bash
cd ~/research/sumo-cloud-digital-twin/infra
export AWS_PROFILE=research
aws sts get-caller-identity --query Account --output text   # research ID? then continue
python functions/build_etl.py
terraform init -backend-config=backend.hcl
terraform plan          # read it — every name should contain "research"
terraform apply
```

### 8. Fill in the secrets and deploy the dashboard

```bash
aws secretsmanager put-secret-value \
  --secret-id "$(terraform output -raw influxdb_token_secret_name)" \
  --secret-string '<research-scoped InfluxDB token>'
aws secretsmanager put-secret-value \
  --secret-id "$(terraform output -raw drive_api_key_secret_name)" \
  --secret-string '<Drive API key>'
./deploy_dashboard.sh        # deploys to the research bucket and prints its URL
```

Then confirm the billing-alarm subscription email AWS sends you.

---

## Guardrails

- **Set the profile automatically per folder.** With [direnv](https://direnv.net/), put the file in
  the **parent** folder so it never ends up in the repo:
  ```bash
  echo 'export AWS_PROFILE=research' > ~/research/.envrc && direnv allow ~/research
  ```
- **Show the active profile in your prompt**, e.g. in `~/.bashrc`:
  ```bash
  PS1='[aws:${AWS_PROFILE:-default}] '"$PS1"
  ```
- **Check the account before every `apply` or `destroy`:** `aws sts get-caller-identity`.
- **Read every `plan`.** In the research copy, every resource name should contain `research`.
- **Never** copy the demo's `backend.hcl`, `terraform.tfvars` or `.env` into the research folder,
  and never run Terraform in the demo folder with `AWS_PROFILE=research` (or the reverse).
- **Optional hardening (needs a small Terraform change):** the AWS provider's
  `allowed_account_ids` setting makes Terraform refuse to run against any other account. Adding it
  per environment would turn the account check into an automatic one.

## Cost

Idle cost is about the same as the demo: roughly US$1–2 a month, mostly the two secrets. The
research EC2 sim host (or Fargate tasks) cost money only while they run.

- **Billing alarm in a member account:** AWS may publish billing metrics only to the management
  account under Organizations. If the research alarm never shows data, create an **AWS Budget** in
  the research account instead, or watch its spend from the management account.

## Tearing the research copy down

This is where a separate account pays off: you can destroy freely. To make the destroy complete in
one go:

1. Set `recovery_window_in_days = 0` on the InfluxDB secret in `infra/storage.tf` and `apply` — or,
   after destroying, run
   `aws secretsmanager delete-secret --secret-id <name> --force-delete-without-recovery`.
   Otherwise a re-apply within 7 days fails because the name is still reserved.
2. **Empty both S3 buckets.** Neither has `force_destroy`. The dashboard bucket is **versioned**, so
   delete all object versions (the S3 console's **Empty** button does this); a plain
   `aws s3 rm --recursive` leaves old versions behind.
3. `terraform destroy`.
4. Keep or delete the research state bucket and lock table, and (if you're finished with it) close
   the research account.

The demo account is not involved at any step.
