"""Offline regression tests for single functions of the patched modules: re-summary files (summaries), the system
prompt count of the trim trigger (conversation._system_tokens) and local model addresses (utils.is_local_url). The
functions are taken out of the module source, so none of Mantella's imports are needed.
Run with a Python 3.11: test_regressions.py <repo Source/backend dir>"""
import ast, ipaddress, logging, os, sys, tempfile
from pathlib import Path
from urllib.parse import urlparse

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
log = logging.getLogger('regressions-test'); log.addHandler(Collect())

# DAC-1: a failed re-summary must not become the newest (and only read) summary file
sm = load('src/remember/summaries.py', {'_write_next_summary_file'}, {'os': os, 'logger': log})
write_next = sm['_write_next_summary_file']
with tempfile.TemporaryDirectory() as folder:
    latest = os.path.join(folder, 'Lydia_summary_3.txt')
    Path(latest).write_text('Old memories.', encoding='utf-8')
    for empty in ('', '   \n\n', None):
        warnings.clear()
        check(f'empty re-summary {empty!r}: no new file', write_next(latest, empty, 'Lydia') is None
              and sorted(os.listdir(folder)) == ['Lydia_summary_3.txt'], os.listdir(folder))
        check(f'empty re-summary {empty!r}: logged', any('kept the current summary file' in w for w in warnings), warnings)
    made = write_next(latest, 'Short version.\n\n', 'Lydia')
    check('re-summary written to the next number', made == os.path.join(folder, 'Lydia_summary_4.txt')
          and Path(made).read_text(encoding='utf-8') == 'Short version.\n\n', made)
    check('old file kept', Path(latest).read_text(encoding='utf-8') == 'Old memories.')
    under = os.path.join(folder, 'Jarl_Balgruuf_the_Greater_summary_9.txt')
    check('names with underscores still numbered', write_next(under, 'x', 'Jarl') == os.path.join(folder, 'Jarl_Balgruuf_the_Greater_summary_10.txt'))

# DAC-2: the system prompt is counted with the tokenizer (list path), not in characters (single Message path)
conv = load('src/conversation/conversation.py', {'_system_tokens'}, {})
class Msg:
    def __init__(self, text): self.text = text
class FakeClient:
    """Like ClientBase.get_count_tokens: a list is tokenized (here 1 token per 4 characters), one Message is len(text)."""
    def get_count_tokens(self, messages):
        if isinstance(messages, list):
            return sum(len(m.text) // 4 + 4 for m in messages) + 2
        return len(messages.text) + 4
system = Msg('x' * 40000)
counted = conv['_system_tokens'](FakeClient(), [system, Msg('Hello')])
check('system prompt counted in tokens', counted == 10006, counted)
check('empty thread: 0', conv['_system_tokens'](FakeClient(), []) == 0)
class Broken:
    def get_count_tokens(self, messages): raise RuntimeError('no tokenizer')
check('counting error: 0', conv['_system_tokens'](Broken(), [system]) == 0)

# DAC-5: local model servers are local; public hosts are not
ut = load('src/utils.py', {'is_local_url'}, {'urlparse': urlparse, 'ipaddress': ipaddress})
is_local = ut['is_local_url']
for url in ('http://localhost:1234/v1', 'http://LOCALHOST:11434/v1', 'http://127.0.0.1:5001/v1', 'http://[::1]:8080/v1',
            'http://192.168.1.20:5000/v1', 'http://10.0.0.5/v1', 'http://172.16.0.2/v1', 'http://172.31.255.1/v1',
            'http://169.254.1.1/v1', 'http://ollama.localhost:11434/v1'):
    check(f'local: {url}', is_local(url))
for url in ('https://openrouter.ai/api/v1', 'https://api.anthropic.com', 'http://172.217.0.1/v1', 'http://172.32.0.1/v1',
            'http://8.8.8.8/v1', 'https://localhost.evil.example/v1', 'https://10.evil.example/v1', 'not a url', ''):
    check(f'not local: {url!r}', not is_local(url))

def load_methods(rel: str, cls: str, names: set[str], extra: dict) -> type:
    """Builds a class of the same name holding only the named methods (so self.__x is mangled as in the module),
    without their decorators."""
    tree = ast.parse((BACKEND / rel).read_text(encoding='utf-8'))
    source = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    methods = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for m in methods:
        m.decorator_list = []
    node = ast.ClassDef(name=cls, bases=[], keywords=[], body=methods, decorator_list=[])
    space = dict(extra)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), rel, 'exec'), space)
    return space[cls]

