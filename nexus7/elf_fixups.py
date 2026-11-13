"""Equal-length .dynstr fixups for the pinned 32-bit Tegra EGL binaries."""
import struct


def replace_dynamic_strings(data, replacements):
    if data[:7] != b"\x7fELF\x01\x01\x01" or struct.unpack_from("<H", data, 18)[0] != 40:
        raise ValueError("expected a little-endian ARM ELF32 image")
    shoff = struct.unpack_from("<I", data, 32)[0]
    entsize, count = struct.unpack_from("<HH", data, 46)
    if entsize != 40 or not count or shoff + count * entsize > len(data):
        raise ValueError("unsupported ELF section table")
    sections = [struct.unpack_from("<10I", data, shoff + i * entsize) for i in range(count)]
    links = {section[6] for section in sections if section[1] == 11}
    if len(links) != 1:
        raise ValueError("expected one dynamic symbol string table")
    index = links.pop()
    if index >= count:
        raise ValueError("invalid .dynsym string table link")
    section = sections[index]
    start, size = section[4], section[5]
    if start + size > len(data):
        raise ValueError("dynamic string table exceeds file")
    strings = data[start:start + size]
    for old, new in replacements:
        if len(old) != len(new) or strings.count(old) != 1:
            raise ValueError("unexpected source binary string: " + repr(old))
        strings = strings.replace(old, new)
    fixed = data[:start] + strings + data[start + size:]
    if len(fixed) != len(data):
        raise AssertionError("ELF layout changed")
    return fixed


def patch_tegra_egl(data):
    return replace_dynamic_strings(data, ((b"NvOsLibraryLoad\0", b"GrouperLibLoad_\0"),
                                          (b"libnvos.so\0", b"libdgv1.so\0")))


def remove_needed(data, library):
    """Remove one unused DT_NEEDED without moving any ELF sections or strings."""
    replace_dynamic_strings(data, [])  # Validate the pinned ARM ELF layout.
    shoff = struct.unpack_from('<I', data, 32)[0]
    entsize, count = struct.unpack_from('<HH', data, 46)
    sections = [struct.unpack_from('<10I', data, shoff + i * entsize) for i in range(count)]
    dynamic = [section for section in sections if section[1] == 6]
    if len(dynamic) != 1:
        raise ValueError('expected one dynamic section')
    section = dynamic[0]
    strings = sections[section[6]]
    string_data = data[strings[4]:strings[4] + strings[5]]
    start, size = section[4], section[5]
    matches = []
    for offset in range(start, start + size, 8):
        tag, value = struct.unpack_from('<II', data, offset)
        if tag == 0:
            break
        if tag == 1 and string_data[value:].split(b'\0', 1)[0] == library.encode():
            matches.append(offset)
    if len(matches) != 1:
        raise ValueError('expected one DT_NEEDED for ' + library)
    offset = matches[0]
    return data[:offset] + data[offset + 8:start + size] + bytes(8) + data[start + size:]
