import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('build_release', Path(__file__).resolve().parents[1] / 'tools/build_release.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        self.binary = self.root / 'helper'
        self.binary.write_bytes(b'test fixture executable')
        for name in builder.RUNTIME_FILES:
            (self.runtime / name).write_text('{"models": [{"slug":"test"}]}' if name.endswith('.json') else '# test fixture\n')

    def build(self, out='out', version='test-1'):
        return builder.build_release(self.binary, self.runtime, self.root / out, version)

    def test_deterministic_whitelist_hashes_and_permissions(self):
        (self.runtime / 'runner.py').write_text('DO NOT SHIP')
        (self.runtime / '.env').write_text('DO NOT SHIP')
        first = self.build()
        for source in self.runtime.iterdir():
            source.chmod(0o700)
        second = self.build('another')
        self.assertEqual(first, second)
        path = self.root / 'out' / first['filename']
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), first['sha256'])
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(set(archive.namelist()), {'bin/device-helper'} | {'runtime/' + name for name in builder.RUNTIME_FILES})
            for entry in first['files']:
                data = archive.read(entry['path'])
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry['sha256'])
                mode = archive.getinfo(entry['path']).external_attr >> 16
                self.assertTrue(stat.S_ISREG(mode))
                self.assertEqual(stat.S_IMODE(mode), int(entry['mode'], 8))
        self.assertEqual(self.build(), first)

    def test_reject_symlink_input(self):
        self.binary.unlink()
        self.binary.symlink_to(self.runtime / 'common.py')
        with self.assertRaises(ValueError): self.build()

    def test_missing_required_file(self):
        (self.runtime / 'outputs.py').unlink()
        with self.assertRaises(FileNotFoundError): self.build()

    def test_broken_source_and_model_catalog(self):
        (self.runtime / 'task_agent.py').write_text('def broken(')
        with self.assertRaises(SyntaxError): self.build()
        (self.runtime / 'task_agent.py').write_text('# fine')
        (self.runtime / 'models.json').write_text('{"models":[]}')
        with self.assertRaises(ValueError): self.build()

    def test_existing_artifact_cannot_be_overwritten(self):
        result = self.build()
        artifact = self.root / 'out' / result['filename']
        artifact.write_bytes(b'corrupted')
        with self.assertRaises(ValueError): self.build()

    def test_metadata_collision_does_not_relabel_release(self):
        self.build()
        with self.assertRaises(ValueError): self.build(version='different')

    def test_provider_overlay_is_package_only_and_pinned(self):
        original = "model = config.get('model', 'deepseek-flash')\nmodels = ('deepseek-flash', 'deepseek-v4-pro')\nname = 'Device DeepSeek gateway'\nother = config.get('model', 'deepseek-flash')\nfeatures = ('plugins', 'remote_plugin', 'computer_use', 'browser_use', 'apps', 'hooks')\neffort = config.get('reasoning_effort', 'high')\nsettings = '{\"disableAllHooks\":true}'\n"
        (self.runtime / 'common.py').write_text(original)
        original += "\nenv.update(CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL='1')\nargs = ['--output-format', 'stream-json', '--verbose', '--model', model]\n"
        (self.runtime / 'common.py').write_text(original)
        result = builder.build_release(self.binary, self.runtime, self.root / 'new', 'test-2', 'abliteration', 'function')
        self.assertEqual((self.runtime / 'common.py').read_text(), original)
        self.assertEqual(result['model'], 'abliterated-model-large-flash')
        with zipfile.ZipFile(self.root / 'new' / result['filename']) as archive:
            source = archive.read('runtime/common.py').decode()
            self.assertNotIn('deepseek', source.lower())
            self.assertIn("('abliterated-model-large-flash',)", source)
            model = json.loads(archive.read('runtime/models.json'))['models'][0]
            self.assertEqual(model['slug'], result['model'])
            self.assertEqual(model['apply_patch_tool_type'], 'function')
            self.assertEqual(model['context_window'], 1048576)
            self.assertIsNone(model['multi_agent_version'])
            self.assertIn("'multi_agent_v2', 'goals'", source)
            self.assertIn("effort = 'none'", source)
            self.assertIn('"alwaysThinkingEnabled":false', source)
            self.assertIn("MAX_THINKING_TOKENS='0'", source)
            self.assertIn("'--effort', 'low'", source)
            self.assertEqual(model['default_reasoning_level'], 'none')
            self.assertEqual(model['supported_reasoning_levels'][0]['effort'], 'none')

    def test_no_patch_tool_catalog_disables_custom_tool(self):
        data = b'{"models":[{"slug":"deepseek-flash","multi_agent_version":"v2"}]}'
        result = json.loads(builder.provider_overlay('models.json', data, 'abliteration', 'none'))
        self.assertIsNone(result['models'][0]['apply_patch_tool_type'])
        self.assertIsNone(result['models'][0]['multi_agent_version'])

    def test_provider_overlay_rejects_unreviewed_source_drift(self):
        with self.assertRaises(ValueError):
            builder.build_release(self.binary, self.runtime, self.root / 'new', 'test-2', 'abliteration')


if __name__ == '__main__':
    unittest.main()