class Registry:
    """src.extensions.registry(): every hook is installed; records the ctx each one gets."""
    def __init__(self): self.ctx = []
    def has(self, hook): return True
    def filter(self, hook, value, ctx): self.ctx.append((hook, ctx)); return value
    def collect(self, hook, ctx): self.ctx.append((hook, ctx)); return [True]
REG = Registry()
sys.modules['src'] = type(sys)('src'); sys.modules['src'].__path__ = []
sys.modules['src.extensions'] = type(sys)('src.extensions'); sys.modules['src.extensions'].registry = lambda: REG
sys.modules['src'].extensions = sys.modules['src.extensions']
from types import SimpleNamespace as NS

# T-5: the dialogue_events hook is told which events go on the talk's first message (stored menu dialogue) and which
# were added during the talk (a typed event shaped like dialogue must not be summarized into memory at once)
Conv = load_methods('src/conversation/conversation.py', 'Conversation', {'update_game_events', '__extension_dialogue_events'}, {'logger': log, 'UserMessage': object})
class Events:
    def __init__(self): self.pending = []
    def get_context_ingame_events(self): return list(self.pending)
    def clear_context_ingame_events(self): self.pending = []
class Message:
    def __init__(self): self.events = []
    def add_event(self, events): self.events += events
    def count_ingame_events(self): return len(self.events)
    def get_ingame_events_text(self): return '\n'.join(self.events)
talk = Conv.__new__(Conv)
ctx_events = Events()
ctx_events.config = NS(max_count_events=20)
ctx_events.npcs_in_conversation = NS(get_player_character=lambda: NS(name='Dovah'), get_non_player_characters=lambda: [NS(name='Lydia')])
ctx_events.world_id, ctx_events.game_days = 'w', 1.0
talk._Conversation__context = ctx_events
talk._Conversation__output_manager = NS(discarded_character_name=None)
talk._Conversation__is_player_interrupting = False
talk._Conversation__first_events_sent = False
talk._Conversation__rememberer = None
check('the first events start as not sent', 'self.__first_events_sent: bool = False' in (BACKEND / 'src/conversation/conversation.py').read_text(encoding='utf-8'))
firsts = []
for text in ('Dovah: Hello. Lydia: Greetings.', 'Dovah: I typed this. Lydia: Sure.', 'Later event.'):
    ctx_events.pending = [text]
    REG.ctx.clear()
    talk.update_game_events(Message())
    firsts.append(REG.ctx[0][1].get('is_first') if REG.ctx and REG.ctx[0][0] == 'dialogue_events' else 'missing')
check('dialogue_events: is_first only on the first message', firsts == [True, False, False], firsts)

# T-12: an NPC who leaves a talk that goes on is saved as a finished talk for them (departed), not as a reload
Summ = load_methods('src/remember/summaries.py', 'Summaries', {'save_conversation_state'}, {'src_extensions': sys.modules['src.extensions'], 'Dict': dict, 'List': list,
                    'message_thread': object, 'Character': object, 'Characters': object})
summ = Summ.__new__(Summ)
summ._Summaries__client = None
def hook_ctx(**kw):
    REG.ctx.clear()
    summ.save_conversation_state(None, [], None, 'w', **kw)
    return REG.ctx[0][1]
left = hook_ctx(is_reload=True, departed=True)
check('departure: not a reload for the extension, marked departed', left['is_reload'] is False and left['departed'] is True, left)
trim = hook_ctx(is_reload=True)
check('trim mid-talk: still a reload, not departed', trim['is_reload'] is True and trim['departed'] is False, trim)
end = hook_ctx(is_reload=False)
check('end of talk: neither', end['is_reload'] is False and end['departed'] is False, end)
conv_src = (BACKEND / 'src/conversation/conversation.py').read_text(encoding='utf-8')
check('departures pass departed to the save', 'departed=departed_npcs is not None' in conv_src)

print('RESULT: ' + ('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'))
sys.exit(1 if FAILS else 0)
