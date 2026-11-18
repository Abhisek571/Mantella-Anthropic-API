"""Offline tests for src/llm/anthropic_native.py against a fake Messages API server.
Run with the native-venv Python: test_native.py <repo Source/backend dir>"""
import asyncio, importlib.util, json, sys, threading, time, types, logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND = Path(sys.argv[1])
sys.path.insert(0, str(BACKEND / 'vendor'))  # the vendored anthropic SDK

# stub src.utils (the real one imports half of Mantella)
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s %(message)s')
src = types.ModuleType('src'); src.__path__ = []
src_llm = types.ModuleType('src.llm'); src_llm.__path__ = []
utils = types.ModuleType('src.utils'); utils.get_logger = lambda: logging.getLogger('mantella')
sys.modules.update({'src': src, 'src.llm': src_llm, 'src.utils': utils})
spec = importlib.util.spec_from_file_location('src.llm.reliability', BACKEND / 'src/llm/reliability.py')
src_llm.reliability = importlib.util.module_from_spec(spec); spec.loader.exec_module(src_llm.reliability)
sys.modules['src.llm.reliability'] = src_llm.reliability
spec = importlib.util.spec_from_file_location('src.llm.anthropic_native', BACKEND / 'src/llm/anthropic_native.py')
native = importlib.util.module_from_spec(spec); spec.loader.exec_module(native)
import anthropic
assert anthropic.__version__ == '0.125.0', anthropic.__version__

FAILS = []
def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (('  -> ' + str(detail)) if not cond else ''))
    if not cond:
        FAILS.append(name)

