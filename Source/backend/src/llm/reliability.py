"""What keeps a talk going when an LLM service misbehaves: settings, stall limit and reading the service's answer.

Settings live in the LLM Parameters box, under "reliability", and never reach the service:
    {"max_tokens": 500, "reliability": {"stall_rescue": true, "stall_seconds": 15,
                                        "fallback_models": ["claude-haiku-5-5", {"service": "OpenRouter", "model": "x/y"}],
                                        "openrouter_provider": {"ignore": ["SomeProvider"], "data_collection": "deny"}}}
"""
import asyncio
import re
import copy
import inspect
import time
from typing import Any, AsyncIterable, AsyncGenerator, Awaitable, Callable

import src.utils as utils

logger = utils.get_logger()

SETTINGS_KEY = 'reliability'
DEFAULT_STALL_SECONDS = 15.0
CLOSE_SECONDS = 5.0
OPENROUTER_ENDPOINT = 'https://openrouter.ai/api/v1'
# The same request gets the same answer: a block, or a limit used up before any spoken text
FINAL_EMPTY_REASONS = {'refusal', 'content_filter', 'safety', 'recitation', 'blocklist', 'prohibited_content', 'spii',
                       'length', 'max_tokens', 'model_context_window_exceeded'}
OUTAGE_TYPES = {'overloaded_error', 'rate_limit_error', 'api_error', 'timeout_error', 'server_error',
                'rate_limit_exceeded', 'server_overloaded'}
TRANSPORT_ERRORS = {'APIConnectionError', 'APITimeoutError', 'TransportError', 'TimeoutError'}


class ModelFailed(Exception):
    """A model stalled on its retry too, or kept reporting a failure in an otherwise normal stream."""


class Attempt:
    """What one streamed request produced."""

    def __init__(self):
        self.spoke = False
        self.got_calls = False
        self.stalled = False
        self.finish: str | None = None
        self.native_finish: str | None = None
        self.refusal = False
        self.error: Any = None
        self.detail = ''


