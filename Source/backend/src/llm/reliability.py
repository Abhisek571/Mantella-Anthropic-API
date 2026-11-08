"""What keeps a talk going when an LLM service misbehaves: settings, stall limit and reading the service's answer.

Settings live in the LLM Parameters box, under "reliability", and never reach the service:
    {"max_tokens": 500, "reliability": {"stall_rescue": true, "stall_seconds": 15,
                                        "fallback_models": ["claude-haiku-5-5", {"service": "OpenRouter", "model": "x/y"}],
                                        "openrouter_provider": {"ignore": ["SomeProvider"], "data_collection": "deny"}}}
"""
import asyncio
import copy
import inspect
from typing import Any, AsyncIterable, AsyncGenerator, Awaitable

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
    if isinstance(raw.get('stall_rescue'), bool):
        settings['stall_rescue'] = raw['stall_rescue']
    elif 'stall_rescue' in raw:
        logger.warning('LLM Parameters: reliability "stall_rescue" should be true or false; using its default')
    seconds = raw.get('stall_seconds', DEFAULT_STALL_SECONDS)
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0:
        settings['stall_seconds'] = float(seconds)
    else:
        logger.warning(f'LLM Parameters: reliability "stall_seconds" should be above 0; using {DEFAULT_STALL_SECONDS:g}')
    models = raw.get('fallback_models', [])
    for entry in models if isinstance(models, list) else [models]:
        if isinstance(entry, str) and entry.strip():
            settings['fallback_models'].append({'service': None, 'model': entry.strip()})
        elif isinstance(entry, dict) and isinstance(entry.get('model'), str) and entry['model'].strip() \
                and isinstance(entry.get('service', ''), str):
            settings['fallback_models'].append({'service': entry.get('service', '').strip() or None, 'model': entry['model'].strip()})
        else:
            logger.warning(f'LLM Parameters: reliability fallback entry {entry!r} ignored: use "model" or {{"service": "...", "model": "..."}}')
    if isinstance(raw.get('openrouter_provider'), dict):
        settings['openrouter_provider'] = raw['openrouter_provider']
    elif 'openrouter_provider' in raw:
        logger.warning('LLM Parameters: reliability "openrouter_provider" should be a {...} block; it was ignored')
    return params, settings


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


async def stall_limited(open_stream: Awaitable[AsyncIterable], seconds: float) -> AsyncGenerator[Any, None]:
    """Yields a stream's events, raising TimeoutError when opening it or waiting for the next event takes longer than
    `seconds`. Any event counts as data, also thinking and keep-alive events. Time the caller spends on an event (voice
    generation) does not count. 0 = no limit. The stream is closed however this ends, also when the reader stops early."""
    async with asyncio.timeout(seconds or None):
        stream = await open_stream
    iterator = aiter(stream)
    try:
        while True:
            try:
                async with asyncio.timeout(seconds or None):
                    event = await anext(iterator)
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


def empty_reply_kind(attempt: Attempt) -> str:
    """Why a reply had no text and no action, from what the service sent: 'final' when asking again would get the same
    answer (filtered, refused, bad request, limit used up), 'outage' when the service reported a failure, else 'transient'."""
    if attempt.refusal or (attempt.finish or '').lower() in FINAL_EMPTY_REASONS or (attempt.native_finish or '').lower() in FINAL_EMPTY_REASONS:
        return 'final'
    if attempt.error is not None:
        return 'outage' if is_outage(attempt.error) else 'final'
    return 'outage' if attempt.finish == 'error' else 'transient'
