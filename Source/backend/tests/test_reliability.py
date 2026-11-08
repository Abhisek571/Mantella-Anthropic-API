"""Offline tests for src/llm/reliability.py and the rescue paths of ClientBase.streaming_call, with fake clients and
no network. Mantella's own modules and the SDKs are stubbed; client_base.py and reliability.py are the real files.
Run with a Python 3.11: test_reliability.py <repo Source/backend dir>"""
import asyncio, importlib.util, logging, sys, time, types
from pathlib import Path
from threading import Lock
from types import SimpleNamespace as NS

BACKEND = Path(sys.argv[1])

records = []
class Collect(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())
log = logging.getLogger('mantella'); log.addHandler(Collect()); log.setLevel(logging.DEBUG)

FAILS = []
def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (('  -> ' + str(detail)) if not cond else ''))
    if not cond:
        FAILS.append(name)


# ---------- stubs ----------
OPENROUTER, NATIVE, OFFICIAL, LOCAL = 'https://openrouter.ai/api/v1', 'https://api.anthropic.com', 'https://api.anthropic.com/v1', 'http://127.0.0.1:5001/v1'
SERVICES = {'openrouter': OPENROUTER, 'anthropic(native,cached)': NATIVE, 'anthropic': OFFICIAL, 'koboldcpp': LOCAL, 'openai': 'https://api.openai.com/v1'}
sounds = []
utils = types.ModuleType('src.utils')
utils.get_logger = lambda: log
utils.time_it = lambda fn: fn
utils.play_error_sound = lambda: sounds.append(1)
utils.resolve_service_endpoint = lambda value: SERVICES.get(value.strip().lower().replace(' ', ''), value)
utils.is_local_url = lambda url: url.startswith('http://127.')
utils.is_anthropic_endpoint = lambda url: url in (OFFICIAL, NATIVE)
utils.ANTHROPIC_NATIVE_ENDPOINT, utils.ANTHROPIC_ENDPOINT, utils.PLAYER2_ENDPOINT = NATIVE, OFFICIAL, 'https://api.player2.game/v1'
utils.get_model_token_limits = lambda: {}
utils.resolve_path = lambda: str(BACKEND / 'no' / 'such' / 'folder')

class Error(Exception):
    def __init__(self, message='', status_code=None, body=None):
        super().__init__(message); self.status_code, self.body = status_code, body
class APIConnectionError(Error): pass
class APITimeoutError(APIConnectionError): pass
class BadRequestError(Error):
    type = 'invalid_request_error'
class RateLimitError(Error): pass

def module(name, **attrs):
    made = types.ModuleType(name); made.__dict__.update(attrs); sys.modules[name] = made
    return made
class Base:
    def __init__(self, *a, **k): pass
class Span:
    def __enter__(self): return self
    def __exit__(self, *a): return False
class Text:
    pass
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


# ---------- fake clients: each model answers from a script, one entry per request ----------
class Hang:
    """Never answers when it is the next thing in a stream."""
class Script:
    def __init__(self): self.calls = []; self.plans = {}
    def plan(self, model, *answers): self.plans[model] = list(answers)
    def next(self, model): return self.plans[model].pop(0) if len(self.plans.get(model, [])) > 1 else self.plans[model][0]
SCRIPT = Script()

def chunk(text=None, finish=None, tool=None, native_finish=None, refusal=None, error=None):
    delta = NS(content=text, tool_calls=[tool] if tool else None, refusal=refusal)
    return NS(choices=[NS(finish_reason=finish, delta=delta, native_finish_reason=native_finish)], error=error)
def tool_part(name='Wave', arguments='{}'):
    return NS(index=0, id='call_1', function=NS(name=name, arguments=arguments))
def text_reply(text='Hello.'):
    return [chunk(text), chunk(finish='stop')]

async def play(events):
    for event in events:
        if event is Hang:
            await asyncio.sleep(60)
        if isinstance(event, Exception):
            raise event
        yield event

class FakeOpenAI:
    closed = 0
    def __init__(self, label): self.chat = NS(completions=NS(create=self.create)); self.label = label
    async def create(self, model, messages, stream, **params):
        SCRIPT.calls.append(NS(model=model, messages=messages, params=params, service=self.label))
        answer = SCRIPT.next(model)
        if isinstance(answer, Exception):
            raise answer
        if answer is Hang:
            await asyncio.sleep(60)
        return play(answer)
    async def close(self): FakeOpenAI.closed += 1

