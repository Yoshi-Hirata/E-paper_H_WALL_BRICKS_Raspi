#!/usr/bin/env bash
# Per-unit network identity for a cloned Radxa: derive the static IPv4
# from the hostname and apply it to the Wi-Fi profile.
#
#   radxa-NN  ->  192.168.51.(100+NN)/24, gateway and DNS 192.168.51.1
#
# All ten appliances run one and the same microSD image; the only input
# that differs per unit is the hostname, which rsetup sets from the
# FAT-formatted /config partition (`update_hostname radxa-05` in
# /config/before.txt, editable from Windows right after writing the
# card). This runs after rsetup on every boot and is idempotent: a unit
# whose profile already carries the derived address is left alone, and
# a hostname outside the radxa-NN scheme means "not a clone, do nothing".
set -euo pipefail

# 192.168.51: 192.168.50 is what home routers and ISPs hand out by default,
# and a travel router whose WAN lands on such a network moves its own LAN
# away from it (seen 2026-09-22: LAN silently became 192.168.51.1 and the
# units were unreachable). The show router's LAN is 192.168.51.1/24.
SUBNET="192.168.51"
BASE=100
PREFIX=24
GATEWAY="${SUBNET}.1"
DNS="${SUBNET}.1"

# rsetup.service is a plain (non-oneshot) unit, so After=rsetup.service
# only orders us behind its *start*: on radxa-02's first boot this ran
# while rsetup was still renaming the host and derived .101 from the
# golden image's name. Wait for rsetup to finish (it exits in a few
# seconds; a minute is generous).
for _ in $(seq 1 60); do
    systemctl is-active --quiet rsetup.service || break
    sleep 1
done

host="$(hostname)"
if [[ ! "$host" =~ ^radxa-([0-9]{2})$ ]]; then
    echo "hostname '$host' is not radxa-NN; leaving the network alone"
    exit 0
fi
unit=$((10#${BASH_REMATCH[1]}))
want="${SUBNET}.$((BASE + unit))/${PREFIX}"

# The exhibition network (2026-09-30): without router or PC, radxa-05
# is a hotspot with this SSID and every other unit carries a client
# profile of the same name (a 10.42.0.x address of its own). Neither is
# the router profile this script gives the per-unit address to - the
# LCD's WIFI row (ui/wifi.py) switches between them - so both are
# skipped below: the hotspot by what it is (mode ap / ipv4.method
# shared), the client profile by this name or SSID.
EXPO_SSID="AZ-Epaper"

field() {   # <profile> <setting> -> its value, "" when nmcli cannot say
    nmcli -t -f "$2" connection show "$1" 2>/dev/null | cut -d: -f2- || true
}

# The router's Wi-Fi profile (system-wide, not MAC-bound, see
# radxa/README). A unit now has more than one wireless profile, and
# before Wi-Fi is up `connection show` lists them alphabetically - so
# "the first" would be the hotspot on radxa-05 and the expo client
# profile everywhere else. Among the profiles that are not the expo
# ones, the active one wins, else the first listed.
conn=""
first=""
active_wifi=""
while IFS=: read -r name type active; do
    [ "$type" = "802-11-wireless" ] || continue
    [ "$active" = "yes" ] && active_wifi="${active_wifi:-$name}"
    mode="$(field "$name" 802-11-wireless.mode)"
    method="$(field "$name" ipv4.method)"
    ssid="$(field "$name" 802-11-wireless.ssid)"
    if [ "$mode" = "ap" ] || [ "$method" = "shared" ] \
            || [ "$name" = "$EXPO_SSID" ] || [ "$ssid" = "$EXPO_SSID" ]; then
        echo "skipping $name (expo profile: mode '${mode:-infrastructure}', ipv4 '$method', ssid '$ssid')"
        continue
    fi
    if [ "$active" = "yes" ]; then
        conn="$name"
    fi
    first="${first:-$name}"
done < <(nmcli -t -f NAME,TYPE,ACTIVE connection show)
conn="${conn:-$first}"
if [ -z "$conn" ]; then
    echo "no Wi-Fi connection profile; nothing to configure"
    exit 0
fi

have="$(nmcli -t -f ipv4.addresses connection show "$conn" | cut -d: -f2-)"
have_dns="$(nmcli -t -f ipv4.dns connection show "$conn" | cut -d: -f2-)"
if [ "$have" = "$want" ] && [ "$have_dns" = "$DNS" ]; then
    echo "$host: $conn already at $want"
    exit 0
fi

echo "$host: setting $conn to $want, dns $DNS (was '${have:-dhcp}', '${have_dns:-none}')"
nmcli connection modify "$conn" \
    ipv4.method manual ipv4.addresses "$want" \
    ipv4.gateway "$GATEWAY" ipv4.dns "$DNS"
# Re-activate so the address is live now, not after the next reboot.
# A failure here is not fatal: the profile is saved and the next boot
# comes up on the new address anyway. Never done over another wireless
# profile that is up (the hotspot, or the expo client network): that
# would move the unit off the network it was put on.
if [ -z "$active_wifi" ] || [ "$active_wifi" = "$conn" ]; then
    nmcli connection up "$conn" || true
else
    echo "$host: $active_wifi is up; $conn gets $want when it is next activated"
fi
