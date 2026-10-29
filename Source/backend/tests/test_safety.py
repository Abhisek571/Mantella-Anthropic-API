"""Offline tests for the safety checks in the patched modules: tool calls limited to the offered actions
(client_base.only_offered), the local-only server guard (start_ui.is_local_request) and safe summary folder names
(summaries._safe_part). The functions are taken out of the module source, so none of Mantella's imports are needed.
Run with a Python 3.11: test_safety.py <repo Source/backend dir>"""
import ast, logging, re, sys
from pathlib import Path

BACKEND = Path(sys.argv[1])

FAILS = []
def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (('  -> ' + str(detail)) if not cond else ''))
    if not cond:
        FAILS.append(name)


def load(rel: str, names: set[str], extra: dict) -> dict:
    """Runs only the named top-level functions and assignments of a module."""
    tree = ast.parse((BACKEND / rel).read_text(encoding='utf-8'))
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in names)
            or (isinstance(n, ast.Assign) and any(getattr(t, 'id', None) in names for t in n.targets))]
    space = dict(extra)
    exec(compile(ast.Module(body=keep, type_ignores=[]), rel, 'exec'), space)
    return space


warnings = []
class Collect(logging.Handler):
    def emit(self, record):
        warnings.append(record.getMessage())
log = logging.getLogger('safety-test'); log.addHandler(Collect())

# offered actions only
cb = load('src/llm/client_base.py', {'_tool_name', 'only_offered'}, {'logger': log})
offered = [{'type': 'function', 'function': {'name': 'Wave'}}, {'type': 'function', 'function': {'name': 'Follow'}}]
calls = [{'id': '1', 'function': {'name': 'Wave', 'arguments': '{}'}}, {'id': '2', 'function': {'name': 'SexWithPlayer', 'arguments': '{}'}}]
kept = cb['only_offered'](calls, offered)
check('offered call kept, hidden one dropped', [c['id'] for c in kept] == ['1'], kept)
check('dropped call is logged', any('SexWithPlayer' in w for w in warnings), warnings)
check('no tools offered: every call dropped', cb['only_offered'](calls, None) == [])
class Fn:
    def __init__(self, name): self.name = name
class Call:
    def __init__(self, name): self.function = Fn(name)
check('SDK objects work too', len(cb['only_offered']([Call('Follow'), Call('Undress')], offered)) == 1)

# local-only server
ui = load('src/ui/start_ui.py', {'LOCAL_HOSTS', '_host_name', 'is_local_request'}, {})
local = ui['is_local_request']
check('game request (no Origin) allowed', local('127.0.0.1:4999', None) and local('localhost:4999', None) and local(None, None))
check('settings page on localhost allowed', local('localhost:4999', 'http://localhost:4999'))
check('IPv6 loopback allowed', local('[::1]:4999', 'http://[::1]:4999'))
check('another website refused', not local('localhost:4999', 'https://evil.example'))
check('sandboxed page (Origin null) refused', not local('localhost:4999', 'null'))
check('DNS rebinding (foreign Host) refused', not local('attacker.example:4999', None))
check('lookalike host refused', not local('localhost.evil.example:4999', None) and not local('localhost:4999', 'http://127.0.0.1.evil.example'))

# safe summary folder names
sm = load('src/remember/summaries.py', {'_UNSAFE_PATH_CHARS', '_safe_part'}, {'re': re})
safe = sm['_safe_part']
check('ordinary names unchanged', safe('Lydia') == 'Lydia' and safe('Whiterun Guard') == 'Whiterun Guard' and safe('CHAD1') == 'CHAD1')
for odd in ('../../x', '..\\..\\x', 'a/b', 'Jarl: Balgruuf', 'Who?', '', '. '):
    out = safe(odd)
    check(f'odd name made safe: {odd!r} -> {out!r}', out and not re.search(r'[<>:"/\\|?*]', out) and '..' not in out and not out.endswith(('.', ' ')))

print('RESULT: ' + ('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'))
sys.exit(1 if FAILS else 0)
