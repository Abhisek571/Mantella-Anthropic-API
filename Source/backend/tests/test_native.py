"""Offline tests for src/llm/anthropic_native.py against a fake Messages API server.
Run with the native-venv Python: test_native.py <repo Source/backend dir>"""
import asyncio, importlib.util, json, sys, threading, types, logging
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
    {'role': 'system', 'content': 'You are Vega.'},
    {'role': 'user', 'content': 'Hello'},
    {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {'name': 'FollowMe', 'arguments': '{"x": 1}'}}]},
    {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'done'},
    {'role': 'user', 'content': [{'type': 'text', 'text': '(Vega follows.) Come on.'},
                                 {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,QUJD', 'detail': 'low'}}]},
    {'role': 'assistant', 'content': ''},
]
system, conv = native.convert_messages(msgs, thinking_store)
check('system joined', system == [{'type': 'text', 'text': 'You are Vega.'}], system)
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
three_turns = [{'role': 'system', 'content': 'You are Vega.'},
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
                     {'role': 'user', 'content': [{'type': 'text', 'text': '(Vega arrives.) Is Riverwood safe?'}, shot]}]
turn_n1 = turn_n[:3] + [{'role': 'user', 'content': '(Vega arrives.) Is Riverwood safe?'}, {'role': 'assistant', 'content': 'Safe enough.'},
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
                             extra_texts=['(Vega knows CHAD well.)'])
last_x = req_x['messages'][-1]['content']
check('extra text is the last block', last_x[-1] == {'type': 'text', 'text': '(Vega knows CHAD well.)'}, last_x)
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
    {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': ': "Vega"}'}},
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

async def run(events, store, extra_texts=None):
    SCENARIO['events'] = events
    client = anthropic.AsyncAnthropic(api_key='test-key', base_url=BASE, max_retries=0)
    state = native.StreamState()
    texts = []
    try:
        request = native.build_request('claude-sonnet-5-5', msgs[:2], {'extra_body': {'thinking': {'type': 'between_tools'}}}, tools, store, cache=True,
                                        extra_texts=extra_texts)
        async for t in native.stream_reply(client, request, store, state):
            texts.append(t)
    finally:
        await client.close()
    return texts, state

store = {}
texts, state = asyncio.run(run(TEXT_AND_TOOL, store))
check('text streamed', ''.join(texts) == 'Lead the way. I am right behind you.', texts)
check('tool call assembled', state.tool_calls == [{'id': 'toolu_9', 'type': 'function', 'function': {'name': 'FollowMe', 'arguments': '{"target": "Vega"}'}}], state.tool_calls)
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

client = anthropic.Anthropic(api_key='test-key', base_url=BASE, max_retries=0)
reply = native.reply_text(client.messages.create(**native.build_request('claude-sonnet-5-5', msgs[:2], {'max_tokens': 1200}, None, {}, cache=False)))
client.close()
check('non-streamed summary', reply == 'A memory.', reply)
check('summary uncached', 'cache_control' not in REQUESTS[-1]['body'] and all('cache_control' not in b for m in REQUESTS[-1]['body']['messages'] for b in m['content']) and 'cache_control' not in REQUESTS[-1]['body']['system'][-1])

server.shutdown()
print('\nRESULT:', 'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}')
sys.exit(1 if FAILS else 0)