def split_settings(llm_params: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """(the LLM parameters without our settings, the settings checked). A bad value is logged and gets its default."""
    settings = {'stall_rescue': None, 'stall_seconds': DEFAULT_STALL_SECONDS, 'fallback_models': [], 'openrouter_provider': None}
    if not llm_params or SETTINGS_KEY not in llm_params:
        return llm_params, settings
    params = {key: value for key, value in llm_params.items() if key != SETTINGS_KEY}
    raw = llm_params[SETTINGS_KEY]
    if not isinstance(raw, dict):
        logger.warning('LLM Parameters: "reliability" should be a {...} block; using its defaults')
        return params, settings
    settings['stall_rescue'] = _stall_rescue(raw)
    settings['stall_seconds'] = _stall_seconds(raw)
    settings['fallback_models'] = _fallback_models(raw)
    settings['openrouter_provider'] = _openrouter_provider(raw)
    return params, settings


def _stall_rescue(raw: dict[str, Any]) -> bool | None:
    if isinstance(raw.get('stall_rescue'), bool):
        return raw['stall_rescue']
    if 'stall_rescue' in raw:
        logger.warning('LLM Parameters: reliability "stall_rescue" should be true or false; using its default')
    return None


def _stall_seconds(raw: dict[str, Any]) -> float:
    seconds = raw.get('stall_seconds', DEFAULT_STALL_SECONDS)
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0:
        return float(seconds)
    logger.warning(f'LLM Parameters: reliability "stall_seconds" should be above 0; using {DEFAULT_STALL_SECONDS:g}')
    return DEFAULT_STALL_SECONDS


def _fallback_models(raw: dict[str, Any]) -> list[dict[str, str | None]]:
    """Each entry as {'service': name or None (the current one), 'model': id}; a bad entry is logged and left out."""
    models = raw.get('fallback_models', [])
    checked = []
    for entry in models if isinstance(models, list) else [models]:
        if isinstance(entry, str) and entry.strip():
            checked.append({'service': None, 'model': entry.strip()})
        elif isinstance(entry, dict) and isinstance(entry.get('model'), str) and entry['model'].strip() \
                and isinstance(entry.get('service', ''), str):
            checked.append({'service': entry.get('service', '').strip() or None, 'model': entry['model'].strip()})
        else:
            logger.warning(f'LLM Parameters: reliability fallback entry {entry!r} ignored: use "model" or {{"service": "...", "model": "..."}}')
    return checked


def _openrouter_provider(raw: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(raw.get('openrouter_provider'), dict):
        return raw['openrouter_provider']
    if 'openrouter_provider' in raw:
        logger.warning('LLM Parameters: reliability "openrouter_provider" should be a {...} block; it was ignored')
    return None


def stall_limit(settings: dict[str, Any], is_local: bool) -> float:
    """Seconds without data before a stream counts as stalled; 0 = off. Off by default for a model on this PC or
    network, which can take minutes to load or to read a long prompt before its first word."""
    if settings['stall_rescue'] is False or (is_local and settings['stall_rescue'] is None):
        return 0
    return settings['stall_seconds']


def is_openrouter(endpoint: str) -> bool:
    """True for any way of writing OpenRouter's address (a trailing slash, another path, the host with a final dot), so
    its rules always apply."""
    from urllib.parse import urlparse
    try:
        host = (urlparse(endpoint).hostname or '').lower().rstrip('.')
    except ValueError:
        return False
    return host == 'openrouter.ai' or host.endswith('.openrouter.ai')


def provider_rules(settings: dict[str, Any], params: dict[str, Any] | None) -> dict[str, Any] | None:
    """The OpenRouter routing rules for a fallback model: "openrouter_provider", else the ones in the LLM Parameters
    (kept there even when the current service has no use for them). None when neither is set. Providers that may keep
    the data are refused unless the rules say otherwise, and OpenRouter may use other providers unless told not to."""
    rules = settings.get('openrouter_provider')
    if rules is None:
        extra_body = (params or {}).get('extra_body')
        rules = extra_body.get('provider') if isinstance(extra_body, dict) else None
    if not isinstance(rules, dict):
        return None
    rules = copy.deepcopy(rules)
    collection = rules.get('data_collection', 'deny')
    collection = collection.strip().lower() if isinstance(collection, str) else collection
    if collection not in ('allow', 'deny'):
        logger.warning(f'LLM Parameters: OpenRouter "data_collection" should be "allow" or "deny", not {collection!r}; using "deny"')
        collection = 'deny'
    rules['data_collection'] = collection
    rules.setdefault('allow_fallbacks', True)
    return rules


def fallback_params(params: dict[str, Any] | None, endpoint: str, rules: dict[str, Any] | None) -> dict[str, Any]:
    """The LLM parameters as a fallback model on this service takes them. Anthropic keeps its own handling in
    ClientBase; OpenRouter gets the routing rules and its form of "reasoning off"; other services get neither."""
    params = copy.deepcopy(params or {})
    extra_body = params.get('extra_body')
    if utils.is_anthropic_endpoint(endpoint) or (not isinstance(extra_body, dict) and not is_openrouter(endpoint)):
        return params
    extra_body = extra_body if isinstance(extra_body, dict) else {}
    thinking = extra_body.pop('thinking', None)
    if is_openrouter(endpoint):
        extra_body['provider'] = rules
        if isinstance(thinking, dict) and thinking.get('type') in ('disabled', 'between_tools'):
            extra_body.setdefault('reasoning', {'enabled': False})
    else:
        extra_body.pop('provider', None)
        extra_body.pop('reasoning', None)
    if extra_body:
        params['extra_body'] = extra_body
    else:
        params.pop('extra_body', None)
    return params


FIRST_EVENT_FACTOR = 4


async def stall_limited(open_stream: Awaitable[AsyncIterable], seconds: float) -> AsyncGenerator[Any, None]:
    """Yields a stream's events, raising TimeoutError when opening it or waiting for the next event takes longer than
    `seconds`. Any event the SDK hands over counts as data, also thinking events. The first event may take
    FIRST_EVENT_FACTOR times as long: a reasoning model can think silently while the service sends only keep-alive
    comments, which the SDK drops. Time the caller spends on an event (voice generation) does not count. 0 = no limit.
    The stream is closed however this ends, also when the reader stops early."""
    async with asyncio.timeout(seconds or None):
        stream = await open_stream
    iterator = aiter(stream)
    first = True
    try:
        while True:
            try:
                async with asyncio.timeout((seconds * FIRST_EVENT_FACTOR if first else seconds) or None):
                    event = await anext(iterator)
                first = False
            except StopAsyncIteration:
                return
            yield event
    finally:
        await close_stream(iterator, stream)


async def close_stream(*parts: Any) -> None:
    """Closes a stream and its iterator (aclose or close, as each has); a stuck connection must not hold up the reply."""
    for part in parts:
        close = getattr(part, 'aclose', None) or getattr(part, 'close', None)
        if not callable(close):
            continue
        try:
            async with asyncio.timeout(CLOSE_SECONDS):
                result = close()
                if inspect.isawaitable(result):
                    await result
        except Exception as e:
            logger.debug(f"Could not close the LLM stream cleanly: {e!r}")


async def close_client(client: Any) -> None:
    """Closes an SDK client; a stuck connection must not hold up the reply or hide the real error."""
    try:
        async with asyncio.timeout(CLOSE_SECONDS):
            await client.close()
    except Exception as e:
        logger.debug(f"Could not close the LLM client cleanly: {e!r}")


def _error_body(error: Any) -> dict:
    body = error if isinstance(error, dict) else getattr(error, 'body', None)
    if not isinstance(body, dict):
        return {}
    inner = body.get('error')
    return inner if isinstance(inner, dict) else body


def _status_code(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_outage(error: Any) -> bool:
    """True for a rate limit, an overloaded or failing service, or a lost connection: another model may answer. False
    for what another try cannot fix (bad request, bad key, no credit). An error inside a stream arrives with HTTP status
    200, so the code in its body goes first. Takes an exception or a stream's error block."""
    if not isinstance(error, dict) and any(cls.__name__ in TRANSPORT_ERRORS for cls in type(error).__mro__):
        return True
    body = _error_body(error)
    metadata = body.get('metadata') if isinstance(body.get('metadata'), dict) else {}
    if body.get('type') in OUTAGE_TYPES or body.get('code') in OUTAGE_TYPES or metadata.get('error_type') in OUTAGE_TYPES:
        return True
    code = _status_code(body.get('code'))
    if code is None or code < 400:
        code = _status_code(getattr(error, 'status_code', None))
    return code is not None and (code in (408, 429) or code >= 500)


HTTP_ATTEMPTS = 3
HTTP_FIRST_DELAY = 1.0  # seconds before the second try; each later wait is twice the one before


def get_with_retry(get: Callable[..., Any], url: str, *, attempts: int = HTTP_ATTEMPTS, first_delay: float = HTTP_FIRST_DELAY,
                   sleep: Callable[[float], None] | None = None, **kwargs: Any) -> Any:
    """get(url, **kwargs) (requests.get or the like), asked again after a growing wait while the answer is a rate limit
    or a server error (is_outage). Returns the last response; an exception from get is raised as it is. sleep: the
    wait (time.sleep when None)."""
    delay = first_delay
    for attempt in range(1, attempts + 1):
        response = get(url, **kwargs)
        if attempt == attempts or not is_outage(response):
            return response
        logger.debug(f"{url} answered {getattr(response, 'status_code', '?')}; trying again in {delay:g} seconds ({attempt}/{attempts - 1})")
        (sleep or time.sleep)(delay)
        delay *= 2


def empty_reply_kind(attempt: Attempt) -> str:
    """Why a reply had no text and no action, from what the service sent: 'final' when asking again would get the same
    answer (filtered, refused, bad request, limit used up), 'outage' when the service reported a failure, else 'transient'."""
    if attempt.refusal or (attempt.finish or '').lower() in FINAL_EMPTY_REASONS or (attempt.native_finish or '').lower() in FINAL_EMPTY_REASONS:
        return 'final'
    if attempt.error is not None:
        return 'outage' if is_outage(attempt.error) else 'final'
    return 'outage' if attempt.finish == 'error' else 'transient'


# ---------------------------------------------------------------------------------------------------------------------
# Planning text kept out of speech
# ---------------------------------------------------------------------------------------------------------------------
# With no thinking before its first action call (Sonnet 5.5 takes thinking "between_tools", the only "off" form
# it accepts), a model about to call an action sometimes writes its own deliberation as plain text: "She's flattered,
# but she has a partner. I'll have her react with arousal and decide." Every text delta is streamed straight into
# speech, so the NPC said it aloud (live test 18: 5 replies, each with action calls; none in 39 replies without).
# PlanningGuard passes the reply on sentence by sentence and leaves such sentences out.

# Wording only a narrator planning the reply uses (never an NPC speaking in character)
PLANNING = re.compile(
    r"\bI(?:'ll| will|'m going to| am going to)\s+(?:have|let|make)\s+(?:her|him|them)\s+(?:react|respond|reply|answer|decide|accept|decline|agree|refuse)\b"
    r"|\b(?:reply|replies|replying|respond|responds|responding|answer|answers|answering|stay|stays|speak|speaks)\s+in character\b"
    r"|\b(?:I'll|I will|I'm going to)\s+(?:answer|reply|respond|speak)\s+as\s+[A-Z]"
    r"|\b(?:rate|rates|rating|rated)\b[^.!?]{0,60}\b(?:appetite|silently|quietly)\b"
    r"|\b(?:the|this|that)\s+(?:attached\s+)?(?:image|screenshot)\s+(?:shows|is|was|appears|seems)\b|\battached\s+(?:image|screenshot|picture)\b"
    r"|\bpoints?\s+(?:to|toward|towards)\s+(?:accepting|declining|agreeing|refusing|a yes|a no)\b"
    r"|\b(?:her|his|their)\s+own\s+choice\s+to\s+(?:accept|decline|agree|refuse)\b"
    r"|\bso\s+(?:she|he|they)(?:'s|'re|\s+is|\s+are)\s+(?:willing|unwilling|agreeable)\b"
    r"|\b(?:the|an|this)\s+(?:tool|function)\s+call\b|\b(?:call|use)\s+the\s+[A-Z]\w+\s+(?:action|tool)\b",
    re.I)
SELF_VERBS = (r"(?:is|isn't|has|hasn't|was|wasn't|seems|would|will|won't|does|doesn't|did|didn't|can't|cannot|knows|"
              r"wants|feels|likes|decides|accepts|declines|agrees|refuses|sees|thinks)")
# "Name: " at the start of a sentence: the speaker in a group talk
SPEAKER_PREFIX = re.compile(r"^\s*([A-Z][\w'. -]{0,40}?):\s+")
FIRST_SECOND_PERSON = re.compile(r"\b(?:I|I'm|I'll|I've|I'd)\b|\b(?:me|my|mine|myself|we|us|our|ours|you|your|yours|yourself|"
                                 r"you're|you'll|you've|ye|thee|thou|thy)\b", re.I)
THIRD_PERSON = re.compile(r"\b(?:she|she's|her|hers|herself|he|he's|him|his|himself|they|them|their)\b", re.I)
SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*\s+|\n+")


class PlanningGuard:
    """Passes a streamed reply on whole sentences at a time and leaves out the model's planning text: a sentence with
    PLANNING wording, or one where the speaking NPC talks of themself by name ("Haelga is giddy ..."); after one of
    those, every sentence of the reply in neither first nor second person (the narration goes on), and the sentence
    held just before it if that one is narration too. lookahead: hold one whole sentence back (only for replies that
    may call actions, the only ones seen doing this; it costs the time one sentence takes to stream). speakers: the
    NPCs in the talk (with one NPC, a sentence naming them in the third person is theirs). Text and order are kept
    exactly; a "Name:" prefix of a left-out sentence goes on to the next sentence kept."""

    def __init__(self, speakers=(), lookahead: bool = True):
        self.speakers = [s.strip() for s in speakers if isinstance(s, str) and s.strip()]
        # whole names and first names ("Lucan Valerius", "Lucan"), for the narration test
        self.names = list(dict.fromkeys(self.speakers + [s.split()[0] for s in self.speakers]))
        self.current = ''  # the speaker the last "Name:" prefix named (a group talk)
        self.lookahead = lookahead
        self.buffer = ''
        self.held: str | None = None
        self.found = False
        self.prefix = ''
        self.dropped: list[str] = []

    def feed(self, text: str) -> list[str]:
        """The text that can be spoken now, given the next streamed piece."""
        self.buffer += text or ''
        out = []
        while True:
            match = SENTENCE_END.search(self.buffer)
            if not match:
                return out
            sentence, self.buffer = self.buffer[:match.end()], self.buffer[match.end():]
            out += self._take(sentence)

    def flush(self) -> list[str]:
        """Everything still kept back, at the reply's end or before its action calls."""
        out, tail = [], ''
        if self.buffer.strip():
            out = self._take(self.buffer)
        else:
            tail = self.buffer
        self.buffer = ''
        if self.held is not None:
            out.append(self._emit(self.held))
            self.held = None
        return [t for t in out + [tail] if t]

    def _speaker(self, sentence: str) -> str:
        prefix = SPEAKER_PREFIX.match(sentence)
        if prefix:
            self.current = prefix.group(1).strip()
            return self.current
        return self.current or (self.speakers[0] if len(self.speakers) == 1 else '')

    def _body(self, sentence: str) -> str:
        prefix = SPEAKER_PREFIX.match(sentence)
        return sentence[prefix.end():] if prefix else sentence

    def planning(self, sentence: str) -> bool:
        body = self._body(sentence)
        if PLANNING.search(body):
            return True
        name = self._speaker(sentence)
        return bool(name) and re.match(rf"\s*{re.escape(name)}(?:'s\s|\s+{SELF_VERBS}\b)", body) is not None

    def narration(self, sentence: str) -> bool:
        body = self._body(sentence)
        if FIRST_SECOND_PERSON.search(body) or '"' in body:
            return False
        return bool(THIRD_PERSON.search(body)) or any(re.search(rf"\b{re.escape(n)}\b", body) for n in self.names)

    def _drop(self, sentence: str) -> None:
        prefix = SPEAKER_PREFIX.match(sentence)
        if prefix:
            self.prefix = prefix.group(0)
        self.dropped.append(sentence.strip())
        logger.log(23, f"Planning text left out of the spoken reply: {sentence.strip()!r}")

    def _emit(self, sentence: str) -> str:
        if self.prefix and not SPEAKER_PREFIX.match(sentence):
            sentence = self.prefix + sentence.lstrip()
        self.prefix = ''
        return sentence

    def _take(self, sentence: str) -> list[str]:
        if self.planning(sentence):
            self.found = True
            if self.held is not None and self.narration(self.held):
                self._drop(self.held)
                self.held = None
            self._drop(sentence)
            return []
        if self.found and self.narration(sentence):
            self._drop(sentence)
            return []
        out = [self._emit(self.held)] if self.held is not None else []
        self.held = None
        if self.lookahead:
            self.held = sentence
        else:
            out.append(self._emit(sentence))
        return out