class FakeNative:
    def __init__(self): self.messages = NS(create=self.create)
    async def create(self, stream, **request):
        SCRIPT.calls.append(NS(model=request['model'], request=request, params=request, service='native'))
        answer = SCRIPT.next(request['model'])
        if isinstance(answer, Exception):
            raise answer
        if answer is Hang:
            await asyncio.sleep(60)
        return play(answer)
    async def close(self): pass

ClientBase.generate_async_client = lambda self: FakeOpenAI(self._base_url)
ClientBase.generate_native_client = lambda self, is_async: FakeNative()
keys = {OPENROUTER: 'test-or', NATIVE: 'test-an', OFFICIAL: 'test-an', 'https://api.openai.com/v1': None}
ClientBase._get_api_key = staticmethod(lambda service, show_error=True: keys.get(ClientBase._get_endpoint(service)))

class Thread:
    def get_openai_messages(self): return [{'role': 'system', 'content': 'You are Lydia.'}, {'role': 'user', 'content': 'Hello'}]
    def get_last_message(self): return None

def client(service='OpenRouter', model='main/model', reliability=None, params=None):
    llm_params = dict(params or {'max_tokens': 300})
    if reliability is not None:
        llm_params['reliability'] = reliability
    return ClientBase(service, model, llm_params, 16000)

def run(the_client, tools=None):
    async def collect():
        return [item async for item in the_client.streaming_call(Thread(), False, tools)]
    SCRIPT.calls.clear(); records.clear(); sounds.clear()
    return asyncio.run(collect())
def said(items): return ''.join(data for kind, data in items if kind == 'content')
def logged(text): return any(text in line for line in records)
def models(): return [call.model for call in SCRIPT.calls]
FAST = {'stall_seconds': 0.05}


# ---------- settings ----------
params, settings = rel.split_settings({'max_tokens': 1, 'reliability': {'stall_rescue': False, 'stall_seconds': 20, 'fallback_models': ['a', {'service': 'OpenRouter', 'model': 'b'}, 7, {'service': 'x'}]}})
check('settings removed from the request', params == {'max_tokens': 1}, params)
check('settings read', settings['stall_rescue'] is False and settings['stall_seconds'] == 20.0)
check('fallback list read, bad entries left out', settings['fallback_models'] == [{'service': None, 'model': 'a'}, {'service': 'OpenRouter', 'model': 'b'}], settings['fallback_models'])
same = {'max_tokens': 1}
check('no settings: parameters untouched', rel.split_settings(same)[0] is same and rel.split_settings(None)[0] is None)
records.clear()
check('bad block: defaults, logged', rel.split_settings({'reliability': 'yes'})[1]['stall_seconds'] == 15.0 and logged('should be a'), records)
for bad in (0, -1, 'soon', True):
    check(f'bad stall_seconds {bad!r}: default', rel.split_settings({'reliability': {'stall_seconds': bad}})[1]['stall_seconds'] == 15.0)
default = rel.split_settings(None)[1]
check('stall rescue on by default, no fallback', rel.stall_limit(default, False) == 15.0 and default['fallback_models'] == [])
check('off by default for a local model', rel.stall_limit(default, True) == 0)
check('local model can opt in', rel.stall_limit(rel.split_settings({'reliability': {'stall_rescue': True}})[1], True) == 15.0)
check('can be turned off', rel.stall_limit(rel.split_settings({'reliability': {'stall_rescue': False}})[1], False) == 0)

