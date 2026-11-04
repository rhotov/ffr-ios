"""Batch translation failures must stop publication and preserve retry inputs."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest


def load_gate():
    # The build entry point requires a real game. Load only these file helpers.
    path = Path(__file__).with_name('dmsh_build.py')
    tree = ast.parse(path.read_text())
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('atomic_json', 'translation_failure_report')]
    scope = dict(os=os, json=json, shutil=shutil, tempfile=tempfile, threading=threading)
    exec(compile(tree, str(path), 'exec'), scope)
    return scope['translation_failure_report']


class TranslationFailures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'; self.work.mkdir()
        self.output = self.root / 'output'; self.output.mkdir()
        self.gate = load_gate()

    def job(self, key, complete=True, geometry=False):
        code = self.work / (key + '.bin'); code.write_bytes(b'original shader input')
        job = dict(key=key, stage='vs', code=str(code), c1='01020304')
        if geometry: job['geometry_topology'] = 3
        if complete:
            (self.work / (key + '.spv')).write_bytes(b'SPIR-V')
            (self.work / (key + '.res.json')).write_text('{}')
        return job

    def run_gate(self, jobs, errors=None, exit_code=0):
        worker = subprocess.CompletedProcess([], exit_code, 'batch output', 'batch diagnostic')
        return self.gate(list(jobs), jobs, str(self.work), str(self.output), errors or {}, [worker])

    def test_success_leaves_outputs_and_has_no_failure_report(self):
        job = self.job('good')
        self.assertIsNone(self.run_gate({'good': job}))
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertTrue((self.work / 'good.spv').exists())

    def test_mixed_failure_preserves_complete_runnable_input_and_notes(self):
        jobs = {key: self.job(key) for key in ('good', 'bad')}
        notes = dict(error='unresolved handle', notes=['exact original warning'])
        (self.work / 'bad.failed.json').write_text(json.dumps(notes))
        message = self.run_gate(jobs, {'bad': 'unresolved handle'}, 1)
        self.assertIn('Metal compilation was not started', message)
        evidence = next(self.output.glob('_translation-failures-*'))
        retry = json.loads((evidence / 'jobs.json').read_text())
        self.assertEqual([job['key'] for job in retry], ['bad'])
        self.assertEqual(Path(retry[0]['code']).read_bytes(), b'original shader input')
        self.assertEqual(retry[0]['c1'], jobs['bad']['c1'])
        self.assertEqual(json.loads((evidence / 'bad.failed.json').read_text()), notes)
        report = {item['key']: item for item in json.loads((self.output / '_b_build.json').read_text())}
        self.assertEqual(report['bad']['st'], 'translation failed')
        self.assertEqual(report['good']['st'], 'not built')
        self.assertFalse((self.output / 'inputs.tsv').exists())

    def test_missing_reflection_is_a_failure_even_with_spirv(self):
        job = self.job('partial')
        (self.work / 'partial.res.json').unlink()
        self.assertIsNotNone(self.run_gate({'partial': job}))

    def test_geometry_requires_reader_output(self):
        job = self.job('geometry', geometry=True)
        self.assertIsNotNone(self.run_gate({'geometry': job}))

    def test_unreported_worker_error_rejects_stale_success_files(self):
        job = self.job('stale')
        self.assertIsNotNone(self.run_gate({'stale': job}, exit_code=9))
        evidence = next(self.output.glob('_translation-failures-*'))
        workers = json.loads((evidence / 'workers.json').read_text())
        self.assertEqual(workers[0]['exit'], 9)
        self.assertEqual(workers[0]['stderr'], 'batch diagnostic')


if __name__ == '__main__':
    unittest.main()
