#!/usr/bin/env bash
#
# Print the full dashboard URL with the live counts + replay endpoints wired in,
# straight from terraform outputs — so you never hand-build the query string
# (and never mix up ?counts=.../?replay=... again; the separator must be &).
#
# Runnable from anywhere (it cd's into its own infra/ directory):
#     ./infra/dashboard_url.sh
#     open "$(./infra/dashboard_url.sh)"          # macOS
#     xdg-open "$(./infra/dashboard_url.sh)"      # Linux desktop
#
# Uses replay_api_url (the API Gateway endpoint that works), NOT replay_url
# (the Lambda Function URL, which 403s on this account).
set -euo pipefail
cd "$(dirname "$0")"

dash=$(terraform output -raw dashboard_url)
ws=$(terraform output -raw websocket_url)
counts=$(terraform output -raw counts_api_url)
replay=$(terraform output -raw replay_api_url)

printf '%s/intersection_map.html?ws=%s&replay=%s&counts=%s&edges=/edges.geojson\n' \
  "$dash" "$ws" "$replay" "$counts"