# ---------- the user's rules on a fallback model ----------
USER = {'max_tokens': 500, 'stop': ['#'], 'extra_body': {'provider': {'allow_fallbacks': True, 'ignore': ['Alibaba'], 'data_collection': 'deny'}, 'reasoning': {'enabled': False}}}
rules = rel.provider_rules(default, USER)
check('rules taken from the LLM Parameters', rules == USER['extra_body']['provider'], rules)
to_or = rel.fallback_params(USER, OPENROUTER, rules)
check('OpenRouter keeps ignore, no retention, fallbacks, reasoning off', to_or['extra_body'] == USER['extra_body'], to_or)
check('own rules win over the LLM Parameters', rel.provider_rules({'openrouter_provider': {'ignore': ['Z']}}, USER) == {'ignore': ['Z'], 'data_collection': 'deny', 'allow_fallbacks': True})
check('retention refused unless the rules say otherwise', rel.provider_rules({'openrouter_provider': {}}, None)['data_collection'] == 'deny')
check('rules can opt out of that themselves', rel.provider_rules({'openrouter_provider': {'data_collection': 'allow'}}, None)['data_collection'] == 'allow')
check('no rules anywhere: none invented', rel.provider_rules(default, {'max_tokens': 1}) is None)
thinking_off = rel.fallback_params({'extra_body': {'thinking': {'type': 'disabled'}}}, OPENROUTER, {'ignore': ['Z']})
check('thinking off becomes reasoning off on OpenRouter', thinking_off['extra_body'] == {'provider': {'ignore': ['Z']}, 'reasoning': {'enabled': False}}, thinking_off)
slash = rel.fallback_params({'max_tokens': 1}, OPENROUTER + '/', {'ignore': ['Z']})
check('OpenRouter written with a trailing slash still gets the rules', slash.get('extra_body', {}).get('provider') == {'ignore': ['Z']}, slash)
check('OpenRouter by host only', rel.is_openrouter('https://openrouter.ai/api/v1/') and not rel.is_openrouter('https://api.openai.com/v1') and not rel.is_openrouter('https://openrouter.ai.evil.example/v1'))
other = rel.fallback_params(USER, 'https://api.openai.com/v1', rules)
check('another service gets neither OpenRouter setting', 'extra_body' not in other and other['max_tokens'] == 500, other)
check('original parameters not changed', USER['extra_body']['provider']['ignore'] == ['Alibaba'] and 'reasoning' in USER['extra_body'])

# ---------- reading the service's answer ----------
def err(status=None, body=None, kind=Error):
    return kind('x', status_code=status, body=body)
for status in (408, 429, 500, 502, 503, 529):
    check(f'status {status} is an outage', rel.is_outage(err(status)))
for status in (400, 401, 402, 403, 404, 413, 422):
    check(f'status {status} is not', not rel.is_outage(err(status)))
check('connection lost is an outage', rel.is_outage(APIConnectionError('x')) and rel.is_outage(APITimeoutError('x')))
class TransportError(Exception): pass
class ReadError(TransportError): pass
check('connection cut inside a stream is an outage', rel.is_outage(ReadError('x')))
check('Anthropic overload inside a stream (status 200)', rel.is_outage(err(200, {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'x'}})))
check('OpenRouter rate limit inside a stream', rel.is_outage(err(None, {'code': 429, 'message': 'x', 'metadata': {'error_type': 'rate_limit_exceeded'}})))
check('OpenRouter bad request inside a stream', not rel.is_outage(err(None, {'code': 400, 'message': 'x'})))
check('a stream error block works too', rel.is_outage({'code': 502}) and not rel.is_outage({'code': 403}))
check('unknown error is not', not rel.is_outage(ValueError('x')))
def kind(**fields):
    attempt = rel.Attempt(); attempt.__dict__.update(fields)
    return rel.empty_reply_kind(attempt)
for reason in ('refusal', 'content_filter', 'length', 'max_tokens', 'model_context_window_exceeded', 'CONTENT_FILTER'):
    check(f'{reason}: no retry', kind(finish=reason) == 'final')
for reason in (None, 'stop', 'end_turn', 'tool_calls', 'stop_sequence'):
    check(f'{reason}: retried', kind(finish=reason) == 'transient')
check('native reason SAFETY behind stop: no retry', kind(finish='stop', native_finish='SAFETY') == 'final')
check('refusal text: no retry', kind(finish='stop', refusal=True) == 'final')
check('finish_reason error: outage', kind(finish='error') == 'outage')
check('error block with 429: outage', kind(finish='error', error={'code': 429}) == 'outage')
check('error block with 400: no retry', kind(finish='error', error={'code': 400}) == 'final')


# ---------- stall rescue ----------
SCRIPT.plan('main/model', Hang, text_reply('Back again.'))
items = run(client(reliability=FAST))
check('stall: cancelled and retried once', said(items) == 'Back again.' and len(SCRIPT.calls) == 2, items)
check('stall: clear log lines', logged('Stall rescue: main/model sent no data for 0.05 seconds') and logged('the retry succeeded'), records)
check('stall: no error sound on a rescue', not sounds)

