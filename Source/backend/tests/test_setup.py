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

# test_long_cache_setting: "Anthropic: 1-Hour Cache" is a real setting in the LLM tab, off by default, read by the route
defs = 'src.config.mantella_config_value_definitions_new'
check('settings list replaced', defs in S.MODULES and (ROOT / 'Source/upstream' / S.MODULES[defs]).is_file())
registered = (BACKEND / S.MODULES[defs]).read_text(encoding='utf-8')
stock = (ROOT / 'Source/upstream' / S.MODULES[defs]).read_text(encoding='utf-8')
check('1-hour cache added to the LLM tab, after Claude Prompt Caching', 'get_claude_prompt_caching_config_value())\n        llm_category.add_config_value(LLMDefinitions.get_anthropic_long_cache_config_value())' in registered)
check('...then the 1-hour cache for the shared part', 'get_anthropic_long_cache_config_value())\n        llm_category.add_config_value(LLMDefinitions.get_anthropic_shared_long_cache_config_value())' in registered)
check('settings list: nothing else changed', registered.replace('        llm_category.add_config_value(LLMDefinitions.get_anthropic_long_cache_config_value())\n', '')
      .replace('        llm_category.add_config_value(LLMDefinitions.get_anthropic_shared_long_cache_config_value())\n', '') == stock)
llm_defs = ast.parse((BACKEND / 'src/config/definitions/llm_definitions.py').read_text(encoding='utf-8'))
getter = next(f for f in ast.walk(llm_defs) if isinstance(f, ast.FunctionDef) and f.name == 'get_anthropic_long_cache_config_value')
call = next(n for n in ast.walk(getter) if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'ConfigValueBool')
check('1-hour cache: bool, key anthropic_long_cache, off by default', call.args[0].value == 'anthropic_long_cache' and call.args[3].value is False, ast.dump(call)[:200])
check('route reads the setting', 'get_bool_value("anthropic_long_cache")' in (BACKEND / 'src/http/routes/mantella_route.py').read_text(encoding='utf-8'))
getter = next(f for f in ast.walk(llm_defs) if isinstance(f, ast.FunctionDef) and f.name == 'get_anthropic_shared_long_cache_config_value')
call = next(n for n in ast.walk(getter) if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'ConfigValueBool')
check('shared-part 1-hour cache: bool, key anthropic_shared_long_cache, on by default',
      call.args[0].value == 'anthropic_shared_long_cache' and call.args[3].value is True, ast.dump(call)[:200])
check('route reads that setting too', 'get_bool_value("anthropic_shared_long_cache")' in (BACKEND / 'src/http/routes/mantella_route.py').read_text(encoding='utf-8'))

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

# Vortex: setup run from a Vortex staging folder says to Deploy (an MO2 folder keeps the MO2 line); no stdin is no crash
check('setup knows a Vortex staging folder', S.under_vortex(Path('C:/Users/x/AppData/Roaming/Vortex/skyrimse/mods/Mantella - Direct AI Connect'))
      and not S.under_vortex(Path('C:/Games/MO2/mods/Mantella - Direct AI Connect')))
setup_src = (ROOT / 'setup_backend.py').read_text(encoding='utf-8')
check('setup tells a Vortex user to Deploy', 'press Deploy' in setup_src and 'INSTALL WITH VORTEX' in (ROOT / 'INSTALL.txt').read_text(encoding='utf-8'))
check('setup with no keyboard input stops with its own message', 'except EOFError' in setup_src)
notice = ' '.join((ROOT / 'SOURCE-AND-LICENSES.txt').read_text(encoding='utf-8').split())
check('the source notice names every changed module', all(rel in notice for rel in S.MODULES.values()),
      [rel for rel in S.MODULES.values() if rel not in notice])

# A13: a running Mantella.exe gives a clear message (not a raw [WinError 5]); the original exe is also found in
# Vortex's staging folder; an already patched exe given as the base is named as such.
import hashlib, os
with tempfile.TemporaryDirectory() as tmp:
    out = Path(tmp) / 'mod' / 'Mantella.exe'
    real_replace = Path.replace
    def busy(self, target):
        raise PermissionError(13, 'Access is denied', str(target), 5)
    Path.replace = busy
    try:
        S.install(b'MZ new build', out)
        check('running Mantella.exe: refused with advice', False, 'no error')
    except ValueError as e:
        check('running Mantella.exe: says to close Mantella and Skyrim, nothing changed',
              'in use' in str(e) and 'close Mantella' in str(e) and 'Nothing changed' in str(e), str(e))
    finally:
        Path.replace = real_replace
    check('...and no temporary file is left', not list(out.parent.glob('.mant-*')), list(out.parent.glob('*')))
    S.install(b'MZ new build', out)
    check('free to replace: written and checked', out.read_bytes() == b'MZ new build')

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    stock = b'MZ stock mantella 0.14 (test)'
    digest = hashlib.sha256(stock).hexdigest()
    contract = tmp / 'contract.json'
    contract.write_text(json.dumps({'bases': {digest: {'name': 'Mantella 0.14', 'patched_sha256': 'x'}}}), encoding='utf-8')
    vortex = tmp / 'appdata' / 'Vortex' / 'skyrimse' / 'mods' / 'Mantella-1234' / S.EXE
    vortex.parent.mkdir(parents=True)
    vortex.write_bytes(stock)
    dac = tmp / 'elsewhere' / 'Direct AI Connect'
    dac.mkdir(parents=True)
    old_contract, old_appdata = S.CONTRACT, os.environ.get('APPDATA')
    S.CONTRACT, os.environ['APPDATA'] = contract, str(tmp / 'appdata')
    try:
        check("no sibling copy: the original exe is found in Vortex's staging folder", S.find_base(dac) == vortex, S.find_base(dac))
        sibling = tmp / 'mods' / 'Mantella' / S.EXE
        sibling.parent.mkdir(parents=True)
        sibling.write_bytes(stock)
        (tmp / 'mods' / 'DAC').mkdir()
        check('a sibling mod folder (MO2) comes first', S.find_base(tmp / 'mods' / 'DAC') == sibling, S.find_base(tmp / 'mods' / 'DAC'))
    finally:
        S.CONTRACT = old_contract
        if old_appdata is None:
            os.environ.pop('APPDATA', None)
        else:
            os.environ['APPDATA'] = old_appdata

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    patched = b'MZ an exe this setup built (test)'
    (tmp / 'Source/backend').mkdir(parents=True)
    (tmp / 'Source/backend/build-contract.json').write_text(json.dumps({'bases': {'f' * 64: {
        'name': 'Mantella 0.14', 'patched_sha256': hashlib.sha256(patched).hexdigest()}}}), encoding='utf-8')
    (tmp / 'Mantella.exe').write_bytes(patched)
    try:
        S.build(tmp / 'Mantella.exe', tmp / 'out' / 'Mantella.exe', root=tmp)
        check('an already patched exe as the base: refused', False, 'no error')
    except ValueError as e:
        check('an already patched exe as the base: named as a Direct AI Connect build, not "Unsupported"',
              'already a Direct AI Connect build' in str(e) and 'Unsupported' not in str(e), str(e))
    check('...nothing written', not (tmp / 'out').exists())

print('RESULT:' + ('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'))
sys.exit(1 if FAILS else 0)
