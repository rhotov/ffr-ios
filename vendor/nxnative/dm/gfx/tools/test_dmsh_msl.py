#!/usr/bin/env python3
"""Synthetic instruction-boundary and native image reflection regressions."""
import struct
import tempfile
import unittest
from pathlib import Path

import dmsh_msl


def module(*instructions):
    words = [0x07230203, 0x00010300, 0, 64, 0]
    for opcode, operands in instructions:
        words += [((len(operands) + 1) << 16) | opcode, *operands]
    return struct.pack(f'<{len(words)}I', *words)


class MslSelection(unittest.TestCase):
    def test_buffer_atomic_and_opcode_in_literal_keep_old_flags(self):
        # AtomicOr on ordinary storage, plus a literal that looks like opcode60.
        data = module((43, [1, 2, (6 << 16) | 60]), (241, [1, 2, 3, 4, 5, 6]))
        flags = ['--msl', '--msl-version', '20300', '--msl-argument-buffers']
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'buffer.spv'; path.write_bytes(data)
            self.assertEqual(dmsh_msl.cross_args(path, flags), flags)
        self.assertEqual(flags[2], '20300')

    def test_image_texel_pointer_selects_native_version_only(self):
        data = module((60, [1, 2, 3, 4, 5]), (241, [6, 7, 2, 8, 9, 10]))
        flags = ['--msl', '--msl-version', '20300', '--msl-argument-buffers']
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'image.spv'; path.write_bytes(data)
            self.assertEqual(dmsh_msl.cross_args(path, flags), [*flags[:2], '30100', *flags[3:]])
        self.assertEqual(flags[2], '20300')

    def test_malformed_instructions_do_not_select_fallback(self):
        for data in (b'', module()[:-1], module((0, [])) + bytes(4),
                     module((60, [1, 2, 3, 4])), module((60, [1, 2, 3, 4, 5]))[:-4]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                dmsh_msl.has_image_atomics(data)

    def test_native_image_access_and_name_boundaries(self):
        src = 'args.cp_i_cb2_4C.atomic_fetch_or(uint3(p), v); args.cp_i_cb2_4A.read(p);'
        self.assertEqual(dmsh_msl.image_access(src, 'cp_i_cb2_4C', 'texture3d<uint, access::read_write>'), {'rw': True, 'atomic': True})
        self.assertEqual(dmsh_msl.image_access(src, 'cp_i_cb2_4A', 'texture3d<uint, access::read>'), {'rw': False, 'atomic': False})
        self.assertEqual(dmsh_msl.image_access(src, 'cp_i_cb2_4', 'texture3d<uint, access::write>'), {'rw': True, 'atomic': False})

    def test_buffer_offsets_preserve_arrays_methods_and_width(self):
        src = (Path(__file__).parent.parent / 'tests/t_tbuf_alignment.metal').read_text()
        result = dmsh_msl.buffer_texture_offsets(src)
        self.assertIn('constant uint2* dm_tbuf [[id(7)]];', result)
        self.assertIn('fixed.read(dm_tbuf_coord(uint(i), spvDescriptorSet0.dm_tbuf[1 + (0)]))', result)
        self.assertIn('cp_tb_cb0_40000000[i % 4].read(dm_tbuf_coord(uint(i), spvDescriptorSet0.dm_tbuf[2 + (i % 4)]))', result)
        self.assertIn('spvDescriptorSet0.dm_tbuf[1 + (0)].y', result)
        self.assertIn('write(uint4(100 + i, 0, 0, 0),dm_tbuf_coord(uint( i), spvDescriptorSet0.dm_tbuf[6 + (0)]))', result)
        self.assertIn('atomic_fetch_add(dm_tbuf_coord(uint(i), spvDescriptorSet0.dm_tbuf[6 + (0)]), uint4(7))', result)
        self.assertIn('x < range.y ? x + range.x : 0xffffffffu', result)
        self.assertIn('data[i] = 200 + i', result)  # Raw buffer writes are unchanged.

    def test_nonbuffer_shader_is_byte_identical(self):
        src = 'struct spvDescriptorSetBuffer0 {\n texture2d<float> color [[id(0)]];\n};\n'
        self.assertEqual(dmsh_msl.buffer_texture_offsets(src), src)

    def test_unknown_buffer_operations_are_rejected(self):
        src = ('struct spvDescriptorSetBuffer0 {\n texture_buffer<float> t [[id(0)]];\n};\n'
               'float4 f = spvDescriptorSet0.t.sample(sampler(), x);')
        with self.assertRaisesRegex(ValueError, 'unsupported buffer texture method'):
            dmsh_msl.buffer_texture_offsets(src)

    def test_image_fences_do_not_receive_coordinate_offsets(self):
        src = ('struct spvDescriptorSetBuffer0 {\n texture_buffer<uint, access::read_write> t [[id(0)]];\n};\n'
               'spvImageFence(spvDescriptorSet0.t); spvDescriptorSet0.t.fence();')
        result = dmsh_msl.buffer_texture_offsets(src)
        self.assertIn('spvImageFence(spvDescriptorSet0.t); spvDescriptorSet0.t.fence();', result)


if __name__ == '__main__':
    unittest.main()
