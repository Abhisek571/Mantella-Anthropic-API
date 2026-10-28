"""One-time setup for Mantella - Direct AI Connect. Requires Python 3.11.

Builds a patched copy of Mantella.exe (Mantella 0.14) inside this mod. Dual Input (type or speak in the same
conversation) is included; the separate Mantella Dual Input mod is not needed.
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
    'src.http.routes.mantella_route': 'src/http/routes/mantella_route.py',
    'src.remember.summaries': 'src/remember/summaries.py',
    'src.ui.start_ui': 'src/ui/start_ui.py',
    # Mantella Dual Input 1.1, bundled
    'src.conversation.conversation': 'src/conversation/conversation.py',
    'src.game_manager': 'src/game_manager.py',
    'src.http.communication_constants': 'src/http/communication_constants.py',
}
# Modules this patch adds (not in stock Mantella)
NEW_MODULES = {
    'src.llm.anthropic_native': 'src/llm/anthropic_native.py',
    'src.extensions': 'src/extensions.py',
    'src.dual_input': 'src/dual_input.py',
}
# Modules whose stock code compiles to different jump offsets on other 3.11
# patch releases (match statements). Gated on everything but jump distances.
JUMP_TOLERANT = {'src.http.routes.mantella_route'}
# Third-party packages added to the executable (pure Python; their other dependencies already ship with Mantella)
VENDOR = 'Source/backend/vendor'
VENDOR_PACKAGES = ('anthropic', 'docstring_parser')


def vendor_modules(root):
    """Compiles the vendored packages. Returns ({module name: (PYZ kind, code)}, SHA-256 over every source file)."""
    import hashlib
    from frozen_patch import compile_source, PYZ_ITEM_MODULE, PYZ_ITEM_PKG
    base = root / VENDOR
    digest = hashlib.sha256()
    modules = {}
    for package in VENDOR_PACKAGES:
        for path in sorted((base / package).rglob('*.py')):
            relative = path.relative_to(base).as_posix()
            source = path.read_bytes()
            digest.update(relative.encode('utf-8') + b'\0' + source + b'\0')
            parts = relative[:-3].split('/')
            if parts[-1] == '__init__':
                name, kind = '.'.join(parts[:-1]), PYZ_ITEM_PKG
            else:
                name, kind = '.'.join(parts), PYZ_ITEM_MODULE
            modules[name] = (kind, compile_source(source, relative))
        # folders without __init__.py (implicit namespace packages, e.g. anthropic/_decoders) still need a
        # package entry, or the frozen importer cannot find the modules inside them
        for folder in sorted(p for p in (base / package).rglob('*') if p.is_dir()):
            relative = folder.relative_to(base).as_posix()
            name = relative.replace('/', '.')
            if name not in modules and any(folder.glob('*.py')):
                digest.update(('namespace:' + relative).encode('utf-8') + b'\0')
                modules[name] = (PYZ_ITEM_PKG, compile_source(b'', relative + '/__init__.py'))
    return modules, digest.hexdigest()


def same_code_ignoring_jumps(left, right):
    """True when two code objects match instruction by instruction, ignoring jump targets."""
    import dis, types
    pairs = [(left, right)]
    while pairs:
        a, b = pairs.pop()
        if (a.co_name, a.co_names, a.co_varnames, a.co_freevars, a.co_cellvars, a.co_argcount, a.co_flags) != \
           (b.co_name, b.co_names, b.co_varnames, b.co_freevars, b.co_cellvars, b.co_argcount, b.co_flags):
            return False
        ia, ib = list(dis.get_instructions(a)), list(dis.get_instructions(b))
        if len(ia) != len(ib):
            return False
        for x, y in zip(ia, ib):
            if x.opname != y.opname:
                return False
            if x.opcode in dis.hasjrel or x.opcode in dis.hasjabs:
                continue
            # nested code objects are compared below; their repr carries a memory address
            if isinstance(x.argval, types.CodeType) and isinstance(y.argval, types.CodeType):
                continue
            if x.argrepr != y.argrepr:
                return False
        ca = [c for c in a.co_consts if isinstance(c, types.CodeType)]
        cb = [c for c in b.co_consts if isinstance(c, types.CodeType)]
        if len(ca) != len(cb) or [c for c in a.co_consts if not isinstance(c, types.CodeType)] != [c for c in b.co_consts if not isinstance(c, types.CodeType)]:
            return False
        pairs.extend(zip(ca, cb))
    return True


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
        raise ValueError('Unsupported Mantella executable. Supported: Mantella 0.14 (the Mantella.exe from the Mantella mod itself). Nothing changed.')

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
        upstream_code = compile_source(upstream, relative)
        expected = code_fingerprint(upstream_code)
        if code_fingerprint(pyz.code(name)) != expected:
            if name not in JUMP_TOLERANT or not same_code_ignoring_jumps(pyz.code(name), upstream_code):
                raise ValueError(f'{name} in the input executable is not stock Mantella v0.14. Nothing changed.')
            expected = code_fingerprint(pyz.code(name))
        upstream_gates[name] = expected

    additions, vendor_hash = vendor_modules(root)
    if not write_contract and contract.get('vendor') != vendor_hash:
        raise ValueError('Modified vendored package files in ' + VENDOR)
    from frozen_patch import PYZ_ITEM_MODULE
    for name, relative in NEW_MODULES.items():
        source = (root / 'Source/backend' / relative).read_bytes()
        module_hashes[name] = sha256(source)
        if not write_contract and contract['modules'].get(name) != module_hashes[name]:
            raise ValueError('Modified source file: ' + relative)
        additions[name] = (PYZ_ITEM_MODULE, compile_source(source, relative))

    data, report = patch_executable(raw, replacements=replacements, expected_exe_sha256=base_hash,
                                    expected_module_fingerprints=upstream_gates, additions=additions)
    if write_contract:
        contract['modules'] = module_hashes
        contract['vendor'] = vendor_hash
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
    """Picks stock Mantella's Mantella.exe from the sibling MO2 mod folders."""
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
    return found.get('Mantella 0.14')


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, help='Path to the Mantella.exe to patch (original Mantella 0.14)')
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--write-contract', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    base = args.base or find_base()
    if base is None:
        raw = input('Full path to Mantella.exe (from the Mantella mod): ').strip().strip('"')
        if not raw:
            raise ValueError('No executable selected. Nothing changed.')
        base = Path(raw)
    report = build(base, args.output, write_contract=args.write_contract)
    print('Patched: ' + report['base_name'] + ' (' + str(base) + ')')
    print('Mantella - Direct AI Connect backend ready: ' + str(args.output))
    print('In MO2\'s left pane, place this mod AFTER (below) Mantella, so its Mantella.exe and scripts win.')
    print('Dual Input is included: turn off the separate Mantella Dual Input mod if you have it.')
    print('Your original files and settings were not changed.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError) as error:
        print('Setup stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
