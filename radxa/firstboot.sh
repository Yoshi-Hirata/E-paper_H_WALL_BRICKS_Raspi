#!/usr/bin/env bash
# Per-unit network identity for a cloned Radxa: derive the static IPv4
# from the hostname and apply it to the Wi-Fi profile.
#
#   radxa-NN  ->  192.168.50.(100+NN)/24, gateway and DNS 192.168.50.1
#
# All ten appliances run one and the same microSD image; the only input
# that differs per unit is the hostname, which rsetup sets from the
# FAT-formatted /config partition (`update_hostname radxa-05` in
# /config/before.txt, editable from Windows right after writing the
# card). This runs after rsetup on every boot and is idempotent: a unit
# whose profile already carries the derived address is left alone, and
# a hostname outside the radxa-NN scheme means "not a clone, do nothing".
set -euo pipefail

SUBNET="192.168.50"
BASE=100
PREFIX=24
GATEWAY="${SUBNET}.1"
DNS="${SUBNET}.1"

host="$(hostname)"
if [[ ! "$host" =~ ^radxa-([0-9]{2})$ ]]; then
    echo "hostname '$host' is not radxa-NN; leaving the network alone"
    exit 0
fi
unit=$((10#${BASH_REMATCH[1]}))
want="${SUBNET}.$((BASE + unit))/${PREFIX}"

# The one Wi-Fi profile (system-wide, not MAC-bound, see radxa/README).
conn="$(nmcli -t -f NAME,TYPE connection show \
        | awk -F: '$2 == "802-11-wireless" { print $1; exit }')"
if [ -z "$conn" ]; then
    echo "no Wi-Fi connection profile; nothing to configure"
    exit 0
fi

have="$(nmcli -t -f ipv4.addresses connection show "$conn" | cut -d: -f2-)"
if [ "$have" = "$want" ]; then
    echo "$host: $conn already at $want"
    exit 0
fi

echo "$host: setting $conn to $want (was '${have:-dhcp}')"
nmcli connection modify "$conn" \
    ipv4.method manual ipv4.addresses "$want" \
    ipv4.gateway "$GATEWAY" ipv4.dns "$DNS"
# Re-activate so the address is live now, not after the next reboot.
# A failure here is not fatal: the profile is saved and the next boot
# comes up on the new address anyway.
nmcli connection up "$conn" || true
