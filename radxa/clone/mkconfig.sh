#!/usr/bin/env bash
# Per-unit /config partition image for a cloned Radxa.
#
#   mkconfig.sh GOLDEN.img NN OUT.img
#
# Copies partition 1 (the 16 MB FAT partition rsetup reads at boot) out
# of the golden image and drops a before.txt into it that names the
# unit; rawdisk.py writes it over the same partition on the card.
# Prints the byte offset of the partition on its last line.
set -euo pipefail
golden="$1"; nn="$2"; out="$3"
SECTOR=512
start=$(sgdisk -i 1 "$golden" 2>/dev/null | awk '/First sector/ {print $3}')
end=$(sgdisk -i 1 "$golden" 2>/dev/null | awk '/Last sector/ {print $3}')
count=$((end - start + 1))
dd if="$golden" of="$out" bs=$SECTOR skip="$start" count="$count" status=none
mnt=$(mktemp -d)
mount -o loop "$out" "$mnt"
printf 'update_hostname radxa-%s\nregenerate_ssh_hostkey\nresize_root\n' "$nn" > "$mnt/before.txt"
rm -f "$mnt/after.txt"
echo "before.txt for radxa-$nn:"; sed 's/^/  /' "$mnt/before.txt"
umount "$mnt"; rmdir "$mnt"
echo "OFFSET $((start * SECTOR))"
