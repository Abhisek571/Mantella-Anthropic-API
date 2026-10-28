# Mantella - Direct AI Connect

Adds **Anthropic (Official)** as an LLM service in [Mantella](https://www.nexusmods.com/skyrimspecialedition/mods/98631), so Claude models can be used with your own Anthropic API key instead of through OpenRouter.

For **Mantella 0.14**. Includes **Mantella Dual Input 1.1** (type or speak in the same conversation), so the separate Dual Input mod is no longer needed.

## Features

- **Anthropic (Official)** in the LLM Service dropdowns: conversation, summary, vision and tool-calling LLMs.
- **Anthropic (Native, cached)** for conversations and summaries: Anthropic's own Messages API with prompt caching, so the repeated part of each prompt (bios, memories, earlier lines) costs about a tenth after the first reply. Declined replies fall back to another Claude model on Opus 5.5, Sonnet 5.5 and Fable 5.1.
- The model list is read live from Anthropic and shows each model's name, context size and capabilities.
- Its own key file, `ANTHROPIC_SECRET_KEY.txt`, next to `GPT_SECRET_KEY.txt`. It never falls back to the other key, so you can mix services, for example Claude for conversations and OpenRouter for summaries.
- `temperature`, `top_p`, `top_k` and the OpenRouter `provider` block are dropped for Anthropic requests, since current Claude models reject them.
- **Dual Input** built in: separate keys to type or speak a reply (Mantella's MCM, General page).
- "Goodbye, <name>" in a one-on-one conversation now ends it. In Mantella 0.14 it left the conversation waiting forever.

## Install

1. Download the ZIP from [Releases](https://github.com/Abhisek571/Mantella-Direct-AI-Connect/releases/latest) or [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/196214) and install it as a separate MO2 mod.
2. Open the mod folder and run **Setup Backend.cmd** with **Python 3.11** installed. It patches Mantella 0.14's own Mantella.exe.
3. Place the mod after (below) Mantella in MO2's left pane and enable it. If you have the separate Mantella Dual Input mod, disable it.
4. Put your Anthropic API key in `ANTHROPIC_SECRET_KEY.txt` in the main Mantella mod folder.
5. In the Mantella UI, set **LLM Service** to **Anthropic (Native, cached)** (or **Anthropic (Official)**), press **Update** and pick a model.

See [INSTALL.txt](INSTALL.txt) for details, faster replies (turning thinking off) and removal.

**Limits:** Anthropic (Official) uses Anthropic's OpenAI-compatible endpoint, which has no prompt caching. Vision and tool-calling LLMs offer only Anthropic (Official).

## Build

Requires CPython 3.11 and a supported `Mantella.exe` (hashes in `Source/backend/build-contract.json`).

```bat
py -3.11 setup_backend.py --base "C:\path\to\Mantella.exe"
```

The build checks the input exe hash, that each replaced module is still stock Mantella v0.14 (`Source/upstream`), the source hashes and the output hash.

Changed modules: `src/utils.py`, `src/llm/client_base.py`, `src/config/definitions/llm_definitions.py`, `vision_definitions.py`, `action_definitions.py`, `src/http/routes/mantella_route.py`. Added: `src/llm/anthropic_native.py`, plus the vendored [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) 0.125.0 and `docstring_parser` 0.18.0 (both MIT, in `Source/backend/vendor`). Their other dependencies already ship inside Mantella.exe.

## Source and license

Unofficial patch of [Mantella](https://github.com/art-from-the-machine/Mantella), under **AGPL-3.0-or-later**. See [SOURCE-AND-LICENSES.txt](SOURCE-AND-LICENSES.txt). No Mantella executable, voice models, personal configuration or API keys are bundled.
