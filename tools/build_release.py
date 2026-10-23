#!/usr/bin/env python3
"""Developer-only immutable helper release builder; never runs on the enrolled PC."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import zipfile

RUNTIME_FILES = ('common.py', 'models.json', 'outputs.py', 'release_client.py', 'task_agent.py')
ABLITERATION_MODEL = 'abliterated-model-large-flash'


def provider_overlay(name, data, profile, patch_tool):
    """Change only newly packaged bytes; never edit the shared runtime source."""
    if profile is None:
        return data
    if profile != 'abliteration':
        raise ValueError('Unknown model profile')
    if name == 'common.py':
        text = data.decode()
        replacements = (
            ("config.get('model', 'deepseek-flash')", "config.get('model', '" + ABLITERATION_MODEL + "')", 2),
            ("('deepseek-flash', 'deepseek-v4-pro')", "('" + ABLITERATION_MODEL + "',)", 1),
            ('Device DeepSeek gateway', 'Device Abliteration gateway', 1),
            ("config.get('reasoning_effort', 'high')", "'none'", 1),
            ('{"disableAllHooks":true}', '{"disableAllHooks":true,"alwaysThinkingEnabled":false}', 1),
            ("CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL='1')", "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL='1', MAX_THINKING_TOKENS='0')", 1),
            ("'--output-format', 'stream-json', '--verbose', '--model', model]", "'--output-format', 'stream-json', '--verbose', '--effort', 'low', '--model', model]", 1),
            ("('plugins', 'remote_plugin', 'computer_use', 'browser_use', 'apps', 'hooks')", "('plugins', 'remote_plugin', 'computer_use', 'browser_use', 'apps', 'hooks', 'multi_agent', 'multi_agent_v2', 'goals')", 1),
        )
        for before, after, count in replacements:
            if text.count(before) != count:
                raise ValueError('Runtime changed; review the model overlay before releasing')
            text = text.replace(before, after)
        return text.encode()
    if name == 'models.json':
        catalog = json.loads(data)
        if len(catalog.get('models', [])) != 1:
            raise ValueError('Expected one model in the source catalog')
        model = catalog['models'][0]
        model.update(slug=ABLITERATION_MODEL, display_name='Abliterated Model Large Flash',
                     description='Abliteration model using the server-managed API connection.',
                     context_window=1048576, max_context_window=1048576,
                     input_modalities=['text', 'image'],
                     apply_patch_tool_type=None if patch_tool == 'none' else patch_tool,
                     multi_agent_version=None, default_reasoning_level='none',
                     supported_reasoning_levels=[{'effort': 'none', 'description': 'Thinking disabled; lowest reasoning level'}])
        result = (json.dumps(catalog, indent=2, sort_keys=True) + '\n').encode()
        if b'deepseek' in result.lower():
            raise ValueError('Unexpected stale provider label in model catalog')
        return result
    return data


def regular_bytes(path):
    path = Path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('Release input must be a regular non-symlink file: ' + str(path))
    return path.read_bytes()


def build_release(binary, runtime, destination, version, model_profile=None, patch_tool='freeform'):
    """Whitelist inputs, normalize ZIP metadata, and refuse immutable overwrites."""
    inputs = [('bin/device-helper', regular_bytes(binary), 0o755)]
    if patch_tool not in ('freeform', 'function', 'none'):
        raise ValueError('Invalid patch tool type')
    inputs += [('runtime/' + name, provider_overlay(name, regular_bytes(Path(runtime) / name), model_profile, patch_tool), 0o644)
               for name in RUNTIME_FILES]
    # Validate source syntax/catalog before shipping a broken worker release.
    for name, data, _ in inputs:
        if name.endswith('.py'):
            compile(data, name, 'exec')
        if name.endswith('models.json'):
            catalog = json.loads(data)
            if not isinstance(catalog.get('models'), list) or not catalog['models']:
                raise ValueError('Model catalog needs a nonempty models list')
    buffer = io.BytesIO()
    files = []
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data, mode in sorted(inputs):
            entry = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | mode) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data, compresslevel=9)
            files.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest(),
                          'size': len(data), 'mode': format(mode, '04o')})
    data = buffer.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    metadata = {'version': version, 'release': version, 'sha256': digest,
                'size': len(data), 'filename': digest + '.zip',
                'entry_binary': 'bin/device-helper',
                'worker_entry': 'runtime/task_agent.py',
                'model_catalog': 'runtime/models.json', 'files': files}
    if model_profile:
        metadata['model'] = ABLITERATION_MODEL
        metadata['model_profile'] = model_profile
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for path, contents in ((destination / metadata['filename'], data),
                           (destination / (digest + '.json'), (json.dumps(metadata, indent=2, sort_keys=True) + '\n').encode())):
        if path.exists():
            if path.is_symlink() or path.read_bytes() != contents:
                raise ValueError('Immutable release already exists with different content: ' + str(path))
        else:
            with path.open('xb') as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--model-profile', choices=('abliteration',))
    parser.add_argument('--patch-tool', choices=('freeform', 'function', 'none'), default='freeform')
    args = parser.parse_args()
    print(json.dumps(build_release(args.binary, args.runtime, args.out, args.version, args.model_profile, args.patch_tool), indent=2))


if __name__ == '__main__':
    main()
