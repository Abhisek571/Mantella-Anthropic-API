"""Offline tests for setup_backend.py and the bundled Dual Input modules.
Run with a Python 3.11: test_setup.py <repo Source/backend dir>"""
import ast, json, sys, tempfile
from pathlib import Path

BACKEND = Path(sys.argv[1])
ROOT = BACKEND.parent.parent
sys.path.insert(0, str(ROOT))
import setup_backend as S

FAILS = []
def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (('  -> ' + str(detail)) if not cond else ''))
    if not cond:
        FAILS.append(name)


# test_dual_input_bundled: Dual Input's backend changes are part of this patch
for name in ('src.conversation.conversation', 'src.game_manager', 'src.http.communication_constants'):
    check(f'{name} replaced', name in S.MODULES and (BACKEND / S.MODULES[name]).is_file() and (ROOT / 'Source/upstream' / S.MODULES[name]).is_file())
check('dual_input module added', 'src.dual_input' in S.NEW_MODULES and (BACKEND / S.NEW_MODULES['src.dual_input']).is_file())

# test_only_stock_base: the patch builds from stock Mantella 0.14 only (Dual Input is now inside it)
contract = json.loads((BACKEND / 'build-contract.json').read_text(encoding='utf-8'))
check('only the stock base', [b['name'] for b in contract['bases'].values()] == ['Mantella 0.14'], [b['name'] for b in contract['bases'].values()])
with tempfile.TemporaryDirectory() as tmp:
    fake = Path(tmp) / 'Mantella.exe'
    fake.write_bytes(b'not mantella')
    try:
        S.build(fake, Path(tmp) / 'out' / 'Mantella.exe')
        check('unsupported exe refused', False, 'no error')
    except ValueError as e:
        check('unsupported exe refused', 'Unsupported Mantella executable' in str(e) and 'Dual Input' not in str(e).split('Supported:')[-1], str(e))
    check('nothing written', not (Path(tmp) / 'out').exists())

# test_written_exe_checked (DACN-07): "backend ready" only when the exe on disk is the one just built; antivirus
# removing or cutting it is named as the likely cause.
with tempfile.TemporaryDirectory() as tmp:
    exe, data = Path(tmp) / 'Mantella.exe', b'MZ patched build'
    exe.write_bytes(data)
    checker = getattr(S, 'check_written', None)
    def written_error(path, expected):
        try:
            checker(path, expected)
            return None
        except ValueError as e:
            return str(e)
    check('check_written exists', checker is not None)
    if checker:
        check('intact exe passes', written_error(exe, data) is None, written_error(exe, data))
        exe.write_bytes(data[:4])
        error = written_error(exe, data)
        check('cut exe refused, antivirus named', error is not None and 'antivirus' in error.lower(), error)
        exe.unlink()
        error = written_error(exe, data)
        check('missing exe refused, antivirus named', error is not None and 'antivirus' in error.lower(), error)
    check('build checks the written exe', 'check_written(output, data)' in (ROOT / 'setup_backend.py').read_text(encoding='utf-8'))


# test_goodbye_name_ends_one_on_one: "goodbye <name>" removes that NPC only when others stay in the talk;
# with one NPC it ends the talk (removing the last NPC left the game waiting forever).
tree = ast.parse((BACKEND / 'src/conversation/conversation.py').read_text(encoding='utf-8'))
fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'goodbye_kind'), None)
check('goodbye_kind exists', fn is not None)
if fn:
    scope = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'conversation.py', 'exec'), scope)
    goodbye_kind = scope['goodbye_kind']
    check('name, one NPC: end', goodbye_kind(dismisses_npc=True, says_goodbye=True, multiple_npcs=False) == 'end')
    check('name, group: remove that NPC', goodbye_kind(dismisses_npc=True, says_goodbye=True, multiple_npcs=True) == 'eject')
    check('plain goodbye: end', goodbye_kind(dismisses_npc=False, says_goodbye=True, multiple_npcs=True) == 'end')
    check('no goodbye: continue', goodbye_kind(dismisses_npc=False, says_goodbye=False, multiple_npcs=False) == 'continue')
    source = (BACKEND / 'src/conversation/conversation.py').read_text(encoding='utf-8')
    check('player_input uses it', 'goodbye_kind(' in source.split('def goodbye_kind', 1)[1])

# test_trim_after_tokens: an extension's 'conversation_limit' (tokens) turns into the share of the model's context
# at which Mantella saves the memory and trims the talk, and the share of recent lines kept (a quarter of the limit)
fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'trim_limits'), None)
check('trim_limits exists', fn is not None)
if fn:
    scope = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'conversation.py', 'exec'), scope)
    trim_limits = scope['trim_limits']
    check('8,000 of 1M: trigger 0.008, keep 0.002', trim_limits(8000, 1_000_000) == (0.008, 0.002), trim_limits(8000, 1_000_000))
    check('off (0 or None): stock behaviour', trim_limits(0, 1_000_000) is None and trim_limits(None, 1_000_000) is None)
    check('never later than stock 90%', trim_limits(950_000, 1_000_000) is None)
    check('bad values ignored', trim_limits('lots', 1_000_000) is None and trim_limits(8000, 0) is None)
    # the limit is for the talk: a 12,000-token system prompt alone must not trigger a reload every reply
    check('system prompt not counted against the limit', trim_limits(8000, 1_000_000, 12_000) == (0.02, 0.002), trim_limits(8000, 1_000_000, 12_000))
    check('tiny limits raised to 4,000', trim_limits(100, 1_000_000) == (0.004, 0.001), trim_limits(100, 1_000_000))
    source = (BACKEND / 'src/conversation/conversation.py').read_text(encoding='utf-8')
    check('continue_conversation uses it', source.count('trim_limits(') >= 3, source.count('trim_limits('))
# test_small_prices_readable: cheap models no longer all show as $0.07 or $0.00
utree = ast.parse((BACKEND / 'src/utils.py').read_text(encoding='utf-8'))
fp = next(n for n in utree.body if isinstance(n, ast.FunctionDef) and n.name == 'format_price')
scope = {}
exec(compile(ast.Module(body=[fp], type_ignores=[]), 'utils.py', 'exec'), scope)
format_price = scope['format_price']
check('prices: 0.065', format_price(0.065) == '$0.065', format_price(0.065))
check('prices: 0.003', format_price(0.003) == '$0.003', format_price(0.003))
check('prices: 0.0001', format_price(0.0001) == '$0.0001', format_price(0.0001))
check('prices: 2.5 and 10', format_price(2.5) == '$2.50' and format_price(10.0) == '$10', (format_price(2.5), format_price(10.0)))
check('prices: free and unknown', format_price(0.0) == 'free' and format_price(-1.0) == 'unknown')
cb = (BACKEND / 'src/llm/client_base.py').read_text(encoding='utf-8')
check('model list shows the cache price', 'input_cache_read' in cb and 'Cache read:' in cb)
ext_source = (BACKEND / 'src/extensions.py').read_text(encoding='utf-8')
check("'conversation_limit' is a known hook", "'conversation_limit'" in ext_source.split('HOOKS =', 1)[1].split('\n', 1)[0])

print('RESULT: ' + ('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'))
sys.exit(1 if FAILS else 0)
