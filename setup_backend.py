"""One-time setup for Mantella - Direct AI Connect. Requires Python 3.11.9 or a newer 3.11.

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
    'src.config.mantella_config_value_definitions_new': 'src/config/mantella_config_value_definitions_new.py',
    'src.config.definitions.vision_definitions': 'src/config/definitions/vision_definitions.py',
    'src.config.definitions.action_definitions': 'src/config/definitions/action_definitions.py',
    'src.http.routes.mantella_route': 'src/http/routes/mantella_route.py',
    'src.remember.summaries': 'src/remember/summaries.py',
    'src.ui.start_ui': 'src/ui/start_ui.py',
    'src.tts.xtts': 'src/tts/xtts.py',
    'src.config.definitions.tts_definitions': 'src/config/definitions/tts_definitions.py',
    # Mantella Dual Input 1.1, bundled
    'src.conversation.conversation': 'src/conversation/conversation.py',
    'src.game_manager': 'src/game_manager.py',
    'src.http.communication_constants': 'src/http/communication_constants.py',
}
# Modules this patch adds (not in stock Mantella)
NEW_MODULES = {
    'src.llm.anthropic_native': 'src/llm/anthropic_native.py',
    'src.llm.reliability': 'src/llm/reliability.py',
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


ANTIVIRUS_HINT = ('This is usually antivirus (for example Windows Defender) removing or blocking the new file. '
                  'Allow Mantella.exe in this mod folder in your antivirus, then run Setup Backend.cmd again.')


def check_written(output, data):
    """Reads the written exe back: it must be there with the size and SHA-256 of the build."""
    import hashlib
    output = Path(output)
    try:
        written = output.read_bytes()
    except OSError:
        raise ValueError(f'Mantella.exe was written but is now missing or cannot be read: {output}. ' + ANTIVIRUS_HINT)
    if len(written) != len(data) or hashlib.sha256(written).digest() != hashlib.sha256(data).digest():
        raise ValueError(f'Mantella.exe on disk is not the file setup just wrote ({len(written):,} of '
                         f'{len(data):,} bytes, or different content): {output}. ' + ANTIVIRUS_HINT)


def build(base, output, root=ROOT, write_contract=False):
    if sys.version_info[:2] != (3, 11):
        raise ValueError('Use Python 3.11 to match the Mantella runtime.')
    # 3.11.8 and older compile some of the patched code differently, so the built exe would not match the contract
    if sys.version_info[:3] < (3, 11, 9):
        raise ValueError('Python %d.%d.%d is too old: install Python 3.11.9 or newer from python.org '
                         '(https://www.python.org/downloads/release/python-3119/) and run Setup Backend.cmd again. '
                         'Nothing changed.' % sys.version_info[:3])
    base, output, root = Path(base).resolve(), Path(output).resolve(), Path(root).resolve()
    if base == output or (output.exists() and base.samefile(output)):
        raise ValueError('Refusing to replace the input Mantella executable.')
    sys.path.insert(0, str(root / 'Source/backend'))
    from frozen_patch import compile_source, patch_executable, sha256, parse_carchive, parse_pyz, code_fingerprint

    contract = json.loads((root / 'Source/backend/build-contract.json').read_text(encoding='utf-8'))
    raw = base.read_bytes()
    base_hash = sha256(raw)
    if not write_contract and any(b.get('patched_sha256') == base_hash for b in contract['bases'].values()):
        raise ValueError(f'{base} is already a Direct AI Connect build, not the original Mantella.exe. Point to the '
                         'Mantella.exe inside the Mantella mod itself (with Vortex: in its staging folder, not in Data). '
                         'Nothing changed.')
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

    install(data, output)
    report['base_name'] = contract['bases'][base_hash]['name']
    return report


IN_USE_HINT = ('Mantella.exe is in use: close Mantella (its window or the game started through it) and Skyrim, then '
               'run Setup Backend.cmd again. Nothing changed.')


def install(data, output):
    """Writes the built exe through a temporary file next to it and checks it on disk."""
    import os, tempfile
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.mant-', suffix='.tmp', dir=output.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        Path(temporary).replace(output)
    except FileNotFoundError:
        raise ValueError(f'The new Mantella.exe disappeared while it was being written to {output}. ' + ANTIVIRUS_HINT)
    except PermissionError:
        # Windows refuses to replace a running exe ([WinError 5] access denied, or 32 in use by another process)
        raise ValueError(IN_USE_HINT)
    finally:
        Path(temporary).unlink(missing_ok=True)
    check_written(output, data)


EXE = 'SKSE/Plugins/MantellaSoftware/Mantella.exe'


def base_candidates(root=ROOT):
    """Where stock Mantella's Mantella.exe can be: the sibling mod folders (Mod Organizer 2, or Vortex when setup runs
    from its staging folder), then Vortex's default staging folders."""
    import os
    yield from Path(root).parent.glob('*/' + EXE)
    appdata = os.environ.get('APPDATA')
    if appdata:
        for game in ('skyrimse', 'skyrimvr'):
            yield from (Path(appdata) / 'Vortex' / game / 'mods').glob('*/' + EXE)


