"""Read selected VPK resources and the KV3 v5 subset used by ping definitions.

Format adaptation: ValveResourceFormat, MIT, commit 73f5ee5a02531f946b5f3e83dc79b264097cf8d3.
Unsupported encodings fail explicitly; this is not a general Valve resource editor.
"""
import io
from pathlib import Path, PurePosixPath
import struct
import zlib


def unpack(fmt, body, offset=0):
    return struct.unpack_from('<' + fmt, body, offset)


def cstring(stream):
    result = bytearray()
    while True:
        char = stream.read(1)
        if not char:
            raise ValueError('Truncated VPK string')
        if char == b'\0':
            return result.decode('utf-8')
        result.extend(char)


def resources(directory, wanted, sources=None):
    index = directory / 'pak01_dir.vpk'
    raw = index.read_bytes()
    magic, version, size = unpack('III', raw)
    if magic != 0x55AA1234 or version not in (1, 2):
        raise ValueError('Unsupported VPK index')
    start = 28 if version == 2 else 12
    if size > len(raw) - start:
        raise ValueError('Truncated VPK tree')
    tree = io.BytesIO(raw[start:start + size])
    result = {}
    while extension := cstring(tree):
        while folder := cstring(tree):
            while name := cstring(tree):
                entry = tree.read(18)
                if len(entry) != 18:
                    raise ValueError('Truncated VPK entry')
                crc, preload, archive, offset, length, end = unpack('IHHIIH', entry)
                if end != 0xFFFF:
                    raise ValueError('Invalid VPK entry')
                prefix = tree.read(preload)
                if len(prefix) != preload:
                    raise ValueError('Truncated VPK preload')
                path = str(PurePosixPath('' if folder == ' ' else folder) / (name + '.' + extension))
                if path not in wanted:
                    continue
                if path in result or length + preload > 8 * 1024 * 1024:
                    raise ValueError('Invalid selected VPK resource')
                source = index if archive == 0x7FFF else directory / f'pak01_{archive:03d}.vpk'
                if sources is not None:
                    sources.add(source)
                absolute_offset = offset + (start + size if archive == 0x7FFF else 0)
                with source.open('rb') as handle:
                    handle.seek(absolute_offset)
                    body = prefix + handle.read(length)
                if len(body) != preload + length or zlib.crc32(body) != crc:
                    raise ValueError('VPK resource failed CRC verification')
                result[path] = body
    if set(result) != set(wanted):
        raise ValueError('Required game definitions are missing')
    return result


def lz4(block, expected):
    if expected > 8 * 1024 * 1024:
        raise ValueError('KV3 buffer too large')
    output = bytearray()
    position = 0
    def length(initial):
        nonlocal position
        value = initial
        if initial == 15:
            while True:
                extra = block[position]; position += 1; value += extra
                if extra != 255:
                    break
        return value
    while position < len(block):
        token = block[position]; position += 1
        count = length(token >> 4)
        if position + count > len(block) or len(output) + count > expected:
            raise ValueError('Invalid LZ4 literal')
        output.extend(block[position:position + count]); position += count
        if position == len(block):
            break
        distance = unpack('H', block, position)[0]; position += 2
        count = length(token & 15) + 4
        if not distance or distance > len(output) or len(output) + count > expected:
            raise ValueError('Invalid LZ4 match')
        for _ in range(count):
            output.append(output[-distance])
    if len(output) != expected:
        raise ValueError('KV3 decompression length mismatch')
    return bytes(output)


def kv3(resource):
    _, header_version, _, table_offset, blocks = unpack('IHHII', resource)
    if header_version != 12 or blocks > 128:
        raise ValueError('Unsupported compiled resource header')
    data = None
    for index in range(blocks):
        entry = 8 + table_offset + index * 12
        kind, offset, size = unpack('4sII', resource, entry)
        if kind == b'DATA':
            start = entry + 4 + offset
            data = resource[start:start + size]
            if len(data) != size:
                raise ValueError('Truncated DATA block')
    if data is None or len(data) < 120 or unpack('I', data)[0] != 0x4B563305:
        raise ValueError('Unsupported KV3 version')
    if unpack('I', data, 20)[0] != 1 or unpack('II', data, 56) != (0, 0):
        raise ValueError('Unsupported KV3 compression or blob storage')
    n1, c1, n2, c2 = unpack('IIII', data, 72)
    if 120 + c1 + c2 != len(data):
        raise ValueError('Unexpected KV3 payload length')
    first = lz4(data[120:120 + c1], n1)
    second = lz4(data[120 + c1:], n2)
    string_bytes = unpack('I', data, 28)[0]
    count = unpack('I', first, (string_bytes + 3) & ~3)[0]
    strings = first[:string_bytes].split(b'\0')
    if not strings or strings[-1] != b'' or len(strings) - 1 != count:
        raise ValueError('Invalid KV3 string table')
    strings = [value.decode('utf-8') for value in strings[:-1]]
    lengths_count = unpack('I', data, 108)[0]
    lengths = io.BytesIO(second[:lengths_count * 4])
    position = lengths_count * 4
    lanes = {}
    for width, count in zip((1, 2, 4, 8), unpack('IIII', data, 88)):
        if count:
            position = (position + width - 1) & ~(width - 1)
        lanes[width] = io.BytesIO(second[position:position + count * width])
        position += count * width
    type_count = unpack('I', data, 40)[0]
    types = io.BytesIO(second[position:position + type_count])
    if second[position + type_count:] != b'\x00\xdd\xee\xff':
        raise ValueError('Invalid KV3 trailer')
    def pop(stream, fmt):
        width = struct.calcsize('<' + fmt)
        body = stream.read(width)
        if len(body) != width:
            raise ValueError('Truncated KV3 lane')
        return unpack(fmt, body)[0]
    def kind():
        value = pop(types, 'B')
        if value & 0xC0:
            raise ValueError('Unsupported KV3 flags')
        return value
    def string():
        index = pop(lanes[4], 'i')
        if index < 0 or index >= len(strings):
            raise ValueError('Invalid KV3 string reference')
        return strings[index]
    def read(tag, depth=0):
        if depth > 64:
            raise ValueError('KV3 nesting too deep')
        if tag == 6:
            return string()
        if tag == 11:
            return pop(lanes[4], 'i')
        if tag in (13, 14, 16):
            return {13: True, 14: False, 16: 1}[tag]
        if tag == 9:
            count = pop(lengths, 'i')
            if not 0 <= count <= 100000:
                raise ValueError('Invalid KV3 object length')
            result = {}
            for _ in range(count):
                child = kind(); key = string()
                result[key] = read(child, depth + 1)
            return result
        if tag in (8, 24):
            count = pop(lanes[1], 'B') if tag == 24 else pop(lanes[4], 'i')
            if not 0 <= count <= 100000:
                raise ValueError('Invalid KV3 array length')
            shared = kind() if tag == 24 else None
            return [read(shared if shared is not None else kind(), depth + 1) for _ in range(count)]
        raise ValueError('Unsupported KV3 type: ' + str(tag))
    result = read(kind())
    if any(stream.read(1) for stream in (lengths, types, *lanes.values())):
        raise ValueError('Unconsumed KV3 lanes')
    return result


def message_tokens(tree):
    result = set()
    if isinstance(tree, dict):
        for key, value in tree.items():
            if key in {'m_strMessageToken', 'm_strSelfMessageToken'} and isinstance(value, str) and value.startswith('#'):
                result.add(value[1:])
            result.update(message_tokens(value))
    elif isinstance(tree, list):
        for value in tree:
            result.update(message_tokens(value))
    return result
