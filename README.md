# Mantella - Direct AI Connect

A patched Mantella.exe for [Mantella](https://www.nexusmods.com/skyrimspecialedition/mods/98631) 0.14: Claude straight from Anthropic with prompt caching, Dual Input built in, and the server side of Mantella Living World.

| Feature | What it does |
|---|---|
| Anthropic (Native, cached) | Anthropic's own Messages API with prompt caching: the repeated part of each prompt (bios, memories, earlier lines) costs a tenth or less after the first reply (conversations only). If Claude declines a reply, Opus 5.5, Sonnet 5.5 and Fable 5.1 retry with another Claude model; Haiku has no fallback |
| Anthropic (Official) | Anthropic's OpenAI-compatible endpoint, for the vision and tool-calling LLMs too (no caching there) |
| Live model list | Each model's name, context size and capabilities, read from Anthropic |
| Own key file | `ANTHROPIC_SECRET_KEY.txt`, next to `GPT_SECRET_KEY.txt`; never mixed up, so Claude for talks and OpenRouter for summaries works |
| Dual Input | Based on Mantella Dual Input 1.1 (modified), built in: separate keys to type or speak a reply. Off by default; enable it and set the keys in Mantella's MCM, General page (Speak Reply is V by default). Speak Reply needs Mantella's speech-to-text working (model downloaded, microphone allowed); the speech model loads on the first Speak Reply, and with no microphone the turn counts as no speech |
| Extensions | Runs the Python parts of Mantella Living World, from the `extensions` folder next to Mantella.exe (settings tab, memory, prompt rules, action gates, knowledge). The Living World tab only appears when Mantella Living World - Integration Hub or Mantella Living World - Knowledge is installed. Extensions can also shape the prompt, tool list, summaries and memory for any LLM service, and NPCs can answer in-game events without the player speaking |
| Fixes | "Goodbye, <name>" ends a one-on-one talk; unsupported sampling settings are dropped for Claude; a streamed reply with no text and no action is retried up to 2 times, on every LLM service; the player description typed in the settings page wins over the MCM one; summary and conversation folder names are made safe |
| Cost log | On Anthropic (Native, cached), each reply's token use (cache read, cache write, uncached, output) and dollar cost is logged for extensions |
| Safety | Mantella's settings server answers only this PC; the model can only run actions it was offered, and keyword actions ("Name: ...") follow the same rule |

## Install

1. Download the ZIP from [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/196214) or the GitHub [Releases](https://github.com/Abhisek571/Mantella-Anthropic-API/releases/latest) page (the repo keeps its old name) and install it as a separate MO2 mod.
2. Run **Setup Backend.cmd** in the mod folder, with **Python 3.11** from python.org installed, including its Python launcher so `py -3.11` works (a uv-managed Python is not found). It builds a patched copy of Mantella 0.14's own Mantella.exe; it does not take the Dual Input exe.
3. Place the mod below Mantella in MO2's left pane, so its Mantella.exe and its `Scripts` win, and enable it. Disable the separate Mantella Dual Input mod if you have it.
4. Put your Anthropic API key in `ANTHROPIC_SECRET_KEY.txt` in the main Mantella mod folder. Under MO2 it must sit where Mantella.exe is shown, or in the working directory. A `secret_keys.json` there is read first.
5. In the Mantella UI, set **LLM Service** to **Anthropic (Native, cached)**, press **Update** and pick a model.

Claude's thinking counts toward `max_tokens`, so with thinking on (always on Opus and Fable) raise Mantella's default 250 to about 2000 in LLM Parameters. Claude thinks before every reply unless told not to, and that hidden thinking is billed. Turn it off with `"extra_body": {"thinking": {"type": "disabled"}}` or OpenRouter's `"reasoning": {"enabled": false}`: the patch sends the form each model accepts (`between_tools` on Sonnet 5.5), and Opus 5.5 and Fable, which cannot turn it off, get the lowest effort instead. INSTALL.txt also covers antivirus and removal.

Requires: [Mantella](https://www.nexusmods.com/skyrimspecialedition/mods/98631) 0.14 (exactly), Python 3.11 from python.org (setup only), an Anthropic API key (billed by Anthropic). Disable the separate Mantella Dual Input mod. Optional: Mantella Living World - Integration Hub and Mantella Living World - Knowledge. Living World actions that need Mantella's Advanced Actions only work with it on. The SDK is bundled, so no pip install is needed.

Build: `py -3.11 setup_backend.py --base "C:\path\to\Mantella.exe"`. It checks the input exe hash, that each replaced module is still stock Mantella 0.14 (`Source/upstream`), and the source and output hashes in `Source/backend/build-contract.json`. Changed modules: `utils`, `llm/client_base`, `config/definitions/llm_definitions`, `vision_definitions`, `action_definitions`, `http/routes/mantella_route`, `http/communication_constants`, `remember/summaries`, `ui/start_ui`, `conversation/conversation`, `game_manager`. Added: `llm/anthropic_native`, `extensions`, `dual_input`, plus the vendored [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) 0.125.0 and `docstring_parser` 0.18.0 (both MIT). Tests: `py -3.11 Source/backend/tests/test_<name>.py Source/backend` (`dual_input`, `extensions`, `native`, `safety`, `setup`).

License: GNU AGPL-3.0-or-later. An unofficial patch of [Mantella](https://github.com/art-from-the-machine/Mantella); see [SOURCE-AND-LICENSES.txt](SOURCE-AND-LICENSES.txt). No Mantella executable, voice models, personal settings or API keys are bundled.
