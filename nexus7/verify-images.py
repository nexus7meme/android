#!/usr/bin/env python3
"""Validate Tilapia image limits and the split-layout first-stage ramdisk."""
import argparse
import hashlib
import json
import lzma
from pathlib import Path
import stat
import struct
import subprocess
import tempfile


def cpio_entries(data):
    offset = 0
    entries = {}
    while offset + 110 <= len(data):
        header = data[offset:offset + 110]
        if header[:6] not in (b'070701', b'070702'):
            raise ValueError('expected a newc ramdisk archive')
        fields = [int(header[6 + i * 8:14 + i * 8], 16) for i in range(13)]
        mode, size, namesize = fields[1], fields[6], fields[11]
        name = data[offset + 110:offset + 110 + namesize - 1].decode()
        start = (offset + 110 + namesize + 3) & ~3
        if start + size > len(data):
            raise ValueError('truncated ramdisk entry')
        if name == 'TRAILER!!!':
            return entries
        entries[name.removeprefix('./')] = (mode, data[start:start + size])
        offset = (start + size + 3) & ~3
    raise ValueError('missing ramdisk trailer')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('product_out', type=Path)
    parser.add_argument('--device', choices=('tilapia', 'grouper'), default='tilapia')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = {}
    for name, limit in (('boot', 8388608), ('recovery', 12582912),
                        ('system', 681574400), ('product', 457179136 if args.device == 'grouper' else 464519168)):
        path = args.product_out / (name + '.img')
        data = path.read_bytes()
        info = dict(path=str(path), size=len(data), partition_limit=limit,
                    sha256=hashlib.sha256(data).hexdigest())
        if name in ('boot', 'recovery'):
            assert data[:8] == b'ANDROID!', name + ': not a legacy Android boot image'
            values = struct.unpack_from('<10I', data, 8)
            kernel_size, kernel_addr, ramdisk_size, ramdisk_addr = values[:4]
            tags, page = values[6:8]
            assert (kernel_addr, ramdisk_addr, tags, page) == (0x10008000, 0x11000000, 0x10000100, 2048)
            assert len(data) <= limit and ramdisk_size > 0, name + ': image limit or missing ramdisk'
            start = page + ((kernel_size + page - 1) // page) * page
            archive = lzma.decompress(data[start:start + ramdisk_size])
            entries = cpio_entries(archive)
            if name == 'boot':
                mode, init = entries['init']
                assert stat.S_ISREG(mode) and init[:4] == b'\x7fELF', 'boot /init must be the static first-stage executable'
                assert struct.unpack_from('<H', init, 18)[0] == 40, 'boot /init must target ARM'
                phoff = struct.unpack_from('<I', init, 28)[0]
                phentsize, phnum = struct.unpack_from('<HH', init, 42)
                assert all(struct.unpack_from('<I', init, phoff + i * phentsize)[0] != 3 for i in range(phnum)), 'first-stage init must not need a dynamic interpreter'
                fstab = entries['fstab.grouper'][1].decode()
                assert 'by-name/APP /system ext4 ro wait,first_stage_mount' in fstab
                assert 'by-name/CAC /product ext4 ro wait,first_stage_mount' in fstab
                assert stat.S_ISLNK(entries['system_ext'][0]) and entries['system_ext'][1] == b'/product/system_ext'
                assert stat.S_ISLNK(entries['cache'][0]) and entries['cache'][1] == b'/data/cache'
            info.update(kernel_size=kernel_size, ramdisk_size=ramdisk_size, ramdisk_entries=len(entries))
            info['expanded_size'] = len(data)
        else:
            header = struct.unpack_from('<I4H4I', data)
            assert header[0] == 0xed26ff3a and header[1] == 1, name + ': expected Android sparse image'
            expanded = header[5] * header[6]
            assert expanded == limit, name + ': expanded size differs from measured partition'
            info['expanded_size'] = expanded
            if name == 'system':
                converter = args.product_out.resolve().parents[2] / 'host/linux-x86/bin/simg2img'
                with tempfile.TemporaryDirectory(prefix='nexus7-system-init-') as temporary:
                    raw = Path(temporary) / 'system.raw.img'
                    subprocess.run([str(converter), str(path), str(raw)], check=True)
                    checked = subprocess.run(['debugfs', '-R', 'stat /init', str(raw)],
                                             check=True, capture_output=True, text=True)
                    assert 'Type: symlink' in checked.stdout, 'system-root /init must not be first-stage init'
                    assert '"/system/bin/init"' in checked.stdout, 'wrong system-root init link'
                    info['system_root_init'] = '/system/bin/init'
        result[name] = info
    output = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.write_text(output)
    print(output, end='')


if __name__ == '__main__':
    main()
