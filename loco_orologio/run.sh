#!/bin/sh
# LOCO orologio come add-on di HA: le opzioni dell'add-on diventano l'ambiente di loco_orologio.py
set -eu
opz() { python3 -c 'import json,sys; v=json.load(open("/data/options.json")).get(sys.argv[1]); print("" if v is None else v)' "$1"; }
export OROLOGIO_ASCOLTO=0.0.0.0
export OROLOGIO_DA="$(opz da)"
export OROLOGIO_PORTA="$(opz porta)"
export OROLOGIO_STATO=/data/stato.json
export OROLOGIO_GETTONE=/data/gettone
export OROLOGIO_SCHEDA="$(opz scheda)"
umask 077
opz gettone > /data/gettone
[ -n "$OROLOGIO_SCHEDA" ] || unset OROLOGIO_SCHEDA
exec python3 -u /opt/loco-orologio/loco_orologio.py
