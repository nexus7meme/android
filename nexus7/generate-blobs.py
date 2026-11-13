#!/usr/bin/env python3
"""Generate Android 11 prebuilts for the original Tilapia/Grouper proprietary files."""
import argparse
import hashlib
import json
from elf_fixups import patch_tegra_egl, replace_dynamic_strings, remove_needed
from pathlib import Path
import re
import subprocess

# Superseded private Stagefright container plugins, not codec dependencies.
# Keep definitions for source inventory, but do not ship these ABI-incompatible
# legacy readers alongside Android 11's platform media extractors.
UNSHIPPED_MODULES = {"libnvasfparserhal", "libnvaviparserhal"}

VENDORS = ("asus/grouper", "asus/tilapia", "broadcom/grouper", "elan/grouper", "invensense/grouper", "nvidia/grouper")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    entries = {}
    fixup_root = source_root / "device/asus/tilapia/lineage/blobfixups"
    fixup_root.mkdir(parents=True, exist_ok=True)
    fixup_records = []
    # Include all supplied device files, including libraries loaded dynamically
    # or omitted from the old hand-maintained vendor copy list.
    for vendor in VENDORS:
        tree = source_root / "vendor" / vendor / "proprietary"
        if not tree.is_dir():
            parser.error("missing vendor source: " + str(tree))
        for blob in sorted(tree.rglob("*")):
            if not blob.is_file():
                continue
            relative = blob.relative_to(tree)
            if relative.parts[0] not in ("vendor", "etc", "lib", "bin"):
                parser.error("unrecognized proprietary install path: " + str(relative))
            dest = "system/" + str(relative)
            # These NVIDIA HALs retain a .tegra SONAME despite the old vendor
            # tree naming their files .tegra3. Select the installed variant
            # explicitly through ro.hardware.gralloc/hwcomposer.
            if dest in ("system/vendor/lib/hw/gralloc.tegra3.so", "system/vendor/lib/hw/hwcomposer.tegra3.so"):
                dest = dest.replace(".tegra3.so", ".tegra.so")
            if dest in entries:
                parser.error("duplicate proprietary destination: " + dest)
            entries[dest] = blob.relative_to(source_root)
    for dest, source in list(entries.items()):
        original = (source_root / source).read_bytes()
        if original[:4] != b"\x7fELF":
            continue
        patched = original
        changes = []
        if dest.startswith("system/vendor/lib/egl/") and dest.endswith("_tegra.so"):
            patched = patch_tegra_egl(patched)
            changes.append("EGL library loading through libdgv1")
        original_symbols = subprocess.check_output(["readelf", "-Ws", str(source_root / source)], text=True)
        if re.search(r"UND\s+(?:__aeabi_[fd]2u?lz|android_atomic_(?:inc|dec|acquire_load|release_store)|socket_loopback_client)(?:\s|$)", original_symbols):
            patched = replace_dynamic_strings(patched, ((b"libc.so\0", b"libg.so\0"),))
            changes.append("Legacy conversion, atomic, or loopback ABI functions through libg")
        if dest == "system/vendor/lib/hw/gps.tegra3.so":
            old = b"gps.samsungMantaandroid.so\0"
            patched = replace_dynamic_strings(patched, ((old, b"gps.tegra3.so\0".ljust(len(old), b"\0")),))
            changes.append("GPS SONAME matches installed HAL filename")
        if dest == "system/vendor/bin/gpsd":
            # No ICU symbols are imported by this daemon. Android 11 no longer
            # provides the historical non-APEX ICU library under this SONAME.
            patched = remove_needed(patched, "libicuuc.so")
            old = b"SSLv3_client_method\0"
            patched = replace_dynamic_strings(patched, ((old, b"TLS_client_method\0".ljust(len(old), b"\0")),))
            changes.append("Drop unused ICU dependency and negotiate supported TLS instead of SSLv3")
            names = [b"_ZN7android13SensorManager" + tail for tail in
                     (b"C1Ev", b"13getSensorListEPPKPKNS_6SensorE", b"16getDefaultSensorEi", b"16createEventQueueEv")]
            patched = replace_dynamic_strings(patched, [(name + b"\0", name.replace(b"android", b"grouper", 1) + b"\0") for name in names])
            changes.append("GPS manager operations forwarded without reusing old object layout")
        if dest == "system/vendor/lib/libnvwinsys.so":
            patched = replace_dynamic_strings(patched, ((b"libgui.so\0", b"libg3d.so\0"),))
            old = b"_ZN7android21SurfaceComposerClient14getDisplayInfoERKNS_2spINS_7IBinderEEEPNS_11DisplayInfoE"
            patched = replace_dynamic_strings(patched, ((old + b"\0", old.replace(b"android", b"grouper", 1) + b"\0"),))
            changes.append("Legacy SurfaceControl API through libg3d")
        if dest == "system/vendor/lib/libxgold-ril.so":
            old = b"/data/data/com.android.phone/network_select_pref\0"
            new = b"/data/vendor/tilapia-radio/network_select_pref\0"
            if patched.count(old) != 1 or len(new) > len(old):
                parser.error("unexpected XMM network-selection path")
            patched = patched.replace(old, new.ljust(len(old), b"\0"))
            changes.append("Network-selection preference moved to dedicated vendor radio state")
        if changes:
            target = fixup_root / Path(dest).name
            target.write_bytes(patched)
            entries[dest] = target.relative_to(source_root)
            fixup_records.append(dict(source=str(source), output=str(target.relative_to(source_root)),
                                      changes=changes, source_sha256=hashlib.sha256(original).hexdigest(),
                                      output_sha256=hashlib.sha256(patched).hexdigest()))
    (fixup_root / "manifest.json").write_text(json.dumps(fixup_records, indent=2) + "\n")
    make = ["# SPDX-License-Identifier: Apache-2.0", "# Generated by generate-blobs.py; do not disable ELF validation.",
            "LOCAL_PATH := $(call my-dir)", ""]
    packages, copies = [], []
    for dest, source in sorted(entries.items()):
        path = source_root / source
        if path.read_bytes()[:4] != b"\x7fELF":
            copies.append((str(source), dest))
            continue
        target = Path(dest)
        shared = target.suffix == ".so"
        name = target.name[:-3] if shared else target.name
        if name in packages:
            parser.error("duplicate module name: " + name)
        packages.append(name)
        output = subprocess.check_output(["readelf", "-dW", str(path)], text=True)
        needed = re.findall(r"Shared library: \[(.*?)\]", output)
        deps = [d[:-3] for d in needed if d.endswith(".so")]
        if "libdgv1" in deps:
            # The shim depends on libnvos; list it explicitly for symbol checks
            # of the other NvOs imports resolved through that dependency.
            deps.append("libnvos")
        # Some vendor objects use logging through a transitive dependency.
        # Name it explicitly so symbol validation sees the actual provider.
        symbols = subprocess.check_output(["readelf", "-Ws", str(path)], text=True)
        if re.search(r"UND\s+__android_log_", symbols) and "liblog" not in deps:
            deps.append("liblog")
        if "libg" in deps:
            deps.append("libc")
        if "libg3d" in deps:
            deps.append("libgui")
        if "libgpsd-compat" in deps:
            deps.append("libsensor")
        # Android 11's public libc stub omits legacy LP32 exports that still
        # exist in the actual libc. Check these old blobs against that library.
        deps = ["libc.bootstrap" if d == "libc" else d for d in deps]
        make += ["include $(CLEAR_VARS)", "LOCAL_MODULE := " + name,
                 "LOCAL_MODULE_CLASS := " + ("SHARED_LIBRARIES" if shared else "EXECUTABLES"),
                 "LOCAL_MODULE_TAGS := optional", "LOCAL_MULTILIB := 32",
                 "LOCAL_SRC_FILES := ../../../../" + str(source),
                 "LOCAL_MODULE_PATH := $(TARGET_OUT)/" + str(target.parent.relative_to("system"))]
        if shared:
            make.append("LOCAL_MODULE_SUFFIX := .so")
        if deps:
            make.append("LOCAL_SHARED_LIBRARIES := " + " ".join(deps))
        make += ["include $(BUILD_PREBUILT)", ""]
    make += ["include $(CLEAR_VARS)", "LOCAL_MODULE := libdgv1",
             "LOCAL_SRC_FILES := libdgv1.c", "LOCAL_CFLAGS := -Wall -Wextra -Werror",
             "LOCAL_MODULE_PATH := $(TARGET_OUT)/vendor/lib",
             "LOCAL_SHARED_LIBRARIES := libnvos libdl liblog", "include $(BUILD_SHARED_LIBRARY)", ""]
    make += ["include $(CLEAR_VARS)", "LOCAL_MODULE := grouper-binder-probe",
             "LOCAL_SRC_FILES := binder_probe.c", "LOCAL_CFLAGS := -Wall -Wextra -Werror",
             "LOCAL_FORCE_STATIC_EXECUTABLE := true", "LOCAL_SYSTEM_SHARED_LIBRARIES :=",
             "LOCAL_STATIC_LIBRARIES := libc", "include $(BUILD_EXECUTABLE)", ""]
    make += ["include $(CLEAR_VARS)", "LOCAL_MODULE := grouper-kernel-probe",
             "LOCAL_SRC_FILES := kernel_probe.c", "LOCAL_CFLAGS := -Wall -Wextra -Werror",
             "LOCAL_FORCE_STATIC_EXECUTABLE := true", "LOCAL_SYSTEM_SHARED_LIBRARIES :=",
             "LOCAL_STATIC_LIBRARIES := libc", "include $(BUILD_EXECUTABLE)", ""]
    make += ["include $(CLEAR_VARS)", "LOCAL_MODULE := grouper-graphics-probe",
             "LOCAL_SRC_FILES := graphics_probe.cpp", "LOCAL_CPPFLAGS := -std=c++17 -Wall -Wextra -Werror",
             "LOCAL_SHARED_LIBRARIES := libui libutils libcutils liblog libhardware",
             "LOCAL_HEADER_LIBRARIES := libhardware_headers",
             "include $(BUILD_EXECUTABLE)", ""]
    make += ["include $(CLEAR_VARS)", "LOCAL_MODULE := grouper-gpu-probe",
             "LOCAL_SRC_FILES := gpu_probe.cpp", "LOCAL_CPPFLAGS := -std=c++17 -Wall -Wextra -Werror",
             "LOCAL_SHARED_LIBRARIES := libEGL libGLESv2", "include $(BUILD_EXECUTABLE)", ""]
    make += ["include $(CLEAR_VARS)", "LOCAL_MODULE := libg", "LOCAL_HEADER_LIBRARIES := libcutils_headers",
             "LOCAL_SRC_FILES := tegra_compat.c ../../../../external/compiler-rt/lib/builtins/fixsfdi.c ../../../../external/compiler-rt/lib/builtins/fixunssfdi.c ../../../../external/compiler-rt/lib/builtins/fixdfdi.c ../../../../external/compiler-rt/lib/builtins/fixunsdfdi.c",
             "LOCAL_LDFLAGS := -Wl,--version-script=$(LOCAL_PATH)/tegra_compat.map",
             "LOCAL_ADDITIONAL_DEPENDENCIES := $(LOCAL_PATH)/tegra_compat.map",
             "LOCAL_MODULE_PATH := $(TARGET_OUT)/vendor/lib",
             "LOCAL_SHARED_LIBRARIES := libc", "include $(BUILD_SHARED_LIBRARY)", ""]
    make += ["include $(CLEAR_VARS)", "LOCAL_MODULE := libg3d",
             "LOCAL_SRC_FILES := gui_compat.cpp", "LOCAL_CPPFLAGS := -std=c++17",
             "LOCAL_MODULE_PATH := $(TARGET_OUT)/vendor/lib",
             "LOCAL_SHARED_LIBRARIES := libgui libui libbinder libutils libbase",
             "include $(BUILD_SHARED_LIBRARY)", ""]
    make += ["# Explicit bring-up test target; excluded from PRODUCT_PACKAGES.",
             "include $(CLEAR_VARS)", "LOCAL_MODULE := grouper-network-probe",
             "LOCAL_SRC_FILES := network_probe.c", "LOCAL_MODULE_TAGS := optional",
             "include $(BUILD_EXECUTABLE)", ""]
    make += ["# Translate the retained XMM library's legacy GSM state/card-status ABI."]
    make += ["include $(CLEAR_VARS)", "LOCAL_MODULE := libtilapia-ril",
             "LOCAL_SRC_FILES := ril_compat.cpp",
             "LOCAL_CPPFLAGS := -std=c++17 -DRIL_SHLIB -Wall -Wextra -Werror",
             "LOCAL_HEADER_LIBRARIES := ril_headers",
             "LOCAL_SHARED_LIBRARIES := liblog libdl",
             "LOCAL_MODULE_PATH := $(TARGET_OUT_VENDOR_SHARED_LIBRARIES)",
             "include $(BUILD_SHARED_LIBRARY)", ""]
    root = source_root / "device/asus/tilapia/lineage"
    root.mkdir(parents=True, exist_ok=True)
    (root / "Android.mk").write_text("\n".join(make))
    product = ["# SPDX-License-Identifier: Apache-2.0", "# Generated by generate-blobs.py."]
    for variable, values in (("PRODUCT_PACKAGES", [p for p in packages if p not in UNSHIPPED_MODULES and p != "libxgold-ril"] + ["libdgv1", "libg", "libg3d"]),
                             ("PRODUCT_COPY_FILES", [s + ":" + d for s, d in copies if not s.startswith("vendor/asus/tilapia/")])):
        if values:
            product.append(variable + " += " + chr(92))
            product += ["    " + value + (" " + chr(92) if i < len(values) - 1 else "")
                        for i, value in enumerate(values)]
            product.append("")
    # The Wi-Fi product shares the prebuilt inventory, not the cellular stack.
    product += ["ifneq ($(TARGET_PRODUCT),lineage_grouper)",
                "PRODUCT_PACKAGES += libxgold-ril",
                "PRODUCT_COPY_FILES += vendor/asus/tilapia/proprietary/etc/apns-conf.xml:system/etc/apns-conf.xml",
                "endif", ""]
    (root / "vendor.mk").write_text("\n".join(product))
    print(f"Generated {len(packages)} ELF prebuilts and {len(copies)} data files; all sources exist")


if __name__ == "__main__":
    main()
