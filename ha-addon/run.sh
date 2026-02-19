#!/usr/bin/env bash
set -euo pipefail

json_file="/data/options.json"

anycubic_ip="$(jq -r '.anycubic_ip' "$json_file")"
mqtt_broker="$(jq -r '.mqtt_broker' "$json_file")"
mqtt_port="$(jq -r '.mqtt_port' "$json_file")"
mqtt_user="$(jq -r '.mqtt_user // ""' "$json_file")"
mqtt_pass="$(jq -r '.mqtt_pass // ""' "$json_file")"
snap_idle="$(jq -r '.snapshot_interval_idle' "$json_file")"
snap_busy="$(jq -r '.snapshot_interval_busy' "$json_file")"
info_int="$(jq -r '.info_update_interval' "$json_file")"

export ANYCUBIC_S1_IP="$anycubic_ip"

# El bridge usa estos nombres internamente; en UI lo llamamos mqtt_* (como toca)
export HA_BROKER="$mqtt_broker"
export HA_PORT="$mqtt_port"
export HA_USER="$mqtt_user"
export HA_PASS="$mqtt_pass"

export SNAPSHOT_INTERVAL_IDLE="$snap_idle"
export SNAPSHOT_INTERVAL_BUSY="$snap_busy"
export INFO_UPDATE_INTERVAL="$info_int"

cd /anycubic
exec python3 asm.py