SCRIPT.plan('main/model', [Hang], text_reply('Back again.'))
items = run(client(reliability=FAST))
check('stall before the first chunk of an open stream: retried', said(items) == 'Back again.' and len(SCRIPT.calls) == 2, items)

SCRIPT.plan('main/model', Hang)
began = time.monotonic()
items = run(client(reliability=FAST))
check('stall twice: only one retry, then a clear error', items == [] and len(SCRIPT.calls) == 2 and logged('also on its retry') and any('also on its retry' in line and 'LLM API' in line for line in records), records)
check('stall twice: error sound, and quick', sounds and time.monotonic() - began < 5)

SCRIPT.plan('main/model', [chunk('I think '), Hang], text_reply('Never.'))
items = run(client(reliability=FAST))
check('stall after speaking: text kept, not asked again', said(items) == 'I think ' and len(SCRIPT.calls) == 1, items)
check('stall after speaking: logged', logged('after it started speaking'), records)

SCRIPT.plan('main/model', [chunk(tool=tool_part('Wave', '{"a"')), Hang], [chunk(tool=tool_part()), chunk(finish='tool_calls')])
items = run(client(reliability=FAST), [{'type': 'function', 'function': {'name': 'Wave'}}])
check('half a tool call before a stall is not used', len(items) == 1 and items[0][0] == 'tool_calls' and items[0][1][0]['function']['arguments'] == '{}', items)

SCRIPT.plan('main/model', [chunk('Slow '), chunk('but '), chunk('steady.'), chunk(finish='stop')])
items = run(client(reliability=FAST))
check('a normal reply is left alone', said(items) == 'Slow but steady.' and len(SCRIPT.calls) == 1 and not logged('Stall rescue'), items)

async def slow_consumer():
    SCRIPT.calls.clear()
    SCRIPT.plan('main/model', [chunk('One. '), chunk('Two.'), chunk(finish='stop')])
    got = []
    async for item in client(reliability={'stall_seconds': 0.05}).streaming_call(Thread(), False):
        got.append(item)
        time.sleep(0.1)  # voice generation blocks between sentences
    return got
items = asyncio.run(slow_consumer())
check('time spent on a voice line is not a stall', said(items) == 'One. Two.' and len(SCRIPT.calls) == 1, items)

off = client(reliability={'stall_rescue': False})
check('rescue off: no limit', off._stall_seconds == 0)
check('rescue on by default', client()._stall_seconds == 15.0)
check('rescue off by default on a local model', client('KoboldCpp', 'local')._stall_seconds == 0)
SCRIPT.plan('main/model', text_reply())
check('rescue off: a reply still works', said(run(off)) == 'Hello.' and len(SCRIPT.calls) == 1)

def native_events(*texts, stop='end_turn'):
    events = [{'type': 'message_start', 'message': {'usage': {'input_tokens': 10, 'cache_read_input_tokens': 5, 'cache_creation_input_tokens': 0}}}]
    for text in texts:
        events.append({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}})
    return events + [{'type': 'message_delta', 'delta': {'stop_reason': stop, 'stop_sequence': None}, 'usage': {'output_tokens': 7}}]
SCRIPT.plan('claude-sonnet-5-5', Hang, native_events('Well met.'))
items = run(client('Anthropic (Native, cached)', 'claude-sonnet-5-5', reliability=FAST))
check('native: stall retried', said(items) == 'Well met.' and len(SCRIPT.calls) == 2, items)
SCRIPT.plan('claude-sonnet-5-5', [{'type': 'message_start', 'message': {'usage': {'input_tokens': 1}}}, {'type': 'ping'}, Hang], native_events('Again.'))
items = run(client('Anthropic (Native, cached)', 'claude-sonnet-5-5', reliability=FAST))
check('native: stall with no text yet retried', said(items) == 'Again.' and len(SCRIPT.calls) == 2, items)
SCRIPT.plan('claude-sonnet-5-5', [{'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Well, '}}, Hang], native_events('Never.'))
items = run(client('Anthropic (Native, cached)', 'claude-sonnet-5-5', reliability=FAST))
check('native: stall after speaking not retried', said(items) == 'Well, ' and len(SCRIPT.calls) == 1, items)


