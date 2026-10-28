"""Anthropic (Native, cached): Claude through Anthropic's own Messages API, which supports prompt caching.

Mantella builds OpenAI-style messages. This module converts them to the Messages API format, adds cache
markers and streams the reply back in the shapes ClientBase already yields: ("content", text) and
("tool_calls", [OpenAI-style tool calls]).

Cache layout: one breakpoint at the end of the system prompt (tools + system: bio, memories, actions), one on
the newest message before its screenshot, and one on the last NPC reply. The screenshot and the names note
come after the last breakpoint, so they never break the cached prefix.
"""
from typing import Any, AsyncGenerator, Callable
import json

import src.utils as utils

logger = utils.get_logger()

# Models that accept server-side refusal fallbacks ("default" form). Haiku has none.
FALLBACK_MODELS = ('claude-fable-5-1', 'claude-opus-5-5', 'claude-opus-5', 'claude-sonnet-5-5')
FALLBACK_BETA = 'server-side-fallback-2026-07-01'
DEFAULT_MAX_TOKENS = 250
# Request parameters passed through as-is (OpenAI-only ones are dropped, see split_params)
NATIVE_PARAMS = ('max_tokens', 'thinking', 'output_config', 'stop_sequences', 'metadata', 'service_tier')


def split_params(request_params: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Turns Mantella's LLM parameters (OpenAI style, with Anthropic extras in extra_body) into Messages API fields.
    Returns (fields, names of ignored parameters)."""
    fields: dict[str, Any] = {}
    ignored: list[str] = []
    for key, value in (request_params or {}).items():
        if key == 'extra_body' and isinstance(value, dict):
            for extra_key, extra_value in value.items():
                if extra_key in NATIVE_PARAMS:
                    fields[extra_key] = extra_value
                else:
                    ignored.append('extra_body.' + extra_key)
        elif key == 'max_completion_tokens':
            fields['max_tokens'] = value
        elif key == 'stop':
            fields['stop_sequences'] = [value] if isinstance(value, str) else list(value)
        elif key == 'reasoning_effort':
            fields.setdefault('output_config', {})['effort'] = value
        elif key in NATIVE_PARAMS:
            fields[key] = value
        elif key not in ('tools', 'stream'):
            ignored.append(key)
    fields.setdefault('max_tokens', DEFAULT_MAX_TOKENS)
    return fields, ignored


def split_entity_lists(tools: list[dict] | None) -> tuple[list[dict] | None, str]:
    """Mantella appends the NPCs in reach to tool parameter descriptions (" Available: A, B" / " No entities
    available."), so the tool list changed whenever someone walked by and the whole cached prefix missed.
    Returns copies of the tools without those lists, and the lists as one line for the newest message."""
    if not tools:
        return tools, ''
    tools = json.loads(json.dumps(tools))
    notes = []
    for tool in tools:
        function = tool.get('function', tool)
        for name, param in ((function.get('parameters') or {}).get('properties') or {}).items():
            description = param.get('description')
            if not isinstance(description, str):
                continue
            if description.endswith(' No entities available.'):
                param['description'] = description[:-len(' No entities available.')]
            elif ' Available: ' in description or description.startswith('Available: '):
                param['description'], names = (' ' + description).rsplit(' Available: ', 1)
                param['description'] = param['description'].strip()
                notes.append(f"{function['name']}.{name}: {names}")
    return tools, ('(Names you can use in actions now: ' + '; '.join(notes) + '.)') if notes else ''


def convert_tools(tools: list[dict] | None) -> list[dict] | None:
    """OpenAI function tools -> Messages API tools, sorted by name so the cached prefix stays byte-identical."""
    if not tools:
        return None
    converted = []
    for tool in tools:
        function = tool.get('function', tool)
        converted.append({
            'name': function['name'],
            'description': function.get('description', ''),
            'input_schema': function.get('parameters') or {'type': 'object', 'properties': {}},
        })
    return sorted(converted, key=lambda t: t['name'])


def _image_block(url: str) -> dict | None:
    if url.startswith('data:') and ';base64,' in url:
        header, data = url.split(';base64,', 1)
        return {'type': 'image', 'source': {'type': 'base64', 'media_type': header[5:] or 'image/jpeg', 'data': data}}
    if url.startswith(('http://', 'https://')):
        return {'type': 'image', 'source': {'type': 'url', 'url': url}}
    return None


def _user_content(content: Any) -> list[dict]:
    if isinstance(content, list):
        blocks = []
        for part in content:
            if not isinstance(part, dict):
                blocks.append({'type': 'text', 'text': str(part)})
            elif part.get('type') == 'text' and part.get('text'):
                blocks.append({'type': 'text', 'text': part['text']})
            elif part.get('type') == 'image_url':
                image = _image_block((part.get('image_url') or {}).get('url', ''))
                if image:
                    blocks.append(image)
        return blocks
    text = '' if content is None else str(content)
    return [{'type': 'text', 'text': text}] if text.strip() else []


def convert_messages(openai_messages: list[dict], thinking_by_tool_call: dict[str, list[dict]]) -> tuple[list[dict], list[dict]]:
    """OpenAI chat messages -> (system blocks, Messages API messages).

    System messages are joined into the top-level system prompt. Tool results become tool_result blocks in a
    user turn. Thinking blocks the API returned with a tool call are put back in front of that tool call, since
    the API expects them unchanged.
    """
    system_texts: list[str] = []
    messages: list[dict] = []

    def add(role: str, blocks: list[dict]):
        if not blocks:
            return
        if messages and messages[-1]['role'] == role:
            messages[-1]['content'].extend(blocks)
        else:
            messages.append({'role': role, 'content': list(blocks)})

    for message in openai_messages:
        role = message.get('role')
        if role in ('system', 'developer'):
            text = message.get('content')
            if isinstance(text, list):
                text = '\n'.join(p.get('text', '') for p in text if isinstance(p, dict))
            if text:
                system_texts.append(str(text))
        elif role == 'user':
            add('user', _user_content(message.get('content')))
        elif role == 'assistant':
            blocks: list[dict] = []
            tool_calls = message.get('tool_calls') or []
            for call in tool_calls:
                blocks.extend(thinking_by_tool_call.get(call.get('id', ''), []))
                break  # the thinking for one assistant turn is stored under its first tool call
            text = message.get('content')
            if isinstance(text, str) and text.strip():
                blocks.append({'type': 'text', 'text': text})
            for call in tool_calls:
                function = call.get('function') or {}
                try:
                    arguments = json.loads(function.get('arguments') or '{}')
                except json.JSONDecodeError:
                    arguments = {}
                blocks.append({'type': 'tool_use', 'id': call.get('id', ''), 'name': function.get('name', ''),
                               'input': arguments if isinstance(arguments, dict) else {}})
            add('assistant', blocks)
        elif role == 'tool':
            content = message.get('content')
            add('user', [{'type': 'tool_result', 'tool_use_id': message.get('tool_call_id', ''),
                          'content': str(content) if content else 'done'}])

    # tool results must open their user turn
    for message in messages:
        if message['role'] == 'user':
            message['content'].sort(key=lambda block: block['type'] != 'tool_result')
    if messages and messages[0]['role'] != 'user':
        messages.insert(0, {'role': 'user', 'content': [{'type': 'text', 'text': '...'}]})

    system = [{'type': 'text', 'text': '\n'.join(system_texts)}] if system_texts else []
    return system, messages


CACHEABLE_BLOCKS = ('text', 'tool_use', 'tool_result')


def mark_history(messages: list[dict]):
    """Cache points on the conversation. Mantella sends the screenshot only with the newest message, so the
    point goes on the last block before it; the names note is added after this. A second point on the last
    NPC reply keeps the older history cached even if the newest message reads differently next time."""
    def mark(blocks: list[dict]) -> bool:
        for block in reversed(blocks):
            if block['type'] in CACHEABLE_BLOCKS:
                block['cache_control'] = {'type': 'ephemeral'}
                return True
        return False

    if not messages:
        return
    newest = messages[-1]['content']
    images = [i for i, block in enumerate(newest) if block['type'] == 'image']
    mark(newest[:images[0]] if images else newest)
    for message in reversed(messages[:-1]):
        if message['role'] == 'assistant' and mark(message['content']):
            return


def ordered_parts(parts: dict) -> tuple[list[tuple[str, str]], set[str]]:
    """(name, text) in the order the extensions asked for, and the names the cache points go after.
    '__order__': names first in that order, other parts after in their own order. '__breakpoints_after__': each
    entry a name or a list of names (the last one present is used); '__breakpoint_after__' (one name) still works."""
    texts = [(name, text) for name, text in parts.items()
             if not name.startswith('__') and isinstance(text, str) and text.strip()]
    order = [name for name in parts.get('__order__') or [] if isinstance(name, str)]
    rank = {name: i for i, name in enumerate(order)}
    texts.sort(key=lambda item: rank.get(item[0], len(order)))  # stable: unknown parts keep their order
    present = [name for name, _ in texts]
    wanted = parts.get('__breakpoints_after__') or ([parts['__breakpoint_after__']] if parts.get('__breakpoint_after__') else [])
    marks = set()
    for entry in wanted:
        group = [entry] if isinstance(entry, str) else list(entry)
        found = [name for name in present if name in group]
        if found:
            marks.add(found[-1])
    return texts, marks


def split_system(system: list[dict], system_parts: Callable[[dict[str, str]], dict[str, str]]) -> list[dict]:
    """Lets an extension reorder or add to the system prompt: {'mantella': text} -> {name: text, ...}, with the
    order and cache points named as in ordered_parts. Any failure keeps the stock system."""
    try:
        texts, marks = ordered_parts(system_parts({'mantella': system[0]['text'] if system else ''}))
    except Exception as e:
        logger.warning(f"Anthropic (Native): system_parts failed, using Mantella's system prompt: {e}")
        return system
    if not texts:
        return system
    blocks = [{'type': 'text', 'text': text} for _, text in texts]
    for block, (name, _) in zip(blocks, texts):
        if name in marks:
            block['marker'] = True
    if not marks:
        blocks[-1]['marker'] = True
    return blocks


def join_system_parts(system_parts: Callable[[dict[str, str]], dict[str, str]], text: str) -> str:
    """The same parts and order as one text, for services without cache points (OpenRouter and others cache the
    longest unchanged start of the prompt on their own). Any failure keeps the stock text."""
    try:
        texts, _ = ordered_parts(system_parts({'mantella': text}))
    except Exception as e:
        logger.warning(f"system_parts failed, using Mantella's system prompt: {e}")
        return text
    return '\n\n'.join(t for _, t in texts) if texts else text


def build_request(model: str, openai_messages: list[dict], request_params: dict[str, Any] | None,
                  tools: list[dict] | None, thinking_by_tool_call: dict[str, list[dict]], cache: bool,
                  extra_texts: list[str] | None = None,
                  system_parts: Callable[[dict[str, str]], dict[str, str]] | None = None) -> dict[str, Any]:
    """Keyword arguments for client.messages.create(). extra_texts go into the newest message after the cache
    point; system_parts (from an extension) splits the system prompt and places its cache point."""
    fields, ignored = split_params(request_params)
    if ignored:
        logger.debug(f"Anthropic (Native): ignoring parameters the Messages API does not take: {', '.join(ignored)}")
    system, messages = convert_messages(openai_messages, thinking_by_tool_call)
    if system_parts:
        system = split_system(system, system_parts)
    if cache and system:
        marked = [block for block in system if block.pop('marker', False)] or system[-1:]
        for block in marked[-2:]:  # at most 2 here: the history takes the other 2 of Anthropic's 4
            block['cache_control'] = {'type': 'ephemeral'}
    else:
        for block in system:
            block.pop('marker', None)
    if cache:
        mark_history(messages)

    request: dict[str, Any] = {'model': model, 'messages': messages, 'max_tokens': fields.pop('max_tokens')}
    if system:
        request['system'] = system
    tools, names_note = split_entity_lists(tools)
    converted_tools = convert_tools(tools)
    if converted_tools:
        request['tools'] = converted_tools
    if messages and messages[-1]['role'] == 'user':
        for text in ([names_note] if names_note else []) + [t for t in (extra_texts or []) if t]:
            messages[-1]['content'].append({'type': 'text', 'text': text})
    for key in ('stop_sequences', 'metadata', 'service_tier'):
        if key in fields:
            request[key] = fields.pop(key)
    # newer fields go through extra_body so the bundled SDK version does not need to know them
    extra_body: dict[str, Any] = dict(fields)
    extra_headers: dict[str, str] = {}
    if model in FALLBACK_MODELS:
        extra_body['fallbacks'] = 'default'
        extra_headers['anthropic-beta'] = FALLBACK_BETA
    if extra_body:
        request['extra_body'] = extra_body
    if extra_headers:
        request['extra_headers'] = extra_headers
    return request


# Price per million tokens: (input, 5-minute cache write, cache read, output). Longest matching model-id prefix wins.
PRICES = {
    'claude-fable-5-1': (10.00, 12.50, 0.25, 50.00),
    'claude-opus-5-5': (4.00, 5.00, 0.20, 20.00),
    'claude-opus-5': (5.00, 6.25, 0.50, 25.00),
    'claude-sonnet-5-5': (2.00, 2.50, 0.10, 10.00),
    'claude-sonnet-5': (2.00, 2.50, 0.20, 10.00),
    'claude-haiku-5-5': (0.10, 0.125, 0.01, 0.50),
    'claude-haiku-4-5': (1.00, 1.25, 0.10, 5.00),
}


def reply_cost(model: str, usage: dict) -> float | None:
    """Dollar cost of one reply from its usage numbers; None for a model without a known price."""
    matches = [prefix for prefix in PRICES if (model or '').startswith(prefix)]
    if not matches:
        return None
    new, write, read, out = PRICES[max(matches, key=len)]
    return (usage.get('uncached', 0) * new + usage.get('cache_write', 0) * write
            + usage.get('cache_read', 0) * read + usage.get('output', 0) * out) / 1e6


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Reads a field from an SDK object or a plain dict (unknown event types arrive as dicts)."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def log_usage(usage: Any, prefix: str = 'Anthropic (Native)') -> dict | None:
    if usage is None:
        return None
    read = _field(usage, 'cache_read_input_tokens', 0) or 0
    written = _field(usage, 'cache_creation_input_tokens', 0) or 0
    uncached = _field(usage, 'input_tokens', 0) or 0
    logger.log(23, f"{prefix}: prompt tokens cached (read) {read}, written to cache {written}, uncached {uncached}")
    return {'cache_read': read, 'cache_write': written, 'uncached': uncached}


class StreamState:
    """Collects one streamed reply: text deltas are passed on, tool calls and thinking blocks are assembled."""

    def __init__(self):
        self.blocks: dict[int, dict] = {}
        self.tool_calls: list[dict] = []
        self.thinking: list[dict] = []
        self.stop_reason: str | None = None
        self.got_text = False
        self.fell_back_to: str | None = None
        self.usage: dict | None = None

    def handle(self, event: Any) -> str | None:
        """Processes one stream event. Returns text to speak, if any."""
        kind = _field(event, 'type')
        if kind == 'message_start':
            message = _field(event, 'message')
            self.usage = log_usage(_field(message, 'usage'))
        elif kind == 'content_block_start':
            block = _field(event, 'content_block')
            block_type = _field(block, 'type')
            index = _field(event, 'index', len(self.blocks))
            if block_type == 'tool_use':
                self.blocks[index] = {'type': 'tool_use', 'id': _field(block, 'id', ''), 'name': _field(block, 'name', ''), 'json': ''}
            elif block_type == 'thinking':
                self.blocks[index] = {'type': 'thinking', 'thinking': _field(block, 'thinking', '') or '', 'signature': _field(block, 'signature', '') or ''}
            elif block_type == 'redacted_thinking':
                self.thinking.append({'type': 'redacted_thinking', 'data': _field(block, 'data', '')})
            elif block_type == 'fallback':
                self.fell_back_to = _field(_field(block, 'to'), 'model')
                logger.log(23, f"Anthropic (Native): the reply was declined and continued on {self.fell_back_to}")
                # tool calls and thinking from the declined attempt are not echoed back
                self.tool_calls.clear()
                self.thinking.clear()
                self.blocks.clear()
        elif kind == 'content_block_delta':
            delta = _field(event, 'delta')
            delta_type = _field(delta, 'type')
            block = self.blocks.get(_field(event, 'index'))
            if delta_type == 'text_delta':
                text = _field(delta, 'text', '')
                if text:
                    self.got_text = True
                    return text
            elif delta_type == 'input_json_delta' and block is not None:
                block['json'] += _field(delta, 'partial_json', '') or ''
            elif delta_type == 'thinking_delta' and block is not None:
                block['thinking'] += _field(delta, 'thinking', '') or ''
            elif delta_type == 'signature_delta' and block is not None:
                block['signature'] += _field(delta, 'signature', '') or ''
        elif kind == 'content_block_stop':
            block = self.blocks.pop(_field(event, 'index'), None)
            if block and block['type'] == 'tool_use':
                self.tool_calls.append({'id': block['id'], 'type': 'function',
                                        'function': {'name': block['name'], 'arguments': block['json'] or '{}'}})
            elif block and block['type'] == 'thinking':
                self.thinking.append({'type': 'thinking', 'thinking': block['thinking'], 'signature': block['signature']})
        elif kind == 'message_delta':
            self.stop_reason = _field(_field(event, 'delta'), 'stop_reason') or self.stop_reason
            output = _field(_field(event, 'usage'), 'output_tokens')
            if self.usage is not None and output is not None:
                self.usage['output'] = output
        return None


async def stream_reply(client: Any, request: dict[str, Any], thinking_by_tool_call: dict[str, list[dict]],
                       state: StreamState) -> AsyncGenerator[str, None]:
    """Streams one reply, yielding text. Tool calls and the stop reason are left in `state`."""
    stream = await client.messages.create(stream=True, **request)
    async for event in stream:
        text = state.handle(event)
        if text:
            yield text
    if state.stop_reason == 'refusal':
        logger.warning("Anthropic (Native): Claude declined this reply (stop_reason=refusal)")
    if state.tool_calls and state.thinking:
        thinking_by_tool_call[state.tool_calls[0]['id']] = list(state.thinking)
        # keep only recent turns; older thinking is not needed once the tool call has been answered
        while len(thinking_by_tool_call) > 20:
            thinking_by_tool_call.pop(next(iter(thinking_by_tool_call)))


def reply_text(message: Any) -> str:
    """Joins the text blocks of a non-streamed reply."""
    log_usage(_field(message, 'usage'))
    if _field(message, 'stop_reason') == 'refusal':
        logger.warning("Anthropic (Native): Claude declined this request (stop_reason=refusal)")
    parts = [_field(block, 'text', '') for block in (_field(message, 'content') or []) if _field(block, 'type') == 'text']
    return ''.join(p for p in parts if p)
