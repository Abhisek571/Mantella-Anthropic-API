# Mantella - Direct AI Connect

A patched Mantella.exe for [Mantella](https://www.nexusmods.com/skyrimspecialedition/mods/98631) 0.14: Claude straight from Anthropic with prompt caching, Dual Input built in, and the server side of Mantella Living World.

| Feature | What it does |
|---|---|
| Anthropic (Native, cached) | Anthropic's own Messages API with prompt caching: the repeated part of each prompt (bios, memories, earlier lines) costs a tenth or less after the first reply (conversations only). If Claude declines a reply, Opus 5.5, Sonnet 5.5 and Fable 5.1 retry with another Claude model; Haiku has no fallback |
| Anthropic (Official) | Anthropic's OpenAI-compatible endpoint, for the vision and tool-calling LLMs too (no caching there) |
| Player2 | LLM service for [Player2](https://player2.game), with its own key file `PLAYER2_SECRET_KEY.txt`. Player2 picks the model; requests use your joules. Not tested: made only from the API documentation on Player2's official website. Player2 does not say which AI model it runs, so it is unknown whether it is strong enough for Mantella's instructions, Advanced Actions and tool calling |
| Live model list | Each model's name, context size and capabilities, read from Anthropic |
| Own key file | `ANTHROPIC_SECRET_KEY.txt`, next to `GPT_SECRET_KEY.txt`; never mixed up, so Claude for talks and OpenRouter for summaries works |
| Dual Input | Based on Mantella Dual Input 1.1 (modified), built in: separate keys to type or speak a reply. Off by default; enable it and set the keys in Mantella's MCM, General page (Speak Reply is V by default). Speak Reply needs Mantella's speech-to-text working (model downloaded, microphone allowed); the speech model loads on the first Speak Reply, and with no microphone the turn counts as no speech |
| Extensions | Runs the Python parts of Mantella Living World, from the `extensions` folder next to Mantella.exe (settings tab, memory, prompt rules, action gates, knowledge). The Living World tab only appears when Mantella Living World - Integration Hub or Mantella Living World - Knowledge is installed. Extensions can also shape the prompt, tool list, summaries and memory for any LLM service, and NPCs can answer in-game events without the player speaking |
| Fixes | "Goodbye, <name>" ends a one-on-one talk; unsupported sampling settings are dropped for Claude; a streamed reply with no text and no action is retried up to 2 times, on every LLM service, unless the service says the same request would fail again (filtered, refused, or out of tokens); the player description typed in the settings page wins over the MCM one; summary and conversation folder names are made safe |
| Stall rescue | A cloud model that sends no data for 15 seconds is cancelled and asked once more. If it had already started speaking, what it said stays and the reply ends there. Works on every LLM service; off by default for local models. See Reliability settings |
| Fallback models | Optional list: when a model is out or rate limited and has said nothing yet, the next one answers that reply. See Reliability settings |
| Cost log | On Anthropic (Native, cached), each reply's token use (cache read, cache write, uncached, output) and dollar cost is logged for extensions |
| Safety | Mantella's settings server answers only this PC; the model can only run actions it was offered, and keyword actions ("Name: ...") follow the same rule |

## Install

1. Download the ZIP from [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/196214) or the GitHub [Releases](https://github.com/Abhisek571/Mantella-Anthropic-API/releases/latest) page (the repo keeps its old name) and install it as a separate MO2 mod.
2. Run **Setup Backend.cmd** in the mod folder. It needs **Python 3.11** from python.org once, with its Python launcher so `py -3.11` works (a uv-managed Python is not found). It builds a patched copy of Mantella 0.14's own Mantella.exe; it does not take the Dual Input exe.
3. Place the mod below Mantella in MO2's left pane, so its Mantella.exe and its `Scripts` win, and enable it. Disable the separate Mantella Dual Input mod if you have it.
4. Put your Anthropic API key in `ANTHROPIC_SECRET_KEY.txt` in the main Mantella mod folder. Under MO2 it must sit where Mantella.exe is shown, or in the working directory. A `secret_keys.json` there is read first.
5. In the Mantella UI, set **LLM Service** to **Anthropic (Native, cached)**, press **Update** and pick a model.

Claude's thinking counts toward `max_tokens`, so with thinking on (always on Opus and Fable) raise Mantella's default 250 to about 2000 in LLM Parameters. Claude thinks before every reply unless told not to, and that hidden thinking is billed. Turn it off with `"extra_body": {"thinking": {"type": "disabled"}}` or OpenRouter's `"reasoning": {"enabled": false}`: the patch sends the form each model accepts (`between_tools` on Sonnet 5.5), and Opus 5.5 and Fable, which cannot turn it off, get the lowest effort instead. INSTALL.txt also covers antivirus and removal.

### Reliability settings

Add a `reliability` block to **LLM Parameters** in the Mantella UI; it is never sent to a service. Without the block, stall rescue is on and there are no fallback models.

```json
"reliability": {
  "stall_rescue": true,
  "stall_seconds": 15,
  "fallback_models": ["claude-haiku-5-5", {"service": "OpenRouter", "model": "some/model"}],
  "openrouter_provider": {"ignore": ["SomeProvider"], "data_collection": "deny"}
}
```

| Setting | What it does |
|---|---|
| `stall_rescue` | `false` turns it off. `true` also turns it on for a local model, which is off by default because a local model can take minutes to start |
| `stall_seconds` | Seconds without data. Raise it for a model that thinks long before its first word |
| `fallback_models` | Tried in order. A plain name uses the current LLM service; `service` picks another, using its own key file. Each reply starts from your main model again. Filtered replies and bad requests do not switch models, and neither does a model that has started speaking |
| `openrouter_provider` | Routing rules for fallback models on OpenRouter. Without it, the `provider` block of your LLM Parameters is used. With neither, OpenRouter fallback models are skipped, so a fallback never goes to a provider you ruled out. `data_collection` is `deny` unless you set it |

A fallback model gets the rest of your LLM Parameters, with thinking off in the form it takes. With Model Profiles on, a matching profile replaces LLM Parameters, so put the block in the profile.

Requires: [Mantella](https://www.nexusmods.com/skyrimspecialedition/mods/98631) 0.14 (exactly), Python 3.11.9 or newer 3.11 from python.org (setup only), an Anthropic API key (billed by Anthropic). Disable the separate Mantella Dual Input mod. Optional: Mantella Living World - Integration Hub and Mantella Living World - Knowledge. Living World actions that need Mantella's Advanced Actions only work with it on. The SDK is bundled, so no pip install is needed.

Build: `py -3.11 setup_backend.py --base "C:\path\to\Mantella.exe"`. It checks the input exe hash, that each replaced module is still stock Mantella 0.14 (`Source/upstream`), and the source and output hashes in `Source/backend/build-contract.json`. Changed modules: `utils`, `llm/client_base`, `config/definitions/llm_definitions`, `vision_definitions`, `action_definitions`, `http/routes/mantella_route`, `http/communication_constants`, `remember/summaries`, `ui/start_ui`, `conversation/conversation`, `game_manager`. Added: `llm/anthropic_native`, `llm/reliability`, `extensions`, `dual_input`, plus the vendored [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) 0.125.0 and `docstring_parser` 0.18.0 (both MIT). Tests: `py -3.11 Source/backend/tests/test_<name>.py Source/backend` (`dual_input`, `extensions`, `native`, `reliability`, `safety`, `setup`).

License: GNU AGPL-3.0-or-later. An unofficial patch of [Mantella](https://github.com/art-from-the-machine/Mantella); see [SOURCE-AND-LICENSES.txt](SOURCE-AND-LICENSES.txt). No Mantella executable, voice models, personal settings or API keys are bundled.