# ---------- smart empty retry ----------
EMPTY = {'stop': [chunk(finish='stop')], 'filter': [chunk(finish='content_filter')], 'length': [chunk(finish='length')], 'cut': []}
SCRIPT.plan('main/model', EMPTY['stop'], EMPTY['cut'], text_reply('At last.'))
items = run(client())
check('transient empties retried up to twice', said(items) == 'At last.' and len(SCRIPT.calls) == 3 and logged('retry 2 succeeded'), (items, records))
SCRIPT.plan('main/model', EMPTY['stop'])
items = run(client())
check('transient: gives up after 2 retries with a line', items == [] and len(SCRIPT.calls) == 3 and logged('still nothing after 2 retries'), records)
for name, reason in (('filter', 'content_filter'), ('length', 'length')):
    SCRIPT.plan('main/model', EMPTY[name], text_reply('Never.'))
    items = run(client())
    check(f'{reason}: sent once', items == [] and len(SCRIPT.calls) == 1, models())
    check(f'{reason}: line says why', logged('not retrying') and logged(f'finish_reason={reason}'), records)
SCRIPT.plan('main/model', [chunk(native_finish='SAFETY', finish='stop')], text_reply('Never.'))
check('blocked by the upstream model: sent once', run(client()) == [] and len(SCRIPT.calls) == 1)
SCRIPT.plan('main/model', [chunk(refusal='No.'), chunk(finish='stop')], text_reply('Never.'))
check('refusal text: sent once', run(client()) == [] and len(SCRIPT.calls) == 1)
SCRIPT.plan('main/model', err(400), text_reply('Never.'))
check('bad request: sent once, logged', run(client()) == [] and len(SCRIPT.calls) == 1 and logged('LLM API Streaming Error') and sounds, records)
SCRIPT.plan('claude-sonnet-5-5', native_events(stop='refusal'), native_events('Never.'))
items = run(client('Anthropic (Native, cached)', 'claude-sonnet-5-5'))
check('native refusal: sent once', items == [] and len(SCRIPT.calls) == 1 and logged('stop_reason=refusal'), records)
SCRIPT.plan('claude-sonnet-5-5', native_events(stop='max_tokens'), native_events('Never.'))
check('native max_tokens: sent once', run(client('Anthropic (Native, cached)', 'claude-sonnet-5-5')) == [] and len(SCRIPT.calls) == 1)
SCRIPT.plan('claude-sonnet-5-5', native_events(), native_events('Second try.'))
items = run(client('Anthropic (Native, cached)', 'claude-sonnet-5-5'))
check('native empty end_turn: retried', said(items) == 'Second try.' and len(SCRIPT.calls) == 2, items)
SCRIPT.plan('main/model', [chunk(tool=tool_part()), chunk(finish='tool_calls')])
items = run(client(), [{'type': 'function', 'function': {'name': 'Wave'}}])
check('an action alone is a reply', len(SCRIPT.calls) == 1 and items[0][0] == 'tool_calls', items)
SCRIPT.plan('main/model', [chunk(tool=tool_part('Hidden')), chunk(finish='tool_calls')])
items = run(client(), [{'type': 'function', 'function': {'name': 'Wave'}}])
check('an action that was not offered is still dropped', items == [] and len(SCRIPT.calls) == 1 and logged('not offered'), items)
# T-10: a reply cut off by the token limit keeps its text but drops its action calls (their arguments may be incomplete)
for reason in ('length', 'max_tokens'):
    records.clear()
    SCRIPT.plan('main/model', [chunk('Follow me. '), chunk(tool=tool_part('Wave', '{"a"')), chunk(finish=reason)])
    items = run(client(), [{'type': 'function', 'function': {'name': 'Wave'}}])
    check(f'{reason} after an action: text kept, action dropped', said(items) == 'Follow me. ' and not any(i[0] == 'tool_calls' for i in items)
          and len(SCRIPT.calls) == 1 and logged('hit the token limit'), (items, records))
SCRIPT.plan('main/model', [chunk(tool=tool_part('Wave', '{"a"')), chunk(finish='length')], text_reply('Never.'))
items = run(client(), [{'type': 'function', 'function': {'name': 'Wave'}}])
check('length with only an action: nothing runs, not asked again', items == [] and len(SCRIPT.calls) == 1, items)
cut_native = native_events('On my way. ', stop='max_tokens')
cut_native[-1:-1] = [{'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Wave'}},
                     {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"a"'}},
                     {'type': 'content_block_stop', 'index': 1}]
