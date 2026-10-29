from threading import Lock
from typing import AsyncGenerator, Any
from enum import Enum
from openai import APIConnectionError, BadRequestError, OpenAI, AsyncOpenAI, RateLimitError
from openai.types.chat import ChatCompletion
import time
import tiktoken
import json
import os
import codecs
import locale
import requests
from pathlib import Path
from src.llm.ai_client import AIClient
from src.llm.message_thread import message_thread
from src.llm.messages import Message, ImageMessage, UserMessage
from src.llm.llm_model_list import LLMModelList
import src.utils as utils
from src.telemetry.telemetry import create_span_from_thread
from src.actions.function_manager import FunctionManager
from src.llm.claude_cache_connector import ClaudeCacheConnector
import src.extensions as src_extensions

logger = utils.get_logger()


def _tool_name(item) -> str:
    """The name of an OpenAI-style tool or tool call, as a dict or an SDK object."""
    function = item.get('function') if isinstance(item, dict) else getattr(item, 'function', None)
    if function is None and isinstance(item, dict):
        return item.get('name') or ''
    return (function.get('name') if isinstance(function, dict) else getattr(function, 'name', '')) or ''


def only_offered(tool_calls: list, tools: list | None) -> list:
    """Drop tool calls for actions this request did not offer. Extensions hide actions per NPC (a stranger gets no
    sex or follower actions); a model that names a hidden action anyway (an injected line, a guess) must not run it."""
    offered = {_tool_name(t) for t in tools or []}
    kept = [call for call in tool_calls if _tool_name(call) in offered]
    for call in tool_calls:
        if _tool_name(call) not in offered:
            logger.warning(f"Ignored a call to action '{_tool_name(call)}': it was not offered for this reply")
    return kept


def read_key_text(path) -> str:
    """Text of a key file, whatever editor saved it: UTF-8 with or without a BOM, UTF-16 with a BOM (PowerShell 5.1
    `>`), else the system code page."""
    raw = Path(path).read_bytes()
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode('utf-16')
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors='replace')


def clean_key(text) -> str:
    """The key from a key file's text or a secret_keys.json value: the first non-empty line, without surrounding
    spaces or matching quotes."""
    for line in str(text).splitlines():
        line = line.strip()
        if len(line) >= 2 and line[0] == line[-1] and line[0] in ('"', "'"):
            line = line[1:-1].strip()
        if line:
            return line
    return ''


class VisionMode(Enum):
    """Vision operating modes for LLM image context"""
    DISABLED = "disabled" # No image client configured
    ALWAYS_ON = "always_on" # Image client active, no Vision action active
    ON_DEMAND = "on_demand" # Image client active, Vision action controls enablement


