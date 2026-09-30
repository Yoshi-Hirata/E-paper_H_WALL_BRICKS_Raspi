#!/usr/bin/env bash
# EXHIBITION mode's network fallback for radxa-05 (the Conductor host):
# at boot, wait up to WAIT_S for any CLIENT Wi-Fi profile to come up on
# its own (the venue router, the office router - whatever NetworkManager
# autoconnects to); if none does, bring the AZ-Epaper hotspot up.
#
# The hotspot profile itself stays autoconnect=no (radxa/README.md, the
# WIFI row): this script is the only thing that starts it unattended, so
# enabling epaper-exhibition-net.service IS "exhibition mode armed" -
# wherever a router the unit knows is in reach, radxa-05 joins it as a
# client exactly as before; where there is none (the venue), it becomes
# the hotspot the other units and the operator's phone join. The LCD's
# WIFI row switches either way at any time. Idempotent, logs either way,
# never fails the boot.
set -uo pipefail

HOTSPOT="${HOTSPOT_PROFILE:-AZ-Epaper}"
WAIT_S="${WAIT_S:-30}"

client_up() {
    # An active wireless connection that is NOT the hotspot profile.
    nmcli -t -f NAME,TYPE,DEVICE connection show --active 2>/dev/null \
        | awk -F: -v hs="$HOTSPOT" '$2 == "802-11-wireless" && $1 != hs && $3 != "" { found = 1 } END { exit !found }'
}

hotspot_up() {
    nmcli -t -f NAME connection show --active 2>/dev/null | grep -qx -- "$HOTSPOT"
}

for _ in $(seq 1 "$WAIT_S"); do
    if client_up; then
        echo "exhibition-net: a client Wi-Fi is up; leaving $HOTSPOT down"
        exit 0
    fi
    if hotspot_up; then
        echo "exhibition-net: $HOTSPOT is already up"
        exit 0
    fi
    sleep 1
done

if ! nmcli -t -f NAME connection show 2>/dev/null | grep -qx -- "$HOTSPOT"; then
    echo "exhibition-net: no client Wi-Fi after ${WAIT_S}s and no $HOTSPOT profile to start"
    exit 0
fi
echo "exhibition-net: no client Wi-Fi after ${WAIT_S}s - starting the $HOTSPOT hotspot"
if nmcli connection up "$HOTSPOT"; then
    echo "exhibition-net: $HOTSPOT is up"
else
    echo "exhibition-net: nmcli could not bring $HOTSPOT up (see journalctl -u NetworkManager)"
fi
exit 0
