#!/usr/bin/env bash
#
# Safely tear down THIS Terraform stack so `terraform destroy` completes in one go.
#
# A bare `terraform destroy` stops half-way here: neither S3 bucket has force_destroy
# (and the dashboard bucket is versioned), and the InfluxDB secret's 7-day recovery
# window blocks a re-apply. This script:
#   1. refuses to run unless the current AWS account is the one you name
#      (--expect-account) and is not listed in $TEARDOWN_PROTECTED_ACCOUNTS
#      (put your demo account ID there, e.g. in ~/.bashrc);
#   2. shows what will be destroyed and asks you to type the account ID;
#   3. optionally saves `terraform output` and a copy of the ETL data bucket (--backup DIR);
#   4. empties both S3 buckets, including every object version and delete marker;
#   5. runs `terraform destroy`;
#   6. force-deletes the two Secrets Manager secrets so their names are free at once.
# Left alone: the Terraform state bucket + lock table (created by hand), the InfluxDB
# bucket (outside Terraform), IAM users, and the AWS account itself.
#
#   ./infra/teardown.sh --expect-account 123456789012 --dry-run        # plan only
#   ./infra/teardown.sh --expect-account 123456789012 --backup ~/twin-backup
#
# Run it with the AWS_PROFILE of the environment you mean to destroy.
set -euo pipefail
cd "$(dirname "$0")"

usage() {
  cat <<'EOF'
usage: teardown.sh --expect-account ACCOUNT_ID [--backup DIR] [--dry-run] [--yes]

  --expect-account ID  required; the 12-digit account this stack lives in. The script
                       aborts if your current credentials belong to any other account.
  --backup DIR         save `terraform output` and a copy of the ETL data bucket to DIR
                       (choose a folder outside the repo).
  --dry-run            show what would be destroyed (terraform plan -destroy); change nothing.
  --yes                skip the confirmation prompts (still requires --expect-account).

Set TEARDOWN_PROTECTED_ACCOUNTS="<demo-account-id>" to make the script refuse that
account no matter what flags are passed.
EOF
}

die() { echo "error: $*" >&2; exit 1; }

expect="" backup="" dry_run=0 yes=0
while [ $# -gt 0 ]; do
  case "$1" in
    --expect-account) expect="${2:-}"; shift 2 ;;
    --backup)         backup="${2:-}"; shift 2 ;;
    --dry-run)        dry_run=1; shift ;;
    --yes)            yes=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                usage >&2; die "unknown argument: $1" ;;
  esac
done
[ -n "$expect" ] || { usage >&2; die "--expect-account is required"; }

for tool in terraform aws python3; do
  command -v "$tool" >/dev/null || die "$tool not found on PATH"
done

