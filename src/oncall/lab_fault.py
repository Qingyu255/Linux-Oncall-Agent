"""Bounded workload primitives invoked only by the trusted operator workflow."""

import argparse
import json
import multiprocessing
import os
import time
from pathlib import Path

LAB_MOUNT = Path("/var/lib/oncall-lab/data")
MARKER = LAB_MOUNT / ".oncall-lab-volume"
FILLER = LAB_MOUNT / ".oncall-fault.bin"
READY = LAB_MOUNT / ".oncall-fault-ready"
WRITE_PROBE = LAB_MOUNT / ".oncall-write-probe"


def burn(seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        sum(i * i for i in range(10000))


def cpu(seconds: int, count: int) -> None:
    workers = [multiprocessing.Process(target=burn, args=(seconds,)) for _ in range(count)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(seconds + 2)
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=2)


def memory(seconds: int, mebibytes: int) -> None:
    chunks: list[bytearray] = []
    deadline = time.monotonic() + seconds
    for _ in range(mebibytes):
        chunk = bytearray(1024 * 1024)
        chunk[::4096] = b"x" * (len(chunk) // 4096)
        chunks.append(chunk)
        if time.monotonic() >= deadline:
            return
    while time.monotonic() < deadline:
        time.sleep(0.2)


def filesystem(seconds: int, reserve_mib: int, maximum_mib: int) -> None:
    if not LAB_MOUNT.is_mount() or MARKER.read_text().strip() != "linux-oncall-dedicated-lab":
        raise RuntimeError("Refusing to fill a path without the dedicated lab mount marker")
    reserve = reserve_mib * 1024 * 1024
    maximum = maximum_mib * 1024 * 1024
    written = 0
    READY.unlink(missing_ok=True)
    with FILLER.open("wb", buffering=0) as stream:
        while written < maximum:
            available = os.statvfs(LAB_MOUNT).f_bavail * os.statvfs(LAB_MOUNT).f_frsize
            if available <= reserve:
                break
            write_size = min(8 * 1024 * 1024, available - reserve, maximum - written)
            if write_size <= 0:
                break
            stream.write(b"\0" * write_size)
            written += write_size
        stream.flush()
        os.fsync(stream.fileno())
    available = os.statvfs(LAB_MOUNT).f_bavail * os.statvfs(LAB_MOUNT).f_frsize
    if available > 32 * 1024 * 1024:
        raise RuntimeError("Lab fill ceiling reached before the filesystem became constrained")
    write_errno = None
    try:
        with WRITE_PROBE.open("wb", buffering=0) as probe:
            intended = max(8 * 1024 * 1024, available * 2)
            attempted = 0
            probe_chunk = b"x" * (1024 * 1024)
            while attempted < intended:
                written_now = probe.write(
                    probe_chunk[: min(len(probe_chunk), intended - attempted)]
                )
                if written_now is None or written_now <= 0:
                    raise OSError(28, "No space left on device")
                attempted += written_now
            os.fsync(probe.fileno())
    except OSError as error:
        write_errno = error.errno
    finally:
        WRITE_PROBE.unlink(missing_ok=True)
    if write_errno is None:
        raise RuntimeError("Controlled write unexpectedly succeeded")
    print(f"controlled_write_errno={write_errno}", flush=True)
    READY.write_text(
        json.dumps(
            {"written_bytes": written, "available_bytes": available, "write_errno": write_errno}
        )
    )
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "scenario", choices=("cpu", "memory", "filesystem"), nargs="?", default="cpu"
    )
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--mebibytes", type=int, default=128)
    parser.add_argument("--reserve-mib", type=int, default=4)
    parser.add_argument("--maximum-mib", type=int, default=1024)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 120 or not 1 <= args.workers <= 2:
        parser.error("Lab permits 1..120 seconds and 1..2 workers")
    if not 16 <= args.mebibytes <= 512:
        parser.error("Memory workload permits 16..512 MiB")
    if not 4 <= args.reserve_mib <= 64 or not 64 <= args.maximum_mib <= 1024:
        parser.error("Filesystem reserve/maximum is outside the safe lab range")
    if args.scenario == "cpu":
        cpu(args.seconds, args.workers)
    elif args.scenario == "memory":
        memory(args.seconds, args.mebibytes)
    else:
        try:
            filesystem(args.seconds, args.reserve_mib, args.maximum_mib)
        finally:
            WRITE_PROBE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