def find_base(root=ROOT):
    """Picks stock Mantella's Mantella.exe from the places base_candidates lists."""
    sys.path.insert(0, str(root / 'Source/backend'))
    from frozen_patch import sha256
    bases = json.loads(CONTRACT.read_text(encoding='utf-8'))['bases']
    found = {}
    for candidate in base_candidates(root):
        if candidate.resolve() == OUTPUT.resolve():
            continue
        try:
            digest = sha256(candidate.read_bytes())
        except OSError:
            continue  # a locked or unreadable copy is not the one to patch
        if digest in bases:
            found.setdefault(bases[digest]['name'], candidate)  # the first place listed wins
    return found.get('Mantella 0.14')


FLOWS_TOOL = Path('tools/Mantella Living World - OStim Expansion/flowgen.py')
FLOWS_TIMEOUT = 600  # seconds; a normal build takes well under a minute
FLOWS_NOT_ENABLED = 3  # flowgen's exit code when the mod it is in is not enabled in the profile it read


def find_flowgens(root=ROOT):
    """Every copy of Mantella Living World - OStim Expansion's flow builder: the one in this mod (All in One) first,
    then those in sibling mod folders."""
    root = Path(root)
    own = [root / FLOWS_TOOL] if (root / FLOWS_TOOL).is_file() else []
    return own + [p for p in sorted(root.parent.glob('*/' + FLOWS_TOOL.as_posix())) if not own or p.resolve() != own[0].resolve()]


def find_flowgen(root=ROOT):
    """The first copy of the flow builder (find_flowgens), or None."""
    return next(iter(find_flowgens(root)), None)


def build_flows(root=ROOT, timeout=FLOWS_TIMEOUT):
    """Builds the OStim flows when OStim Expansion is installed. Says nothing when it is not. A copy whose mod is not
    enabled in the Mod Organizer 2 profile the flow builder reads (it names that profile) is passed over for the next.
    Returns True (built), False (failed) or None (not installed or not enabled). Never stops the setup."""
    import subprocess
    copies = find_flowgens(root)
    if not copies:
        return None
    print()
    print('Building OStim flows...', flush=True)
    sys.stderr.flush()
    # a question from the flow builder (where Mod Organizer 2 is) can be answered in this window; with no
    # window to answer in, it gets no input and stops with its own message instead of waiting forever
    stdin = None if sys.stdin is not None and sys.stdin.isatty() else subprocess.DEVNULL
    for flowgen in copies:
        problem = None
        try:
            result = subprocess.run([sys.executable, str(flowgen), '--auto'], stdin=stdin, timeout=timeout)
            if result.returncode == FLOWS_NOT_ENABLED:
                continue
            if result.returncode != 0:
                problem = 'see the message above'
        except subprocess.TimeoutExpired:
            took = f'{timeout // 60} minutes' if timeout >= 60 else f'{timeout} seconds'
            problem = f'it took longer than {took} and was stopped'
        except OSError as error:
            problem = str(error)
        if problem:
            print(f'Warning: the OStim flows were not built ({problem}). The backend is built and ready. '
                  f'Run Build Flows.cmd in {flowgen.parent} later.', flush=True)
            return False
        return True
    print('The OStim flows were not built: OStim Expansion is not enabled in the profile named above. The backend is '
          'built and ready.', flush=True)
    return None


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
    build_flows()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError) as error:
        print('Setup stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
