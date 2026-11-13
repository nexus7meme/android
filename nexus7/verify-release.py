#!/usr/bin/env python3
"""Check the Tilapia user/test-keys release candidate against its target files."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


def properties(data):
    result = {}
    for line in data.decode().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            result[key] = value
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('rom', type=Path)
    p.add_argument('target_files', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--build-number', default='n7-181-20261112')
    p.add_argument('--device', choices=('tilapia', 'grouper'), default='tilapia')
    a = p.parse_args()
    with zipfile.ZipFile(a.rom) as rom, zipfile.ZipFile(a.target_files) as tf:
        assert rom.testzip() is None, 'ROM ZIP CRC failed'
        assert tf.testzip() is None, 'target-files ZIP CRC failed'
        metadata = properties(rom.read('META-INF/com/android/metadata'))
        assert metadata['pre-device'] == a.device
        assert metadata['ota-type'] == 'BLOCK'
        expected = f'google/lineage_{a.device}/{a.device}:11/RQ3A.211001.001/{a.build_number}:user/test-keys'
        assert metadata['post-build'] == expected, metadata['post-build']
        assert rom.read('boot.img') == tf.read('IMAGES/boot.img'), 'boot mismatch'
        names = tf.namelist()
        defaults = properties(b'\n'.join(tf.read(n) for n in names
            if n.startswith(('ROOT/', 'SYSTEM/', 'PRODUCT/', 'SYSTEM_EXT/', 'VENDOR/'))
            and n.endswith(('/default.prop', '/prop.default', '/build.prop'))))
        for key, value in {'ro.build.type': 'user', 'ro.build.tags': 'test-keys',
                           'ro.secure': '1', 'ro.debuggable': '0',
                           'ro.adb.secure': '1', 'ro.config.low_ram': 'true'}.items():
            assert defaults.get(key) == value, (key, defaults.get(key))
        for key, value in defaults.items():
            if key.endswith('.build.fingerprint') or key == 'ro.build.fingerprint':
                assert value == expected, (key, value)
            if key.endswith('.build.version.incremental') or key == 'ro.build.version.incremental':
                assert value == a.build_number, (key, value)
        if a.device == 'grouper':
            assert defaults.get('ro.radio.noril') == 'true'
            assert not any(n.endswith(('/rild', '/libxgold-ril.so', '/apns-conf.xml')) for n in names)
            assert not any('/android.hardware.telephony.' in n for n in names)
            assert 'android.hardware.radio' not in tf.read('SYSTEM/vendor/etc/vintf/manifest.xml').decode()
        else:
            assert any(n.endswith('/priv-app/CarrierConfig/CarrierConfig.apk') for n in names), 'missing carrier configuration service'
            assert any(n.endswith('/etc/permissions/com.android.carrierconfig.xml') for n in names), 'missing carrier configuration permission allowlist'
            assert any(n.endswith('/libtilapia-ril.so') for n in names), 'missing legacy RIL adapter'
        assert defaults.get('ro.bt.bdaddr_from_controller') == 'true', 'missing controller-address device configuration'
        assert b'Controller OTP bdaddr' in tf.read('SYSTEM/vendor/lib/libbt-vendor.so'), 'Broadcom OTP address path not compiled'
        assert b'Deferring factory address to controller OTP' in tf.read('SYSTEM/vendor/lib/hw/android.hardware.bluetooth@1.0-impl.so'), 'missing HAL controller-address support'
        forbidden = re.compile(r'(^|/)(grouper-(binder|kernel|graphics|gpu|network)-probe|su)$')
        assert not any(forbidden.search(n) for n in names), 'debug executable in image'
        assert not any(n.endswith('.pk8') or '/private-keys/' in n for n in names), 'private key in image'
        rc = tf.read('ROOT/init.grouper.rc').decode()
        assert 'on property:ro.build.type=userdebug\n    start adbd' not in rc
        assert 'on property:ro.debuggable=1\n    mount debugfs' in rc
        script = rom.read('META-INF/com/google/android/updater-script').decode()
        for mount in ('/cache', '/product', '/system', '/system_root'):
            assert f'assert(!is_mounted("{mount}"));' in script
        assert 'if is_mounted("/cache") then' in script
        assert script.index('if is_mounted("/cache") then') < script.index('block_image_update(')
        assert 'by-name/APP' in script and 'by-name/CAC' in script and 'by-name/LNX' in script
        assert not any(x in script for x in ('by-name/UDA', 'by-name/SOS', 'by-name/RAD', 'by-name/RDO', 'by-name/USP'))
        result = {'rom': a.rom.name, 'bytes': a.rom.stat().st_size,
                  'sha256': hashlib.file_digest(a.rom.open('rb'), 'sha256').hexdigest(),
                  'fingerprint': expected, 'metadata': metadata,
                  'security_properties': {k: defaults.get(k) for k in
                    ('ro.secure', 'ro.debuggable', 'ro.adb.secure', 'ro.config.low_ram')},
                  'security_patch': defaults.get('ro.build.version.security_patch'),
                  'zip_crc': 'pass', 'boot_matches_target_files': True, 'bluetooth_controller_otp_configured': True,
                  'debug_executables_absent': True, 'signing': 'public test keys, requested by user',
                  'hardware_release_validation': 'pending'}
    a.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
