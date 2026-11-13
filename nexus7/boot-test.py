#!/usr/bin/env python3
"""Temporarily boot a verified Tilapia image and collect ADB boot diagnostics."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--diagnostic-recovery", type=Path, help="temporarily boot this known recovery for logs if init returns to bootloader")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    image = args.image.resolve()
    data = image.read_bytes()
    if data[:8] != b"ANDROID!" or len(data) > 12582912:
        parser.error("not a legacy Android image within the verified recovery size limit")
    fields = struct.unpack_from("<10I", data, 8)
    if (fields[1], fields[6], fields[7]) != (0x10008000, 0x10000100, 2048) or fields[3] not in (0x11000000, 0x12000000):
        parser.error("image layout does not match the connected Tilapia's verified boot headers")
    adb = ["adb", "-s", args.serial]
    fastboot = ["fastboot", "-s", args.serial]
    device = subprocess.check_output(adb + ["shell", "getprop", "ro.product.device"], text=True, timeout=15).strip()
    if device != "tilapia":
        parser.error("ADB device is not identified as tilapia")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(image, output / "tested-recovery.img")
    result = dict(sha256=hashlib.sha256(data).hexdigest(), image_size=len(data), status="prepared", ramdisk_load_address=hex(fields[3]))
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    subprocess.run(adb + ["reboot", "bootloader"], check=True, timeout=20)
    for _ in range(20):
        devices = subprocess.check_output(["fastboot", "devices"], text=True, timeout=5)
        if any(line.split()[0] == args.serial for line in devices.splitlines() if line.split()):
            break
        time.sleep(1)
    else:
        raise RuntimeError("tablet did not appear in fastboot; no boot command sent")
    boot = subprocess.run(fastboot + ["boot", str(image)], capture_output=True, text=True, timeout=45)
    (output / "fastboot.txt").write_text(boot.stdout + boot.stderr)
    print(boot.stdout + boot.stderr, flush=True)
    boot.check_returncode()
    result["status"] = "temporary_boot_sent"
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    for attempt in range(90):
        devices = subprocess.check_output(["adb", "devices"], text=True, timeout=5)
        rows = [line.split() for line in devices.splitlines()]
        state = next((row[1] for row in rows if len(row) >= 2 and row[0] == args.serial), None)
        if state in ("device", "recovery"):
            version = subprocess.check_output(adb + ["shell", "getprop", "ro.build.version.release"], text=True, timeout=15).strip()
            result.update(status="adb_returned", adb_state=state, android_version=version)
            for name, command in (("last_kmsg.txt", ["cat", "/proc/last_kmsg"]),
                                  ("dmesg.txt", ["dmesg"]),
                                  ("cmdline.txt", ["cat", "/proc/cmdline"])):
                capture = subprocess.run(adb + ["shell"] + command, capture_output=True, timeout=20)
                (output / name).write_bytes(capture.stdout + capture.stderr)
            break
        if attempt % 5 == 0:
            devices = subprocess.check_output(["fastboot", "devices"], text=True, timeout=5)
            if any(line.split()[0] == args.serial for line in devices.splitlines() if line.split()):
                result["status"] = "returned_to_bootloader"
                break
        time.sleep(1)
    else:
        result["status"] = "no_adb_after_90_seconds"
    if result["status"] == "returned_to_bootloader" and args.diagnostic_recovery:
        backup = args.diagnostic_recovery.resolve()
        if backup.read_bytes()[:8] != b"ANDROID!" or backup.stat().st_size > 12582912:
            raise RuntimeError("invalid diagnostic recovery image")
        subprocess.run(fastboot + ["boot", str(backup)], check=True, timeout=45)
        for attempt in range(45):
            devices = subprocess.check_output(["adb", "devices"], text=True, timeout=5)
            rows = [line.split() for line in devices.splitlines()]
            if any(len(row) >= 2 and row[0] == args.serial and row[1] in ("device", "recovery") for row in rows):
                capture = subprocess.run(adb + ["shell", "cat", "/proc/last_kmsg"], capture_output=True, timeout=20)
                (output / "failed-kernel-last_kmsg.txt").write_bytes(capture.stdout + capture.stderr)
                result["diagnostic_recovery_booted"] = True
                break
            time.sleep(1)
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
