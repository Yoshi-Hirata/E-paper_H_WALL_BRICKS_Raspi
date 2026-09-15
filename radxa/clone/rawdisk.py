r"""Raw read/write of a Windows physical drive (needs an elevated process).

    rawdisk.py read  N OUT.img LOG
        whole \\.\PhysicalDriveN -> file

    rawdisk.py write N IN.img LOG [OFFSET PATCH.img]
        file -> \\.\PhysicalDriveN, then optionally PATCH.img at byte
        OFFSET (the per-unit /config partition), then a fresh backup GPT
        header at the end of the disk and the primary header pointed at
        it - the image is truncated after the root filesystem, so the
        golden card's backup header is not in it.

Progress lines go to LOG; the last line is "DONE <bytes>" or "ERROR ...".

Access goes through the Win32 API directly (CreateFile / ReadFile /
WriteFile), the way Win32DiskImager does it: Python's open() on a
physical drive reads fine but its writes fail with EBADF, and Windows
refuses writes to a disk whose volumes are mounted unless each volume
is locked and dismounted first (a blank card comes formatted FAT32 and
mounted as a drive letter). wsl --mount cannot attach USB card readers
(HCS 0x8007000f), so this is the read/write primitive for the golden
image workflow.
"""
import ctypes
import os
import struct
import sys
import time
import zlib

CHUNK = 8 * 1024 * 1024
SECTOR = 512

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
FILE_BEGIN = 0
INVALID_HANDLE = ctypes.c_void_p(-1).value
IOCTL_DISK_GET_LENGTH_INFO = 0x7405C
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080
FSCTL_LOCK_VOLUME = 0x90018
FSCTL_UNLOCK_VOLUME = 0x9001C
FSCTL_DISMOUNT_VOLUME = 0x90020
FILE_DEVICE_DISK = 7

if sys.platform == "win32":
    import ctypes.wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wt.HANDLE
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                wt.DWORD, wt.DWORD, wt.HANDLE]
    k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                             ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                              ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    k32.SetFilePointerEx.argtypes = [wt.HANDLE, ctypes.c_longlong,
                                     ctypes.POINTER(ctypes.c_longlong), wt.DWORD]
    k32.DeviceIoControl.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
                                    ctypes.c_void_p, wt.DWORD,
                                    ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    k32.FlushFileBuffers.argtypes = [wt.HANDLE]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.GetLogicalDrives.restype = wt.DWORD


def _fail(what: str) -> OSError:
    err = ctypes.get_last_error()
    return OSError(err, f"{what}: [WinError {err}] {ctypes.FormatError(err)}")


class RawDisk:
    """A Win32 handle with the file-like read/seek/write the rest expects."""

    def __init__(self, path: str, write: bool = False):
        access = GENERIC_READ | (GENERIC_WRITE if write else 0)
        self.h = k32.CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                 None, OPEN_EXISTING, 0, None)
        if self.h is None or self.h == INVALID_HANDLE:
            raise _fail(f"open {path}")
        self.path = path

    def ioctl(self, code: int, out_len: int = 0) -> bytes:
        out = ctypes.create_string_buffer(out_len) if out_len else None
        got = wt.DWORD(0)
        if not k32.DeviceIoControl(self.h, code, None, 0, out, out_len,
                                   ctypes.byref(got), None):
            raise _fail(f"ioctl 0x{code:X} on {self.path}")
        return out.raw[:got.value] if out else b""

    def size(self) -> int:
        return struct.unpack("<q", self.ioctl(IOCTL_DISK_GET_LENGTH_INFO, 8))[0]

    def seek(self, offset: int) -> None:
        if not k32.SetFilePointerEx(self.h, offset, None, FILE_BEGIN):
            raise _fail(f"seek {offset}")

    def read(self, n: int) -> bytes:
        buf = ctypes.create_string_buffer(n)
        got = wt.DWORD(0)
        if not k32.ReadFile(self.h, buf, n, ctypes.byref(got), None):
            raise _fail("ReadFile")
        return buf.raw[:got.value]

    def write(self, data: bytes) -> int:
        done = wt.DWORD(0)
        if not k32.WriteFile(self.h, data, len(data), ctypes.byref(done), None):
            raise _fail("WriteFile")
        if done.value != len(data):
            raise OSError(f"short write: {done.value} of {len(data)}")
        return done.value

    def flush(self) -> None:
        if not k32.FlushFileBuffers(self.h):
            raise _fail("FlushFileBuffers")

    def close(self) -> None:
        if self.h is not None:
            k32.CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def volumes_on_disk(number: int) -> list:
    r"""Drive letters whose volume lives on \\.\PhysicalDrive<number>."""
    letters = []
    mask = k32.GetLogicalDrives()
    for i in range(26):
        if not mask & (1 << i):
            continue
        letter = chr(ord("A") + i)
        h = k32.CreateFileW(rf"\\.\{letter}:", 0, FILE_SHARE_READ | FILE_SHARE_WRITE,
                            None, OPEN_EXISTING, 0, None)
        if h is None or h == INVALID_HANDLE:
            continue                      # network drive, empty reader, ...
        try:
            out = ctypes.create_string_buffer(12)
            got = wt.DWORD(0)
            if k32.DeviceIoControl(h, IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 0,
                                   out, 12, ctypes.byref(got), None):
                dev_type, dev_num, _part = struct.unpack("<III", out.raw)
                if dev_type == FILE_DEVICE_DISK and dev_num == number:
                    letters.append(letter)
        finally:
            k32.CloseHandle(h)
    return letters


