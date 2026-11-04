#!/usr/bin/env python3
"""C1 proof outcomes/order and serial-versus-process equivalence; no game data."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import c1_guard


class ParallelC1(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.library = Path(self.temp.name) / 'b-c1-equivalence'; self.library.mkdir()
        (self.library / 'inputs.tsv').write_text('game\tsynthetic\ninputs\t0123456789ab\n')

    def shader(self, name, index, definitions='', stage='vp', reflection='valid'):
        source = definitions + ('\nfloat4 result = (*' + stage + '_c1)._m0[' + index + '];\n' if index is not None else '')
        (self.library / name).write_text(source)
        key = name[:-len('_b.metal')] if name.endswith('_b.metal') else name[:-6]
        metadata = self.library / (key + '_b.json')
        if reflection == 'valid':
            # 16 bytes -> 256-byte allocation + 256-byte zero gap: indices0..31.
            metadata.write_text(json.dumps({'c1': '00' * 16}))
        elif reflection == 'invalid':
            metadata.write_text('{')
        return key

    def test_conservative_cases_and_order_match_serial(self):
        cases = [
            ('safe_mask', '_2', 'uint _1 = input; uint _2 = _1 & 3u;', 'vp', 'valid', False),
            ('unknown_plus_mask', '_1 + (_2 & 3u)', 'uint _1 = input; uint _2 = other;', 'fp', 'valid', True),
            ('too_wide_mask', '_2', 'uint _1 = input; uint _2 = _1 & 63u;', 'vp', 'valid', True),
            ('negative', '-1', '', 'fp', 'valid', True),
            ('compute_unknown', '_1', 'uint _1 = input;', 'cp', 'valid', True),
            ('clamped', 'clamp(int(input), 0, 3)', '', 'gp', 'valid', False),
            ('missing_json', '0', '', 'vp', 'missing', True),
            ('invalid_json', '0', '', 'vp', 'invalid', True),
            ('no_access', None, '', 'cp', 'missing', False),
            ('mutated', '_1', 'uint _1 = 0u; _1++;', 'vp', 'valid', True),
            ('last_valid', '31u', '', 'vp', 'valid', False),
            ('past_span', '32u', '', 'vp', 'valid', True),
            ('duplicate_definition', '_2', 'uint _2 = 3u; uint _2 = 3u;', 'vp', 'valid', False),
            ('conflicting_definition', '_2', 'uint _2 = 3u; uint _2 = input;', 'vp', 'valid', True),
            ('unknown_call', 'unsupported(input)', '', 'fp', 'valid', True),
        ]
        expected = []
        # Several chunks ensure the ordered process map has independent tasks.
        for repeat in range(4):
            for label, index, definitions, stage, reflection, unknown in reversed(cases):
                key = self.shader(f'{repeat}_{label}_b.metal', index, definitions, stage, reflection)
                if unknown:
                    expected.append(key)
        expected.sort()
        serial = c1_guard.unproven_keys(str(self.library), 1)
        parallel = c1_guard.unproven_keys(str(self.library), 3)
        self.assertEqual(serial, expected)
        self.assertEqual(parallel, serial)

    def test_variants_keep_original_filename_order_and_duplicates(self):
        for name in ['x_bg0.metal', 'x_b.metal', 'x.metal', 'a_b.metal']:
            self.shader(name, 'input')
        expected = ['a', 'x', 'x', 'x_bg0']
        self.assertEqual(c1_guard.unproven_keys(str(self.library), 1), expected)
        self.assertEqual(c1_guard.unproven_keys(str(self.library), 2), expected)

    def test_source_failure_propagates_and_preserves_output(self):
        self.shader('a_b.metal', 'input')
        (self.library / 'z_b.metal').write_bytes(b'\xff')
        output = Path(self.temp.name) / 'c1_full.tsv'; output.write_text('previous successful table\n')
        for workers in (1, 2):
            with self.subTest(workers=workers), mock.patch.dict(os.environ, {'J': str(workers)}), \
                 mock.patch('sys.argv', ['c1_guard.py', str(self.library), str(output)]):
                with self.assertRaises(UnicodeDecodeError):
                    c1_guard.main()
                self.assertEqual(output.read_text(), 'previous successful table\n')

    def test_empty_library_and_invalid_worker_count(self):
        self.assertEqual(c1_guard.unproven_keys(str(self.library), 3), [])
        with self.assertRaises(ValueError):
            c1_guard.unproven_keys(str(self.library), 0)


if __name__ == '__main__':
    unittest.main()
