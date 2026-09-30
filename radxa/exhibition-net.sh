#!/usr/bin/env bash
# EXHIBITION mode's network fallback for radxa-05 (the Conductor host):
# at boot, wait for any CLIENT Wi-Fi profile to come up on its own (the
# venue router, the office router - whatever NetworkManager autoconnects
# to); if none does, bring the AZ-Epaper hotspot up.
#
# The hotspot profile itself stays autoconnect=no (radxa/README.md, the
# WIFI row): this script is the only thing that starts it unattended, so
# enabling epaper-exhibition-net.service IS "exhibition mode armed" -
# wherever a router the unit knows is in reach, radxa-05 joins it as a
# client exactly as before; where there is none (the venue), it becomes
# the hotspot the other units and the operator's phone join. The LCD's
# WIFI row switches either way at any time. Idempotent, logs either way,
# never fails the boot.
#
# Two things the first version got wrong (review, 2026-09-30): a profile
# that is "activating" and then fails counted as up (radxa-05 sat on no
# network) - only STATE `activated` counts now; and an office router that
# boots slowly after a joint power cut lost to the 30 s clock - so as long
# as the SSID of a KNOWN client profile is visible in a scan, this keeps
# waiting (up to SCAN_WAIT_S more) before falling back to the hotspot.
set -uo pipefail

HOTSPOT="${HOTSPOT_PROFILE:-AZ-Epaper}"
WAIT_S="${WAIT_S:-90}"            # plain wait for a client profile to come up
SCAN_WAIT_S="${SCAN_WAIT_S:-600}" # ...longer, while a known router is in sight

client_up() {
    # An ACTIVATED wireless connection that is NOT the hotspot profile.
    nmcli -t -f NAME,TYPE,DEVICE,STATE connection show --active 2>/dev/null \
        | awk -F: -v hs="$HOTSPOT" \
              '$2 == "802-11-wireless" && $1 != hs && $3 != "" && $4 == "activated" { found = 1 } END { exit !found }'
}

hotspot_up() {
    nmcli -t -f NAME,STATE connection show --active 2>/dev/null \
        | awk -F: -v hs="$HOTSPOT" '$1 == hs && $2 == "activated" { found = 1 } END { exit !found }'
}

known_ssids() {
    # The SSIDs of every wireless profile that is not the hotspot.
    nmcli -t -f NAME,TYPE connection show 2>/dev/null \
        | awk -F: -v hs="$HOTSPOT" '$2 == "802-11-wireless" && $1 != hs { print $1 }' \
        | while IFS= read -r name; do
            nmcli -t -f 802-11-wireless.ssid connection show "$name" 2>/dev/null | cut -d: -f2-
          done | grep -v '^$'
}

known_visible() {
    local wanted seen
    wanted="$(known_ssids)"
    [ -n "$wanted" ] || return 1
    seen="$(nmcli -t -f SSID dev wifi list --rescan yes 2>/dev/null | grep -v '^$')"
    [ -n "$seen" ] || return 1
    grep -Fxq -f <(printf '%s\n' "$wanted") <(printf '%s\n' "$seen")
}

settled() {
    if client_up; then
        echo "exhibition-net: a client Wi-Fi is activated; leaving $HOTSPOT down"
        return 0
    fi
    if hotspot_up; then
        echo "exhibition-net: $HOTSPOT is already up"
        return 0
    fi
    return 1
}

for _ in $(seq 1 "$WAIT_S"); do
    settled && exit 0
    sleep 1
done

# No client profile came up in WAIT_S. A known router still in sight is
# worth waiting for (it may be booting after the same power cut).
waited=0
while [ "$waited" -lt "$SCAN_WAIT_S" ] && known_visible; do
    echo "exhibition-net: a known router is visible but not joined yet - waiting (${waited}s)"
    sleep 10
    waited=$((waited + 10))
    settled && exit 0
done

if ! nmcli -t -f NAME connection show 2>/dev/null | grep -qx -- "$HOTSPOT"; then
    echo "exhibition-net: no client Wi-Fi and no $HOTSPOT profile to start"
    exit 0
fi
echo "exhibition-net: no client Wi-Fi - starting the $HOTSPOT hotspot"
if nmcli connection up "$HOTSPOT"; then
    echo "exhibition-net: $HOTSPOT is up"
else
    echo "exhibition-net: nmcli could not bring $HOTSPOT up (see journalctl -u NetworkManager)"
fi
exit 0
