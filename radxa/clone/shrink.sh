#!/usr/bin/env bash
# Shrink the golden Radxa image: minimise the ext4 root filesystem and
# truncate the file right after it. The GPT partition table is left
# untouched (partition 3 still spans the whole 28.9 GB card), so a
# clone written to a same-size card needs only `resize_root` (rsetup,
# resize2fs) on first boot; the backup GPT header is re-created at the
# end of the target disk by the writer (rawdisk.py write).
#
#   shrink.sh FULL.img OUT.img
set -euo pipefail
full="$1"; out="$2"
SECTOR=512

echo "== partition table"
sgdisk -p "$full"
start=$(sgdisk -i 3 "$full" 2>/dev/null | awk '/First sector/ {print $3}')
echo "root partition starts at sector $start"

# WSL has no udev, so partition nodes (loopNp3) never appear; attach
# the root partition by offset instead.
end=$(sgdisk -i 3 "$full" 2>/dev/null | awk '/Last sector/ {print $3}')
cp --sparse=always "$full" "$out"
part=$(losetup -f --show -o $((start * SECTOR)) --sizelimit $(((end - start + 1) * SECTOR)) "$out")
trap 'losetup -d "$part" 2>/dev/null || true' EXIT
echo "== fsck"
e2fsck -fy "$part" || [ $? -le 1 ]
echo "== shrink filesystem to minimum"
resize2fs -M "$part"
e2fsck -fy "$part" || [ $? -le 1 ]
blocks=$(dumpe2fs -h "$part" 2>/dev/null | awk -F: '/^Block count/ {gsub(/ /,"",$2); print $2}')
bsize=$(dumpe2fs -h "$part" 2>/dev/null | awk -F: '/^Block size/ {gsub(/ /,"",$2); print $2}')
losetup -d "$part"; trap - EXIT
fs_bytes=$((blocks * bsize))
end_bytes=$((start * SECTOR + fs_bytes))
# round up to a whole MiB so the file stays 512-aligned with headroom
end_bytes=$(( (end_bytes + 1048575) / 1048576 * 1048576 ))
echo "filesystem $blocks x $bsize = $fs_bytes bytes; image end at $end_bytes"
truncate -s "$end_bytes" "$out"
ls -la "$out"
echo "== verify"
sgdisk -v "$out" 2>&1 | tail -3 || true
part=$(losetup -f --show -o $((start * SECTOR)) --sizelimit "$fs_bytes" "$out"); e2fsck -fn "$part" | tail -1; losetup -d "$part"
echo "DONE"
