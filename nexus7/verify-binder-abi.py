#!/usr/bin/env python3
"""Cross-compile and compare Binder layouts/ioctls with Android 11's userspace header."""
import argparse
from pathlib import Path
import struct
import subprocess
import tempfile

EXPRESSIONS = [
    "BINDER_CURRENT_PROTOCOL_VERSION",
    "sizeof(binder_size_t)", "sizeof(binder_uintptr_t)",
    "sizeof(struct flat_binder_object)", "sizeof(struct binder_write_read)",
    "sizeof(struct binder_transaction_data)", "sizeof(struct binder_transaction_data_sg)",
    "sizeof(struct binder_transaction_data_secctx)",
    "sizeof(struct binder_buffer_object)", "sizeof(struct binder_fd_array_object)",
    "offsetof(struct binder_transaction_data, sender_euid)",
    "offsetof(struct binder_transaction_data, data_size)",
    "offsetof(struct binder_transaction_data, data)",
    "BINDER_WRITE_READ", "BINDER_VERSION", "BINDER_SET_CONTEXT_MGR",
    "BINDER_SET_CONTEXT_MGR_EXT", "BINDER_GET_NODE_DEBUG_INFO", "BINDER_GET_NODE_INFO_FOR_REF",
    "BC_TRANSACTION", "BC_TRANSACTION_SG", "BC_REPLY_SG",
    "BR_TRANSACTION", "BR_TRANSACTION_SEC_CTX", "BR_REPLY",
    "BINDER_TYPE_PTR", "BINDER_TYPE_FDA", "FLAT_BINDER_FLAG_TXN_SECURITY_CTX",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel_header", type=Path)
    parser.add_argument("userspace_header", type=Path)
    parser.add_argument("--toolchain-prefix", required=True)
    parser.add_argument("--uapi-include", type=Path, required=True, help="include directory produced by make headers_install")
    args = parser.parse_args()
    source = "#include <stddef.h>\nconst unsigned long binder_abi[] = {\n" + ",\n".join(EXPRESSIONS) + "\n};\n"
    with tempfile.TemporaryDirectory(prefix="tilapia-binder-abi-") as temp:
        work = Path(temp)
        (work / "probe.c").write_text(source)
        # Android 11 bionic sys/types.h defines these through the same ARM UAPI
        # types. The kernel-only toolchain does not ship a libc sysroot.
        (work / "types.h").write_text("#include <linux/types.h>\n"
                                    "typedef __kernel_pid_t pid_t;\n"
                                    "typedef __kernel_uid32_t uid_t;\n")
        for mode in (64, 32):
            results = []
            for label, header in (("kernel", args.kernel_header), ("userspace", args.userspace_header)):
                obj = work / (label + ".o")
                binary = work / (label + ".bin")
                command = [args.toolchain_prefix + "gcc", "-std=gnu99", "-c", str(work / "probe.c"),
                           "-I", str(args.uapi_include.resolve()), "-include", str(work / "types.h"), "-include", str(header.resolve()), "-o", str(obj)]
                if mode == 32:
                    command.append("-DBINDER_IPC_32BIT=1")
                subprocess.run(command, check=True)
                subprocess.run([args.toolchain_prefix + "objcopy", "-O", "binary", "-j", ".rodata", str(obj), str(binary)], check=True)
                data = binary.read_bytes()
                if len(data) != 4 * len(EXPRESSIONS):
                    raise RuntimeError("Unexpected ABI probe output size; use the 32-bit ARM toolchain")
                results.append(struct.unpack("<" + "I" * len(EXPRESSIONS), data))
            for expression, kernel, user in zip(EXPRESSIONS, *results):
                if kernel != user:
                    raise RuntimeError(f"ABI {mode}: {expression}: kernel {kernel:#x} != userspace {user:#x}")
            print(f"PASS: {mode}-bit Binder wire ABI on ARM: {len(EXPRESSIONS)} layouts, offsets, flags and ioctls match Android 11")


if __name__ == "__main__":
    main()
