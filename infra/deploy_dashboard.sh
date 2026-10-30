#!/usr/bin/env bash
#
# Deploy the dashboard and print the ready-to-open URL:
#   1. upload intersection_map.html + edges.geojson to the dashboard S3 bucket
#   2. invalidate the CloudFront cache for them
#   3. print the fully-wired URL (ws + replay + counts + edges attached)
#
# Optional first argument --rebuild-names also refreshes the counts dropdown's
# id -> name index (a one-off / occasional step, not needed on every deploy).
#
#   ./infra/deploy_dashboard.sh
#   ./infra/deploy_dashboard.sh --rebuild-names
#
# Runnable from anywhere; it cd's into its own infra/ directory.
set -euo pipefail
cd "$(dirname "$0")"

scripts="../smart_city_digital_twin/2D_simulation/scripts"
html="$scripts/intersection_map.html"
edges="../smart_city_digital_twin/2D_simulation/data/output/network/edges.geojson"
[[ -f "$html" ]] || { echo "dashboard not found: $html" >&2; exit 1; }

# Generate the road-overlay GeoJSON if it's missing (needs the net file present).
if [[ ! -f "$edges" ]]; then
  echo "edges.geojson missing — generating it ..."
  python3 "$scripts/edges_geojson.py" -o "$edges" || \
    echo "could not generate edges.geojson (net file absent?); the road overlay will 404" >&2
fi

# --- optional: refresh the intersection id -> name index --------------------
if [[ "${1:-}" == "--rebuild-names" ]]; then
  fn=$(aws lambda list-functions \
        --query "Functions[?ends_with(FunctionName, '-etl-traffic-counts')].FunctionName | [0]" \
        --output text)
  if [[ -z "$fn" || "$fn" == "None" ]]; then
    echo "could not find the etl-traffic-counts function; skipping name rebuild" >&2
  else
    echo "rebuilding intersection names via $fn ..."
    aws lambda invoke --cli-read-timeout 360 --function-name "$fn" \
      --payload '{"rebuild_names": true}' --cli-binary-format raw-in-base64-out /dev/stdout
    echo
  fi
fi

# --- upload + invalidate -----------------------------------------------------
bucket=$(terraform output -raw dashboard_bucket_name)
domain=$(terraform output -raw dashboard_url | sed 's#https://##')

echo "uploading intersection_map.html -> s3://$bucket/ ..."
aws s3 cp "$html" "s3://$bucket/intersection_map.html" --content-type "text/html"

paths="/intersection_map.html"
if [[ -f "$edges" ]]; then
  echo "uploading edges.geojson -> s3://$bucket/ ..."
  aws s3 cp "$edges" "s3://$bucket/edges.geojson" --content-type "application/geo+json"
  paths="$paths /edges.geojson"
fi

# The map loads the intersection nodes from ../data/output/intersection_geo.csv,
# which resolves to /data/output/intersection_geo.csv when served at the bucket
# root — upload it there so the hosted page can fetch it (otherwise it 404s and
# shows the "Could not load intersection_geo.csv" banner).
csv="../smart_city_digital_twin/2D_simulation/data/output/intersection_geo.csv"
if [[ -f "$csv" ]]; then
  echo "uploading intersection_geo.csv -> s3://$bucket/data/output/ ..."
  aws s3 cp "$csv" "s3://$bucket/data/output/intersection_geo.csv" --content-type "text/csv"
  paths="$paths /data/output/intersection_geo.csv"
else
  echo "intersection_geo.csv missing ($csv); the intersection layer will 404" >&2
fi

dist=$(aws cloudfront list-distributions \
        --query "DistributionList.Items[?DomainName=='$domain'].Id | [0]" --output text)
echo "invalidating CloudFront ($dist) ..."
inv=$(aws cloudfront create-invalidation --distribution-id "$dist" \
        --paths $paths --query "Invalidation.Id" --output text)
echo "invalidation $inv started (usually completes in ~1-2 min)"

# --- the URL to open ---------------------------------------------------------
# ws  = live vehicle feed (API Gateway WebSocket; browsers connect, the emitter forwards here)
# replay/counts = history/compare + CCC counts read endpoints; edges = road overlay
ws=$(terraform output -raw websocket_url)
counts=$(terraform output -raw counts_api_url)
replay=$(terraform output -raw replay_api_url)
echo
echo "open once the invalidation completes:"
printf 'https://%s/intersection_map.html?ws=%s&replay=%s&counts=%s&edges=/edges.geojson\n' \
  "$domain" "$ws" "$replay" "$counts"
echo
echo "(live vehicles need a producer forwarding to the same ws endpoint:"
printf '  run_traci.py --emit-target %s )\n' "$ws"