SCRIPT.plan('claude-sonnet-5-5', cut_native, native_events('Never.'))
items = run(client('Anthropic (Native, cached)', 'claude-sonnet-5-5'), [{'type': 'function', 'function': {'name': 'Wave'}}])
check('native max_tokens after an action: text kept, action dropped', said(items) == 'On my way. ' and not any(i[0] == 'tool_calls' for i in items)
      and len(SCRIPT.calls) == 1, items)


# ---------- fallback chain ----------
CHAIN = {'fallback_models': ['backup/one', 'backup/two'], **FAST}
RULED = {'max_tokens': 300, 'extra_body': USER['extra_body']}
SCRIPT.plan('main/model', err(503)); SCRIPT.plan('backup/one', text_reply('Backup here.'))
items = run(client(reliability=CHAIN, params=RULED))
check('outage: next model answers', said(items) == 'Backup here.' and models() == ['main/model', 'backup/one'], models())
check('the switch is logged', logged('asking backup/one instead of main/model'), records)
check('rules on the fallback request', SCRIPT.calls[1].params['extra_body']['provider'] == USER['extra_body']['provider'] and SCRIPT.calls[1].params['extra_body']['reasoning'] == {'enabled': False}, SCRIPT.calls[1].params)
check('settings never reach a service', all('reliability' not in call.params for call in SCRIPT.calls))
check('no error sound when a fallback answers', not sounds)

SCRIPT.plan('main/model', err(429)); SCRIPT.plan('backup/one', err(502)); SCRIPT.plan('backup/two', text_reply('Third.'))
items = run(client(reliability=CHAIN, params=RULED))
check('rate limit, then the whole list in order', said(items) == 'Third.' and models() == ['main/model', 'backup/one', 'backup/two'], models())

SCRIPT.plan('main/model', Hang); SCRIPT.plan('backup/one', text_reply('Rescued.'))
items = run(client(reliability=CHAIN, params=RULED))
check('stall on retry too: next model', said(items) == 'Rescued.' and models() == ['main/model', 'main/model', 'backup/one'], models())

SCRIPT.plan('main/model', err(503)); SCRIPT.plan('backup/one', err(503)); SCRIPT.plan('backup/two', err(500, body=None))
items = run(client(reliability=CHAIN, params=RULED))
check('all out: nothing said, clear error, no crash', items == [] and len(models()) == 3 and logged('LLM API Streaming Error') and sounds, records)

the_client = client(reliability=CHAIN, params=RULED)
SCRIPT.plan('main/model', err(503)); SCRIPT.plan('backup/one', text_reply('Backup.'))
run(the_client)
SCRIPT.plan('main/model', text_reply('Main is back.'))
items = run(the_client)
check('the next reply tries the first model again', said(items) == 'Main is back.' and models() == ['main/model'], models())

SCRIPT.plan('main/model', [chunk('I was saying '), err(503)]); SCRIPT.plan('backup/one', text_reply('Never.'))
items = run(client(reliability=CHAIN, params=RULED))
check('never switches after speaking', said(items) == 'I was saying ' and models() == ['main/model'], models())
check('cut-off reply logged', logged('LLM API Streaming Error') and sounds, records)

SCRIPT.plan('main/model', err(400)); SCRIPT.plan('backup/one', text_reply('Never.'))
check('bad request: no fallback', run(client(reliability=CHAIN, params=RULED)) == [] and models() == ['main/model'], models())
SCRIPT.plan('main/model', err(401)); SCRIPT.plan('backup/one', text_reply('Never.'))
check('bad key: no fallback', run(client(reliability=CHAIN, params=RULED)) == [] and models() == ['main/model'], models())
SCRIPT.plan('main/model', EMPTY['filter']); SCRIPT.plan('backup/one', text_reply('Never.'))
check('filtered: no fallback', run(client(reliability=CHAIN, params=RULED)) == [] and models() == ['main/model'], models())
SCRIPT.plan('main/model', EMPTY['stop']); SCRIPT.plan('backup/one', text_reply('Never.'))
check('plain empties: no fallback', run(client(reliability=CHAIN, params=RULED)) == [] and models() == ['main/model'] * 3, models())
SCRIPT.plan('main/model', [chunk(finish='error')]); SCRIPT.plan('backup/one', text_reply('Backup.'))
items = run(client(reliability=CHAIN, params=RULED))
check('reported failure: retried, then next model', said(items) == 'Backup.' and models() == ['main/model'] * 3 + ['backup/one'], models())

