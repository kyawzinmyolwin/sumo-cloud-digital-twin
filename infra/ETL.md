# ETL deploy — CCC traffic-count workbooks

Terraform in `etl.tf` stands up the CCC Miovision traffic-count pipeline:

```
EventBridge (schedule)
      │
      ▼
etl-traffic-counts (Lambda)  ──►  S3 bucket (var.etl_data_bucket_name)
  walk public CCC Drive folder        raw-traffic-data/<folder>/<workbook>
  → download new/updated              processed-traffic-data/<intersection>/<date>.json
  → parse (traffic_counts_parser)     rejected-traffic-data/<folder>/<workbook>.json
  → store raw + cleaned               manifest/drive_manifest.json
      ▲                                     │
   Drive API key                            ▼
   (Secrets Manager)            query-traffic-counts (Lambda) ◄── API Gateway (HTTP)
                                  GET /traffic-counts?intersection=&date=
```

The Lambda code lives in `smart_city_digital_twin/2D_simulation/scripts/etl/`
(unit-tested, AWS-free core + thin handlers). `functions/build_etl.py` stages the
deployable bundles under `build/`.

## Prerequisites
- A Google Drive **API key** with the Drive API enabled (see the probe steps).
- `etl_data_bucket_name` set to a globally-unique name in `terraform.tfvars`.

## Deploy

```bash
# 1. Stage the Lambda bundles (archive_file can't pip-install).
python functions/build_etl.py

# 2. Create the infra (the Drive key secret is created empty).
terraform -chdir=infra init      # if not already
terraform -chdir=infra apply

# 3. Put the Drive API key into the secret (never in tfvars/state).
aws secretsmanager put-secret-value \
  --secret-id "$(terraform -chdir=infra output -raw drive_api_key_secret_name)" \
  --secret-string 'YOUR_DRIVE_API_KEY'
```

Re-run `python functions/build_etl.py` before any `apply` that should pick up
Lambda code changes (the zip hash drives redeploys).

## First backfill (bounded)
A single scheduled run does incremental work, but the first pass sees ~1,451
files — too many for one 300 s invocation. Backfill in bounded batches; the
manifest lets each run resume where the last stopped:

```bash
FN="$(terraform -chdir=infra output -raw etl_data_bucket >/dev/null 2>&1; \
      echo christchurch-digital-twin-prod-etl-traffic-counts)"
# invoke a few times with a cap until 'selected' reaches 0
aws lambda invoke --function-name "$FN" \
  --payload '{"max_files": 100}' --cli-binary-format raw-in-base64-out /dev/stdout
```

## Query it
```bash
API="$(terraform -chdir=infra output -raw counts_api_url)"
curl "$API?intersection=I0007&date=2016-08-24"   # that survey's rows
curl "$API?intersection=I0007"                   # dates available for it
curl "$API"                                       # list intersections (discovery)
```

## Notes
- **Scheduled cadence:** `etl_schedule_expression` (default `rate(7 days)`).
- **Rejected files:** pedestrian counts, blank templates and summaries land under
  `rejected-traffic-data/` with a reason — expected, not an error.
- **Secret recovery window** is 0 so destroy/recreate isn't blocked (CLAUDE.md gotcha).
- **Terraform state** may hold resource metadata — as with the rest of `infra/`,
  keep state in the S3 backend, not the repo.
