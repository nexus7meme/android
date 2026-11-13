#!/usr/bin/env python3
"""Verify shipped APK signatures and the compiled Tilapia microG resource."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import ssl
import subprocess
import tempfile
import zipfile
import os

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('source_root', type=Path)
p.add_argument('target_files', type=Path)
p.add_argument('--device', choices=('tilapia', 'grouper'), default='tilapia')
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
root = a.source_root.resolve()
env = os.environ.copy()
env['JAVA_HOME'] = str(root / 'prebuilts/jdk/jdk11/linux-x86')
java = root / 'prebuilts/jdk/jdk11/linux-x86/bin/java'
apksigner = root / 'out/host/linux-x86/framework/apksigner.jar'
assert apksigner.is_file(), 'build the checkout apksigner host target first'
aapt = root / 'out/host/linux-x86/bin/aapt2'
results = []
with zipfile.ZipFile(a.target_files) as z, tempfile.TemporaryDirectory(prefix='tilapia-apk-check-') as td:
    certs = {}
    for line in z.read('META/apkcerts.txt').decode().splitlines():
        fields = dict(re.findall(r'(\w+)="([^"]*)"', line))
        certs[fields['name']] = fields['certificate']
    for name in z.namelist():
        if not name.startswith(('SYSTEM/', 'PRODUCT/', 'SYSTEM_EXT/', 'VENDOR/')) or not name.endswith('.apk'):
            continue
        path = Path(td) / Path(name).name
        path.write_bytes(z.read(name))
        r = subprocess.run([str(java), '-Xmx256m', '-jar', str(apksigner), 'verify', '--print-certs', str(path)],
                           env=env, capture_output=True, text=True, check=True)
        digests = re.findall(r'Signer #\d+ certificate SHA-256 digest: ([0-9a-f]+)', r.stdout)
        assert digests, (name, 'no signer')
        cert = certs[path.name]
        if cert not in ('PRESIGNED', 'EXTERNAL'):
            der = ssl.PEM_cert_to_DER_cert((root / cert).read_text())
            expected = hashlib.sha256(der).hexdigest()
            assert digests == [expected], (name, 'unexpected signer', digests)
        results.append({'path': name, 'signature_verifies': True, 'certificate_source': cert,
                        'signer_sha256': digests})
        if path.name == 'framework-res.apk':
            resources = subprocess.check_output([str(aapt), 'dump', 'resources', str(path)], text=True)
            match = re.search(r'bool/config_enableMicrogSignatureSpoofing\s*\n(.*?)(?=\n\s*resource |\Z)', resources, re.S)
            assert match and re.search(r'\btrue\b', match[1]), 'microG user-build opt-in not enabled'
            if a.device == 'grouper':
                for flag in ('config_sms_capable', 'config_mobile_data_capable'):
                    value = re.search(r'bool/' + flag + r'\s*\n(.*?)(?=\n\s*resource |\Z)', resources, re.S)
                    assert value and re.search(r'\bfalse\b', value[1]), flag
        path.unlink()
assert any(Path(r['path']).name == 'framework-res.apk' for r in results)
a.output.write_text(json.dumps({'apk_count': len(results), 'compiled_microg_opt_in': True,
                                'apks': results}, indent=2) + '\n')
print(f'{len(results)} APK signatures verify; compiled microG opt-in enabled')
