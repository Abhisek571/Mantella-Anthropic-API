# Changelog

Every change to Mantella - Direct AI Connect. The Nexus page lists only the main changes for each version.

## Unreleased

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
