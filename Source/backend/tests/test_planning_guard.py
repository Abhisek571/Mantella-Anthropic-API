"""Offline tests for reliability.PlanningGuard: the model's own planning text, written before its action calls when it has
no thinking before the first one, is never spoken (live test 18), while ordinary speech passes on unchanged.
Run with a Python 3.11: test_planning_guard.py <repo Source/backend dir>"""
import importlib.util, logging, sys, types
from pathlib import Path

BACKEND = Path(sys.argv[1])
utils = types.ModuleType('src.utils')
utils.get_logger = lambda: logging.getLogger('mantella')
src = types.ModuleType('src')
src.utils = utils
sys.modules.setdefault('src', src)
sys.modules['src.utils'] = utils
spec = importlib.util.spec_from_file_location('reliability', BACKEND / 'src' / 'llm' / 'reliability.py')
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

FAILS = []
def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (('  -> ' + str(detail)) if not cond else ''))
    if not cond:
        FAILS.append(name)


def run(text, speakers, chunk=7, lookahead=True):
    guard = R.PlanningGuard(speakers, lookahead)
    out = []
    for i in range(0, len(text), chunk):
        out += guard.feed(text[i:i + chunk])
    return ''.join(out + guard.flush())


# the five spoken replies of live test 18 (each came with action calls)
check('"I\'ll have her react ... decide" is left out, with the narration just before it',
      run("She's flattered, but she has a partner. The Dibella invocation intrigues her, though. I'll have her react with "
          "arousal and decide.", ['Haelga']) == "She's flattered, but she has a partner. ")
check('"points to accepting" and the attached image remark are left out',
      run("Her usual attitude toward attractive men points to accepting. The attached image shows a text document rather "
          "than anything in Riverwood, so I'll ignore it and answer as Haelga.", ['Haelga']) == '')
check('the speaking NPC talking of herself by name is left out',
      run("Haelga is giddy and fond of him, and the two are alone now, so this is her own choice to accept.", ['Haelga']) == '')
check('"rate ... appetite silently and reply in character" goes, with the narration before it',
      run("Sigrid has no gold on her to hand over, so the gift can't be taken from her. I'll rate Alvor's appetite silently "
          "and reply in character.", ['Sigrid', 'Alvor']) == '')
check('a group reply with no name prefix: "so she is willing" and the narration after it go',
      run("Haelga is a devoted follower of Dibella, flirtatious, and drawn to attractive men, so she is willing. The illusion "
          "hides them from everyone but this conversation, and Lucan is only a bystander.", ['Lucan Valerius', 'Haelga']) == '')
check('a group reply: the planning part goes, the next speaker keeps their line',
      run("Haelga: Haelga is a devoted follower of Dibella. Lucan is only a bystander. Lucan Valerius: By the Nine, not here!",
          ['Haelga', 'Lucan Valerius']) == 'Lucan Valerius: By the Nine, not here!')

# ordinary speech is never touched, also with names, third persons and quotes in it
for text, speakers in (
        ("Well, look who found me again, handsome. Keep your voice low, Alvor is standing right there. What brings you out?", ['Haelga']),
        ("Sven is a fool, and his lute is worse. The mill's been busy since spring.", ['Camilla Valerius']),
        ("Haelga: Oh... right there... don't stop!\nLucan Valerius: By the Nine, I'm staring at that cart until this is over!", ['Haelga', 'Lucan Valerius']),
        ("I'll have him bring the sword tomorrow. It's good steel.", ['Alvor']),
        ("Lucan says the claw was stolen. \"Find it,\" he told me.", ['Camilla Valerius'])):
    for lookahead in (True, False):
        check(f'speech unchanged (lookahead {lookahead}): {text[:40]}', run(text, speakers, lookahead=lookahead) == text,
              run(text, speakers, lookahead=lookahead))
check('a whitespace-only tail stays after the held sentence', run('Hello there.  ', ['Gerdur']) == 'Hello there.  ')
check('a left-out sentence\'s "Name:" goes on to the next line kept',
      run("Alvor: I'll rate Alvor's appetite silently. Fine work, that.", ['Alvor', 'Sigrid'], lookahead=False)
      == 'Alvor: Fine work, that.')
check('without lookahead the first sentence passes at once',
      R.PlanningGuard(['Gerdur'], lookahead=False).feed('Good day to you. How') == ['Good day to you. '])
check('with lookahead it waits for the next one', R.PlanningGuard(['Gerdur']).feed('Good day to you. How') == [])

body = (BACKEND / 'src' / 'llm' / 'client_base.py').read_text(encoding='utf-8')
body = body[body.index('async def streaming_call('):body.index('def _server_actions(')]
check('streaming_call passes every reply through the guard, flushed before the action calls and on a failure',
      'reliability.PlanningGuard(' in body and body.count('guard.flush()') >= 3 and 'guard.feed(item[1])' in body)

print('\n' + ('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: ' + ', '.join(FAILS)))
sys.exit(1 if FAILS else 0)
