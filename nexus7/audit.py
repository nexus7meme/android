#!/usr/bin/env python3
"""Report concrete source-port blockers; passing does not certify a bootable ROM."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = args.source_root.resolve()
    findings = []

    def report(level, area, detail):
        findings.append(dict(level=level, area=area, detail=detail))

    def read(path):
        file = root / path
        if not file.is_file():
            report("BLOCKER", "sources", "Missing " + path)
            return ""
        return file.read_text(errors="replace")

    version = read("kernel/asus/grouper/Makefile")
    match = [re.search(r"^" + field + r" = (\d+)$", version, re.M)
             for field in ("VERSION", "PATCHLEVEL", "SUBLEVEL")]
    if all(match):
        report("INFO", "kernel", "Linux " + ".".join(m.group(1) for m in match))
    binder = read("kernel/asus/grouper/drivers/staging/android/binder.c")
    headers = list((root / "kernel/asus/grouper").glob("**/binder.h"))
    abi = "\n".join(h.read_text(errors="replace") for h in headers)
    if not re.search(r'"hwbinder"|binder_devices|binder_device', binder):
        report("BLOCKER", "binder", "No evidence of independent hwbinder/vndbinder contexts in the driver")
    if "BC_TRANSACTION_SG" not in abi or "BINDER_TYPE_PTR" not in abi:
        report("BLOCKER", "binder", "Missing scatter-gather/buffer-object ABI used by HIDL transactions")
    if "BINDER_SET_CONTEXT_MGR_EXT" not in abi:
        report("REVIEW", "binder", "No extended context manager ioctl; Android 11 has a fallback, but transaction security context support needs review")
    if "BC_TRANSACTION_SG" in abi and "binder_devices" in binder:
        report("REVIEW", "binder", "Multi-context/scatter-gather backport present; on-device transaction and lifetime tests still required")
    config_path = "kernel/asus/grouper/arch/arm/configs/lineageos_tilapia_defconfig"
    config = read(config_path)
    for option in ("CONFIG_SECCOMP", "CONFIG_SECCOMP_FILTER", "CONFIG_CGROUPS", "CONFIG_SECURITY_SELINUX"):
        if option + "=y" not in config:
            report("BLOCKER", "kernel", option + " is not enabled in the source defconfig")
    if "CONFIG_NAMESPACES=y" not in config:
        report("BLOCKER", "kernel", "Namespaces disabled; Android 11 mount namespaces need kernel configuration/backport work")
    syscalls = read("kernel/asus/grouper/include/linux/syscalls.h")
    for call in ("memfd_create", "getrandom"):
        if call not in syscalls:
            report("REVIEW", "kernel", call + " not declared; audit Android 11 userspace fallbacks/backports")

    for path in ("build/envsetup.sh", "vendor/lineage/config/common_mini_tablet_wifionly.mk",
                 "vendor/lineage/config/telephony.mk"):
        if not (root / path).is_file():
            report("BLOCKER", "platform", "Missing " + path + "; sync LineageOS 18.1 sources")
    board = read("device/asus/tilapia/lineage/BoardConfig.mk")
    if "TARGET_RECOVERY_FSTAB := device/asus/tilapia/lineage/recovery.fstab" not in board:
        report("BLOCKER", "device", "Tilapia recovery fstab is not selected")
    manifest = root / "device/asus/tilapia/lineage/manifest.xml"
    if not manifest.is_file() or "DEVICE_MANIFEST_FILE := device/asus/tilapia/lineage/manifest.xml" not in board:
        report("BLOCKER", "HAL", "No Android 11 device VINTF manifest or validated HIDL service configuration")
    common = read("device/asus/grouper/device-common.mk")
    obsolete = [name for name in ("audio.a2dp.default", "libemoji", "dhcpcd.conf", "PerformanceControl")
                if re.search(r"(?<![\w.])" + re.escape(name) + r"(?![\w.])", common)]
    if obsolete and "$(filter-out audio.a2dp.default libemoji dhcpcd.conf PerformanceControl NfcNxp,$(PRODUCT_PACKAGES))" not in common:
        report("BLOCKER", "product", "Inherited Nougat module list requires migration: " + ", ".join(obsolete))
    policy = read("device/asus/tilapia/lineage/sepolicy/hardware.te")
    if policy:
        report("REVIEW", "SELinux", "Scoped Android 11 policy selected; full-system HAL permissions still need hardware testing")
    ril = root / "vendor/asus/tilapia/proprietary/vendor/lib/libxgold-ril.so"
    if not ril.is_file():
        report("BLOCKER", "radio", "Missing Tilapia libxgold-ril.so")
    else:
        report("REVIEW", "radio", "libxgold-ril.so available; Android 11 RIL ABI/radio HAL and SIM/data registration untested")
    # Inspect the actual ARM binaries without executing them.
    elf_copies = []
    for path in sorted((root / "vendor").glob("*/grouper/*-vendor-blobs.mk")):
        for line in path.read_text(errors="replace").splitlines():
            if ":" in line and ".so:" in line:
                elf_copies.append(line.strip().rstrip("\\").strip())
    prebuilts = root / "device/asus/tilapia/lineage/Android.mk"
    if elf_copies and not prebuilts.is_file():
        report("BLOCKER", "blobs", str(len(elf_copies)) + " ELF library PRODUCT_COPY_FILES entries need Android 11 prebuilt module definitions")
    elif prebuilts.is_file():
        definitions = prebuilts.read_text()
        modules = re.findall(r"^LOCAL_MODULE := (.+)$", definitions, re.M)
        report("INFO", "blobs", str(len(re.findall(r"^LOCAL_MODULE_CLASS :=", definitions, re.M))) + " explicit ELF prebuilt modules; dependency/symbol checks still need a platform build")
        for source in re.findall(r"^LOCAL_SRC_FILES := ../../../../(.+)$", definitions, re.M):
            if not (root / source).is_file():
                report("BLOCKER", "blobs", "Missing prebuilt source: " + source)
        if "LOCAL_CHECK_ELF_FILES := false" in definitions:
            report("BLOCKER", "blobs", "ELF validation disabled in the generated prebuilt definitions")
    reader = shutil.which("readelf")
    if reader:
        for relative in ("vendor/nvidia/grouper/proprietary/vendor/lib/hw/hwcomposer.tegra3.so",
                         "vendor/nvidia/grouper/proprietary/vendor/lib/egl/libEGL_tegra.so",
                         "vendor/asus/tilapia/proprietary/vendor/lib/libxgold-ril.so"):
            path = root / relative
            if not path.is_file():
                report("BLOCKER", "blobs", "Missing " + relative)
                continue
            output = subprocess.run([reader, "-dW", str(path)], capture_output=True, text=True, check=True).stdout
            deps = re.findall(r"Shared library: \[(.*?)\]", output)
            report("INFO", "ELF", path.name + " needs " + ", ".join(deps))
            if "TEXTREL" in output:
                report("BLOCKER", "linker", path.name + " contains text relocations; establish scoped legacy blob compatibility")
    else:
        report("REVIEW", "ELF", "Install readelf to inspect proprietary library dependencies")
    report("REVIEW", "graphics", "Tegra HWC1/EGL blobs need Android 11 composer/allocator integration and symbol compatibility validation")
    report("REVIEW", "storage", "Stock system limit is 681574400 bytes; measure images and device partitions before selecting a layout")
    blocked = any(f["level"] == "BLOCKER" for f in findings)
    if args.json:
        print(json.dumps(dict(ready_for_build=not blocked, findings=findings), indent=2))
    else:
        for f in findings:
            print(f"{f['level']:7} {f['area']:10} {f['detail']}")
        print("\nSource blockers remain." if blocked else "\nNo known source blockers detected; build and boot testing still required.")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