SCRIPT.plan('main/model', err(503)); SCRIPT.plan('backup/one', err(404, body={'error': {'code': 404, 'message': 'No endpoints found'}})); SCRIPT.plan('backup/two', text_reply('Third.'))
items = run(client(reliability=CHAIN, params=RULED))
check('a fallback that cannot serve the rules is passed over', said(items) == 'Third.', models())

SCRIPT.plan('main/model', err(503)); SCRIPT.plan('backup/one', text_reply('Never.'))
items = run(client(reliability={'fallback_models': ['backup/one']}))
check('OpenRouter without provider rules: fallback refused', items == [] and models() == ['main/model'] and logged('no OpenRouter provider rules'), records)
SCRIPT.plan('main/model', err(503)); SCRIPT.plan('backup/one', text_reply('Own rules.'))
items = run(client(reliability={'fallback_models': ['backup/one'], 'openrouter_provider': {'ignore': ['Alibaba']}}))
SCRIPT.plan('main/model', err(503)); SCRIPT.plan('backup/one', text_reply('Never.'))
items = run(client(reliability={'fallback_models': [{'service': OPENROUTER + '/', 'model': 'backup/one'}]}, params={'max_tokens': 300}))
check('OpenRouter URL with a slash and no rules: fallback refused too', items == [] and models() == ['main/model'] and logged('no OpenRouter provider rules'), models())
SCRIPT.plan('main/model', err(503)); SCRIPT.plan('backup/one', text_reply('Own rules.'))
items = run(client(reliability={'fallback_models': ['backup/one'], 'openrouter_provider': {'ignore': ['Alibaba']}}))
check('own rules allow it', said(items) == 'Own rules.' and SCRIPT.calls[1].params['extra_body']['provider'] == {'ignore': ['Alibaba'], 'data_collection': 'deny', 'allow_fallbacks': True}, SCRIPT.calls[-1].params)

SCRIPT.plan('main/model', err(503))
check('off unless set: an outage stays one request', run(client(params=RULED)) == [] and models() == ['main/model'] and logged('LLM API Streaming Error'), models())

# Claude first, then OpenRouter: the set-up this mod is mostly used with
USER_CHAIN = {'fallback_models': [{'service': 'OpenRouter', 'model': 'deepseek/backup'}], **FAST}
SCRIPT.plan('claude-sonnet-5-5', err(529, body={'type': 'error', 'error': {'type': 'overloaded_error'}})); SCRIPT.plan('deepseek/backup', text_reply('From OpenRouter.'))
the_client = client('Anthropic (Native, cached)', 'claude-sonnet-5-5', reliability=USER_CHAIN, params=USER)
items = run(the_client)
check('Anthropic out: OpenRouter answers', said(items) == 'From OpenRouter.' and SCRIPT.calls[1].service == OPENROUTER, SCRIPT.calls)
check('Anthropic never gets the OpenRouter settings', 'provider' not in SCRIPT.calls[0].request.get('extra_body', {}), SCRIPT.calls[0].request.get('extra_body'))
check('OpenRouter gets no Alibaba, no retention, reasoning off', SCRIPT.calls[1].params['extra_body'] == USER['extra_body'], SCRIPT.calls[1].params)
check('OpenRouter gets plain messages', SCRIPT.calls[1].messages[0]['role'] == 'system')
check('main client still has no OpenRouter settings', 'extra_body' not in (the_client._request_params or {}) or 'provider' not in the_client._request_params['extra_body'])

# OpenRouter first, then Claude: each model gets the thinking-off form it takes
SCRIPT.plan('main/model', err(503)); SCRIPT.plan('claude-sonnet-5-5', native_events('From Claude.'))
items = run(client(reliability={'fallback_models': [{'service': 'Anthropic (Native, cached)', 'model': 'claude-sonnet-5-5'}]}, params=USER))
check('OpenRouter out: Claude answers with thinking off', said(items) == 'From Claude.' and SCRIPT.calls[1].request['extra_body']['thinking'] == {'type': 'between_tools'} and 'provider' not in SCRIPT.calls[1].request['extra_body'], SCRIPT.calls[-1].request)

