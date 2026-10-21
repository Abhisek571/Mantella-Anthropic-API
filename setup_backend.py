"""One-time setup for Mantella - Anthropic API. Requires Python 3.11.

Builds a patched copy of Mantella.exe (Mantella 0.14, or Mantella Dual Input 1.1) inside this mod.
"""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parent
CONTRACT = ROOT / 'Source/backend/build-contract.json'
OUTPUT = ROOT / 'SKSE/Plugins/MantellaSoftware/Mantella.exe'
MODULES = {
    'src.utils': 'src/utils.py',
    'src.llm.client_base': 'src/llm/client_base.py',
    'src.config.definitions.llm_definitions': 'src/config/definitions/llm_definitions.py',
    'src.config.definitions.vision_definitions': 'src/config/definitions/vision_definitions.py',
    'src.config.definitions.action_definitions': 'src/config/definitions/action_definitions.py',
}


def build(base, output, root=ROOT, write_contract=False):
    if sys.version_info[:2] != (3, 11):
        raise ValueError('Use Python 3.11 to match the Mantella runtime.')
    base, output, root = Path(base).resolve(), Path(output).resolve(), Path(root).resolve()
    if base == output or (output.exists() and base.samefile(output)):
        raise ValueError('Refusing to replace the input Mantella executable.')
    sys.path.insert(0, str(root / 'Source/backend'))
    from frozen_patch import compile_source, patch_executable, sha256, parse_carchive, parse_pyz, code_fingerprint

    contract = json.loads((root / 'Source/backend/build-contract.json').read_text(encoding='utf-8'))
    raw = base.read_bytes()
    base_hash = sha256(raw)
    if not write_contract and base_hash not in contract['bases']:
        raise ValueError('Unsupported Mantella executable. Supported: Mantella 0.14 or Mantella Dual Input 1.1. Nothing changed.')

    replacements, module_hashes = {}, {}
    for name, relative in MODULES.items():
        source = (root / 'Source/backend' / relative).read_bytes()
        module_hashes[name] = sha256(source)
        if not write_contract and contract['modules'][name] != module_hashes[name]:
            raise ValueError('Modified source file: ' + relative)
        replacements[name] = compile_source(source, relative)

    # each replaced module must still be the stock Mantella v0.14 code in the input executable
    pyz = parse_pyz(parse_carchive(raw).entry_data('PYZ.pyz'))
    upstream_gates = {}
    for name, relative in MODULES.items():
        upstream = (root / 'Source/upstream' / relative).read_bytes()
        expected = code_fingerprint(compile_source(upstream, relative))
        if code_fingerprint(pyz.code(name)) != expected:
            raise ValueError(f'{name} in the input executable is not stock Mantella v0.14. Nothing changed.')
        upstream_gates[name] = expected

    data, report = patch_executable(raw, replacements=replacements, expected_exe_sha256=base_hash, expected_module_fingerprints=upstream_gates)
    if write_contract:
        contract['modules'] = module_hashes
        contract['bases'][base_hash]['patched_sha256'] = sha256(data)
        (root / 'Source/backend/build-contract.json').write_text(json.dumps(contract, indent=2) + '\n', encoding='utf-8')
    elif sha256(data) != contract['bases'][base_hash]['patched_sha256']:
        raise ValueError('Built executable does not match the verified release. Nothing changed.')

    output.parent.mkdir(parents=True, exist_ok=True)
    import os, tempfile
    fd, temporary = tempfile.mkstemp(prefix='.mant-', suffix='.tmp', dir=output.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        Path(temporary).replace(output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    report['base_name'] = contract['bases'][base_hash]['name']
    return report


def find_base(root=ROOT):
    """Picks the Mantella.exe to patch from the sibling MO2 mod folders. Dual Input wins over stock Mantella."""
    sys.path.insert(0, str(root / 'Source/backend'))
    from frozen_patch import sha256
    bases = json.loads(CONTRACT.read_text(encoding='utf-8'))['bases']
    found = {}
    for candidate in root.parent.glob('*/SKSE/Plugins/MantellaSoftware/Mantella.exe'):
        if candidate.resolve() == OUTPUT.resolve():
            continue
        digest = sha256(candidate.read_bytes())
        if digest in bases:
            found[bases[digest]['name']] = candidate
    for name in ('Mantella Dual Input 1.1', 'Mantella 0.14'):
        if name in found:
            return found[name]
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, help='Path to the Mantella.exe to patch (original Mantella 0.14 or Mantella Dual Input 1.1)')
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--write-contract', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    base = args.base or find_base()
    if base is None:
        raw = input('Full path to Mantella.exe (from the Mantella mod, or from Mantella Dual Input): ').strip().strip('"')
        if not raw:
            raise ValueError('No executable selected. Nothing changed.')
        base = Path(raw)
    report = build(base, args.output, write_contract=args.write_contract)
    print('Patched: ' + report['base_name'] + ' (' + str(base) + ')')
    print('Mantella - Anthropic API backend ready: ' + str(args.output))
    print('In MO2\'s left pane, place this mod AFTER (below) ' + report['base_name'].rsplit(' ', 1)[0] + ', so its Mantella.exe wins.')
    print('Your original files and settings were not changed.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError) as error:
        print('Setup stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
