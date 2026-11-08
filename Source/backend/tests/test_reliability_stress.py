"""Stress tests for the reply rescue code: src/llm/reliability.py, ClientBase.streaming_call and its attempt, retry
and fallback loop, anthropic_native.stream_reply and the usage log in src/extensions.py. Fake clients, no network.
Streams stall or fail at every position, get cancelled from outside, and run many times in a row; every reply is
checked against the same rules: nothing spoken twice, no other model once something was spoken, OpenRouter always
gets the privacy rules, settings never reach a request, every failure is logged and nothing escapes, time is bounded.
Run with a Python 3.11: test_reliability_stress.py <repo Source/backend dir>"""
import asyncio, importlib.util, logging, random, sys, time, types
from pathlib import Path
from types import SimpleNamespace as NS

BACKEND = Path(sys.argv[1])
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 20261106

records = []
class Collect(logging.Handler):
    def emit(self, record):
        records.append((record.levelno, record.getMessage()))
log = logging.getLogger('mantella'); log.addHandler(Collect()); log.setLevel(logging.DEBUG); log.propagate = False

FAILS = []
def check(name, cond, detail=''):
    if not cond:
        print('FAIL ' + name + '  -> ' + str(detail)[:1500])
        FAILS.append(name)
    else:
        print('PASS ' + name)


# ---------- stubs (Mantella's own modules and the SDKs); client_base.py, reliability.py, anthropic_native.py and
# extensions.py are the real files ----------
OPENROUTER, NATIVE, OFFICIAL, LOCAL, OPENAI = ('https://openrouter.ai/api/v1', 'https://api.anthropic.com', 'https://api.anthropic.com/v1',
                                               'http://127.0.0.1:5001/v1', 'https://api.openai.com/v1')
# the same table and normalising as src/utils.py resolve_service_endpoint
KNOWN = {'openai': OPENAI, 'openrouter': OPENROUTER, 'or': OPENROUTER, 'anthropic(official)': OFFICIAL, 'anthropic': OFFICIAL,
         'https://api.anthropic.com/v1/': OFFICIAL, 'anthropic(native,cached)': NATIVE, 'anthropic-native': NATIVE,
         'https://api.anthropic.com/': NATIVE, 'kobold': LOCAL, 'koboldcpp': LOCAL}
def resolve(value):
    normalized = value.strip().lower().replace(' ', '')
    return KNOWN.get(normalized, normalized)
sounds = []
utils = types.ModuleType('src.utils')
utils.get_logger = lambda: log
utils.time_it = lambda fn: fn
utils.play_error_sound = lambda: sounds.append(1)
utils.resolve_service_endpoint = resolve
from urllib.parse import urlparse
utils.is_local_url = lambda url: urlparse(url).netloc.startswith(('10.', '127.', '192.168', '172.'))
utils.is_anthropic_endpoint = lambda url: url in (OFFICIAL, NATIVE)
utils.ANTHROPIC_NATIVE_ENDPOINT, utils.ANTHROPIC_ENDPOINT, utils.PLAYER2_ENDPOINT = NATIVE, OFFICIAL, 'https://api.player2.game/v1'
utils.get_model_token_limits = lambda: {}
utils.resolve_path = lambda: str(BACKEND / 'no' / 'such' / 'folder')

class Error(Exception):
    """Like openai.APIError: status_code, body and the code/type read from the body."""
    def __init__(self, message='', status_code=None, body=None):
        super().__init__(message); self.status_code, self.body = status_code, body
        inner = body.get('error', body) if isinstance(body, dict) else {}
        inner = inner if isinstance(inner, dict) else {}
        self.code, self.type = inner.get('code'), inner.get('type')
class APIConnectionError(Error): pass
class APITimeoutError(APIConnectionError): pass
class BadRequestError(Error): pass
class RateLimitError(Error): pass
class TransportError(Exception): pass
class ReadError(TransportError): pass

def module(name, **attrs):
    made = types.ModuleType(name); made.__dict__.update(attrs); sys.modules[name] = made
    return made
class Base:
    def __init__(self, *a, **k): pass
class Span:
    def __enter__(self): return self
    def __exit__(self, *a): return False
module('openai', APIConnectionError=APIConnectionError, BadRequestError=BadRequestError, OpenAI=Base, AsyncOpenAI=Base, RateLimitError=RateLimitError)
module('openai.types'); module('openai.types.chat', ChatCompletion=object)
module('tiktoken', get_encoding=lambda name: NS(encode=lambda text: list(text)), encoding_for_model=lambda name: NS(encode=lambda text: list(text)), Encoding=object)
module('requests')
src = module('src'); src.__path__ = []
src_llm = module('src.llm'); src_llm.__path__ = []
sys.modules['src.utils'] = utils; src.utils = utils
module('src.llm.ai_client', AIClient=Base)
module('src.llm.message_thread', message_thread=object)
module('src.llm.messages', Message=type('Message', (), {}), ImageMessage=type('ImageMessage', (), {}), UserMessage=type('UserMessage', (), {}))
module('src.llm.llm_model_list', LLMModelList=object)
module('src.telemetry'); module('src.telemetry.telemetry', create_span_from_thread=lambda name: Span())
module('src.actions'); module('src.actions.function_manager', FunctionManager=NS(is_vision_action_active=lambda: False))
module('src.llm.claude_cache_connector', ClaudeCacheConnector=lambda: NS(is_applicable=lambda url, model: False))

