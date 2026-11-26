#!/bin/sh
# LOCO controller come add-on di HA: le opzioni dell'add-on diventano l'ambiente di loco_controller.py
set -eu
opz() { python3 -c 'import json,sys; v=json.load(open("/data/options.json")).get(sys.argv[1]); print("" if v is None else v)' "$1"; }
export CONTROLLER_DATI=/data
export CONTROLLER_SCHEDA="$(opz scheda)"
export CONTROLLER_DIAGNOSI="$(opz diagnosi | tr A-Z a-z)"
# s6-overlay v3 non passa l'ambiente del contenitore al CMD: il gettone del Supervisor (per Mosquitto) sta qui
if [ -z "${SUPERVISOR_TOKEN:-}" ] && [ -r /run/s6/container_environment/SUPERVISOR_TOKEN ]; then
  SUPERVISOR_TOKEN="$(cat /run/s6/container_environment/SUPERVISOR_TOKEN)"; export SUPERVISOR_TOKEN
fi
umask 077
exec python3 -u /opt/loco-controller/loco_controller.py
