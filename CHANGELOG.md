# Changelog

Every change to Mantella - Direct AI Connect. The Nexus page lists only the main changes for each version.

## Unreleased

### New
- Anthropic (Native, cached): up to 4 cache points placed by value: the newest message, the system prompt's own points (with Mantella Living World: the part every NPC shares, the NPC's fixed text, the end), the end of the action list, the last NPC reply. The action list and shared rules are read from the cache when you start talking to another NPC with the same actions within 5 minutes; before, the first reply of every talk wrote the whole prompt to the cache again.
- Without extensions, Mantella's "Current Scene" section (place, time, weather) moves from the system prompt to the newest message, so the cached prompt no longer has to be written again when the in-game hour turns.
- A cache point on a part too short for the model to cache only takes a slot nothing else needs.
- New setting "Anthropic: 1-Hour Cache" (LLM tab, advanced, off by default): keeps the cache for an hour instead of 5 minutes. Cache writes then cost twice the prompt price instead of 1.25 times, so it only saves money if you often pause 5 to 60 minutes.
- The cache log line also shows roughly where each cache point sits (in tokens), so a read can be matched to the point it reached. The cost log prices 1-hour cache writes correctly.
- Anthropic (Native, cached): the reply after an action's result keeps the action list (with actions switched off for that one reply) instead of dropping it, so the action list and system prompt are still read from the cache on that reply and the next one, instead of the whole prompt being written again. Other LLM services work as before.
- Extensions get the base ids of the NPCs being answered (`speaking_ids`) before each reply, so they know who speaks even before the game has written its speaker file.
- New extension hook `server_actions`: an extension can hand the game an action for a reply itself (Mantella Living World's experimental Console uses it). The `memory_text` hook is now told the game time of the update it is built for.
- Setup Backend.cmd also builds the OStim flows when Mantella Living World - OStim Expansion is installed (as its own mod or in the All in One), so Build Flows.cmd is only needed after changing animation packs. If the flows cannot be built, setup says so and the backend stays ready.
- Run Setup Backend.cmd again after updating.

### Fixed
- Group and radiant talks raise every reply limit you set (max_tokens, max_completion_tokens, or max_tokens in extra_body) to at least 250, so a low limit under another name no longer wins; a whole-number decimal limit such as 500.0 counts. Dual Input: text typed while an NPC took the turn is no longer dropped silently; a notification says it was not sent.
- Group and radiant talks keep your own reply limit when it is set as max_completion_tokens (raised to 250 at least); before, a second limit of 250 replaced it and thinking models could reply with nothing.
- XTTS: a voice request that hangs now gives up after a time limit instead of freezing the talk and its end; an XTTS error is logged, and an NPC with no voice model no longer stops the line with an error.
- Setup tells Vortex users to Deploy, and INSTALL.txt has Vortex steps. Setup run with no keyboard input stops with its own message.
- The source notice lists every changed Mantella module.
- With Mantella's "Conversation summaries" switched off, extensions still save their own memories at the end of a talk (Mantella Living World saves its memories and starts the next talk fresh). Only Mantella's own summary is skipped.
- An NPC action that waits for its result (for example Inventory) no longer loses its follow-up reply when a game event asks the NPCs to react in between.
- Setup Backend.cmd builds the OStim flows only from a copy of OStim Expansion that is enabled in the Mod Organizer 2 profile it reads, and the setup output names that profile (the one last selected in Mod Organizer 2).
- Mantella no longer freezes (game and web UI stop responding) when a conversation starts before the NPC has joined it, for example right after the conversation restarts. The game now asks for your reply instead, and a reply that fails with an error no longer leaves Mantella stuck either.
- A helper model call made by an extension while a reply is running (Mantella Living World's Console) no longer waits for that reply to finish, which froze Mantella and its web UI.
- Lip files are generated even when the game is started from a launcher that sets NoDefaultCurrentDirectoryInExePath.
- A conversation started after loading a save in which the same NPCs were still listed in an earlier talk now sends those NPCs to Mantella. Before, the start request could carry no NPCs at all.
- Setup Backend.cmd: pointing it at an already patched Mantella.exe, or running it while Mantella.exe is in use, now says what to do and changes nothing. It also finds stock Mantella's Mantella.exe in Vortex's staging folder, and skips a copy it cannot read.

## 1.4.0 (2026-11-09)

### New
- An empty reply is retried up to 2 times on every service, unless the service says the request would fail again.

### Fixed
- Models that think silently before their first word were cancelled by the stall rescue; the first word may now take four times as long.
- A reply cut off by max_tokens ran half-written actions, and later dropped all of them; now only the last action, the only one that can be cut off, is dropped.
- First-turn and departed-NPC handling fixes.
- XTTS Accent now warns that it can cause static: the setting is renamed "XTTS Accent (can cause static)", its description starts with a warning, and the log warns once when it is on. It stays off by default and works as before when a player turns it on. With it on, XTTS reads English text with another language's rules (e.g. Portuguese for Imperials); in testing about 1 in 5 accented lines broke, with short lines running 7-14 seconds of static.

## 1.3.2 (2026-11-05)
- Removed the Portable Python option: run Setup Backend.cmd with Python 3.11.9 installed.
- If Python 3.11 is missing, the setup says where to get it.
- The install guide GIF also shows the Python install.

## 1.3.1 (2026-11-05)
- Portable Python option, so Python did not have to be installed (removed again in 1.3.2).
- If Python 3.11 is missing, the setup tells you how to get it.
- Step by step install guide GIF on the mod page.

## 1.3.0 (2026-11-03)
- Adds Player2 as an LLM service, with its own key file PLAYER2_SECRET_KEY.txt next to GPT_SECRET_KEY.txt. Not tested against Player2's service; Player2 does not say which AI model it runs.
- Setup asks for Python 3.11.9 or a newer 3.11. Run Setup Backend.cmd again after updating.

## 1.2.1 (2026-10-30)
- No change to the patched Mantella.exe; no need to run Setup Backend again.

## 1.2 (2026-10-29)
- Thinking off now works on every Claude model. This fixes empty replies and NPCs answering the same line twice on Sonnet 5.5.
- Opus 5.5 and Fable, which cannot turn thinking off, get the lowest effort instead (or the reasoning_effort you set).
- A thinking-off line written for another model no longer makes every reply fail with error 400.
- The empty-reply log shows which stop sequence ended the reply.
- Run Setup Backend.cmd again after updating.

## 1.1 (2026-10-29)
- Renamed to Mantella - Direct AI Connect.
- Anthropic (Native, cached): Claude through Anthropic's own API with prompt caching and model fallbacks; Anthropic (Official) kept.
- Dual Input built in (based on Mantella Dual Input 1.1, modified); disable the separate mod.
- Runs the Mantella Living World extensions with any LLM service.
- Thinking is sent back only on the latest tool turn, so newer Anthropic accounts no longer get errors after an action.
- Key files are read with any encoding.
- Setup verifies the exe it wrote and names antivirus when it is missing or cut short.
- The zip includes the four Papyrus script overrides and the full backend source.

## 1.0 (2026-10-21)
- First release. For Mantella 0.14. Install the ZIP as an MO2 mod and run Setup Backend.cmd once (Python 3.11).