def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, BACKEND / relative)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded
rel = load('src.llm.reliability', 'src/llm/reliability.py'); src_llm.reliability = rel
native = load('src.llm.anthropic_native', 'src/llm/anthropic_native.py'); src_llm.anthropic_native = native
ext = load('src.extensions', 'src/extensions.py'); src.extensions = ext
ext._registry = ext.Registry()
cb = load('src.llm.client_base', 'src/llm/client_base.py')
ClientBase = cb.ClientBase
rel.CLOSE_SECONDS = 0.2  # a client that will not close holds a reply up this long


# ---------- fakes that count what they open ----------
class Hang:
    """Never answers when it is the next thing in a stream."""
class Sleep:
    def __init__(self, seconds): self.seconds = seconds
    def __repr__(self): return f'Sleep({self.seconds})'

class Count:
    def reset(self):
        self.streams_open = 0; self.streams_made = 0; self.clients_open = 0; self.clients_made = 0
COUNT = Count(); COUNT.reset()

class Script:
    def __init__(self): self.calls = []; self.plans = {}; self.spoken = []  # spoken: one list of texts per stream
    def plan(self, model, *answers): self.plans[model] = list(answers)
    def next(self, model):
        plans = self.plans.get(model) or [[chunk('(no plan)'), chunk(finish='stop')]]
        return plans.pop(0) if len(plans) > 1 else plans[0]
SCRIPT = Script()

def chunk(text=None, finish=None, tool=None, native_finish=None, refusal=None, error=None):
    delta = NS(content=text, tool_calls=[tool] if tool else None, refusal=refusal)
    return NS(choices=[NS(finish_reason=finish, delta=delta, native_finish_reason=native_finish)], error=error)
def usage_chunk():
    return NS(choices=[], error=None, usage=NS(prompt_tokens=10, completion_tokens=3))
def tool_part(name='Wave', arguments='{}', index=0, call_id='call_1'):
    return NS(index=index, id=call_id, function=NS(name=name, arguments=arguments))
def text_reply(text='Hello.'):
    return [chunk(text), chunk(finish='stop')]

def text_of(event):
    if isinstance(event, dict):
        delta = event.get('delta') or {}
        return delta.get('text') if delta.get('type') == 'text_delta' else None
    choices = getattr(event, 'choices', None)
    return choices[0].delta.content if choices and choices[0].delta else None

async def play(events, spoken):
    COUNT.streams_open += 1; COUNT.streams_made += 1
    try:
        for event in events:
            if event is Hang:
                await asyncio.sleep(3600)
            if isinstance(event, Sleep):
                await asyncio.sleep(event.seconds)
                continue
            if isinstance(event, BaseException):
                raise event
            if text_of(event):
                spoken.append(text_of(event))
            yield event
    finally:
        COUNT.streams_open -= 1

class FakeOpenAI:
    close_hangs = False
    def __init__(self, label):
        self.chat = NS(completions=NS(create=self.create)); self.label = label; self.open = True
        COUNT.clients_open += 1; COUNT.clients_made += 1
    async def create(self, model, messages, stream, **params):
        SCRIPT.calls.append(NS(model=model, messages=messages, params=params, service=self.label))
        spoken = []; SCRIPT.spoken.append(spoken)  # one list per request, filled by its stream
        answer = SCRIPT.next(model)
        if isinstance(answer, BaseException):
            raise answer
        if answer is Hang:
            await asyncio.sleep(3600)
        if isinstance(answer, Sleep):
            await asyncio.sleep(answer.seconds)
            answer = [nstart(), ntext('After a slow start.'), nend()] if self.label == 'native' else text_reply('After a slow start.')
        return play(answer, spoken)
    async def close(self):
        if self.open:
            self.open = False; COUNT.clients_open -= 1
        if FakeOpenAI.close_hangs:
            await asyncio.sleep(3600)

class FakeNative(FakeOpenAI):
    def __init__(self):
        super().__init__('native'); self.messages = NS(create=self.create_native)
    async def create_native(self, stream, **request):
        return await self.create(request['model'], request.get('messages'), stream, **{k: v for k, v in request.items() if k not in ('model', 'messages')})

ClientBase.generate_async_client = lambda self: FakeOpenAI(self._base_url)
ClientBase.generate_native_client = lambda self, is_async: FakeNative()
KEYS = {OPENROUTER: 'test-or', NATIVE: 'test-an', OFFICIAL: 'test-an'}
# like the real lookup: Anthropic has its own key, every other service falls back to GPT_SECRET_KEY.txt (the OpenRouter key)
ClientBase._get_api_key = staticmethod(lambda service, show_error=True: KEYS.get(resolve(service), None if utils.is_anthropic_endpoint(resolve(service)) else 'test-gpt'))

class Thread:
    def get_openai_messages(self): return [{'role': 'system', 'content': 'You are Lydia.'}, {'role': 'user', 'content': 'Hello'}]
    def get_last_message(self): return None

