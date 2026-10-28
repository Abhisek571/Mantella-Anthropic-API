"""Optional extensions: Python packages in the extensions/ folder next to Mantella.exe.

Each package's __init__.py defines register(api) and calls api.on(hook, fn) for the hooks it needs. A package that
fails to import or register is logged and skipped. With no packages, every hook leaves Mantella's own values as
they are, so requests stay exactly as without this module.

Hooks:
    filter_tools(tools, ctx) -> tools        the actions offered to the model
    system_parts(parts, ctx) -> parts        system prompt split into named parts; '__order__' sorts them and
                                             '__breakpoints_after__' names the cache points (Anthropic (Native));
                                             other services get the parts joined in that order
    extra_user_text(ctx) -> str              text added to the newest message, after the cache point
    summarize(ctx) -> bool                   True when the extension saved this conversation's memory itself
    memory_text(text, ctx) -> str            the memory text put into the prompt
    conversation_limit(tokens) -> tokens     save the memory and trim the talk once it is this long (0 = stock:
                                             90% of the model's context)
    ui_tab(gr) -> None                       builds a section inside the "Living World" tab of the settings UI
    ui_page(gr, config) -> None              builds a whole tab of its own (with gr.Tab(...)) after that one; config
                                             is Mantella's ConfigLoader, so a page can read and change settings
Filter hooks run in load order (folder names sorted); returning None keeps the value unchanged.
"""
import importlib.util
import os
import sys
import types
from collections import deque
from typing import Any, Callable

import src.utils as utils

logger = utils.get_logger()

EXTENSIONS_DIR = 'extensions'
HOOKS = ('filter_tools', 'system_parts', 'extra_user_text', 'summarize', 'memory_text', 'conversation_limit', 'ui_tab', 'ui_page')
USAGE_LOG_SIZE = 200


class Api:
    """What an extension's register(api) receives."""

    def __init__(self, registry: 'Registry', name: str):
        self._registry = registry
        self.name = name

    def on(self, hook: str, fn: Callable) -> None:
        if hook not in HOOKS:
            raise ValueError(f"unknown hook '{hook}' (known: {', '.join(HOOKS)})")
        self._registry._hooks.setdefault(hook, []).append((self.name, fn))

    def usage_log(self) -> list[dict]:
        """Token use per reply, oldest first: {time, npc, cache_read, cache_write, uncached, output, model}."""
        return list(self._registry._usage)


class Registry:
    def __init__(self):
        self.names: list[str] = []
        self._hooks: dict[str, list[tuple[str, Callable]]] = {}
        self._usage: deque = deque(maxlen=USAGE_LOG_SIZE)

    def api_for(self, name: str) -> Api:
        return Api(self, name)

    def has(self, hook: str) -> bool:
        return bool(self._hooks.get(hook))

    def hook_fns(self, hook: str) -> list[tuple[str, Callable]]:
        """(extension name, fn) pairs in load order, for callers that need their own error handling."""
        return list(self._hooks.get(hook, []))

    def _call(self, name: str, hook: str, fn: Callable, *args) -> Any:
        try:
            return fn(*args)
        except Exception as e:
            logger.warning(f"Extension '{name}' failed in {hook}: {e}")
            return None

    def filter(self, hook: str, value: Any, *args) -> Any:
        """Passes value through every registered fn(value, *args) in load order; None keeps it."""
        for name, fn in self._hooks.get(hook, []):
            result = self._call(name, hook, fn, value, *args)
            if result is not None:
                value = result
        return value

    def collect(self, hook: str, *args) -> list:
        """Calls every registered fn(*args); returns the non-empty results in load order."""
        results = []
        for name, fn in self._hooks.get(hook, []):
            result = self._call(name, hook, fn, *args)
            if result:
                results.append(result)
        return results

    def record_usage(self, entry: dict) -> None:
        self._usage.append(entry)


def load_all(root: str) -> Registry:
    """Imports each root/<name>/__init__.py that defines register(api), in name order."""
    registry = Registry()
    if not os.path.isdir(root):
        return registry
    # parent package, so an extension can import its own modules relatively (from . import helper)
    parent = types.ModuleType('mantella_extensions')
    parent.__path__ = [os.path.abspath(root)]
    sys.modules['mantella_extensions'] = parent
    for name in sorted(os.listdir(root)):
        init = os.path.join(root, name, '__init__.py')
        if not os.path.isfile(init):
            continue
        module_name = f'mantella_extensions.{name}'
        try:
            spec = importlib.util.spec_from_file_location(module_name, init, submodule_search_locations=[os.path.join(root, name)])
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            register = getattr(module, 'register', None)
            if not callable(register):
                sys.modules.pop(module_name, None)
                logger.warning(f"Extension '{name}' has no register(api); skipped")
                continue
            before = {hook: len(fns) for hook, fns in registry._hooks.items()}
            try:
                register(registry.api_for(name))
            except Exception:
                # drop whatever it registered before failing
                for hook, fns in registry._hooks.items():
                    del fns[before.get(hook, 0):]
                raise
            registry.names.append(name)
            logger.log(23, f"Extension loaded: {name}")
        except Exception as e:
            sys.modules.pop(module_name, None)
            logger.warning(f"Extension '{name}' could not be loaded and was skipped: {e}")
    return registry


_registry: Registry | None = None


def registry() -> Registry:
    """The extensions next to Mantella.exe, loaded on first use."""
    global _registry
    if _registry is None:
        _registry = load_all(EXTENSIONS_DIR)
    return _registry
