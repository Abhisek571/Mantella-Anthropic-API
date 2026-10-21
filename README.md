# Mantella - Anthropic API

Adds **Anthropic (Official)** as an LLM service in [Mantella](https://www.nexusmods.com/skyrimspecialedition/mods/98631), so Claude models can be used with your own Anthropic API key instead of through OpenRouter.

For **Mantella 0.14**. Also works on top of **Mantella Dual Input 1.1** and keeps it working.

## Features

- **Anthropic (Official)** in the LLM Service dropdowns: conversation, summary, vision and tool-calling LLMs.
- The model list is read live from Anthropic and shows each model's name, context size and capabilities.
- Its own key file, `ANTHROPIC_SECRET_KEY.txt`, next to `GPT_SECRET_KEY.txt`. It never falls back to the other key, so you can mix services, for example Claude for conversations and OpenRouter for summaries.
- `temperature`, `top_p`, `top_k` and the OpenRouter `provider` block are dropped for Anthropic requests, since current Claude models reject them.

## Install

1. Download the ZIP from [Releases](https://github.com/Abhisek571/Mantella-Anthropic-API/releases/latest) or [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/196214) and install it as a separate MO2 mod.
2. Open the mod folder and run **Setup Backend.cmd** with **Python 3.11** installed. It patches Mantella Dual Input 1.1 if installed, otherwise Mantella 0.14.
3. Place the mod after (below) the mod setup names in MO2's left pane and enable it.
4. Put your Anthropic API key in `ANTHROPIC_SECRET_KEY.txt` in the main Mantella mod folder.
5. In the Mantella UI, set **LLM Service** to **Anthropic (Official)**, press **Update** and pick a model.

See [INSTALL.txt](INSTALL.txt) for details, faster replies (turning thinking off) and removal.

**Limits:** uses Anthropic's OpenAI-compatible endpoint, which has no prompt caching, so each reply resends the full prompt at the normal input price.

## Build

Requires CPython 3.11 and a supported `Mantella.exe` (hashes in `Source/backend/build-contract.json`).

```bat
py -3.11 setup_backend.py --base "C:\path\to\Mantella.exe"
```

The build checks the input exe hash, that each replaced module is still stock Mantella v0.14 (`Source/upstream`), the source hashes and the output hash.

Changed modules: `src/utils.py`, `src/llm/client_base.py`, `src/config/definitions/llm_definitions.py`, `vision_definitions.py`, `action_definitions.py`.

## Source and license

Unofficial patch of [Mantella](https://github.com/art-from-the-machine/Mantella), under **AGPL-3.0-or-later**. See [SOURCE-AND-LICENSES.txt](SOURCE-AND-LICENSES.txt). No Mantella executable, voice models, personal configuration or API keys are bundled.