def client(service='OpenRouter', model='main/model', reliability=None, params=None):
    llm_params = dict(params if params is not None else {'max_tokens': 300})
    if reliability is not None:
        llm_params['reliability'] = reliability
    return ClientBase(service, model, llm_params, 16000)

SETTING_KEYS = {'reliability', 'stall_rescue', 'stall_seconds', 'fallback_models', 'openrouter_provider'}
def keys_deep(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from keys_deep(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from keys_deep(inner)

class Result:
    pass

def run(the_client, tools=None, cancel_after=None, consumer_delay=0.0):
    """One reply. Returns what came out, what escaped, how long it took and what was left open."""
    SCRIPT.calls.clear(); SCRIPT.spoken.clear(); records.clear(); sounds.clear(); COUNT.reset()
    result = Result(); result.items = []; result.escaped = None; result.cancelled = False
    async def collect():
        async for item in the_client.streaming_call(Thread(), False, tools):
            result.items.append(item)
            if consumer_delay:
                time.sleep(consumer_delay)  # voice generation blocks the loop between sentences
    async def main():
        task = asyncio.ensure_future(collect())
        if cancel_after is not None:
            done, _ = await asyncio.wait([task], timeout=cancel_after)
            if not done:
                task.cancel(); result.cancelled = True
        try:
            await task
        except asyncio.CancelledError:
            result.escaped = 'CancelledError'
        except BaseException as e:
            result.escaped = e
        for _ in range(5):
            await asyncio.sleep(0)
        result.tasks_left = len([t for t in asyncio.all_tasks() if t is not asyncio.current_task()])
        result.streams_open, result.clients_open = COUNT.streams_open, COUNT.clients_open
    began = time.monotonic()
    asyncio.run(main())
    result.elapsed = time.monotonic() - began
    result.said = ''.join(data for kind, data in result.items if kind == 'content')
    result.calls = list(SCRIPT.calls); result.models = [call.model for call in SCRIPT.calls]
    result.spoken = [list(s) for s in SCRIPT.spoken]
    result.logs = list(records)
    result.lock_free = not the_client._generation_lock.locked()
    result.main_model = the_client.model_name
    return result

def logged(result, text):
    return any(text in line for _, line in result.logs)
def warned(result):
    return [line for level, line in result.logs if level >= logging.WARNING]

def invariants(name, result, max_seconds, privacy=True, allow_cancel=False):
    """The rules every reply keeps, whatever the services did."""
    problems = []
    if result.escaped is not None and not (allow_cancel and result.cancelled and result.escaped == 'CancelledError'):
        problems.append(f'escaped: {result.escaped!r}')
    # nothing spoken twice: every word came from one stream, in its order
    speaking_streams = [s for s in result.spoken if s]
    if result.said:
        if not any(''.join(s).startswith(result.said) and result.said for s in speaking_streams):
            problems.append(f'said {result.said!r} is not one stream\'s words {speaking_streams}')
    # no other request once something was spoken
    first = next((i for i, s in enumerate(result.spoken) if s), None)
    if first is not None and first + 1 < len(result.calls):
        problems.append(f'asked again after speaking: {result.models}')
    # OpenRouter always gets the rules; settings never reach a request
    for index, call in enumerate(result.calls):
        if SETTING_KEYS & set(keys_deep(call.params)):
            problems.append(f'settings in request {index}: {call.params}')
        if privacy and call.model != result.main_model and rel.is_openrouter(call.service):
            provider = (call.params.get('extra_body') or {}).get('provider')
            if not isinstance(provider, dict) or provider.get('data_collection') not in ('deny', 'allow'):
                problems.append(f'OpenRouter fallback without no-retention rules: {call.params}')
    # every failure leaves a clear line
    nothing = not result.items
    if nothing and not (result.cancelled and result.escaped == 'CancelledError') and not warned(result):
        problems.append('a reply with nothing in it and no warning or error line')
    if result.elapsed > max_seconds:
        problems.append(f'took {result.elapsed:.2f}s, limit {max_seconds:.2f}s')
    if result.streams_open or result.clients_open or result.tasks_left:
        problems.append(f'left open: streams {result.streams_open}, clients {result.clients_open}, tasks {result.tasks_left}')
    if not result.lock_free:
        problems.append('generation lock still held')
    check(name, not problems, problems + [f'models={result.models}', f'items={result.items}', f'logs={result.logs[-6:]}'])
    return not problems


def err(status=None, body=None, kind=Error):
    return kind('x', status_code=status, body=body)

ST = 0.05  # stall limit used below
FAST = {'stall_seconds': ST}
RULES = {'ignore': ['Alibaba']}
CHAIN = {'fallback_models': ['backup/one'], 'openrouter_provider': RULES, **FAST}
TOOLS = [{'type': 'function', 'function': {'name': 'Wave'}}]
def bound(calls, extra=0.0):
    """Time one reply may take: per request the open wait, one stall wait and closing, plus the planned sleeps."""
    return calls * (2 * ST + rel.CLOSE_SECONDS) + extra + 0.5


# ================= 1. stalls at every position =================
BASE = [chunk('First, '), chunk(tool=tool_part('Wave', '{"a"')), usage_chunk(), chunk('second. '), chunk(tool=tool_part(None, ': 1}')), chunk(finish='stop')]
def spoken_before(events, position):
    return ''.join(text_of(e) or '' for e in events[:position])

SCRIPT.plan('main/model', Hang, text_reply('Back again.'))
r = run(client(reliability=CHAIN))
invariants('stall before headers: retried once', r, bound(2))
check('stall before headers: the retry answers', r.said == 'Back again.' and r.models == ['main/model'] * 2, r.models)

for position in range(len(BASE) + 1):
    events = BASE[:position] + [Hang] + BASE[position:]
    SCRIPT.plan('main/model', events, text_reply('Retry.')); SCRIPT.plan('backup/one', text_reply('Backup.'))
    r = run(client(reliability=CHAIN), TOOLS)
    before = spoken_before(events, position)
    invariants(f'stall at position {position}', r, bound(3))
    if before:
        check(f'stall at {position} after speaking: kept, ended, logged', r.said == before and r.models == ['main/model'] and logged(r, 'after it started speaking'), (r.said, r.models))
        check(f'stall at {position} after speaking: no half action', not any(kind == 'tool_calls' for kind, _ in r.items), r.items)
    else:
        check(f'stall at {position} before speaking: retried', r.said == 'Retry.' and r.models == ['main/model'] * 2, (r.said, r.models))

# stall on the retry too, at every position, then the fallback
for position in range(3):
    events = [usage_chunk(), chunk(tool=tool_part('Wave', '{'))][:position] + [Hang]
    SCRIPT.plan('main/model', events); SCRIPT.plan('backup/one', text_reply('Backup.'))
    r = run(client(reliability=CHAIN), TOOLS)
    invariants(f'stall twice at position {position}: fallback', r, bound(3))
    check(f'stall twice at position {position}: backup answers', r.said == 'Backup.' and r.models == ['main/model'] * 2 + ['backup/one'], r.models)

# mid-sentence: a stream that stalls inside a word
SCRIPT.plan('main/model', [chunk('I was go'), Hang]); SCRIPT.plan('backup/one', text_reply('Never.'))
r = run(client(reliability=CHAIN))
invariants('stall mid-word', r, bound(1))
check('stall mid-word: nothing repeated, no fallback', r.said == 'I was go' and r.models == ['main/model'], r.models)

# native: stalls at every position
def nstart(): return {'type': 'message_start', 'message': {'usage': {'input_tokens': 10, 'cache_read_input_tokens': 5}}}
def ntext(text): return {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}}
def ntool_start(index=1): return {'type': 'content_block_start', 'index': index, 'content_block': {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Wave'}}
def ntool_json(part, index=1): return {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'input_json_delta', 'partial_json': part}}
def nstop(index=1): return {'type': 'content_block_stop', 'index': index}
def nend(reason='end_turn'): return {'type': 'message_delta', 'delta': {'stop_reason': reason, 'stop_sequence': None}, 'usage': {'output_tokens': 7}}
NBASE = [nstart(), {'type': 'ping'}, ntext('Well, '), ntool_start(), ntool_json('{"a"'), ntext('then. '), ntool_json(': 1}'), nstop(), nend('tool_use')]
NCHAIN = {'fallback_models': [{'service': 'OpenRouter', 'model': 'backup/one'}], 'openrouter_provider': RULES, **FAST}
for position in range(len(NBASE) + 1):
    events = NBASE[:position] + [Hang] + NBASE[position:]
    SCRIPT.plan('claude-sonnet-5-5', events, [nstart(), ntext('Retry.'), nend()]); SCRIPT.plan('backup/one', text_reply('Backup.'))
    ext._registry = ext.Registry(); ext._registry.speaking = 'Lydia'
    r = run(client('Anthropic (Native, cached)', 'claude-sonnet-5-5', reliability=NCHAIN), TOOLS)
    before = spoken_before(events, position)
    invariants(f'native stall at position {position}', r, bound(3))
    if before:
        check(f'native stall at {position} after speaking: kept', r.said == before and len(r.calls) == 1, (r.said, r.models))
    else:
        check(f'native stall at {position} before speaking: retried', r.said == 'Retry.' and len(r.calls) == 2, (r.said, r.models))
    usage = ext._registry.api_for('t').usage_log()
    expected = (1 if position >= 1 else 0) + (0 if before else 1)  # each request whose message_start arrived
    check(f'native stall at {position}: usage logged per request that started', len(usage) == expected and all(e['npc'] == 'Lydia' for e in usage), usage)


# ================= 2. errors at every position =================
ERRORS = {'503': lambda: err(503), '429': lambda: err(429), 'overloaded in stream': lambda: err(200, {'type': 'error', 'error': {'type': 'overloaded_error'}}),
          'connection': lambda: APIConnectionError('connection lost'), 'read cut': lambda: ReadError('peer closed'),
          '400': lambda: err(400, {'error': {'code': 400, 'message': 'bad'}}), '401': lambda: err(401), 'own bug': lambda: ValueError('bad value')}
OUTAGES = {'503', '429', 'overloaded in stream', 'connection', 'read cut'}
for label, make in ERRORS.items():
    for position in range(len(BASE) + 1):
        events = BASE[:position] + [make()]
        SCRIPT.plan('main/model', events); SCRIPT.plan('backup/one', text_reply('Backup.'))
        r = run(client(reliability=CHAIN), TOOLS)
        before = spoken_before(events, position)
        ok = invariants(f'error {label} at position {position}', r, bound(2))
        if before:
            check(f'error {label} at {position} after speaking: kept, no fallback, logged as error', r.said == before and r.models == ['main/model'] and logged(r, 'LLM API'), (r.said, r.models, r.logs[-3:]))
        elif label in OUTAGES:
            check(f'error {label} at {position}: fallback answers', r.said == 'Backup.' and r.models == ['main/model', 'backup/one'], r.models)
        else:
            check(f'error {label} at {position}: not an outage, no fallback, logged', r.items == [] and r.models == ['main/model'] and logged(r, 'LLM API'), (r.models, r.logs[-3:]))
    # before headers
    SCRIPT.plan('main/model', make()); SCRIPT.plan('backup/one', text_reply('Backup.'))
    r = run(client(reliability=CHAIN), TOOLS)
    invariants(f'error {label} before headers', r, bound(2))
    check(f'error {label} before headers: fallback only for an outage', (r.said == 'Backup.') == (label in OUTAGES), r.models)

# a stream that reports a failure in a chunk (OpenRouter: an error block, finish_reason error) at every position
for position in range(len(BASE)):
    events = BASE[:position] + [chunk(finish='error', error={'code': 502, 'message': 'Provider returned error'})]
    SCRIPT.plan('main/model', events); SCRIPT.plan('backup/one', text_reply('Backup.'))
    r = run(client(reliability=CHAIN), TOOLS)
    before = spoken_before(events, position)
    invariants(f'error chunk at position {position}', r, bound(4))
    if before:
        check(f'error chunk at {position} after speaking: kept, no fallback', r.said == before and r.models == ['main/model'], r.models)
        check(f'error chunk at {position} after speaking: the cut is logged', any('error' in line.lower() or 'failure' in line.lower() for line in warned(r)), r.logs)
    else:
        check(f'error chunk at {position} before speaking: no half action used', not any(kind == 'tool_calls' for kind, _ in r.items), r.items)
        check(f'error chunk at {position} before speaking: retried, then fallback', r.said == 'Backup.' and r.models[-1] == 'backup/one', r.models)

# native: errors at every position
for label in ('overloaded in stream', '400', 'read cut'):
    for position in range(len(NBASE) + 1):
        events = NBASE[:position] + [ERRORS[label]()]
        SCRIPT.plan('claude-sonnet-5-5', events); SCRIPT.plan('backup/one', text_reply('Backup.'))
        ext._registry = ext.Registry(); ext._registry.speaking = 'Lydia'
        r = run(client('Anthropic (Native, cached)', 'claude-sonnet-5-5', reliability=NCHAIN), TOOLS)
        before = spoken_before(events, position)
        invariants(f'native error {label} at position {position}', r, bound(2))
        if before:
            check(f'native error {label} at {position} after speaking: kept, logged', r.said == before and len(r.calls) == 1 and logged(r, 'LLM API'), r.models)
        elif label in OUTAGES:
            check(f'native error {label} at {position}: fallback answers', r.said == 'Backup.', r.models)
        usage = ext._registry.api_for('t').usage_log()
        if position >= 1:
            check(f'native error {label} at {position}: the started request is in the usage log', len(usage) == 1 and usage[0]['npc'] == 'Lydia', usage)


# ================= 3. cancelled from outside =================
rng = random.Random(SEED)
SHAPES = {
    'slow text': lambda: [chunk('One. '), Sleep(0.02), chunk('Two. '), Sleep(0.02), chunk('Three.'), chunk(finish='stop')],
    'hang': lambda: [chunk(tool=tool_part()), Hang],
    'hang before headers': lambda: Hang,
    'slow open': lambda: Sleep(0.03),
}
for i in range(60):
    shape = rng.choice(list(SHAPES))
    use_native = rng.random() < 0.3
    delay = rng.uniform(0, 0.12)
    if use_native:
        plan = [nstart(), Sleep(0.02), ntext('Native one. '), Sleep(0.02), ntext('Two.'), nend()] if shape == 'slow text' else SHAPES[shape]()
        SCRIPT.plan('claude-sonnet-5-5', plan, [nstart(), ntext('Retry.'), nend()])
        the_client = client('Anthropic (Native, cached)', 'claude-sonnet-5-5', reliability=NCHAIN)
    else:
        SCRIPT.plan('main/model', SHAPES[shape](), text_reply('Retry.'))
        the_client = client(reliability=CHAIN)
    SCRIPT.plan('backup/one', [chunk('Backup '), Sleep(0.03), chunk('line.'), chunk(finish='stop')])
    r = run(the_client, TOOLS, cancel_after=delay)
    invariants(f'cancel {i} ({shape}, native={use_native}, after {delay:.3f}s)', r, bound(4, 0.2), allow_cancel=True)
    # the same client works afterwards
    SCRIPT.plan('main/model', text_reply('Next.')); SCRIPT.plan('claude-sonnet-5-5', [nstart(), ntext('Next.'), nend()])
    r2 = run(the_client)
    check(f'cancel {i}: the next reply works', r2.said == 'Next.' and r2.escaped is None, (r2.said, r2.escaped))


# ================= 4. every fallback list shape =================
SHAPES_OF_LIST = {
    'empty list': [],
    'not a list': 'backup/one',
    'a single dict': {'service': 'OpenRouter', 'model': 'backup/one'},
    'None': None,
    'number': 5,
    'junk entries': [None, 5, '', '   ', {}, {'service': 'OpenRouter'}, {'model': ''}, {'model': 7}, {'service': 9, 'model': 'x'}, ['backup/one'], True, 'backup/one'],
    'duplicates': ['backup/one', 'backup/one', 'backup/one'],
    'same as the main model': ['main/model', 'backup/one'],
    'unknown service': [{'service': 'NoSuchService', 'model': 'x/y'}, 'backup/one'],
    'OpenRouter as a URL with a slash': [{'service': 'https://openrouter.ai/api/v1/', 'model': 'backup/one'}],
    'OpenRouter in capitals': [{'service': 'OPENROUTER', 'model': 'backup/one'}],
    'Open Router with a space': [{'service': ' Open Router ', 'model': 'backup/one'}],
    'OpenRouter URL in capitals': [{'service': 'HTTPS://OPENROUTER.AI/API/V1', 'model': 'backup/one'}],
    'OpenRouter URL with a port': [{'service': 'https://openrouter.ai:443/api/v1', 'model': 'backup/one'}],
    'OpenRouter URL with a trailing dot': [{'service': 'https://openrouter.ai./api/v1', 'model': 'backup/one'}],
    'OpenRouter subdomain': [{'service': 'https://eu.openrouter.ai/api/v1', 'model': 'backup/one'}],
    'local fallback': [{'service': 'KoboldCpp', 'model': 'backup/one'}],
    'Anthropic fallback': [{'service': 'Anthropic', 'model': 'backup/one'}],
}
for address in ('https://openrouter.ai./api/v1', 'https://OPENROUTER.AI../api/v1', 'https://eu.openrouter.ai./x', 'https://openrouter.ai:443/api/v1/'):
    check(f'{address} is OpenRouter', rel.is_openrouter(address))
for address in ('https://openrouter.ai.evil.example/v1', 'https://notopenrouter.ai/v1', 'https://openrouter.ai@evil.example/v1', 'openrouter.ai/api/v1', 'http://[::1/'):
    check(f'{address} is not OpenRouter', not rel.is_openrouter(address))
for shape, entries in SHAPES_OF_LIST.items():
    for rules in (None, RULES):
        reliability = {'fallback_models': entries, **FAST}
        if rules:
            reliability['openrouter_provider'] = rules
        SCRIPT.plan('main/model', err(503)); SCRIPT.plan('backup/one', text_reply('Backup.')); SCRIPT.plan('x/y', text_reply('Unknown.'))
        try:
            the_client = client(reliability=reliability)
            made = True
        except Exception as e:
            made = e
        check(f'list {shape} (rules={bool(rules)}): the client is made', made is True, made)
        if made is not True:
            continue
        r = run(the_client)
        invariants(f'list {shape} (rules={bool(rules)})', r, bound(6))
        or_calls = [c for c in r.calls[1:] if rel.is_openrouter(c.service) or 'openrouter' in c.service]
        for c in or_calls:
            provider = (c.params.get('extra_body') or {}).get('provider')
            check(f'list {shape} (rules={bool(rules)}): OpenRouter request has the rules', isinstance(provider, dict) and provider.get('data_collection') == 'deny', c.params)
        if not rules:
            check(f'list {shape} without rules: no unruled OpenRouter request', not or_calls, [(c.service, c.params) for c in r.calls])
        check(f'list {shape} (rules={bool(rules)}): at most one fallback request per usable entry', len(r.calls) <= 1 + 12, r.models)


# ================= 5. reliability blocks with wrong types =================
BAD_BLOCKS = [None, 'on', 5, [], ['stall_rescue'], True, {'stall_rescue': 'yes'}, {'stall_rescue': 1}, {'stall_seconds': '15'},
              {'stall_seconds': None}, {'stall_seconds': []}, {'stall_seconds': float('nan')}, {'stall_seconds': float('inf')},
              {'stall_seconds': -0.0}, {'stall_seconds': 1e-12}, {'stall_seconds': 1e300},
              {'openrouter_provider': 'deny'}, {'openrouter_provider': ['x']}, {'openrouter_provider': None},
              {'openrouter_provider': {'data_collection': None}, 'fallback_models': ['backup/one']},
              {'openrouter_provider': {'data_collection': 'Allow '}, 'fallback_models': ['backup/one']},
              {'openrouter_provider': {'data_collection': 1}, 'fallback_models': ['backup/one']},
              {'openrouter_provider': {'data_collection': 'DENY'}, 'fallback_models': ['backup/one']},
              {'unknown': 1}, {'fallback_models': {'model': None}}]
for block in BAD_BLOCKS:
    records.clear()
    try:
        params, settings = rel.split_settings({'max_tokens': 5, 'reliability': block})
        ok = params == {'max_tokens': 5} and isinstance(settings['stall_seconds'], float) and settings['stall_seconds'] > 0 \
             and settings['stall_seconds'] == settings['stall_seconds']  # inf is allowed: no limit in practice
    except Exception as e:
        ok, settings = False, e
    check(f'bad block {block!r}: read without error, a usable stall limit', ok, settings)
    the_client = client(reliability=block)
    SCRIPT.plan('main/model', err(503)); SCRIPT.plan('backup/one', text_reply('Backup.'))
    r = run(the_client)
    invariants(f'bad block {block!r}: a reply', r, bound(3))
SCRIPT.plan('main/model', text_reply('Fine.'))
check('a tiny stall limit still lets a quick stream through', run(client(reliability={'stall_seconds': 0.001, 'fallback_models': []})).escaped is None)
# a data_collection value that is not "allow" or "deny" must not open retention
for value in (None, 1, '', ['deny'], 'maybe', True):
    rules = rel.provider_rules({'openrouter_provider': {'data_collection': value}}, None)
    check(f'data_collection {value!r}: sent as deny', rules['data_collection'] == 'deny', rules)
for value, sent in (('allow', 'allow'), ('deny', 'deny'), ('Allow ', 'allow'), ('DENY', 'deny')):
    check(f'data_collection {value!r}: sent as {sent}', rel.provider_rules({'openrouter_provider': {'data_collection': value}}, None)['data_collection'] == sent)
check('data_collection from extra_body checked the same way', rel.provider_rules({'openrouter_provider': None}, {'extra_body': {'provider': {'data_collection': None}}})['data_collection'] == 'deny')


# ================= 6. very fast and very slow streams =================
many = [chunk(f'w{i} ') for i in range(5000)] + [chunk(finish='stop')]
SCRIPT.plan('main/model', many)
r = run(client(reliability=FAST))
invariants('5000 chunks at once', r, 10)
check('5000 chunks at once: every word, in order', r.said == ''.join(f'w{i} ' for i in range(5000)) and len(r.calls) == 1, len(r.said))

drip = []
for i in range(15):
    drip += [Sleep(0.15), chunk(f'd{i} ')]  # Windows timers tick every ~16 ms, so the margin is kept wide
SCRIPT.plan('main/model', drip + [chunk(finish='stop')])
r = run(client(reliability={'stall_seconds': 0.25}))
invariants('slow but steady (each chunk just inside the limit)', r, 15 * 0.15 + 1.5)
check('slow but steady: not counted as a stall', r.said == ''.join(f'd{i} ' for i in range(15)) and len(r.calls) == 1, (r.said, r.models, r.logs))

SCRIPT.plan('main/model', Sleep(ST * 3), text_reply('Second.'))
r = run(client(reliability=FAST))
invariants('slow first byte over the limit', r, bound(2, ST * 3))
check('slow first byte: retried', r.said == 'Second.' and len(r.calls) == 2, r.models)

SCRIPT.plan('main/model', [chunk('One. '), chunk('Two. '), chunk('Three.'), chunk(finish='stop')])
r = run(client(reliability=FAST), consumer_delay=ST * 2)
invariants('slow voice generation between sentences', r, 2)
check('slow voice generation is not a stall', r.said == 'One. Two. Three.' and len(r.calls) == 1, r.models)

native_many = [nstart()] + [ntext(f'n{i} ') for i in range(3000)] + [nend()]
SCRIPT.plan('claude-sonnet-5-5', native_many)
r = run(client('Anthropic (Native, cached)', 'claude-sonnet-5-5', reliability=FAST))
invariants('native 3000 events at once', r, 10)
check('native 3000 events: every word', r.said == ''.join(f'n{i} ' for i in range(3000)), len(r.said))

# a client that will not close does not hold the reply up for long
FakeOpenAI.close_hangs = True
SCRIPT.plan('main/model', Hang, text_reply('Closed late.'))
r = run(client(reliability=FAST))
FakeOpenAI.close_hangs = False
invariants('a client that will not close', r, bound(2))
check('a client that will not close: the reply goes on', r.said == 'Closed late.', r.models)


# ================= 7. many replies in a row, random services =================
def random_events(rng, native_kind, label):
    events = []
    if native_kind:
        events.append(nstart())
    for i in range(rng.randint(0, 6)):
        roll = rng.random()
        if roll < 0.35:
            events.append(ntext(f'{label}-{i} ') if native_kind else chunk(f'{label}-{i} '))
        elif roll < 0.5:
            events.append(Sleep(rng.uniform(0, ST * 0.5)))
        elif roll < 0.6:
            events.append({'type': 'ping'} if native_kind else usage_chunk())
        elif roll < 0.7:
            events += [ntool_start(), ntool_json('{"a": ')] if native_kind else [chunk(tool=tool_part('Wave', '{"a": '))]
        elif roll < 0.75:
            events.append(Hang)
        elif roll < 0.85:
            events.append(rng.choice([err(503), err(429), err(400), APIConnectionError('lost'), ReadError('cut'), ValueError('x')]))
        elif roll < 0.9 and not native_kind:
            events.append(chunk(finish='error', error={'code': rng.choice([400, 429, 502])}))
    if rng.random() < 0.7:
        events.append(nend(rng.choice(['end_turn', 'end_turn', 'refusal', 'max_tokens'])) if native_kind else chunk(finish=rng.choice(['stop', 'stop', 'content_filter', 'length'])))
    return events

def random_answer(rng, native_kind, label):
    roll = rng.random()
    if roll < 0.08:
        return Hang
    if roll < 0.18:
        return rng.choice([err(503), err(429), err(400), err(401), APIConnectionError('lost')])
    return random_events(rng, native_kind, label)

rng = random.Random(SEED + 1)
SEQ_CLIENTS = [client(reliability={'fallback_models': ['backup/one', {'service': 'Anthropic (Native, cached)', 'model': 'claude-sonnet-5-5'}, 'backup/two'], 'openrouter_provider': RULES, **FAST}),
               client('Anthropic (Native, cached)', 'claude-sonnet-5-5', reliability={'fallback_models': [{'service': 'OpenRouter', 'model': 'backup/one'}, {'service': 'openrouter', 'model': 'backup/two'}], **FAST},
                      params={'max_tokens': 300, 'extra_body': {'provider': {'ignore': ['Alibaba']}, 'thinking': {'type': 'disabled'}}}),
               client(reliability={'fallback_models': ['backup/one'], **FAST}),  # no rules: OpenRouter fallbacks refused
               client(reliability={'stall_seconds': ST})]
total_calls = 0
broken = 0
for i in range(400):
    the_client = rng.choice(SEQ_CLIENTS)
    for model in ('main/model', 'claude-sonnet-5-5', 'backup/one', 'backup/two'):
        SCRIPT.plan(model, *[random_answer(rng, model == 'claude-sonnet-5-5', f'{model}#{n}') for n in range(5)])
    ext._registry = ext.Registry(); ext._registry.speaking = f'NPC {i}'
    r = run(the_client, TOOLS if rng.random() < 0.5 else None)
    total_calls += len(r.calls)
    links = 1 + len(the_client._reliability['fallback_models'])
    sleeps = 0.0
    if not invariants(f'reply {i}', r, bound(len(r.calls), ST * 3 * len(r.calls)) ):
        broken += 1
    if len(r.calls) > 4 * links:
        check(f'reply {i}: requests bounded by 4 per model', False, r.models)
    for entry in ext._registry.api_for('t').usage_log():
        if entry['npc'] != f'NPC {i}':
            check(f'reply {i}: usage entry names this talk', False, entry)
check('400 replies in a row: none broke a rule', broken == 0, broken)
print(f'(400 replies made {total_calls} requests)')


# ================= 8. a partial tool call ended by a reported failure is never used =================
SCRIPT.plan('main/model', [chunk(tool=tool_part('Wave', '{"a"')), chunk(finish='error', error={'code': 502})]); SCRIPT.plan('backup/one', [chunk(tool=tool_part('Wave', '{}')), chunk(finish='tool_calls')])
r = run(client(reliability=CHAIN), TOOLS)
invariants('half action then an error chunk', r, bound(4))
calls = [data for kind, data in r.items if kind == 'tool_calls']
check('half action then an error chunk: only a whole action is used', calls and all(c[0]['function']['arguments'] == '{}' for c in calls), r.items)


# ================= 8b. the reader stops early (the player interrupts) =================
for use_native, close in ((False, True), (False, False), (True, True), (True, False)):
    COUNT.reset(); SCRIPT.calls.clear(); SCRIPT.spoken.clear()
    SCRIPT.plan('main/model', [chunk('One. '), Sleep(0.01), chunk('Two. '), Hang])
    SCRIPT.plan('claude-sonnet-5-5', [nstart(), ntext('One. '), Sleep(0.01), ntext('Two. '), Hang])
    the_client = client('Anthropic (Native, cached)', 'claude-sonnet-5-5', reliability=FAST) if use_native else client(reliability=FAST)
    async def stop_early():
        stream = the_client.streaming_call(Thread(), False)
        async for item in stream:
            break
        if close:
            await stream.aclose()
        del stream
        for _ in range(5):
            await asyncio.sleep(0)
        return COUNT.streams_open, COUNT.clients_open, the_client._generation_lock.locked()
    left = asyncio.run(stop_early())
    check(f'reader stops early (native={use_native}, closed={close}): nothing left open, lock free', left == (0, 0, False), left)


# ================= 9. the usage log =================
reg = ext.Registry(); reg.speaking = 'Lydia, Faendal'
reg.record_usage({'model': 'm', 'output': 1})
reg.record_usage({'model': 'm', 'npc': 'kept'})
for n in range(ext.USAGE_LOG_SIZE + 50):
    reg.record_usage({'n': n})
log_now = reg.api_for('t').usage_log()
check('usage log keeps its size limit', len(log_now) == ext.USAGE_LOG_SIZE and log_now[-1]['n'] == ext.USAGE_LOG_SIZE + 49)
check('usage log copy cannot change the log', (log_now.clear() or True) and len(reg.api_for('t').usage_log()) == ext.USAGE_LOG_SIZE)


print('RESULT: ' + ('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS[:40]}'))
sys.exit(1 if FAILS else 0)
