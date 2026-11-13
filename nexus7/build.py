#!/usr/bin/env python3
"""Build Nexus 7 LineageOS 18.1 with generated legacy vendor fixups."""
import argparse
import os
from pathlib import Path
import shutil
import sys
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('source_root', type=Path)
parser.add_argument('--clean-installed', action='store_true', help='clean installed outputs while retaining compiled objects')
parser.add_argument('--jobs', type=int, default=8)
parser.add_argument('--device', choices=('tilapia', 'grouper'), default='tilapia')
parser.add_argument('--variant', choices=('user', 'userdebug', 'eng'), default='user')
parser.add_argument('--build-number', default='n7-181-20261112',
                    help='stable identity shared by incrementally built partition properties')
parser.add_argument('targets', nargs='*', default=['bootimage', 'recoveryimage'])
args = parser.parse_args()
root = args.source_root.resolve()
if args.jobs < 1 or not (root / 'build/envsetup.sh').is_file():
    parser.error('positive job count and a synchronized Android checkout required')
# Recreate generated blob fixups from the pinned public vendor inputs.
subprocess.run([sys.executable, str(Path(__file__).with_name('generate-blobs.py')), str(root)], check=True)
# Ninja sanitizes LD_LIBRARY_PATH. Both legacy prebuilts and newly built
# LLVM tools already search their adjacent lib64 runtime directory.
bundled = root / 'prebuilts/gcc/linux-x86/host/x86_64-linux-glibc2.17-4.8/sysroot/usr/lib'
for relative in ('prebuilts/clang/host/linux-x86/clang-3289846/lib64',
                 'out/soong/host/linux-x86/lib64', 'out/host/linux-x86/lib64'):
    libs = root / relative
    libs.mkdir(parents=True, exist_ok=True)
    for name in ('libncurses', 'libtinfo'):
        source = bundled / (name + '.so.5.9')
        if not source.is_file():
            parser.error('missing bundled host library: ' + str(source))
        target = libs / (name + '.so.5.9')
        if target.exists() and target.read_bytes() != source.read_bytes():
            parser.error('refusing to overwrite different host library: ' + str(target))
        shutil.copy2(source, target)
        link = libs / (name + '.so.5')
        if not link.is_symlink():
            link.symlink_to(name + '.so.5.9')
# Property-generation rules can keep an old fingerprint when BUILD_NUMBER
# changes. Clean installed outputs before rebuilding a different identity;
# compiled objects remain available. Never rename a stale ZIP as a new release.
old_props = root / 'out/target/product' / args.device / 'system/build.prop'
clean_identity = args.clean_installed
if old_props.is_file():
    props = dict(line.split('=', 1) for line in old_props.read_text().splitlines()
                 if '=' in line and not line.startswith('#'))
    clean_identity = clean_identity or (props.get('ro.build.version.incremental') != args.build_number
                      or props.get('ro.build.type') != args.variant)
    if clean_identity:
        print('Build identity changed: cleaning installed outputs before rebuilding.', flush=True)
env = os.environ.copy()
# A changing default build number can leave unchanged vendor/product props with
# an older fingerprint during incremental userdebug bring-up builds.
env["BUILD_NUMBER"] = args.build_number
env["MKE2FS_CONFIG"] = str(root / "system/extras/ext4_utils/mke2fs.conf")
raise SystemExit(subprocess.run(
    ['bash', '-c', 'source build/envsetup.sh && lunch "$1" && shift && '
     + ('m installclean && ' if clean_identity else '') + 'm "$@"',
     'tilapia-build', 'lineage_' + args.device + '-' + args.variant, '-j' + str(args.jobs)] + args.targets,
    cwd=root, env=env).returncode)
