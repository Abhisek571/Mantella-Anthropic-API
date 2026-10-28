"""Offline tests for src/extensions.py (loose Python packages in extensions/ that hook into requests and the UI).
Run with a Python 3.11: test_extensions.py <repo Source/backend dir>"""
import importlib.util, logging, sys, tempfile, types
from pathlib import Path

BACKEND = Path(sys.argv[1])

# stub src.utils (the real one imports half of Mantella)
records = []
class Collect(logging.Handler):
    def emit(self, record):
        records.append(record)
log = logging.getLogger('mantella'); log.addHandler(Collect()); log.setLevel(logging.DEBUG)
src = types.ModuleType('src'); src.__path__ = []
utils = types.ModuleType('src.utils'); utils.get_logger = lambda: log
sys.modules.update({'src': src, 'src.utils': utils})
spec = importlib.util.spec_from_file_location('src.extensions', BACKEND / 'src/extensions.py')
ext = importlib.util.module_from_spec(spec); spec.loader.exec_module(ext)

FAILS = []
def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (('  -> ' + str(detail)) if not cond else ''))
    if not cond:
        FAILS.append(name)

def make(root: Path, name: str, code: str):
    (root / name).mkdir(parents=True)
    (root / name / '__init__.py').write_text(code, encoding='utf-8')

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / 'extensions'
    make(root, 'alpha', "def register(api):\n"
                        "    api.on('extra_user_text', lambda ctx: 'alpha note')\n"
                        "    api.on('filter_tools', lambda tools, ctx: tools + ['alpha'])\n")
    make(root, 'broken', "def register(api)\n    pass\n")                      # syntax error
    make(root, 'noregister', "VALUE = 1\n")
    make(root, 'raises', "def register(api):\n"
                         "    api.on('filter_tools', lambda tools, ctx: 1 / 0)\n")
    make(root, 'zulu', "def register(api):\n"
                       "    api.on('filter_tools', lambda tools, ctx: tools + ['zulu'])\n"
                       "    api.on('memory_text', lambda text, ctx: None)\n")
    (root / 'loose_file.py').write_text("raise SystemExit('never imported')\n", encoding='utf-8')

    records.clear()
    reg = ext.load_all(str(root))
    # test_loads_package_with_register
    check('loads packages with register()', reg.names == ['alpha', 'raises', 'zulu'], reg.names)
    check('extra_user_text collected', reg.collect('extra_user_text', {}) == ['alpha note'], reg.collect('extra_user_text', {}))
    # test_broken_extension_skipped_and_logged
    warned = ' '.join(r.getMessage() for r in records if r.levelno >= logging.WARNING)
    check('broken extension skipped and logged', 'broken' in warned and 'broken' not in reg.names, warned)
    check('package without register() skipped', 'noregister' not in reg.names)
    # test_hooks_run_in_load_order (and a raising hook leaves the value as it was)
    records.clear()
    check('hooks run in load order', reg.filter('filter_tools', ['base'], {}) == ['base', 'alpha', 'zulu'], reg.filter('filter_tools', ['base'], {}))
    check('raising hook logged', any('raises' in r.getMessage() for r in records if r.levelno >= logging.WARNING))
    check('None from a filter keeps the value', reg.filter('memory_text', 'stock memory', {}) == 'stock memory')
    check('has() for registered hook', reg.has('filter_tools') and not reg.has('ui_tab'))
    # usage log, capped
    for i in range(ext.USAGE_LOG_SIZE + 5):
        reg.record_usage({'cache_read': i})
    log_entries = reg.api_for('alpha').usage_log()
    check('usage log capped', len(log_entries) == ext.USAGE_LOG_SIZE and log_entries[-1]['cache_read'] == ext.USAGE_LOG_SIZE + 4, len(log_entries))

    # test_no_extensions_is_stock_behaviour
    empty = ext.load_all(str(Path(tmp) / 'missing'))
    tools = [{'type': 'function'}]
    check('no folder: nothing loaded', empty.names == [])
    check('no folder: filter returns the same object', empty.filter('filter_tools', tools, {}) is tools)
    check('no folder: nothing collected', empty.collect('extra_user_text', {}) == [])
    check('no folder: no hooks', not any(empty.has(h) for h in ext.HOOKS))

# a package split into modules imports its own parts relatively
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / 'extensions'
    make(root, 'multi', "from . import helper\ndef register(api):\n    api.on('extra_user_text', lambda ctx: helper.NOTE)\n")
    (root / 'multi' / 'helper.py').write_text("NOTE = 'from a sub-module'\n", encoding='utf-8')
    records.clear()
    reg = ext.load_all(str(root))
    check('relative imports inside a package work', reg.names == ['multi'] and reg.collect('extra_user_text', {}) == ['from a sub-module'],
          (reg.names, [r.getMessage() for r in records if r.levelno >= logging.WARNING]))

# unknown hook names raise for the extension author (caught at register time and logged)
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / 'extensions'
    make(root, 'typo', "def register(api):\n    api.on('filtertools', lambda t, c: t)\n")
    records.clear()
    reg = ext.load_all(str(root))
    check('unknown hook name: extension skipped and logged', reg.names == [] and any('filtertools' in r.getMessage() for r in records), reg.names)

# the shared registry loads lazily, once
calls = []
orig = ext.load_all
ext.load_all = lambda root: (calls.append(root), orig(root))[1]
ext._registry = None
first = ext.registry(); second = ext.registry()
ext.load_all = orig
check('registry() loads once', first is second and calls == [ext.EXTENSIONS_DIR], calls)

print('RESULT: ' + ('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'))
sys.exit(1 if FAILS else 0)
