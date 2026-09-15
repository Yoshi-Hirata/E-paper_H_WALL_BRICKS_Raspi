"""Raw read/write of a Windows physical drive (needs an elevated process).

    rawdisk.py read  N OUT.img LOG
        whole \\.\PhysicalDriveN -> file

    rawdisk.py write N IN.img LOG [OFFSET PATCH.img]
        file -> \\.\PhysicalDriveN, then optionally PATCH.img at byte
        OFFSET (the per-unit /config partition), then a fresh backup GPT
        header at the end of the disk and the primary header pointed at
        it - the image is truncated after the root filesystem, so the
        golden card's backup header is not in it.

Progress lines go to LOG; the last line is "DONE <bytes>" or "ERROR ...".
wsl --mount cannot attach USB card readers (HCS 0x8007000f), so this is
the read/write primitive for the golden image workflow.
"""
import ctypes
import os
import struct
import sys
import time
import zlib

CHUNK = 8 * 1024 * 1024
SECTOR = 512
GPT_HEADER_SIZE = 92


def disk_size(handle_path: str) -> int:
    """IOCTL_DISK_GET_LENGTH_INFO, in bytes."""
    import ctypes.wintypes as wt
    h = ctypes.windll.kernel32.CreateFileW(
        handle_path, 0x80000000, 0x3, None, 3, 0, None)
    if h in (ctypes.c_void_p(-1).value, -1):
        raise OSError(ctypes.get_last_error(), "CreateFile failed")
    length = ctypes.c_longlong(0)
    out = wt.DWORD(0)
    ok = ctypes.windll.kernel32.DeviceIoControl(
        h, 0x7405C, None, 0, ctypes.byref(length), 8, ctypes.byref(out), None)
    ctypes.windll.kernel32.CloseHandle(h)
    if not ok:
        raise OSError("IOCTL_DISK_GET_LENGTH_INFO failed")
    return length.value


def copy(src, dst, total, log, t0):
    done = 0
    while done < total:
        buf = src.read(min(CHUNK, total - done))
        if not buf:
            break
        dst.write(buf)
        done += len(buf)
        if done % (CHUNK * 32) == 0 or done == total:
            rate = done / max(time.monotonic() - t0, 1e-6) / 1e6
            log.write(f"{done} / {total} ({done * 100 // total}%) "
                      f"{rate:.0f} MB/s\n")
    return done


def _crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def fix_gpt(dev, cap: int, log) -> None:
    """Rebuild the backup GPT at the disk end from the primary header."""
    last = cap // SECTOR - 1
    dev.seek(SECTOR)
    hdr = bytearray(dev.read(SECTOR))
    if hdr[:8] != b"EFI PART":
        raise RuntimeError("no GPT signature at LBA 1")
    hsize = struct.unpack_from("<I", hdr, 12)[0]
    entries_lba, n_entries, esize = struct.unpack_from("<QII", hdr, 72)
    entries_bytes = n_entries * esize
    entries_sectors = (entries_bytes + SECTOR - 1) // SECTOR
    dev.seek(entries_lba * SECTOR)
    entries = dev.read(entries_sectors * SECTOR)
    if _crc(entries[:entries_bytes]) != struct.unpack_from("<I", hdr, 88)[0]:
        raise RuntimeError("primary partition entries CRC mismatch")
    # The partitions must fit on this disk.
    for i in range(n_entries):
        e = entries[i * esize:(i + 1) * esize]
        if e[:16] == b"\0" * 16:
            continue
        end_lba = struct.unpack_from("<Q", e, 40)[0]
        if end_lba > last - entries_sectors - 1:
            raise RuntimeError(f"partition {i + 1} ends at LBA {end_lba}, "
                               f"beyond this disk (last usable "
                               f"{last - entries_sectors - 1})")
    backup_entries_lba = last - entries_sectors
    last_usable = backup_entries_lba - 1

    def finish(h: bytearray, my, alt, part_lba) -> bytes:
        struct.pack_into("<Q", h, 24, my)
        struct.pack_into("<Q", h, 32, alt)
        struct.pack_into("<Q", h, 48, last_usable)
        struct.pack_into("<Q", h, 72, part_lba)
        struct.pack_into("<I", h, 16, 0)
        struct.pack_into("<I", h, 16, _crc(bytes(h[:hsize])))
        return bytes(h)

    primary = finish(bytearray(hdr), 1, last, entries_lba)
    backup = finish(bytearray(hdr), last, 1, backup_entries_lba)
    dev.seek(backup_entries_lba * SECTOR)
    dev.write(entries)
    dev.seek(last * SECTOR)
    dev.write(backup)
    dev.seek(SECTOR)
    dev.write(primary)
    log.write(f"GPT: backup header at LBA {last}, entries at "
              f"{backup_entries_lba}, last usable {last_usable}\n")


def main() -> int:
    mode, number, path, log_path = sys.argv[1:5]
    dev_path = rf"\\.\PhysicalDrive{number}"
    log = open(log_path, "w", buffering=1)
    t0 = time.monotonic()
    try:
        if mode == "read":
            total = disk_size(dev_path)
            log.write(f"disk {dev_path} size {total}\n")
            with open(dev_path, "rb", buffering=0) as src, \
                    open(path, "wb") as dst:
                done = copy(src, dst, total, log, t0)
            log.write(f"DONE {done}\n")
        elif mode == "write":
            total = os.path.getsize(path)
            cap = disk_size(dev_path)
            if total > cap:
                raise RuntimeError(f"image {total} larger than disk {cap}")
            if total % SECTOR:
                raise RuntimeError("image is not sector aligned")
            patch = None
            if len(sys.argv) >= 7:
                patch = (int(sys.argv[5]), sys.argv[6])
            log.write(f"disk {dev_path} size {cap}, image {total}\n")
            with open(dev_path, "r+b", buffering=0) as dev:
                with open(path, "rb") as src:
                    done = copy(src, dev, total, log, t0)
                if patch:
                    offset, pfile = patch
                    with open(pfile, "rb") as pf:
                        data = pf.read()
                    if offset % SECTOR or len(data) % SECTOR:
                        raise RuntimeError("patch not sector aligned")
                    dev.seek(offset)
                    dev.write(data)
                    log.write(f"patched {len(data)} bytes at {offset}\n")
                fix_gpt(dev, cap, log)
                dev.flush()
                os.fsync(dev.fileno())
            log.write(f"DONE {done}\n")
        else:
            raise RuntimeError(f"unknown mode {mode}")
    except Exception as exc:  # noqa: BLE001
        log.write(f"ERROR {exc!r}\n")
        return 1
    finally:
        log.write(f"elapsed {time.monotonic() - t0:.0f}s\n")
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
