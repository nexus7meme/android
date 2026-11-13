#!/usr/bin/env python3
"""Apply the exact full OTA to blank host files and compare the written images.

The legacy simulator cannot parse tabbed fstabs or map '/' to system.img. Only
its source fixture metadata is normalized. The ROM ZIP is not modified. Host
extraction of addon scripts/permissions is skipped; addon programs are simulated
by the platform tool. This is not an on-device recovery/install test.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import struct
import tempfile
import zipfile

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('source_root', type=Path)
p.add_argument('target_files', type=Path)
p.add_argument('rom', type=Path)
p.add_argument('--device', choices=('tilapia', 'grouper'), default='tilapia')
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
root = a.source_root.resolve()
a.output.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='tilapia-ota-sim-', dir=a.output.parent) as td:
    work = Path(td)
    fixture = work / 'blank-source.zip'
    zero = b'\0' * (1024 * 1024)
    with zipfile.ZipFile(a.target_files) as tf, zipfile.ZipFile(fixture, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as dest:
        for name in tf.namelist():
            if name.endswith('/build.prop') or name.startswith('META/'):
                if not name.endswith('/'):
                    dest.writestr(name, tf.read(name))
        fstab_names = [n for n in tf.namelist() if n.startswith(('RECOVERY/RAMDISK/', 'BOOT/RAMDISK/')) and n.endswith('/recovery.fstab')]
        assert fstab_names
        for name in fstab_names:
            lines = []
            for line in tf.read(name).decode().splitlines():
                if not line.strip() or line.lstrip().startswith('#'):
                    continue
                fields = line.split()
                if fields[1] == '/':
                    fields[1] = '/system'
                lines.append(' '.join(fields))
            dest.writestr(name, '\n'.join(lines) + '\n')
        for image, size in [('system', 681574400), ('product', 457179136 if a.device == 'grouper' else 464519168), ('boot', tf.getinfo('IMAGES/boot.img').file_size)]:
            with dest.open('IMAGES/' + image + '.img', 'w') as f:
                if image != 'boot':
                    # Android sparse DONT_CARE expands to a blank file. Match
                    # the ext4 sparse-image declaration in misc_info.txt.
                    assert size % 4096 == 0
                    f.write(struct.pack('<I4H4I', 0xed26ff3a, 1, 0, 28, 12,
                                        4096, size // 4096, 1, 0))
                    f.write(struct.pack('<2H2I', 0xcac3, 0, size // 4096, 12))
                else:
                    while size:
                        chunk = min(size, len(zero))
                        f.write(zero[:chunk])
                        size -= chunk
    skip = work / 'skip.txt'
    skip.write_text('package_extract_dir\nset_metadata_recursive\n')
    with a.output.with_suffix('.log').open('w') as log:
        subprocess.run([str(root / 'out/host/linux-x86/bin/update_host_simulator'),
            '--source', str(fixture), '--ota_package', str(a.rom.resolve()),
            '--skip_functions', str(skip), '--work_dir', str(work), '--keep_images'],
            stdout=log, stderr=subprocess.STDOUT, check=True)
    results = []
    with zipfile.ZipFile(a.target_files) as tf:
        for image in ('system', 'product', 'boot'):
            expected = work / (image + '.expected')
            data = work / (image + '.input')
            with tf.open('IMAGES/' + image + '.img') as src, data.open('wb') as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
            if image != 'boot':
                subprocess.run([str(root / 'out/host/linux-x86/bin/simg2img'), str(data), str(expected)], check=True)
            else:
                data.rename(expected)
            actual = work / (image + '.img')
            expected_hash = hashlib.file_digest(expected.open('rb'), 'sha256').hexdigest()
            actual_hash = hashlib.file_digest(actual.open('rb'), 'sha256').hexdigest()
            assert actual_hash == expected_hash, (image, actual_hash, expected_hash)
            results.append({'image': image, 'bytes': actual.stat().st_size, 'sha256': actual_hash, 'matches': True})
    # Fault injection: keep /cache mounted by stubbing unmount(). The exact
    # release guard must reject the update before any block-image command.
    failure_zip = work / 'blocked-cache-test.zip'
    with zipfile.ZipFile(a.rom) as rom, zipfile.ZipFile(failure_zip, 'w') as dest:
        script = rom.read('META-INF/com/google/android/updater-script')
        prefix = b'assert(mount("ext4", "EMMC", "/dev/block/platform/sdhci-tegra.3/by-name/CAC", "/cache"));\n'
        dest.writestr('META-INF/com/google/android/updater-script', prefix + script)
    failure_skip = work / 'failure-skip.txt'
    failure_skip.write_text(skip.read_text() + 'unmount\n')
    failure_dir = work / 'blocked-cache'
    failure_dir.mkdir()
    failure_log = a.output.with_name('blocked-cache-simulation.log')
    with failure_log.open('w') as log:
        r = subprocess.run([str(root / 'out/host/linux-x86/bin/update_host_simulator'),
            '--source', str(fixture), '--ota_package', str(failure_zip),
            '--skip_functions', str(failure_skip), '--work_dir', str(failure_dir)],
            stdout=log, stderr=subprocess.STDOUT)
    assert r.returncode != 0 and 'assert failed: !is_mounted("/cache")' in failure_log.read_text(), 'mounted-cache guard did not reject failed unmount'
a.output.write_text(json.dumps({'status': 'pass', 'starting_images': 'blank zero-filled files',
    'source_fstab_normalization': 'whitespace and system-root image alias only',
    'rom_zip_modified': False, 'failed_cache_unmount_rejected': True, 'external_addon_programs_executed': False,
    'host_only_skipped_functions': ['package_extract_dir', 'set_metadata_recursive'],
    'images': results, 'on_device_recovery_validation': 'pending'}, indent=2) + '\n')
print('Full OTA wrote host System, Product and boot files matching target images')