# --- 1. are we in the account we mean to destroy? ---------------------------------
actual=$(aws sts get-caller-identity --query Account --output text)
[ "$actual" = "$expect" ] || die "current credentials are for account $actual, not $expect (check AWS_PROFILE)"
protected_list="${TEARDOWN_PROTECTED_ACCOUNTS:-}"
for protected in ${protected_list//,/ }; do
  if [ "$protected" = "$actual" ]; then
    die "account $actual is in TEARDOWN_PROTECTED_ACCOUNTS; refusing"
  fi
done

terraform output -json >/dev/null 2>&1 || die "terraform state not readable here (run terraform init -backend-config=backend.hcl first)"

# Terraform reads the ETL zip sources even to destroy, so they must exist.
if [ ! -d build/etl_ingest ] || [ ! -d build/etl_query ]; then
  echo "ETL bundles missing in infra/build/ (Terraform needs them even to destroy); building..."
  python3 functions/build_etl.py || die "functions/build_etl.py failed; fix that, then re-run"
fi

out() { terraform output -raw "$1" 2>/dev/null || true; }
dash_bucket=$(out dashboard_bucket_name)
etl_bucket=$(out etl_data_bucket)
influx_secret=$(out influxdb_token_secret_name)
drive_secret=$(out drive_api_key_secret_name)
env_name=$(echo 'var.environment' | terraform console 2>/dev/null | tr -d '"' || true)
n_resources=$( (terraform state list 2>/dev/null || true) | wc -l | tr -d ' ')

# --- 2. show what will go ------------------------------------------------------------
cat <<EOF

About to tear down this Terraform stack:
  AWS account       : $actual   (profile: ${AWS_PROFILE:-default})
  environment       : ${env_name:-unknown}
  resources in state: $n_resources
  dashboard bucket  : ${dash_bucket:-<none>}
  ETL data bucket   : ${etl_bucket:-<none>}
  secrets           : ${influx_secret:-<none>}, ${drive_secret:-<none>}
Not touched: Terraform state bucket + lock table, the InfluxDB bucket, IAM users.

EOF

[ "$n_resources" -gt 0 ] || { echo "Nothing in state; nothing to destroy."; exit 0; }

if [ "$dry_run" -eq 1 ]; then
  terraform plan -destroy
  echo "Dry run only; nothing was changed."
  exit 0
fi

if [ "$yes" -ne 1 ]; then
  read -r -p "Type the account ID ($actual) to continue: " answer
  [ "$answer" = "$actual" ] || die "confirmation did not match; nothing was changed"
fi

# --- 3. optional backup ---------------------------------------------------------------
if [ -n "$backup" ]; then
  mkdir -p "$backup"
  terraform output > "$backup/terraform-outputs.txt"   # sensitive outputs print as <sensitive>
  if [ -n "$etl_bucket" ] && aws s3api head-bucket --bucket "$etl_bucket" >/dev/null 2>&1; then
    echo "backing up s3://$etl_bucket -> $backup/etl-data ..."
    aws s3 sync "s3://$etl_bucket" "$backup/etl-data" --only-show-errors
  fi
  echo "backup written to $backup"
fi

# --- 4. empty the buckets (every version + delete marker) -----------------------------
# Turns one page of list-object-versions JSON into a delete-objects payload (max 1000).
read -r -d '' to_payload <<'PY' || true
import json, sys
raw = sys.stdin.read().strip()
data = json.loads(raw) if raw else {}
objs = [{"Key": v["Key"], "VersionId": v["VersionId"]}
        for k in ("Versions", "DeleteMarkers") for v in (data.get(k) or [])][:1000]
print(len(objs))
print(json.dumps({"Objects": objs, "Quiet": True}))
PY

empty_bucket() {
  local bucket="$1" payload tmp count result deleted=0
  aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1 || { echo "  s3://$bucket: not found, skipping"; return 0; }
  tmp=$(mktemp)
  while :; do
    payload=$(aws s3api list-object-versions --bucket "$bucket" --max-items 1000 --output json \
              | python3 -c "$to_payload")
    count=$(printf '%s\n' "$payload" | head -n1)
    [ "$count" -gt 0 ] || break
    printf '%s\n' "$payload" | tail -n +2 > "$tmp"
    result=$(aws s3api delete-objects --bucket "$bucket" --delete "file://$tmp" --output json)
    if printf '%s' "$result" | grep -q '"Errors"'; then
      rm -f "$tmp"; die "could not delete some objects in s3://$bucket: $result"
    fi
    deleted=$((deleted + count))
  done
  rm -f "$tmp"
  echo "  s3://$bucket: emptied ($deleted object versions removed)"
}

echo "Emptying S3 buckets..."
for b in "$dash_bucket" "$etl_bucket"; do
  if [ -n "$b" ]; then empty_bucket "$b"; fi
done

# --- 5. destroy -------------------------------------------------------------------------
echo "Running terraform destroy (CloudFront can take 15+ minutes)..."
if [ "$yes" -eq 1 ]; then
  terraform destroy -auto-approve
else
  terraform destroy
fi

# --- 6. free the secret names now instead of after the recovery window ------------------
for s in "$influx_secret" "$drive_secret"; do
  [ -n "$s" ] || continue
  if aws secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery >/dev/null 2>&1; then
    echo "  secret $s: permanently deleted"
  else
    echo "  secret $s: already gone (or check it in the console)"
  fi
done

backend_bucket=$(sed -n 's/^[[:space:]]*bucket[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' backend.hcl 2>/dev/null | head -n1)
cat <<EOF

Teardown complete for account $actual.
Still there on purpose:
  - Terraform state bucket ${backend_bucket:-<see backend.hcl>} and the tf-state-lock table
    (delete by hand only if you're finished with this account)
  - the InfluxDB bucket (managed in InfluxDB Cloud, not Terraform)
  - IAM users and the account itself
EOF