class LockedVolumes:
    """Lock + dismount every volume on the disk for the duration of a write."""

    def __init__(self, number: int, log):
        self.handles = []
        self.log = log
        for letter in volumes_on_disk(number):
            vol = RawDisk(rf"\\.\{letter}:", write=True)
            vol.ioctl(FSCTL_LOCK_VOLUME)
            vol.ioctl(FSCTL_DISMOUNT_VOLUME)
            log.write(f"locked and dismounted {letter}:\n")
            self.handles.append(vol)

    def release(self) -> None:
        for vol in self.handles:
            try:
                vol.ioctl(FSCTL_UNLOCK_VOLUME)
            except OSError:
                pass
            vol.close()
        self.handles = []


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


def fix_gpt(dev, cap: int, log, image_bytes=None) -> None:
    """Rebuild the backup GPT at the disk end from the primary header.

    Cards of one nominal size differ by tens of MB, so the last
    partition (the root, spanning the golden card) is clamped to this
    disk when it would run past the end - legal as long as the part of
    it the image actually carries (the shrunken filesystem) still fits.
    rsetup's resize_root then grows the filesystem to the clamped end.
    """
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
    entries = bytearray(dev.read(entries_sectors * SECTOR))
    if _crc(entries[:entries_bytes]) != struct.unpack_from("<I", hdr, 88)[0]:
        raise RuntimeError("primary partition entries CRC mismatch")
    backup_entries_lba = last - entries_sectors
    last_usable = backup_entries_lba - 1

    # The partitions must fit on this disk; only the last one may be
    # clamped, and only down to what the image contains.
    used = [(struct.unpack_from("<Q", entries, i * esize + 32)[0], i)
            for i in range(n_entries)
            if entries[i * esize:i * esize + 16] != b"\0" * 16]
    last_index = max(used)[1] if used else -1
    image_last_lba = (image_bytes // SECTOR - 1) if image_bytes else None
    for start_lba, i in used:
        off = i * esize
        end_lba = struct.unpack_from("<Q", entries, off + 40)[0]
        if end_lba <= last_usable:
            continue
        if i != last_index or start_lba > last_usable or (
                image_last_lba is not None and image_last_lba > last_usable):
            raise RuntimeError(f"partition {i + 1} ends at LBA {end_lba}, "
                               f"beyond this disk (last usable {last_usable})")
        struct.pack_into("<Q", entries, off + 40, last_usable)
        log.write(f"GPT: partition {i + 1} clamped from LBA {end_lba} to "
                  f"{last_usable} (card {(end_lba - last_usable) * SECTOR // 1048576}"
                  f" MB smaller than the golden one)\n")
    entries = bytes(entries)
    entries_crc = _crc(entries[:entries_bytes])

    def finish(h: bytearray, my, alt, part_lba) -> bytes:
        struct.pack_into("<Q", h, 24, my)
        struct.pack_into("<Q", h, 32, alt)
        struct.pack_into("<Q", h, 48, last_usable)
        struct.pack_into("<Q", h, 72, part_lba)
        struct.pack_into("<I", h, 88, entries_crc)
        struct.pack_into("<I", h, 16, 0)
        struct.pack_into("<I", h, 16, _crc(bytes(h[:hsize])))
        return bytes(h)

    primary = finish(bytearray(hdr), 1, last, entries_lba)
    backup = finish(bytearray(hdr), last, 1, backup_entries_lba)
    dev.seek(backup_entries_lba * SECTOR)
    dev.write(entries)
    dev.seek(last * SECTOR)
    dev.write(backup)
    dev.seek(entries_lba * SECTOR)
    dev.write(entries)
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
            with RawDisk(dev_path) as src, open(path, "wb") as dst:
                total = src.size()
                log.write(f"disk {dev_path} size {total}\n")
                done = copy(src, dst, total, log, t0)
            log.write(f"DONE {done}\n")
        elif mode == "write":
            total = os.path.getsize(path)
            if total % SECTOR:
                raise RuntimeError("image is not sector aligned")
            patch = None
            if len(sys.argv) >= 7:
                patch = (int(sys.argv[5]), sys.argv[6])
            locked = LockedVolumes(int(number), log)
            try:
                with RawDisk(dev_path, write=True) as dev:
                    cap = dev.size()
                    if total > cap:
                        raise RuntimeError(f"image {total} larger than disk {cap}")
                    log.write(f"disk {dev_path} size {cap}, image {total}\n")
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
                    fix_gpt(dev, cap, log, image_bytes=total)
                    dev.flush()
            finally:
                locked.release()
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
