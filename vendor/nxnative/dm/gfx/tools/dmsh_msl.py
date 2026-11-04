"""Select native Metal image atomics without changing other shader output."""
import re
import struct


def has_image_atomics(data):
    """OpImageTexelPointer names image memory; buffer/workgroup atomics do not.

    Read instruction boundaries rather than searching words: opcode 60 can also
    occur in an ID, literal or string. Reject truncated input before selecting a
    language version so a malformed module cannot silently use the old path.
    """
    if len(data) < 20 or len(data) % 4:
        raise ValueError('invalid SPIR-V module length')
    words = struct.unpack(f'<{len(data) // 4}I', data)
    if words[0] != 0x07230203:
        raise ValueError('invalid SPIR-V magic')
    image_atomic = False
    at = 5
    while at < len(words):
        count, opcode = words[at] >> 16, words[at] & 0xffff
        if not count or at + count > len(words):
            raise ValueError(f'invalid SPIR-V instruction at word {at}')
        if opcode == 60:  # OpImageTexelPointer
            if count != 6:
                raise ValueError('invalid OpImageTexelPointer length')
            image_atomic = True
        at += count
    return image_atomic


def cross_args(path, defaults):
    """Keep historical MSL 2.3 flags byte-for-byte for non-image-atomic SPIR-V.

    SPIRV-Cross before MSL 3.1 emulates image atomics through a linear buffer;
    that path neither linearizes 3D coordinates nor shares Metal texture memory.
    MSL 3.1 emits native texture methods, retaining dimensions and coherence.
    """
    with open(path, 'rb') as source:
        atomic = has_image_atomics(source.read())
    args = list(defaults)
    if atomic:
        args[args.index('--msl-version') + 1] = '30100'
    return args


def image_access(msl, name, type_name):
    """Native image binding requirements from its actual MSL declaration/use."""
    atomic = bool(re.search(r'\b' + re.escape(name) + r'\s*\.\s*atomic_\w+\s*\(', msl))
    writable = bool(re.search(r'\baccess::(?:write|read_write)\b', type_name))
    return {'rw': writable or atomic, 'atomic': atomic}


def buffer_texture_offsets(msl):
    """Address an aligned Metal view without changing the NVN buffer's range.

    Metal requires a device-specific byte alignment even for buffer textures.
    The replay binds an aligned-down view and supplies (prefix texels, logical
    width) for each argument slot. This keeps GPU writes and aliases coherent;
    copying CPU memory to a temporary texture would not.
    """
    body = re.search(r'struct spvDescriptorSetBuffer0\s*\{(.*?)\};', msl, re.S)
    if not body:
        return msl
    members = list(re.finditer(r'^\s*(.+?)\s+(\w+)\s*\[\[id\((\d+)\)\]\];', body[1], re.M))
    buffers = [m for m in members if 'texture_buffer<' in m[1]]
    if not buffers:
        return msl
    def array_count(ty):
        a = re.fullmatch(r'array<.+,\s*(\d+)>', ty.strip())
        return int(a[1]) if a else 1
    aid = max(int(m[3]) + array_count(m[1]) for m in members)
    if aid >= 192:
        raise ValueError('buffer texture metadata exceeds the argument table')

    def end_group(text, start):
        stack = []
        for i in range(start, len(text)):
            if text[i] in '([':
                stack.append(text[i])
            elif text[i] in ')]':
                if not stack or (stack.pop(), text[i]) not in (('(', ')'), ('[', ']')):
                    raise ValueError('unbalanced buffer texture expression')
                if not stack:
                    return i
        raise ValueError('unterminated buffer texture expression')

    edits = []
    for member in buffers:
        name, base = member[2], int(member[3])
        for use in re.finditer(r'\bspvDescriptorSet0\.' + re.escape(name) + r'\b', msl):
            at = use.end()
            index = '0'
            if msl[at:at+1] == '[':
                end = end_group(msl, at)
                index = msl[at+1:end]
                at = end + 1
            method = re.match(r'\.(\w+)\(', msl[at:])
            if not method:
                # SPIRV-Cross emits image barriers through this generic helper;
                # fences refer to the resource, never to a texel coordinate.
                if re.search(r'\bspvImageFence\(\s*$', msl[:use.start()]) and msl[at:at+1] == ')':
                    continue
                raise ValueError(f'unsupported buffer texture use: {msl[use.start():at+60]}')
            op = method[1]
            start = at + method.end() - 1
            end = end_group(msl, start)
            meta = f'spvDescriptorSet0.dm_tbuf[{base} + ({index})]'
            if op == 'fence':
                continue
            if op == 'get_width':
                if msl[start+1:end].strip():
                    raise ValueError('buffer texture width query has arguments')
                edits.append((use.start(), end+1, meta + '.y'))
                continue
            if op not in ('read', 'write') and not op.startswith('atomic_'):
                raise ValueError(f'unsupported buffer texture method: {op}')
            # Coordinates are first for reads/atomics, second for writes.
            args, pos, i = [], start + 1, start + 1
            while i < end:
                if msl[i] in '([':
                    i = end_group(msl, i) + 1
                elif msl[i] == ',':
                    args.append((pos, i)); pos = i + 1; i += 1
                else:
                    i += 1
            args.append((pos, end))
            lo, hi = args[1 if op == 'write' else 0]
            edits.append((lo, hi, f'dm_tbuf_coord(uint({msl[lo:hi]}), {meta})'))
    parts, at = [], 0
    for lo, hi, value in sorted(edits):
        if lo < at:
            raise ValueError('nested buffer texture access needs lowering before offset compensation')
        parts.extend((msl[at:lo], value)); at = hi
    parts.append(msl[at:]); msl = ''.join(parts)
    # Insert before rewriting can shift the struct; look it up again.
    body = re.search(r'struct spvDescriptorSetBuffer0\s*\{(.*?)\};', msl, re.S)
    msl = msl[:body.end(1)] + f'    constant uint2* dm_tbuf [[id({aid})]];\n' + msl[body.end(1):]
    helper = ('\n// Invalid logical coordinates stay out of bounds of the expanded view.\n'
              'inline uint dm_tbuf_coord(uint x, uint2 range)\n'
              '{ return x < range.y ? x + range.x : 0xffffffffu; }\n\n')
    return msl[:body.start()] + helper + msl[body.start():]