class ClientBase(AIClient):
    '''Base class for connecting to OpenAI-compatible endpoints

    Handles API key management, client generation (sync/async), request execution,
    token counting, endpoint resolution, and model list retrieval
    '''
    api_token_limits = {}
    # Extra attempts when a streamed reply comes back with no text and no tool call
    EMPTY_REPLY_RETRIES = 2
    tiktoken_cache_dir = "data"
    os.environ["TIKTOKEN_CACHE_DIR"] = tiktoken_cache_dir

    def __init__(self, api_url: str, llm: str, llm_params: dict[str, Any] | None, custom_token_count: int, prompt_caching_enabled: bool = False) -> None:
        '''
        Args:
            api_url (str): The API endpoint URL or a known service name (e.g., 'OpenAI', 'OpenRouter')
            llm (str): The name of the language model to use
            llm_params (dict[str, Any] | None): Additional parameters for the LLM requests (eg temperature, max_tokens)
            custom_token_count (int): A fallback token limit if the model's limit isn't known
            prompt_caching_enabled (bool): Whether Claude prompt caching is enabled for OpenRouter
        '''
        super().__init__()
        self._generation_lock: Lock = Lock()
        self._base_url = self._get_endpoint(api_url)
        # Anthropic (Native, cached) talks to the Messages API through the Anthropic SDK instead of the OpenAI client
        self._is_native: bool = self._base_url == utils.ANTHROPIC_NATIVE_ENDPOINT
        self._thinking_by_tool_call: dict[str, Any] = {}
        self._model_name: str = ClientBase._anthropic_model_id(self._base_url, llm)
        self._startup_async_client: AsyncOpenAI | None = None
        self._request_params: dict[str, Any] | None = ClientBase._strip_unsupported_anthropic_params(self._base_url, llm_params)
        self._image_client = None
        self._function_client = None
        self._enable_vision_next_call: bool = False
        self._vision_mode: VisionMode = VisionMode.DISABLED

        if not utils.is_local_url(self._base_url): # Cloud LLM
            self._is_local: bool = False
            api_key = ClientBase._get_api_key(api_url)
            if api_key:
                self._api_key = api_key
            else:
                self._api_key: str = 'abc123'
        else: # Local LLM
            self._is_local: bool = True
            self._api_key: str = 'abc123'

        referer = "https://art-from-the-machine.github.io/Mantella/"
        xtitle = "Mantella"
        self._header: dict[str, str] = {"HTTP-Referer": referer, "X-Title": xtitle}
        self._claude_cache = ClaudeCacheConnector()
        self._caching_enabled: bool = prompt_caching_enabled
        self._token_limit: int = self.__get_token_limit(self._model_name, custom_token_count, self._is_local)
        self._encoding = self.__get_model_encoding(api_url, self._model_name)


    def _determine_vision_mode(self) -> VisionMode:
        """Determine vision operating mode based on configuration and action availability
        
        Returns:
            VisionMode: The operating mode for vision
        """
        if not self._image_client:
            return VisionMode.DISABLED
        
        if FunctionManager.is_vision_action_active():
            return VisionMode.ON_DEMAND
        else:
            return VisionMode.ALWAYS_ON
    
    def enable_vision_for_next_call(self):
        """Enable vision for the next streaming_call"""
        self._enable_vision_next_call = True

    def _should_enable_vision(self) -> bool:
        """Determine if vision should be enabled for this LLM call"""
        # Vision in always-on mode (Vision enabled and vision action is inactive)
        if self._vision_mode == VisionMode.ALWAYS_ON:
            return True
        # Vision explicitly requested via tool call
        elif self._enable_vision_next_call:
            return True
        else:
            return False


    @property
    def token_limit(self) -> int:
        """The token limit of the model
        """
        return self._token_limit

    @property
    def model_name(self) -> str:
        """The name of the model
        """
        return self._model_name
    
    @property
    def is_local(self) -> bool:
        """Is the model run locally?
        """
        return self._is_local
    
    @property
    def api_key(self) -> str:
        """The secret key
        """
        return self._api_key

    @property
    def max_tokens_param(self) -> int:
        """Returns the max_tokens value from request params, or defaults to 250"""
        return self._request_params.get("max_tokens", 250) if self._request_params else 250


    @utils.time_it
    def generate_async_client(self) -> AsyncOpenAI:
        """Generates a new AsyncOpenAI client already setup to be used right away.
        Close the client after usage using 'await client.close()'

        The client needs to be closed after every call (and a new one created for the next call) to avoid connection issues

        Use :func:`streaming_call` for a normal streaming call to the LLM

        Returns:
            AsyncOpenAI: The new async client object
        """
        return AsyncOpenAI(api_key=self._api_key, base_url=self._base_url, default_headers=self._header)


    @utils.time_it
    def generate_sync_client(self) -> OpenAI:
        """Generates a new OpenAI client already setup to be used right away.
        Close the client after usage using 'client.close()'

        Use :func:`request_call` for a normal call to the LLM

        Returns:
            OpenAI: The new sync client object
        """
        return OpenAI(api_key=self._api_key, base_url=self._base_url, default_headers=self._header)


    def generate_native_client(self, is_async: bool):
        """Anthropic SDK client for Anthropic (Native, cached). Close it after every call, like the OpenAI clients."""
        import anthropic
        if is_async:
            return anthropic.AsyncAnthropic(api_key=self._api_key)
        return anthropic.Anthropic(api_key=self._api_key)


    @utils.time_it
    def _native_request_call(self, messages: Message | message_thread) -> str | None:
        """One-shot (non-streamed) request through the Messages API, used for summaries"""
        from src.llm import anthropic_native
        with self._generation_lock:
            logger.log(28, 'Getting LLM response...')
            if isinstance(messages, Message) or isinstance(messages, ImageMessage):
                openai_messages = [messages.get_openai_message()]
            else:
                openai_messages = messages.get_openai_messages()
            # one-off prompts are not reused, so caching them would only add the cache-write cost
            request = anthropic_native.build_request(self.model_name, openai_messages, self._request_params, None, {}, cache=False)
            client = self.generate_native_client(is_async=False)
            try:
                return anthropic_native.reply_text(client.messages.create(**request)) or None
            except Exception as e:
                logger.error(f"Anthropic (Native) request failed: {e}")
                return None
            finally:
                client.close()


    @utils.time_it
    def _request_call_full(self, messages: Message | message_thread) -> ChatCompletion | None:
        """Returns the full chat completion object
        
        Args:
            messages: The messages to send to the LLM
            
        Returns:
            The full chat completion object or None if the request failed
        """
        with self._generation_lock:
            sync_client = self.generate_sync_client()        
            chat_completion: ChatCompletion = None
            logger.log(28, 'Getting LLM response...')

            if isinstance(messages, Message) or isinstance(messages, ImageMessage):
                openai_messages = [messages.get_openai_message()]
            else:
                openai_messages = messages.get_openai_messages()
            
            if self._request_params:
                request_params = self._request_params
            else:
                request_params: dict[str, Any] = {}

            # Apply Claude cache breakpoint after all message transformations
            if self._caching_enabled and self._claude_cache.is_applicable(self._base_url, self._model_name):
                try:
                    openai_messages = self._claude_cache.transform_messages(openai_messages)
                except Exception as e:
                    logger.debug(f"Claude caching transform failed: {e}")

            try:
                chat_completion = sync_client.chat.completions.create(
                    model=self.model_name,
                    messages=openai_messages,
                    **request_params,
                )
            except RateLimitError:
                logger.warning('Could not connect to LLM API, retrying in 5 seconds...')
                time.sleep(5)
            finally:
                sync_client.close()

            return chat_completion


    @utils.time_it
    def request_call(self, messages: Message | message_thread) -> str | None:
        """Makes a request to the LLM and returns just the message content
        
        Args:
            messages: The messages to send to the LLM
            
        Returns:
            The LLM response message content or None if the request failed
        """
        if self._is_native:
            reply = self._native_request_call(messages)
            if not reply:
                logger.info(f"LLM Response failed")
            return reply
        chat_completion: ChatCompletion = self._request_call_full(messages)
        
        if (
            not chat_completion or 
            not chat_completion.choices or 
            chat_completion.choices.__len__() < 1 or 
            not chat_completion.choices[0].message.content
        ):
            logger.info(f"LLM Response failed")
            return None
        
        reply = chat_completion.choices[0].message.content
        return reply
        
    
    @utils.time_it
    async def streaming_call(self, messages: Message | message_thread, is_multi_npc: bool, tools: list[dict] = None) -> AsyncGenerator[tuple[str, str | list] | None, None]:
        with create_span_from_thread("llm_streaming_call") as span:
            with self._generation_lock:
                logger.log(28, 'Getting LLM response...')

                if self._request_params:
                    request_params = self._request_params.copy() # copy of self._request_params to allow temporary override
                else:
                    request_params: dict[str, Any] = {}
                if is_multi_npc: # override max_tokens to be at least 250 in radiant / multi-NPC conversations
                    request_params["max_tokens"] = max(self.max_tokens_param, 250)
                async_client = None
                try:
                    # Prepare the messages including the image if provided
                    vision_hints = ''
                    if isinstance(messages, Message):
                        openai_messages = [messages.get_openai_message()]
                        if isinstance(messages, UserMessage):
                            vision_hints = messages.get_ingame_events_text()
                    else:
                        openai_messages = messages.get_openai_messages()
                        last_message = messages.get_last_message()
                        if isinstance(last_message, UserMessage):
                            vision_hints = last_message.get_ingame_events_text()
                    
                    # Determine if vision should be enabled for this call
                    if self._should_enable_vision():
                        if self._image_client:
                            openai_messages = self._image_client.add_image_to_messages(openai_messages, vision_hints)
                            logger.log(23, f"Vision enabled for this LLM call")
                        else:
                            logger.warning("Vision tool called but Vision not enabled in config - ignoring")
                        self._enable_vision_next_call = False  # Reset flag after use

                    # Extensions (none installed = no change): trim the actions, add text after the cache point
                    extensions = src_extensions.registry()
                    ext_ctx = {'model': self.model_name, 'is_multi_npc': is_multi_npc, 'messages': openai_messages}
                    if tools:
                        tools = extensions.filter('filter_tools', tools, ext_ctx)
                    extra_texts = extensions.collect('extra_user_text', ext_ctx)
                    if extra_texts and not self._is_native:
                        openai_messages = self._with_extra_text(openai_messages, extra_texts)
                    if not self._is_native and extensions.has('system_parts'):
                        openai_messages = self._with_system_parts(openai_messages, lambda parts: extensions.filter('system_parts', parts, ext_ctx))

                    # Handle tool calling: use dedicated function client if available, otherwise use main LLM
                    if tools:
                        if self._function_client:
                            pre_fetched_tool_calls = only_offered(self._function_client.check_for_actions(messages, tools) or [], tools)
                            if pre_fetched_tool_calls:
                                yield ("tool_calls", pre_fetched_tool_calls)
                        else:
                            # If custom function LLM isn't enabled, let the main LLM handle tool calling as well as text generation
                            request_params["tools"] = tools
                    
                    # Apply Claude cache breakpoint after all message transformations
                    if not self._is_native and self._caching_enabled and self._claude_cache.is_applicable(self._base_url, self._model_name):
                        try:
                            openai_messages = self._claude_cache.transform_messages(openai_messages)
                        except Exception as e:
                            logger.debug(f"Claude caching transform failed: {e}")

                    native_request = None
                    if self._is_native:
                        from src.llm import anthropic_native
                        system_parts = (lambda parts: extensions.filter('system_parts', parts, ext_ctx)) if extensions.has('system_parts') else None
                        native_request = anthropic_native.build_request(self.model_name, openai_messages, request_params,
                                                                        request_params.get("tools"), self._thinking_by_tool_call, cache=True,
                                                                        extra_texts=extra_texts, system_parts=system_parts)

                    # A stream that ends with no text and no tool call would leave the NPC silent.
                    # Nothing has been yielded yet in that case, so ask again before giving up.
                    for attempt in range(1 + self.EMPTY_REPLY_RETRIES):
                        if native_request is not None:
                            state = anthropic_native.StreamState()
                            async_client = self.generate_native_client(is_async=True)
                            try:
                                async for text in anthropic_native.stream_reply(async_client, native_request, self._thinking_by_tool_call, state):
                                    yield ("content", text)
                            finally:
                                await async_client.close()
                                async_client = None
                            if state.usage:
                                extensions.record_usage({'time': time.time(), 'model': self.model_name, **state.usage,
                                                         'cost': anthropic_native.reply_cost(self.model_name, state.usage)})
                            offered_calls = only_offered(list(state.tool_calls), request_params.get("tools"))
                            if offered_calls:
                                yield ("tool_calls", offered_calls)
                            if state.got_text or state.tool_calls:
                                if attempt > 0:
                                    logger.log(23, f"Empty-reply safety net: retry {attempt} succeeded")
                                break
                            if attempt < self.EMPTY_REPLY_RETRIES:
                                logger.warning(f"Empty-reply safety net: the LLM returned nothing (stop_reason={state.stop_reason}, stop_sequence={state.stop_sequence!r}), retrying ({attempt + 1}/{self.EMPTY_REPLY_RETRIES})...")
                            else:
                                logger.warning(f"Empty-reply safety net: still nothing after {self.EMPTY_REPLY_RETRIES} retries (stop_reason={state.stop_reason}, stop_sequence={state.stop_sequence!r})")
                            continue

                        # Create async client for main LLM streaming (after function client has run if applicable)
                        if self._startup_async_client:
                            async_client = self._startup_async_client
                            self._startup_async_client = None # do not reuse the same client
                        else:
                            async_client = self.generate_async_client()

                        # Dict to track partial tool calls by index
                        accumulated_tool_calls = {}
                        got_content = False
                        finish_reason = None

                        try:
                            async for chunk in await async_client.chat.completions.create(
                                model=self.model_name,
                                messages=openai_messages,
                                stream=True,
                                **request_params,
                            ):
                                try:
                                    if chunk and chunk.choices and chunk.choices[0].finish_reason:
                                        finish_reason = chunk.choices[0].finish_reason
                                    if chunk and chunk.choices and chunk.choices[0].delta:
                                        delta = chunk.choices[0].delta

                                        # Handle regular content
                                        if delta.content:
                                            got_content = True
                                            yield ("content", delta.content)

                                        # Accumulate tool calls by index
                                        if delta.tool_calls:
                                            for tool_call in delta.tool_calls:
                                                idx = tool_call.index
                                                if idx not in accumulated_tool_calls:
                                                    accumulated_tool_calls[idx] = {
                                                        "id": tool_call.id if tool_call.id else "",
                                                        "type": "function",
                                                        "function": {
                                                            "name": "",
                                                            "arguments": ""
                                                        }
                                                    }

                                                # Accumulate the parts
                                                if tool_call.id:
                                                    accumulated_tool_calls[idx]["id"] = tool_call.id
                                                if tool_call.function and tool_call.function.name:
                                                    accumulated_tool_calls[idx]["function"]["name"] += tool_call.function.name
                                                if tool_call.function and tool_call.function.arguments:
                                                    accumulated_tool_calls[idx]["function"]["arguments"] += tool_call.function.arguments

                                except Exception as e:
                                    logger.error(f"LLM API Connection Error: {e}")
                                    break
                        finally:
                            await async_client.close()
                            async_client = None

                        # After streaming completes, yield any accumulated tool calls
                        if accumulated_tool_calls:
                            tool_calls_list = only_offered([accumulated_tool_calls[i] for i in sorted(accumulated_tool_calls.keys())], request_params.get("tools"))
                            if tool_calls_list:
                                yield ("tool_calls", tool_calls_list)

                        if got_content or accumulated_tool_calls:
                            if attempt > 0:
                                logger.log(23, f"Empty-reply safety net: retry {attempt} succeeded")
                            break
                        if attempt < self.EMPTY_REPLY_RETRIES:
                            logger.warning(f"Empty-reply safety net: the LLM returned nothing (finish_reason={finish_reason}), retrying ({attempt + 1}/{self.EMPTY_REPLY_RETRIES})...")
                        else:
                            logger.warning(f"Empty-reply safety net: still nothing after {self.EMPTY_REPLY_RETRIES} retries (finish_reason={finish_reason})")
                except Exception as e:
                    utils.play_error_sound()
                    if isinstance(e, APIConnectionError):
                        if e.code in [401, 'invalid_api_key']: # incorrect API key
                            if self._base_url == 'https://api.openai.com/v1':
                                service_connection_attempt = 'OpenRouter' # check if player means to connect to OpenRouter
                            else:
                                service_connection_attempt = 'OpenAI' # check if player means to connect to OpenAI
                            logger.error(f"Invalid API key. If you are trying to connect to {service_connection_attempt}, please choose an {service_connection_attempt} model via the 'model' setting in MantellaSoftware/config.ini. If you are instead trying to connect to a local model, please ensure the service is running.")
                        else:
                            logger.error(f"LLM API Error: {e}")
                    elif isinstance(e, BadRequestError):
                        if (e.type == 'invalid_request_error') and (self._image_client): # invalid request
                            logger.error(f"Invalid request. Try disabling Vision in Mantella's settings and try again.")
                        else:
                            logger.error(f"LLM API Streaming Error: {e}")
                    else:
                        logger.error(f"LLM API Streaming Error: {e}")
                finally:
                    if async_client:
                        await async_client.close()


    @classmethod
    @utils.time_it
    def _get_endpoint(cls, value: str) -> str:
        '''Resolve a service name or alias to an endpoint URL.
        Returns the normalized input as-is if not a known service (assumed to be a direct URL).'''
        return utils.resolve_service_endpoint(value)
    

    @staticmethod
    def _with_system_parts(openai_messages: list[dict], system_parts) -> list[dict]:
        """Copy of the messages with the system prompt rebuilt from the extensions' parts, in their order (the same
        layout the native path sends, so the unchanged start of the prompt stays cacheable on other services)."""
        from src.llm import anthropic_native
        if not openai_messages or openai_messages[0].get('role') != 'system' or not isinstance(openai_messages[0].get('content'), str):
            return openai_messages
        text = anthropic_native.join_system_parts(system_parts, openai_messages[0]['content'])
        return [dict(openai_messages[0], content=text)] + openai_messages[1:]

    @staticmethod
    def _with_extra_text(openai_messages: list[dict], texts: list[str]) -> list[dict]:
        """Copy of the messages with extension text added to the newest user message."""
        for index in range(len(openai_messages) - 1, -1, -1):
            message = openai_messages[index]
            if message.get('role') != 'user':
                continue
            content = message.get('content')
            if isinstance(content, list):
                content = list(content) + [{'type': 'text', 'text': text} for text in texts]
            else:
                content = '\n'.join([content or ''] + texts).strip()
            return openai_messages[:index] + [dict(message, content=content)] + openai_messages[index + 1:]
        return openai_messages

    @staticmethod
    def _anthropic_model_id(base_url: str, model: str) -> str:
        '''Turns a saved model-list label such as "Claude Haiku 5.5 (claude-haiku-5-5) | Context: 1m" back into its model ID'''
        if not utils.is_anthropic_endpoint(base_url) or not model:
            return model
        name = model.split(' | ')[0].strip()
        if name.endswith(')') and '(' in name:
            model_id = name[name.rfind('(') + 1:-1].strip()
            if model_id:
                logger.info(f"Anthropic: using model ID '{model_id}' from the saved label '{model}'")
                return model_id
        return name


    @staticmethod
    def _strip_unsupported_anthropic_params(base_url: str, llm_params: dict[str, Any] | None) -> dict[str, Any] | None:
        '''Removes request parameters that the Anthropic endpoint rejects or that only apply to OpenRouter.
        Current Claude models return a 400 for non-default sampling values, so temperature / top_p / top_k are dropped.'''
        if not utils.is_anthropic_endpoint(base_url) or not llm_params:
            return llm_params
        params = dict(llm_params)
        removed = [key for key in ('temperature', 'top_p', 'top_k') if params.pop(key, None) is not None]
        extra_body = params.get('extra_body')
        if isinstance(extra_body, dict) and 'provider' in extra_body:
            extra_body = dict(extra_body)
            extra_body.pop('provider')
            removed.append('extra_body.provider')
            if extra_body:
                params['extra_body'] = extra_body
            else:
                params.pop('extra_body')
        if removed:
            logger.info(f"Anthropic: ignoring LLM parameters not supported by Anthropic: {', '.join(removed)}")
        return params


    def __get_llm_priority(self, llm: str, priority: str, api_url: str) -> str:
        '''https://openrouter.ai/docs/features/provider-routing'''
        # Priority is only compatible with OpenRouter
        if self._get_endpoint(api_url) != 'https://openrouter.ai/api/v1':
            return ''
        
        # Free models cannot have a priority
        if llm.endswith(':free'):
            return ''
        
        priorities = {
            'Balanced': '',
            'Price': ':price', 
            'Speed': ':nitro'
        }
        return priorities.get(priority, '')
    

    @utils.time_it
    @staticmethod
    def _get_api_key(service: str, show_error: bool = True) -> str | None:
        '''Resolves an API key for the given service.

        Lookup order:
        1. secret_keys.json in the mod folder
        2. secret_keys.json next to the executable
        3. GPT_SECRET_KEY.txt in the mod folder
        4. GPT_SECRET_KEY.txt next to the executable

        Args:
            service (str): The LLM service name / URL
        '''
        mod_parent_folder = Path(utils.resolve_path()).parent.parent.parent
        target_service = ClientBase._get_endpoint(service)

        api_key = None
        for folder in [mod_parent_folder, Path('.')]:
            json_path = folder / 'secret_keys.json'
            try:
                keys_dict = json.loads(read_key_text(json_path))
                if not isinstance(keys_dict, dict):
                    logger.warning(f'{json_path} should hold {{"service name": "key"}} pairs; it was ignored')
                    keys_dict = {}
                for api_name, api_key_val in keys_dict.items():
                    if ClientBase._get_endpoint(str(api_name)) == target_service:
                        matching_api_key = clean_key(api_key_val)
                        if matching_api_key:
                            api_key = matching_api_key
                            break
                if api_key:
                    break
            except (FileNotFoundError, PermissionError, ValueError):  # ValueError covers bad JSON and text
                pass

        # Anthropic has its own key file and never falls back to GPT_SECRET_KEY.txt (that key belongs to another service)
        is_anthropic = utils.is_anthropic_endpoint(target_service)
        key_file_name = 'ANTHROPIC_SECRET_KEY.txt' if is_anthropic else 'GPT_SECRET_KEY.txt'

        if not api_key:
            for folder in [mod_parent_folder, Path('.')]:
                try:
                    val = clean_key(read_key_text(folder / key_file_name))
                    if val:
                        api_key = val
                        break
                except (FileNotFoundError, PermissionError):
                    pass

        if not api_key or api_key == '':
                if show_error and is_anthropic:
                    utils.play_error_sound()
                    logger.critical(f'''No secret key found for service '{service}' in ANTHROPIC_SECRET_KEY.txt.
Please create an API key in the Claude Console (https://platform.claude.com/settings/keys) and paste it in ANTHROPIC_SECRET_KEY.txt, in the same folder as GPT_SECRET_KEY.txt.''')
                    time.sleep(3)
                elif show_error:
                    utils.play_error_sound()
                    logger.critical(f'''No secret key found for service '{service}' in GPT_SECRET_KEY.txt.
Please create a secret key and paste it in your Mantella mod folder's GPT_SECRET_KEY.txt file.
If you are using OpenRouter (default), you can create a secret key in Account -> Keys once you have created an account: https://openrouter.ai/
If using OpenAI, see here on how to create a secret key: https://help.openai.com/en/articles/4936850-where-do-i-find-my-openai-api-key
If you are running a model locally, please ensure the service (eg Kobold / Text generation web UI) is selected and running.
For more information, see here: https://art-from-the-machine.github.io/Mantella/''')
                    time.sleep(3)

        return api_key
    

    @utils.time_it
    def __get_token_limit(self, llm, custom_token_count: int, is_local):
        '''Determines the token limit for the given LLM, using known values, manual overrides, or a default

        Args:
            llm (str): The name of the language model
            custom_token_count (int): A user-defined fallback token count from the config
            is_local (bool): Whether the model is running locally

        Returns:
            token_limit (int): The determined token limit for the model
        '''
        manual_limits = utils.get_model_token_limits()
        token_limit_dict = {**self.api_token_limits, **manual_limits}

        if '/' in llm:
            llm = llm.split('/')[-1]

        if llm in token_limit_dict:
            token_limit = token_limit_dict[llm]
        else:
            logger.log(23, f"Could not find number of available tokens for {llm}. Defaulting to token count of {custom_token_count}. This number can be changed via the `Large Language Model`->`Custom Token Count` / `Vision`->`Custom Vision Model Token Count` settings in the Mantella UI")
            try:
                token_limit = custom_token_count
            except ValueError:
                logger.error(f"Invalid custom_token_count value: {custom_token_count}. It should be a valid integer. Please update your configuration.")
                token_limit = 4096  # Default to 4096 in case of an error.
        if token_limit <= 4096:
            if is_local:
                llm = 'Local language model'
            logger.warning(f"{llm} has a low token count of {token_limit}. For better NPC memories, try changing to a model with a higher token count")
        
        return token_limit
    

    @utils.time_it
    def __get_model_encoding(self, api_url: str, llm: str) -> tiktoken.Encoding:
        '''Gets the appropriate tiktoken encoding for the specified model or a sensible default

        Args:
            api_url (str): The API service/URL being used
            llm (str): The name of the language model

        Returns:
            tiktoken.Encoding: The encoding object for token counting
        '''
        chosenmodel = llm
        # if using an alternative API to OpenAI, use encoding for GPT-3.5 by default
        # NOTE: this encoding may not be the same for all models, leading to incorrect token counts
        #       this can lead to the token limit of the given model being overrun
        try:
            if api_url == 'OpenAI':
                encoding = tiktoken.encoding_for_model(chosenmodel) # get encoding for specific model
            else:
                encoding = tiktoken.get_encoding('cl100k_base') # get generic encoding
        except:
            try:
                encoding = tiktoken.get_encoding('cl100k_base') # try loading a generic encoding
            except:
                logger.error('Error loading model. If you are using an alternative to OpenAI, please find the setting `Large Language Model`->`LLM Service` in the Mantella UI and follow the instructions to change this setting')
                raise
        
        return encoding
    
    @utils.time_it
    def get_count_tokens(self, messages: message_thread | list[Message] | Message | str) -> int:
        if isinstance(messages, message_thread | list) :
            return self.__num_tokens_from_messages(messages)
        elif isinstance(messages, Message):
            return self.__num_tokens_from_message(messages)
        else:
            return len(self._encoding.encode(messages))

    @utils.time_it
    def is_too_long(self, messages: message_thread | list[Message] | Message | str, token_limit_percent: float) -> bool:
        countTokens: int = self.get_count_tokens(messages)
        return countTokens > self.token_limit * token_limit_percent

    @utils.time_it
    def __num_tokens_from_messages(self, messages: message_thread | list[Message]) -> int:
        '''Calculates token count for a list of messages formatted for OpenAI API calls

        Args:
            messages (message_thread | list[Message]): The messages to count tokens for

        Returns:
            num_tokens (int): The estimated total token count
        '''
        messages_to_check = []
        if isinstance(messages, message_thread):
            messages_to_check = messages.get_openai_messages()
        else:
            for m in messages:
                messages_to_check.append(m.get_openai_message())

        # note: this calculation is based on GPT-3.5, future models may deviate from this
        num_tokens = 0
        for message in messages_to_check:
            num_tokens += 4  # every message follows <im_start>{role/name}\n{content}<im_end>\n
            for key, value in message.items():
                if isinstance(value, str):
                    num_tokens += len(self._encoding.encode(value))
                    if key == "name":  # if there's a name, the role is omitted
                        num_tokens += -1  # role is always required and always 1 token
        num_tokens += 2  # every reply is primed with <im_start>assistant
        return num_tokens
    
    @utils.time_it
    def __num_tokens_from_message(self, message_to_measure: Message | str) -> int:
        '''Calculates the approximate token count for a single Message object or string,
        considering the overhead of the message structure

        Args:
            message_to_measure (Message | str): The message or string content

        Returns:
            num_tokens (int): Estimated token count
        '''
        text: str = ""
        if isinstance(message_to_measure, Message):
            text = message_to_measure.get_formatted_content()
        else:
            text = message_to_measure

        num_tokens = 4 # every message follows <im_start>{role/name}\n{content}<im_end>\n
        num_tokens += len(text)
        if isinstance(message_to_measure, Message) and message_to_measure.get_openai_message().__contains__("name"):# if there's a name, the role is omitted
            num_tokens += -1# role is always required and always 1 token
        
        return num_tokens
    
    @staticmethod
    def _get_anthropic_model_list(is_vision: bool, show_key_error: bool) -> LLMModelList:
        '''Lists the models available to the Anthropic API key via GET /v1/models (live, so new models appear automatically)'''
        default_model = "claude-haiku-5-5"
        secret_key = ClientBase._get_api_key("Anthropic", show_error=show_key_error)
        if not secret_key:
            return LLMModelList([("No secret key found in ANTHROPIC_SECRET_KEY.txt", "Custom model")], "Custom model", allows_manual_model_input=True)
        try:
            headers = {"x-api-key": secret_key, "anthropic-version": "2023-06-01"}
            models: list[dict] = []
            params: dict[str, Any] = {"limit": 1000}
            while True:
                response = requests.get(f"{utils.ANTHROPIC_ENDPOINT}/models", headers=headers, params=params, timeout=15)
                if response.status_code != 200:
                    raise Exception(f"Anthropic API returned status {response.status_code}: {response.text}")
                page = response.json()
                models.extend(page.get("data", []))
                if not page.get("has_more") or not page.get("last_id"):
                    break
                params["after_id"] = page["last_id"]

            options = []
            for model in models:
                model_id = model.get("id", "unknown")
                try:
                    capabilities = model.get("capabilities") or {}
                    context_size = model.get("max_input_tokens") or 0
                    display_parts = [f"{model.get('display_name', model_id)} ({model_id})"]
                    if context_size:
                        display_parts.append(f"Context: {utils.format_context_size(context_size)}")
                        ClientBase.api_token_limits[model_id] = context_size
                    if (capabilities.get("image_input") or {}).get("supported"):
                        display_parts.append("✅ Vision")
                    display_parts.append("✅ Advanced Actions")
                    if (capabilities.get("thinking") or {}).get("supported"):
                        display_parts.append("⚠️ Reasoning")
                    display_name = " | ".join(display_parts)
                except Exception:
                    display_name = model_id
                options.append((display_name, model_id))

            if is_vision and any('✅ Vision' in name for name, _ in options):
                options = [(name, model_id) for name, model_id in options if '✅ Vision' in name]
            if not any(model_id == default_model for _, model_id in options) and options:
                default_model = options[0][1]
            # no manual input: with custom values allowed, the UI saves the display label instead of the model ID
            return LLMModelList(options, default_model, allows_manual_model_input=False)
        except Exception as e:
            utils.play_error_sound()
            error = f"Failed to retrieve list of models from Anthropic. A valid API key in 'ANTHROPIC_SECRET_KEY.txt' is required. Error: {e}"
            return LLMModelList([(error, "error")], "error", allows_manual_model_input=False)

    @staticmethod
    def get_model_list(service: str, default_model: str = "google/gemma-4-26b-a4b-it:free", is_vision: bool = False, is_tool_calling: bool = False, show_key_error: bool = False) -> LLMModelList:
        if utils.is_anthropic_endpoint(ClientBase._get_endpoint(service)):
            return ClientBase._get_anthropic_model_list(is_vision, show_key_error)
        if service not in ['OpenAI', 'OpenRouter', 'NanoGPT']:
            return LLMModelList([("Custom model","Custom model")], "Custom model", allows_manual_model_input=True)
        try:
            if service == "OpenAI":
                default_model = "gpt-4o-mini"
                models = utils.get_openai_model_list()
                # OpenAI models are not a "live" list, so manual input needs to be allowed for when new models not listed are released
                allow_manual_model_input = True
            elif service == "OpenRouter":
                default_model = default_model
                secret_key = ClientBase._get_api_key("OpenRouter", show_error=show_key_error)
                if not secret_key:
                    return LLMModelList([(f"No secret key found for OpenRouter", "Custom model")], "Custom model", allows_manual_model_input=True)
                # NOTE: while a secret key is not needed for this request, this may change in the future
                client = OpenAI(api_key=secret_key, base_url='https://openrouter.ai/api/v1')
                # don't log initial 'HTTP Request: GET https://openrouter.ai/api/v1/models "HTTP/1.1 200 OK"'
                models = client.models.list()
                client.close()
                allow_manual_model_input = False
            elif service == "NanoGPT":
                default_model = "mistral-small-31-24b-instruct"
                secret_key = ClientBase._get_api_key("NanoGPT", show_error=show_key_error)
                if not secret_key:
                    return LLMModelList([(f"No secret key found for NanoGPT", "Custom model")], "Custom model", allows_manual_model_input=True)
                
                # Use requests to call NanoGPT with detailed=true parameter
                # (the OpenAI SDK's models.list() doesn't support the ?detailed=true query param
                #  needed to get pricing, context length, and capabilities data)
                headers = {"Authorization": f"Bearer {secret_key}"}
                url = "https://nano-gpt.com/api/v1/models?detailed=true"
                response = requests.get(url, headers=headers)
                
                if response.status_code != 200:
                    raise Exception(f"NanoGPT API returned status {response.status_code}: {response.text}")
                
                models_data = response.json()
                allow_manual_model_input = False

            options = []
            multiplier = 1_000_000

            # Handle different response formats based on service
            if service == "NanoGPT":
                # NanoGPT returns detailed JSON response directly
                models_list = models_data.get("data", [])
                for model in models_list:
                    try:
                        model_id = model.get("id", "unknown")
                        model_name = model.get("name", model_id)
                        context_size = model.get("context_length", 0)
                        pricing = model.get("pricing", {})
                        capabilities = model.get("capabilities", {})
                        
                        # Always try to show detailed information, even if some parts are missing
                        model_display_parts = [f"{model_name} ({model_id})"]
                        
                        # Add context size if available
                        if context_size and context_size > 0:
                            model_display_parts.append(f"Context: {utils.format_context_size(context_size)}")
                            ClientBase.api_token_limits[model_id.split('/')[-1]] = context_size
                        
                        # Add pricing if available (NanoGPT prices are already in USD per million tokens)
                        if pricing:
                            prompt_cost = float(pricing.get("prompt", 0))
                            completion_cost = float(pricing.get("completion", 0))
                            
                            if prompt_cost >= 0 or completion_cost >= 0:
                                cost_parts = []
                                cost_parts.append(f"Prompt: {utils.format_price(prompt_cost)}")
                                cost_parts.append(f"Completion: {utils.format_price(completion_cost)}")
                                model_display_parts.append(f"Cost per 1M tokens: {'. '.join(cost_parts)}")
                        
                        # Add capability markers using the capabilities field
                        if capabilities.get("vision"):
                            model_display_parts.append("✅ Vision")
                        if capabilities.get("tool_calling"):
                            model_display_parts.append("✅ Advanced Actions")
                        if capabilities.get("reasoning"):
                            model_display_parts.append("⚠️ Reasoning")
                        
                        # Join all available parts with " | "
                        model_display_name = " | ".join(model_display_parts)
                     
                    except Exception as e:
                        # Fallback to model ID if parsing fails
                        model_display_name = model.get("id", "unknown")
                    
                    options.append((model_display_name, model.get("id", "unknown")))
            else:
                # OpenRouter and OpenAI use the existing models.data format
                for model in models.data:
                    try:
                        if model.model_extra:
                            context_size: int = model.model_extra["context_length"]
                            prompt_cost: float = float(model.model_extra["pricing"]["prompt"]) * multiplier
                            completion_cost: float = float(model.model_extra["pricing"]["completion"]) * multiplier
                            vision_available: str = ' | ✅ Vision' if model.model_extra["architecture"]["modality"] == 'text+image->text' else ''
                            tool_calling_available: str = ' | ✅ Advanced Actions' if 'tools' in model.model_extra['supported_parameters'] else ''
                            reasoning_model: str = ' | ⚠️ Reasoning' if 'reasoning' in model.model_extra['supported_parameters'] else ''
                            cache_read = model.model_extra["pricing"].get("input_cache_read")
                            cache_text = f". Cache read: {utils.format_price(float(cache_read) * multiplier)}" if cache_read not in (None, '', '0') else ''
                            # OpenRouter lists one price per model; each provider may charge differently
                            model_display_name = f"{model.id} | Context: {utils.format_context_size(context_size)} | Cost per 1M tokens: Prompt: {utils.format_price(prompt_cost)}. Completion: {utils.format_price(completion_cost)}{cache_text}{vision_available}{tool_calling_available}{reasoning_model}"

                            ClientBase.api_token_limits[model.id.split('/')[-1]] = context_size
                        else:
                            model_display_name = model.id
                    except:
                        model_display_name = model.id
                    options.append((model_display_name, model.id))
            
            # check if any models are marked as vision-capable
            has_vision_models = any('✅ Vision' in name for name, _ in options)
            # filter model list if this is supposed to be a vision model list and there are models explicitly marked as such
            if is_vision and has_vision_models:
                options = [(name, model_id) for name, model_id in options if '✅ Vision' in name]

            # check if any models are marked as tool-calling-capable
            has_tool_calling_models = any('✅ Advanced Actions' in name for name, _ in options)
            # filter model list if this is supposed to be a tool-calling model list and there are models explicitly marked as such
            if is_tool_calling and has_tool_calling_models:
                options = [(name, model_id) for name, model_id in options if '✅ Advanced Actions' in name]
            
            return LLMModelList(options, default_model, allows_manual_model_input=allow_manual_model_input)
        except Exception as e:
            utils.play_error_sound()
            error = f"Failed to retrieve list of models from {service}. A valid API key in 'GPT_SECRET_KEY.txt' is required. The file is in your mod folder of Mantella. Error: {e}"
            return LLMModelList([(error,"error")], "error", allows_manual_model_input=False)