# ---------- conversion ----------
thinking_store = {'call_1': [{'type': 'thinking', 'thinking': 'note', 'signature': 'sig'}]}
msgs = [
    {'role': 'system', 'content': 'You are Mjoll.'},
    {'role': 'user', 'content': 'Hello'},
    {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {'name': 'FollowMe', 'arguments': '{"x": 1}'}}]},
    {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'done'},
    {'role': 'user', 'content': [{'type': 'text', 'text': '(Mjoll follows.) Come on.'},
                                 {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,QUJD', 'detail': 'low'}}]},
    {'role': 'assistant', 'content': ''},
]
system, conv = native.convert_messages(msgs, thinking_store)
check('system joined', system == [{'type': 'text', 'text': 'You are Mjoll.'}], system)
check('roles alternate', [m['role'] for m in conv] == ['user', 'assistant', 'user'], [m['role'] for m in conv])
a = conv[1]['content']
check('thinking put back before tool_use', [b['type'] for b in a] == ['thinking', 'tool_use'], a)
check('tool input parsed', a[1]['input'] == {'x': 1} and a[1]['id'] == 'call_1')
u = conv[2]['content']
check('tool_result first in user turn', [b['type'] for b in u] == ['tool_result', 'text', 'image'], u)
check('image converted', u[2]['source'] == {'type': 'base64', 'media_type': 'image/jpeg', 'data': 'QUJD'}, u[2])
check('empty assistant dropped', conv[-1]['role'] == 'user')
# test_only_latest_tool_turn_replays_thinking (DACN-01): a replayed thinking block is rejected once anything before
# it changed, and only the tool-use turn being continued needs its thinking, so earlier turns carry none.
think = lambda n: [{'type': 'thinking', 'thinking': 'plan ' + n, 'signature': 'sig' + n}]
call = lambda i: {'role': 'assistant', 'content': None, 'tool_calls': [{'id': i, 'type': 'function', 'function': {'name': 'FollowMe', 'arguments': '{}'}}]}
three_turns = [{'role': 'system', 'content': 'You are Mjoll.'},
               {'role': 'user', 'content': 'Follow me.'}, call('call_a'), {'role': 'tool', 'tool_call_id': 'call_a', 'content': 'done'},
               {'role': 'assistant', 'content': 'Lead on.'},
               {'role': 'user', 'content': 'Wait here.'}, {'role': 'assistant', 'content': 'As you wish.'},
               {'role': 'user', 'content': 'Follow me again.'}, call('call_b'), {'role': 'tool', 'tool_call_id': 'call_b', 'content': 'done'}]
_, conv3 = native.convert_messages(three_turns, {'call_a': think('a'), 'call_b': think('b')})
with_thinking = [i for i, m in enumerate(conv3) if any(b['type'] == 'thinking' for b in m['content'])]
check('only the last tool-call turn carries thinking', with_thinking == [len(conv3) - 2] and conv3[-2]['content'][0] == think('b')[0], [(m['role'], [b['type'] for b in m['content']]) for m in conv3])
_, conv4 = native.convert_messages(three_turns + [{'role': 'assistant', 'content': 'Coming.'}, {'role': 'user', 'content': 'Good.'}], {'call_a': think('a'), 'call_b': think('b')})
check('finished tool turn: no thinking replayed', not any(b['type'] == 'thinking' for m in conv4 for b in m['content']), conv4)
_, conv2 = native.convert_messages([{'role': 'assistant', 'content': 'Hi'}], {})
check('leading assistant gets a user turn', conv2[0]['role'] == 'user')

tools = [{'type': 'function', 'function': {'name': 'Zeta', 'description': 'z', 'parameters': {'type': 'object', 'properties': {}}}},
         {'type': 'function', 'function': {'name': 'Alpha', 'description': 'a', 'parameters': {'type': 'object', 'properties': {}}}}]
params = {'max_tokens': 300, 'stop': ['#'], 'extra_body': {'thinking': {'type': 'between_tools'}, 'provider': {'x': 1}}}
req = native.build_request('claude-sonnet-5-5', msgs[:2], params, tools, {}, cache=True)
check('max_tokens', req['max_tokens'] == 300)
check('stop -> stop_sequences', req['stop_sequences'] == ['#'])
check('system cache marker', req['system'][-1].get('cache_control') == {'type': 'ephemeral'}, req['system'])
check('history cache marker', req['messages'][-1]['content'][-1].get('cache_control') == {'type': 'ephemeral'}, req['messages'])
check('thinking passed', req['extra_body'].get('thinking') == {'type': 'between_tools'})
check('openrouter provider dropped', 'provider' not in req['extra_body'])
check('fallbacks for sonnet 5.5', req['extra_body'].get('fallbacks') == 'default' and req['extra_headers']['anthropic-beta'] == native.FALLBACK_BETA)
check('tools sorted + converted', [t['name'] for t in req['tools']] == ['Alpha', 'Zeta'] and 'input_schema' in req['tools'][0])
# the reply after an action result: same tools (cached prefix unchanged), tool_choice none, nothing else different
req_none = native.build_request('claude-sonnet-5-5', msgs[:2], params, tools, {}, cache=True, no_tool_use=True)
check('no_tool_use: tool_choice none', req_none.get('tool_choice') == {'type': 'none'}, req_none.get('tool_choice'))
check('no_tool_use: tools byte-identical to a normal reply', json.dumps(req_none['tools'], sort_keys=True) == json.dumps(req['tools'], sort_keys=True))
check('no_tool_use: system identical to a normal reply', json.dumps(req_none['system'], sort_keys=True) == json.dumps(req['system'], sort_keys=True))
check('no_tool_use: only tool_choice added', {k: v for k, v in req_none.items() if k != 'tool_choice'} == req)
check('normal reply: no tool_choice sent', 'tool_choice' not in req)
check('no_tool_use without tools: no tool_choice', 'tool_choice' not in native.build_request('claude-sonnet-5-5', msgs[:2], params, None, {}, cache=True, no_tool_use=True))
req_h = native.build_request('claude-haiku-5-5', msgs[:2], None, None, {}, cache=False)
check('no fallbacks for haiku', 'extra_headers' not in req_h and 'fallbacks' not in req_h.get('extra_body', {}))
check('no cache when off', 'cache_control' not in req_h['system'][-1] and 'extra_body' not in req_h)
check('default max_tokens', req_h['max_tokens'] == native.DEFAULT_MAX_TOKENS)

# ---------- thinking off: each model gets the form it accepts (no thinking setting = hidden, billed thinking) ----------
def thinking_fields(model, params):
    return native.split_params(params, model)[0]
off_or = {'max_tokens': 500, 'extra_body': {'reasoning': {'enabled': False}, 'provider': {'x': 1}}}
check('reasoning off -> sonnet 5.5 between_tools', thinking_fields('claude-sonnet-5-5', off_or).get('thinking') == {'type': 'between_tools'})
check('reasoning off -> haiku 5.5 disabled', thinking_fields('claude-haiku-5-5', off_or).get('thinking') == {'type': 'disabled'})
f_opus = thinking_fields('claude-opus-5-5', off_or)
check('reasoning off -> opus 5.5 low effort, no thinking field', 'thinking' not in f_opus and f_opus.get('output_config') == {'effort': 'low'}, f_opus)
check('reasoning off -> fable keeps a set effort', thinking_fields('claude-fable-5-1', {'reasoning_effort': 'medium', 'extra_body': {'reasoning': {'enabled': False}}}).get('output_config') == {'effort': 'medium'})
check('haiku line on sonnet 5.5 -> between_tools', thinking_fields('claude-sonnet-5-5', {'extra_body': {'thinking': {'type': 'disabled'}}}).get('thinking') == {'type': 'between_tools'})
check('sonnet line on haiku 5.5 -> disabled', thinking_fields('claude-haiku-5-5', {'extra_body': {'thinking': {'type': 'between_tools'}}}).get('thinking') == {'type': 'disabled'})
check('off line on opus 5.5 dropped', 'thinking' not in thinking_fields('claude-opus-5-5', {'extra_body': {'thinking': {'type': 'disabled'}}}))
f_x = thinking_fields('claude-sonnet-5-5', {'reasoning_effort': 'xhigh', 'extra_body': {'reasoning': {'enabled': False}}})
check('thinking off caps effort at high', f_x.get('output_config') == {'effort': 'high'} and f_x.get('thinking') == {'type': 'between_tools'}, f_x)
check('no reasoning setting: left alone', 'thinking' not in thinking_fields('claude-sonnet-5-5', {'max_tokens': 500}))
check('reasoning on: left alone', 'thinking' not in thinking_fields('claude-sonnet-5-5', {'extra_body': {'reasoning': {'enabled': True}}}))
check('adaptive kept', thinking_fields('claude-sonnet-5-5', {'extra_body': {'thinking': {'type': 'adaptive'}, 'reasoning': {'enabled': False}}}).get('thinking') == {'type': 'adaptive'})
req_off = native.build_request('claude-sonnet-5-5', msgs[:2], off_or, None, {}, cache=True)
check('build_request sends thinking off', req_off['extra_body'].get('thinking') == {'type': 'between_tools'} and 'reasoning' not in req_off['extra_body'], req_off.get('extra_body'))

# ---------- tool list stays byte-identical when nearby NPCs change (reply-2 cache miss) ----------
def attack_tool(names_text):
    return [{'type': 'function', 'function': {'name': 'Attack', 'description': 'Attack someone.', 'parameters': {
        'type': 'object', 'properties': {'target': {'type': 'string', 'description': 'Who to attack.' + names_text}}}}}]
talk = [{'role': 'system', 'content': 'You are a knight.'}, {'role': 'user', 'content': 'Is Riverwood safe?'}]
req_a = native.build_request('claude-sonnet-5-5', talk, None, attack_tool(' Available: Pilgrim, Lucan Valerius'), {}, cache=True)
req_b = native.build_request('claude-sonnet-5-5', talk, None, attack_tool(' Available: Camilla Valerius'), {}, cache=True)
req_c = native.build_request('claude-sonnet-5-5', talk, None, attack_tool(' No entities available.'), {}, cache=True)
check('tool list stable after nearby change', json.dumps(req_a['tools']) == json.dumps(req_b['tools']) == json.dumps(req_c['tools']), req_a['tools'])
check('names leave the tool schema', 'Lucan' not in json.dumps(req_a['tools']) and 'No entities' not in json.dumps(req_c['tools']))
last_a = req_a['messages'][-1]['content']
check('names moved to the latest message', last_a[-1]['type'] == 'text' and 'Attack.target' in last_a[-1]['text'] and 'Pilgrim, Lucan Valerius' in last_a[-1]['text'], last_a)
check('no names note when none listed', all('Attack.target' not in b.get('text', '') for b in native.build_request('claude-sonnet-5-5', talk, None, attack_tool(''), {}, cache=True)['messages'][-1]['content']))
bare = [{'type': 'function', 'function': {'name': 'Attack', 'description': 'Attack someone.', 'parameters': {
    'type': 'object', 'properties': {'target': {'type': 'string', 'description': 'Available: Lydia'}}}}}]
req_bare = native.build_request('claude-sonnet-5-5', talk, None, bare, {}, cache=True)
check('names stripped when the base description is empty', 'Lydia' not in json.dumps(req_bare['tools']) and 'Attack.target: Lydia' in req_bare['messages'][-1]['content'][-1]['text'], (req_bare['tools'], req_bare['messages'][-1]))
check('caller tools not modified', 'Pilgrim' in attack_tool(' Available: Pilgrim')[0]['function']['parameters']['properties']['target']['description'])

# ---------- conversation cache point survives the next reply (history re-written every reply) ----------
def cached_blocks(req):
    """Message blocks up to and including the last cache marker, without the markers."""
    flat = [(m['role'], b) for m in req['messages'] for b in m['content']]
    marked = [i for i, (_, b) in enumerate(flat) if 'cache_control' in b]
    if not marked:
        return None
    return json.dumps([(r, {k: v for k, v in b.items() if k != 'cache_control'}) for r, b in flat[:marked[-1] + 1]])

def all_blocks(req):
    return json.dumps([(m['role'], {k: v for k, v in b.items() if k != 'cache_control'}) for m in req['messages'] for b in m['content']])

shot = {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,QUJD'}}
turn_n = talk[:1] + [{'role': 'user', 'content': 'Hello'}, {'role': 'assistant', 'content': 'Well met.'},
                     {'role': 'user', 'content': [{'type': 'text', 'text': '(Mjoll arrives.) Is Riverwood safe?'}, shot]}]
turn_n1 = turn_n[:3] + [{'role': 'user', 'content': '(Mjoll arrives.) Is Riverwood safe?'}, {'role': 'assistant', 'content': 'Safe enough.'},
                        {'role': 'user', 'content': [{'type': 'text', 'text': 'Why?'}, shot]}]
req_n = native.build_request('claude-sonnet-5-5', turn_n, None, attack_tool(' Available: Pilgrim'), {}, cache=True)
req_n1 = native.build_request('claude-sonnet-5-5', turn_n1, None, attack_tool(' Available: Lucan Valerius'), {}, cache=True)
cached = cached_blocks(req_n)
check('history has a cache point', cached is not None, req_n['messages'])
check('cached history is a prefix of the next request', cached is not None and all_blocks(req_n1).startswith(cached[:-1]), (cached, all_blocks(req_n1)))
last_n = req_n['messages'][-1]['content']
check('cache point not on screenshot or names note', all('cache_control' not in b for b in last_n if b['type'] == 'image' or 'Attack.target' in b.get('text', '')), last_n)
check('no top-level auto cache', 'cache_control' not in req_n.get('extra_body', {}), req_n.get('extra_body'))
check('at most 4 cache points', 1 + sum('cache_control' in b for m in req_n['messages'] for b in m['content']) <= 4)
image_first = turn_n[:3] + [{'role': 'user', 'content': [shot, {'type': 'text', 'text': 'Is Riverwood safe?'}]}]
req_if = native.build_request('claude-sonnet-5-5', image_first, None, None, {}, cache=True)
check('image first: cache point before the image', 'cache_control' in req_if['messages'][-2]['content'][-1] and all('cache_control' not in b for b in req_if['messages'][-1]['content']), req_if['messages'])
req_tr = native.build_request('claude-sonnet-5-5', msgs[:4], None, None, thinking_store, cache=True)
check('tool result turn cached at its end', 'cache_control' in req_tr['messages'][-1]['content'][-1], req_tr['messages'][-1])
# ---------- extension inputs: extra text after the cache point, system split into parts ----------
req_x = native.build_request('claude-sonnet-5-5', turn_n, None, attack_tool(' Available: Pilgrim'), {}, cache=True,
                             extra_texts=['(Mjoll knows Player well.)'])
last_x = req_x['messages'][-1]['content']
check('extra text is the last block', last_x[-1] == {'type': 'text', 'text': '(Mjoll knows Player well.)'}, last_x)
check('extra text after the cache point', all('cache_control' not in b for b in last_x[[b['type'] for b in last_x].index('image'):]), last_x)
check('no extra text = same request', json.dumps(native.build_request('claude-sonnet-5-5', turn_n, None, None, {}, cache=True, extra_texts=[])) == json.dumps(native.build_request('claude-sonnet-5-5', turn_n, None, None, {}, cache=True)))
def parts_hook(parts):
    return {'rules': 'Speak plainly.', 'bio': parts['mantella'], 'scene': 'It is raining.', '__breakpoint_after__': 'bio'}
req_p = native.build_request('claude-sonnet-5-5', turn_n, None, None, {}, cache=True, system_parts=parts_hook)
check('system split into parts', [b['text'] for b in req_p['system']] == ['Speak plainly.', 'You are a knight.', 'It is raining.'], req_p['system'])
check('system cache point after the named part', [('cache_control' in b) for b in req_p['system']] == [False, True, False], req_p['system'])
check('still at most 4 cache points', sum('cache_control' in b for b in req_p['system']) + sum('cache_control' in b for m in req_p['messages'] for b in m['content']) <= 4)
req_pe = native.build_request('claude-sonnet-5-5', turn_n, None, None, {}, cache=True, system_parts=lambda parts: {'a': 'One.', 'b': ''})
check('empty parts dropped, cache point on the last part', [b['text'] for b in req_pe['system']] == ['One.'] and 'cache_control' in req_pe['system'][-1], req_pe['system'])
req_pbad = native.build_request('claude-sonnet-5-5', turn_n, None, None, {}, cache=True, system_parts=lambda parts: 1 / 0)
check('failing system_parts falls back to the stock system', [b['text'] for b in req_pbad['system']] == ['You are a knight.'], req_pbad['system'])
# test_order_and_two_cache_points: '__order__' sorts parts (unknown ones after, in their own order);
# '__breakpoints_after__' entries are a name or a list of names (the last present one is used)
def ordered_hook(parts):
    return {'bio': 'Bio.', 'memory': 'Memory.', 'extra': 'Extra.', 'rules': 'Rules.', 'respond': 'Respond.',
            '__order__': ['rules', 'bio', 'knows', 'memory', 'respond'],
            '__breakpoints_after__': [['bio', 'identity', 'knows'], 'respond']}
req_o = native.build_request('claude-sonnet-5-5', turn_n, None, None, {}, cache=True, system_parts=ordered_hook)
check('parts in the declared order, unknown last', [b['text'] for b in req_o['system']] == ['Rules.', 'Bio.', 'Memory.', 'Respond.', 'Extra.'], [b['text'] for b in req_o['system']])
check('cache points after the stable part and after respond', [('cache_control' in b) for b in req_o['system']] == [False, True, False, True, False], req_o['system'])
check('two system cache points still within 4', sum('cache_control' in b for b in req_o['system']) + sum('cache_control' in b for m in req_o['messages'] for b in m['content']) <= 4)
check('joined text for other services', native.join_system_parts(ordered_hook, 'x') == 'Rules.\n\nBio.\n\nMemory.\n\nRespond.\n\nExtra.')
check('join keeps the stock text when the hook fails', native.join_system_parts(lambda parts: 1 / 0, 'Stock.') == 'Stock.')

# ---------- cache layout: shared part, NPC part, end of the prompt, newest message (at most 4 points) ----------
def marks(req):
    """Which blocks carry a cache point: ('tools', i) / ('system', i) / ('messages', i, j)."""
    out = [('tools', i) for i, t in enumerate(req.get('tools') or []) if 'cache_control' in t]
    out += [('system', i) for i, b in enumerate(req.get('system') or []) if 'cache_control' in b]
    out += [('messages', i, j) for i, m in enumerate(req['messages']) for j, b in enumerate(m['content']) if 'cache_control' in b]
    return out

def big_tools(n=30):
    return [{'type': 'function', 'function': {'name': f'Action{i:02d}', 'description': 'Does a thing in the world. ' * 8, 'parameters': {
        'type': 'object', 'properties': {'target': {'type': 'string', 'description': 'Who it is done to.'}}}}} for i in range(n)]

RULES = 'What you wear is your own choice. ' * 60          # same for every NPC
def hub_hook(name, memory):
    """Like the Integration Hub: rules shared by all NPCs, this NPC's fixed text, memory, reply instruction."""
    return lambda parts: {'prompt_rules': RULES, 'mantella_rules': f'# Rules\nSpeak only as {name}.',
                          'overview': f'# Overview\nYou are {name} in Skyrim. ' + 'A Nord warrior of Riverwood. ' * 80,
                          'history': '# History\n' + memory, 'respond': f'Respond as {name} now in English.',
                          '__order__': ['prompt_rules', 'mantella_rules', 'overview', 'history', 'respond'],
                          '__breakpoints_after__': [['prompt_rules'], ['overview'], 'respond']}

talk_m = [{'role': 'system', 'content': 'stock'}, {'role': 'user', 'content': 'Hello'}, {'role': 'assistant', 'content': 'Well met.'},
          {'role': 'user', 'content': [{'type': 'text', 'text': 'Is Riverwood safe?'}, shot]}]
req_m = native.build_request('claude-sonnet-5-5', talk_m, None, big_tools(), {}, cache=True, system_parts=hub_hook('Mjoll', '[Day 2] We fished.'),
                             extra_texts=['Current scene: Riverwood. The time is 12 in the afternoon.'])
check('block order: shared rules first, then the NPC, memory, reply instruction', [b['text'][:12] for b in req_m['system']] ==
      [RULES[:12], '# Rules\nSpea', '# Overview\nY', '# History\n[D', 'Respond as M'], [b['text'][:12] for b in req_m['system']])
check('cache points: shared part, NPC part, end of the prompt, newest message', marks(req_m) == [('system', 0), ('system', 2), ('system', 4), ('messages', 2, 0)], marks(req_m))
check('at most 4 cache points (shared, NPC, end, newest)', len(marks(req_m)) == 4)
# the shared cached prefix (actions + Living World rules) is byte-identical for another NPC with the same actions
req_l = native.build_request('claude-sonnet-5-5', talk_m, None, big_tools(), {}, cache=True, system_parts=hub_hook('Lydia', ''))
strip = lambda v: json.dumps(v, sort_keys=True).replace(', "cache_control": {"type": "ephemeral"}', '')
check('shared part is the same for another NPC', strip(req_l['tools']) == strip(req_m['tools']) and strip(req_l['system'][0]) == strip(req_m['system'][0]))
check('NPC-specific text comes after the shared cache point', all('Mjoll' not in json.dumps(x) for x in (req_m['tools'], req_m['system'][0])))
# nothing that changes during a talk sits before a cache point: the scene goes after the last one
def cached_prefix(req):
    flat = [('t', t) for t in req.get('tools') or []] + [('s', b) for b in req.get('system') or []] + \
           [('m', b) for m in req['messages'] for b in m['content']]
    last = max(i for i, (_, b) in enumerate(flat) if 'cache_control' in b)
    return json.dumps([b for _, b in flat[:last + 1]])
check('scene after the last cache point', 'The time is' not in cached_prefix(req_m) and 'The time is' in json.dumps(req_m['messages'][-1]))
# stock Mantella without extensions: its '# Current Scene' section leaves the cached system prompt for the newest message
STOCK = ('# Overview\nYou are Mjoll in Skyrim.\n\n# Background\nInga is a Nord.\n\n# History\n\n\n# Current Scene\n'
         'You are now in Riverwood. The time is {hour} in the afternoon. \n\n# Rules\nStay in character.\n\nRespond as Mjoll now in English.')
stock_turn = lambda hour: [{'role': 'system', 'content': STOCK.format(hour=hour)}] + talk_m[1:]
req_s1 = native.build_request('claude-sonnet-5-5', stock_turn(1), None, None, {}, cache=True)
req_s2 = native.build_request('claude-sonnet-5-5', stock_turn(2), None, None, {}, cache=True)
check('stock prompt: scene taken out of the system prompt', 'Riverwood' not in req_s1['system'][0]['text'] and '# Rules\nStay in character.' in req_s1['system'][0]['text']
      and '# History\n\n# Rules' in req_s1['system'][0]['text'], req_s1['system'][0]['text'])
check('stock prompt: system prompt the same when the hour turns', req_s1['system'] == req_s2['system'])
check('stock prompt: scene in the newest message, after its cache point', req_s1['messages'][-1]['content'][-1] == {'type': 'text', 'text': 'Current scene: You are now in Riverwood. The time is 1 in the afternoon.'}
      and 'Riverwood. The time' not in cached_prefix(req_s1), req_s1['messages'][-1])
req_s_off = native.build_request('claude-sonnet-5-5', stock_turn(1), None, None, {}, cache=False)
check('cache off: prompt sent as Mantella wrote it', req_s_off['system'][0]['text'] == STOCK.format(hour=1).strip() and 'cache_control' not in json.dumps(req_s_off), req_s_off['system'])
check('no scene section: system text unchanged', native.build_request('claude-sonnet-5-5', talk, None, None, {}, cache=True)['system'][0]['text'] == 'You are a knight.')
# without extensions, a long action list gets its own cache point (shared by NPCs with the same actions)
req_t = native.build_request('claude-sonnet-5-5', talk_m, None, big_tools(), {}, cache=True)
check('no extension: newest, end of system, end of actions, last NPC reply', marks(req_t) == [('tools', len(req_t['tools']) - 1), ('system', 0), ('messages', 1, 0), ('messages', 2, 0)], marks(req_t))
# a point whose prefix is too short to be cached only fills a spare slot
short_hook = lambda parts: dict(hub_hook('Mjoll', 'x')(parts), prompt_rules='Be brief.')
req_short = native.build_request('claude-sonnet-5-5', talk_m, None, None, {}, cache=True, system_parts=short_hook)
check('short shared part loses its slot to the last NPC reply', marks(req_short) == [('system', 2), ('system', 4), ('messages', 1, 0), ('messages', 2, 0)], marks(req_short))
check('minimum by model', native.cache_minimum('claude-sonnet-5-5') == 512 and native.cache_minimum('claude-haiku-4-5') == 4096
      and native.cache_minimum('claude-sonnet-5') == 1024 and native.cache_minimum('some-model') == 1024)
# 1-hour cache setting: every point gets the same ttl (a longer ttl may not follow a shorter one)
native.long_cache = True
try:
    req_1h = native.build_request('claude-sonnet-5-5', talk_m, None, big_tools(), {}, cache=True, system_parts=hub_hook('Mjoll', 'x'))
finally:
    native.long_cache = False
controls = [b['cache_control'] for b in (req_1h.get('tools') or []) + req_1h['system'] + [b for m in req_1h['messages'] for b in m['content']] if 'cache_control' in b]
check('1-hour cache: ttl 1h on every point', len(controls) == 4 and all(c == {'type': 'ephemeral', 'ttl': '1h'} for c in controls), controls)
def controls_in_order(req):
    return [b['cache_control'] for b in (req.get('tools') or []) + req['system'] + [b for m in req['messages'] for b in m['content']] if 'cache_control' in b]
default = controls_in_order(req_m)
check('default: the shared part (first system point) keeps 1 hour, every other point 5 minutes with no ttl field',
      default[0] == {'type': 'ephemeral', 'ttl': '1h'} and all(c == {'type': 'ephemeral'} for c in default[1:]) and len(default) == 4, default)
long_first = lambda cs: all('ttl' in c for c in cs[:sum('ttl' in c for c in cs)])
req_t1h = native.build_request('claude-sonnet-5-5', talk_m, None, big_tools(), {}, cache=True, system_parts=hub_hook('Mjoll', 'x'))
check('every 1-hour point comes before any 5-minute point (Anthropic: a longer ttl may not follow a shorter one)',
      long_first(default) and long_first(controls_in_order(req_t1h)) and long_first(controls_in_order(req_t)), controls_in_order(req_t1h))
native.shared_long_cache = False
try:
    req_5m = native.build_request('claude-sonnet-5-5', talk_m, None, big_tools(), {}, cache=True, system_parts=hub_hook('Mjoll', '[Day 2] We fished.'))
finally:
    native.shared_long_cache = True
check('shared-part setting off: 5-minute cache everywhere, no ttl field', all(c == {'type': 'ephemeral'} for c in controls_in_order(req_5m)),
      controls_in_order(req_5m))
one_point = native.build_request('claude-sonnet-5-5', talk, None, None, {}, cache=True)
check('a prompt with one system point and no actions (no shared part of its own): 5 minutes',
      all(c == {'type': 'ephemeral'} for c in controls_in_order(one_point)), controls_in_order(one_point))
check('1-hour setting does not touch requests without caching', 'cache_control' not in json.dumps(native.build_request('claude-sonnet-5-5', talk_m, None, big_tools(), {}, cache=False)))
# usage log: cache points listed, 1-hour writes priced at twice the input price
st1 = native.StreamState()
st1.points = native.cache_points(req_m)
st1.handle({'type': 'message_start', 'message': {'usage': {'cache_read_input_tokens': 900, 'cache_creation_input_tokens': 300, 'input_tokens': 50,
                                                             'cache_creation': {'ephemeral_1h_input_tokens': 300, 'ephemeral_5m_input_tokens': 0}}}})
check('cache points rise in prompt order', len(st1.points) == 4 and st1.points == sorted(st1.points) and st1.points[0] > 1000, st1.points)
check('1-hour writes recorded', st1.usage == {'cache_read': 900, 'cache_write': 300, 'uncached': 50, 'cache_write_1h': 300}, st1.usage)
check('1-hour writes cost 2x input', abs(native.reply_cost('claude-sonnet-5-5', {'cache_write': 300, 'cache_write_1h': 300}) - 300 * 4.00 / 1e6) < 1e-12)
st = native.StreamState()
st.handle({'type': 'message_start', 'message': {'usage': {'cache_read_input_tokens': 4512, 'cache_creation_input_tokens': 84, 'input_tokens': 790}}})
st.handle({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 41}})
check('stream state keeps usage', st.usage == {'cache_read': 4512, 'cache_write': 84, 'uncached': 790, 'output': 41}, st.usage)
# reply cost at Sonnet 5.5 prices: input $2, cache write $2.50, cache read $0.10, output $10 per million
usage = {'cache_read': 4629, 'cache_write': 151, 'uncached': 790, 'output': 41}
check('reply cost at Sonnet 5.5 prices', abs(native.reply_cost('claude-sonnet-5-5', usage) - (4629 * 0.10 + 151 * 2.50 + 790 * 2 + 41 * 10) / 1e6) < 1e-12, native.reply_cost('claude-sonnet-5-5', usage))
check('dated model id priced like its family', native.reply_cost('claude-sonnet-5-5-20260601', usage) == native.reply_cost('claude-sonnet-5-5', usage))
check('longest prefix wins', native.reply_cost('claude-sonnet-5-5', usage) != native.reply_cost('claude-sonnet-5', usage))
check('unknown model has no cost', native.reply_cost('some-other-model', usage) is None)
check('no history cache when off', all('cache_control' not in b for m in native.build_request('claude-haiku-5-5', turn_n, None, None, {}, cache=False)['messages'] for b in m['content']))

# ---------- fake server ----------
REQUESTS = []
SCENARIO = {'events': []}

def sse(events):
    out = b''
    for e in events:
        out += f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode()
    return out

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        REQUESTS.append({'path': self.path, 'body': body, 'headers': {k.lower(): v for k, v in self.headers.items()}})
        if body.get('stream'):
            data = sse(SCENARIO['events'])
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.send_header('Content-Length', str(len(data))); self.end_headers()
            self.wfile.write(data)
        else:
            data = json.dumps({'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': body['model'],
                               'content': [{'type': 'text', 'text': 'A memory.'}], 'stop_reason': 'end_turn', 'stop_sequence': None,
                               'usage': {'input_tokens': 10, 'output_tokens': 3, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers()
            self.wfile.write(data)

server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f'http://127.0.0.1:{server.server_address[1]}'

def start(model='claude-sonnet-5-5', cache_read=1500):
    return {'type': 'message_start', 'message': {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': model, 'content': [],
            'stop_reason': None, 'stop_sequence': None,
            'usage': {'input_tokens': 40, 'output_tokens': 1, 'cache_read_input_tokens': cache_read, 'cache_creation_input_tokens': 120}}}
def stop(reason='end_turn'):
    return [{'type': 'message_delta', 'delta': {'stop_reason': reason, 'stop_sequence': None}, 'usage': {'output_tokens': 9}},
            {'type': 'message_stop'}]

TEXT_AND_TOOL = [start(),
    {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}},
    {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': 'Plan: follow.'}},
    {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': 'SIG'}},
    {'type': 'content_block_stop', 'index': 0},
    {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}},
    {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': 'Lead the way. '}},
    {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': 'I am right behind you.'}},
    {'type': 'content_block_stop', 'index': 1},
    {'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'tool_use', 'id': 'toolu_9', 'name': 'FollowMe', 'input': {}}},
    {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': '{"target"'}},
    {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': ': "Mjoll"}'}},
    {'type': 'content_block_stop', 'index': 2},
] + stop('tool_use')

FALLBACK = [start(model='claude-sonnet-5'),
    {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'fallback', 'from': {'model': 'claude-sonnet-5-5'}, 'to': {'model': 'claude-sonnet-5'}}},
    {'type': 'content_block_stop', 'index': 0},
    {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'text', 'text': ''}},
    {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': 'Fine.'}},
    {'type': 'content_block_stop', 'index': 1},
] + stop()

REFUSAL = [start(), ] + stop('refusal')

async def run(events, store, extra_texts=None, no_tool_use=False):
    SCENARIO['events'] = events
    client = anthropic.AsyncAnthropic(api_key='test-key', base_url=BASE, max_retries=0)
    state = native.StreamState()
    texts = []
    try:
        request = native.build_request('claude-sonnet-5-5', msgs[:2], {'extra_body': {'thinking': {'type': 'between_tools'}}}, tools, store, cache=True,
                                        extra_texts=extra_texts, no_tool_use=no_tool_use)
        async for t in native.stream_reply(client, request, store, state):
            texts.append(t)
    finally:
        await client.close()
    return texts, state

store = {}
texts, state = asyncio.run(run(TEXT_AND_TOOL, store))
check('text streamed', ''.join(texts) == 'Lead the way. I am right behind you.', texts)
check('tool call assembled', state.tool_calls == [{'id': 'toolu_9', 'type': 'function', 'function': {'name': 'FollowMe', 'arguments': '{"target": "Mjoll"}'}}], state.tool_calls)
check('stop reason', state.stop_reason == 'tool_use')
check('thinking stored by tool call', (store.get('toolu_9') if isinstance(store.get('toolu_9'), dict) else {}).get('thinking') == [{'type': 'thinking', 'thinking': 'Plan: follow.', 'signature': 'SIG'}], store)
# test_thinking_dropped_when_prefix_changed (DACN-01): the continuation keeps the block only while system, tools and
# the earlier messages are what the block was made with; a per-request note moving to the newest message changes them.
follow_up = msgs[:2] + [{'role': 'assistant', 'content': None, 'tool_calls': state.tool_calls},
                        {'role': 'tool', 'tool_call_id': 'toolu_9', 'content': 'done'}]
same = native.build_request('claude-sonnet-5-5', follow_up, None, tools, store, cache=True)
check('unchanged prefix: thinking replayed', [b['type'] for b in same['messages'][-2]['content']][:1] == ['thinking'], same['messages'][-2])
scene_store = {}
asyncio.run(run(TEXT_AND_TOOL, scene_store, ['Current scene: Riverwood.']))
moved = native.build_request('claude-sonnet-5-5', follow_up, None, tools, scene_store, cache=True, extra_texts=['Current scene: Riverwood.'])
check('scene note moved off the earlier message: thinking dropped', [b['type'] for b in moved['messages'][-2]['content']] == ['tool_use'], moved['messages'][-2])
no_tools = native.build_request('claude-sonnet-5-5', follow_up, None, None, store, cache=True)
check('tools changed: thinking dropped', [b['type'] for b in no_tools['messages'][-2]['content']] == ['tool_use'], no_tools['messages'][-2])
r = REQUESTS[-1]
check('native path', r['path'] == '/v1/messages', r['path'])
check('api key header', r['headers'].get('x-api-key') == 'test-key')
check('fallback beta header', native.FALLBACK_BETA in r['headers'].get('anthropic-beta', ''), r['headers'].get('anthropic-beta'))
check('body: history cached + fallbacks + thinking', 'cache_control' not in r['body'] and any('cache_control' in b for m in r['body']['messages'] for b in m['content']) and r['body'].get('fallbacks') == 'default' and r['body'].get('thinking') == {'type': 'between_tools'}, r['body'])
check('body: system cached', r['body']['system'][-1].get('cache_control') == {'type': 'ephemeral'})

texts, state = asyncio.run(run(FALLBACK, {}))
check('fallback block handled', ''.join(texts) == 'Fine.' and state.fell_back_to == 'claude-sonnet-5', (texts, state.fell_back_to))

texts, state = asyncio.run(run(REFUSAL, {}))
check('refusal = empty, no crash', texts == [] and state.stop_reason == 'refusal' and not state.got_text)

# the reply after an action result, on the wire: same tools as a normal reply, tool_choice none
asyncio.run(run(FALLBACK, {}))
normal_body = REQUESTS[-1]['body']
texts, state = asyncio.run(run(FALLBACK, {}, no_tool_use=True))
none_body = REQUESTS[-1]['body']
check('wire: tool_choice none sent', none_body.get('tool_choice') == {'type': 'none'}, none_body.get('tool_choice'))
check('wire: tools and system identical to a normal reply', none_body['tools'] == normal_body['tools'] and none_body['system'] == normal_body['system'] and 'tool_choice' not in normal_body)
check('wire: text still streamed', ''.join(texts) == 'Fine.', texts)

# T-10: a reply cut off by max_tokens keeps its text but drops its tool calls (their arguments may be incomplete)
cut_store = {}
texts, state = asyncio.run(run(TEXT_AND_TOOL[:-2] + stop('max_tokens'), cut_store))
check('max_tokens: text kept', ''.join(texts) == 'Lead the way. I am right behind you.', texts)
check('max_tokens: tool calls dropped', state.tool_calls == [] and state.stop_reason == 'max_tokens', state.tool_calls)
check('max_tokens: no thinking stored for a dropped call', cut_store == {}, cut_store)

client = anthropic.Anthropic(api_key='test-key', base_url=BASE, max_retries=0)
reply = native.reply_text(client.messages.create(**native.build_request('claude-sonnet-5-5', msgs[:2], {'max_tokens': 1200}, None, {}, cache=False)))
client.close()
check('non-streamed summary', reply == 'A memory.', reply)
check('summary uncached', 'cache_control' not in REQUESTS[-1]['body'] and all('cache_control' not in b for m in REQUESTS[-1]['body']['messages'] for b in m['content']) and 'cache_control' not in REQUESTS[-1]['body']['system'][-1])

# ---------- stall limit with the real SDK: a silent server is cut off on time, whatever part of the stream it hangs in ----------
class SilentHandler(BaseHTTPRequestHandler):
    """Sends MODE's events (none = no headers at all), then says nothing while the connection stays open."""
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        if SILENT['events'] is not None:
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            self.wfile.write(sse(SILENT['events'])); self.wfile.flush()
        SILENT['release'].wait(30)

SILENT = {'events': None, 'release': threading.Event()}
silent_server = ThreadingHTTPServer(('127.0.0.1', 0), SilentHandler)
silent_server.daemon_threads = True
threading.Thread(target=silent_server.serve_forever, daemon=True).start()

async def run_silent(events, stall):
    SILENT['events'] = events
    client = anthropic.AsyncAnthropic(api_key='test-key', base_url=f'http://127.0.0.1:{silent_server.server_address[1]}', max_retries=0)
    state = native.StreamState()
    texts, stalled = [], False
    began = time.monotonic()
    try:
        request = native.build_request('claude-sonnet-5-5', msgs[:2], None, None, {}, cache=False)
        async for t in native.stream_reply(client, request, {}, state, stall):
            texts.append(t)
    except TimeoutError:
        stalled = True
    finally:
        await src_llm.reliability.close_client(client)
    return texts, stalled, time.monotonic() - began, state

texts, stalled, took, state = asyncio.run(run_silent(None, 0.5))
check('no headers: stalled on time', stalled and texts == [] and 0.4 < took < 5, (stalled, texts, took))
said = [start(), {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Well, '}}]
texts, stalled, took, state = asyncio.run(run_silent(said, 0.5))
check('silent after some text: stalled, text kept', stalled and texts == ['Well, '] and 0.4 < took < 5, (stalled, texts, took))
check('usage kept from the stalled reply', state.usage and state.usage['uncached'] == 40, state.usage)
SILENT['release'].set()
silent_server.shutdown()

server.shutdown()
print('\nRESULT:', 'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}')
sys.exit(1 if FAILS else 0)