SCRIPT.plan('main/model', err(503)); SCRIPT.plan('gpt/backup', text_reply('Never.'))
items = run(client(reliability={'fallback_models': [{'service': 'OpenAI', 'model': 'gpt/backup'}]}, params=RULED))
check('no key for the fallback service: skipped, logged', items == [] and models() == ['main/model'] and logged('no secret key found for OpenAI'), records)
skipping = client(reliability={'fallback_models': [{'service': 'OpenAI', 'model': 'gpt/backup'}]}, params=RULED)
warned = 0
for _ in range(3):
    SCRIPT.plan('main/model', err(503)); run(skipping)
    warned += sum('no secret key found for OpenAI' in line for line in records)
check('skipped fallback remembered: warned once over three replies', warned == 1 and skipping._fallback_clients == {0: False}, (warned, skipping._fallback_clients))

# the reply keeps the actions, extension text and tools it was prepared with
tools = [{'type': 'function', 'function': {'name': 'Wave'}}]
SCRIPT.plan('main/model', err(503)); SCRIPT.plan('backup/one', [chunk(tool=tool_part()), chunk(finish='tool_calls')])
ext._registry.api_for('t').on('extra_user_text', lambda ctx: 'Note.')
items = run(client(reliability=CHAIN, params=RULED), tools)
check('fallback offered the same actions', SCRIPT.calls[1].params.get('tools') == tools and items[0][0] == 'tool_calls', SCRIPT.calls[1].params)
check('extension text on the fallback request too', 'Note.' in SCRIPT.calls[1].messages[-1]['content'], SCRIPT.calls[1].messages)

# ---------- usage log ----------
ext._registry = ext.Registry(); ext._registry.speaking = 'Lydia'
SCRIPT.plan('claude-sonnet-5-5', Hang, native_events('Hello.'))
SCRIPT.plan('claude-sonnet-5-5', [{'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}}, Hang], native_events('Hello.'))
run(client('Anthropic (Native, cached)', 'claude-sonnet-5-5', reliability=FAST))
usage = ext._registry.api_for('t').usage_log()
check('native: stalled request and its retry both logged', len(usage) == 2 and all(entry['npc'] == 'Lydia' and entry['model'] == 'claude-sonnet-5-5' for entry in usage), usage)
SCRIPT.plan('main/model', text_reply())
ext._registry = ext.Registry()
run(client())
check('other services: nothing in the usage log', ext._registry.api_for('t').usage_log() == [])

# ---------- one-shot native requests (summaries) ----------
class SyncNative:
    plan = []
    calls = 0
    def __init__(self): self.messages = NS(create=self.create)
    def create(self, **request):
        SyncNative.calls += 1
        answer = SyncNative.plan.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {'content': [{'type': 'text', 'text': answer}], 'stop_reason': 'end_turn', 'usage': {'input_tokens': 5, 'output_tokens': 3}}
    def close(self): pass
naps = []
real_sleep = cb.time.sleep
cb.time.sleep = lambda seconds: naps.append(seconds)
summary_client = client('Anthropic (Native, cached)', 'claude-sonnet-5-5')
summary_client.generate_native_client = lambda is_async: SyncNative()
def one_shot(*answers):
    SyncNative.plan, SyncNative.calls = list(answers), 0; naps.clear(); records.clear()
    return summary_client.request_call(Thread())
overloaded = err(529, body={'type': 'error', 'error': {'type': 'overloaded_error'}})
check('native one-shot: overloaded, then answered', one_shot(overloaded, 'Summary.') == 'Summary.' and SyncNative.calls == 2 and naps == [5], (SyncNative.calls, naps))
check('native one-shot: lost connection retried', one_shot(APIConnectionError('reset'), APIConnectionError('reset'), 'Summary.') == 'Summary.' and SyncNative.calls == 3)
check('native one-shot: retries bounded, then None', one_shot(overloaded, overloaded, overloaded, 'Never.') is None and SyncNative.calls == 3 and naps == [5, 5] and logged('request failed'), (SyncNative.calls, naps))
check('native one-shot: bad request not retried', one_shot(err(400, body={'type': 'error', 'error': {'type': 'invalid_request_error'}}), 'Never.') is None and SyncNative.calls == 1 and naps == [])
check('native one-shot: plain answer sent once', one_shot('Summary.') == 'Summary.' and SyncNative.calls == 1 and naps == [])
cb.time.sleep = real_sleep

print('RESULT: ' + ('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'))
sys.exit(1 if FAILS else 0)
