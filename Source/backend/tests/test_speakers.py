"""Offline tests: the speakers' base ids handed to extensions (Registry.speaking_ids) and the reply after an action
result keeping its tools for Anthropic (Native). The helpers are taken out of conversation.py and game_manager.py, so
none of Mantella's imports are needed.
Run with a Python 3.11: test_speakers.py <repo Source/backend dir>"""
import ast, importlib.util, logging, sys, types
from pathlib import Path
from types import SimpleNamespace as NS

BACKEND = Path(sys.argv[1])

FAILS = []
def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (('  -> ' + str(detail)) if not cond else ''))
    if not cond:
        FAILS.append(name)

log = logging.getLogger('speakers-test')
src = types.ModuleType('src'); src.__path__ = []
utils = types.ModuleType('src.utils'); utils.get_logger = lambda: log
sys.modules.update({'src': src, 'src.utils': utils})
spec = importlib.util.spec_from_file_location('src.extensions', BACKEND / 'src/extensions.py')
ext = importlib.util.module_from_spec(spec); spec.loader.exec_module(ext)
sys.modules['src.extensions'] = ext; src.extensions = ext

# ---------- Registry default: older extensions see an empty list, never a missing attribute ----------
check('Registry.speaking_ids defaults to []', ext.Registry().speaking_ids == [])
check('each Registry has its own list', ext.Registry().speaking_ids is not ext.Registry().speaking_ids)

# ---------- helpers from conversation.py ----------
rel = 'src/conversation/conversation.py'
source = (BACKEND / rel).read_text(encoding='utf-8')
tree = ast.parse(source)
helper = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_speaker_base_id']
conversation_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Conversation')
note = [n for n in conversation_class.body if isinstance(n, ast.FunctionDef) and n.name == '__note_speaking_npcs']
check('helpers present', len(helper) == 1 and len(note) == 1)
space = {'logger': log}
exec(compile(ast.Module(body=helper, type_ignores=[]), rel, 'exec'), space)
speaker_base_id = space['_speaker_base_id']

# the same text game_manager.load_character makes from the game's FormID int (stock convert_to_skyrim_hex_format)
def skyrim_hex(form_id: int) -> str:
    if form_id < 0:
        form_id += 2**32
    return f'{form_id:x}'.upper().rjust(8, '0')
# the Hub's game-side name for npc/<id>.json: MLW_Channel.BaseId = StringUtil.Substring(Hex(id), 2)
def hub_name(form_id: int) -> str:
    return skyrim_hex(form_id)[2:]

gm_source = (BACKEND / 'src/game_manager.py').read_text(encoding='utf-8')
check('game_manager keeps the Hub-style id before masking', 'hub_base_id: str = base_id[-6:]' in gm_source
      and gm_source.index('hub_base_id: str = base_id[-6:]') < gm_source.index("if base_id.startswith('FE'):"))
check('game_manager puts it on the Character', 'character.hub_base_id = hub_base_id' in gm_source)

lydia = 0x000A2C94                       # Skyrim.esm
mod_npc = 0x2B013489                     # a full plugin at load-order slot 2B
light = -0x01EDC544                      # 0xFE123ABC as Papyrus's signed int: light plugin FE123, record ABC
for label, form_id in (('Skyrim.esm', lydia), ('full plugin', mod_npc), ('light plugin', light)):
    full = skyrim_hex(form_id)
    stock = full[-3:].rjust(6, '0') if full.startswith('FE') else full[-6:]   # Mantella's own base_id
    npc = NS(hub_base_id=full[-6:], base_id=stock)
    check(f'{label}: id matches the Hub file name', speaker_base_id(npc) == hub_name(form_id), (speaker_base_id(npc), hub_name(form_id)))
check('light plugin keeps its FExxx digits (not Mantella\'s 000ABC)', speaker_base_id(NS(hub_base_id='123ABC', base_id='000ABC')) == '123ABC')
check('example from the brief', speaker_base_id(NS(hub_base_id='013489', base_id='013489')) == '013489')
check('fallback to base_id when hub_base_id is missing', speaker_base_id(NS(base_id='a2c94')) == '0A2C94')
check('0x prefix and lower case normalised', speaker_base_id(NS(hub_base_id='0x0a2c94')) == '0A2C94')
check('longer id: last 6 digits', speaker_base_id(NS(hub_base_id='2B013489')) == '013489')
check('no usable id: empty', speaker_base_id(NS(base_id='')) == '' and speaker_base_id(NS()) == '' and speaker_base_id(NS(base_id='zz')) == '')

# ---------- __note_speaking_npcs fills both fields ----------
ns = {'logger': log, '_speaker_base_id': speaker_base_id}
exec(compile(ast.Module(body=note, type_ignores=[]), rel, 'exec'), ns)
note_fn = ns['__note_speaking_npcs']
ext._registry = ext.Registry()
ext.registry = lambda: ext._registry
npcs = [NS(name='Lydia', hub_base_id='0A2C94', base_id='0A2C94'), NS(name='Ysolda', hub_base_id='', base_id='')]
def holder(context):  # the method was compiled outside its class, so self.__context is not name-mangled
    return NS(**{'__context': context})
fake_self = holder(NS(npcs_in_conversation=NS(get_non_player_characters=lambda: npcs)))
note_fn(fake_self)
check('speaking names kept for the usage log', ext._registry.speaking == 'Lydia, Ysolda', ext._registry.speaking)
check('speaking_ids set, NPCs without an id left out', ext._registry.speaking_ids == ['0A2C94'], ext._registry.speaking_ids)
broken = holder(NS(npcs_in_conversation=NS(get_non_player_characters=lambda: 1 / 0)))
note_fn(broken)
check('a failure does not raise and leaves the last values', ext._registry.speaking_ids == ['0A2C94'])

# ---------- the reply after an action result ----------
start = source[source.index('def __start_generating_npc_sentences'):source.index('def __offered_keyword_actions')]
check('speakers noted before the generation thread starts', start.index('self.__note_speaking_npcs()') < start.index('Thread(target=thread_target)'))
check('tools still made when actions may not be called', 'if self.context.config.advanced_actions_enabled:' in start
      and 'advanced_actions_enabled and allow_tool_use' not in start)
check('wrapped as NoToolUse when actions may not be called', 'if not allow_tool_use and tools:' in start and 'tools = NoToolUse(tools)' in start)
resume = source[source.index('def resume_after_interrupting_action'):source.index('def retrieve_sentence_from_queue')]
check('action-result reply still asks for no tool use', 'self.__start_generating_npc_sentences(allow_tool_use=False)' in resume)
check('keyword actions passed the same way on every reply', 'keyword_actions = self.__offered_keyword_actions(self.context.config.actions)' in start
      and 'allow_tool_use' not in start[start.index('keyword_actions ='):start.index('def thread_target')])

print('RESULT: ' + ('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'))
sys.exit(1 if FAILS else 0)
