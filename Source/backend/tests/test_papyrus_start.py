"""Offline check of the game script's conversation start (Scripts/Source/MantellaConversation.psc): the actor
dictionaries sent with the start request are always built in this session. SKSE_HTTP keeps its dictionaries in memory
only, while _actorHandles and the Participants list are kept in the save, so after a load the old handles point at
nothing ("No NPC in the conversation" on the first talk after a load when everyone was still in Participants).
Run with any Python 3: test_papyrus_start.py <repo Source/backend dir>"""
import re, sys
from pathlib import Path

BACKEND = Path(sys.argv[1]).resolve()
PSC = (BACKEND.parent.parent / 'Scripts/Source/MantellaConversation.psc').read_text(encoding='utf-8')
FAILS = []
def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (('  -> ' + str(detail)) if not cond else ''))
    if not cond:
        FAILS.append(name)


def func(head):
    i = PSC.lower().index(head.lower())
    end = re.compile(r'^\s*endfunction', re.I | re.M).search(PSC, i)
    return PSC[i:end.end()]


start = func('function StartConversation(')
build = func('int[] function BuildNpcsInConversationArray(')
add = func('Function AddActors(')
check('BuildNpcsInConversationArray counts its builds', '_actorBuilds += 1' in build and re.search(r'^int _actorBuilds = 0', PSC, re.M))
check('StartConversation rebuilds the actor dictionaries when AddActors added nobody (all kept in Participants)',
      re.search(r'int buildsBefore = _actorBuilds\s+AddActors\(actorsToStartConversationWith\)', start) is not None
      and re.search(r'If _actorBuilds == buildsBefore\s+BuildNpcsInConversationArray\(\)\s+EndIf', start) is not None)
check('...before the start request takes them', start.index('BuildNpcsInConversationArray()') < start.index('AddCurrentActorsAndContext(handle, true)'))
check('AddActors still builds only when someone joined (no double build on a normal start)',
      re.search(r'If \(actorsAdded\.Length > 0\)\s+CauseReassignmentOfParticipantAlias\(\)\s+BuildNpcsInConversationArray\(\)', add) is not None)


# The same rule as a model: a start sends handles made in this session, built once.
def start_model(participants, actors, handles, session, fixed=True):
    builds = 0
    added = [a for a in actors if a not in participants]
    participants = participants + added
    if added:
        handles, builds = [(session, a) for a in participants], builds + 1
    if fixed and builds == 0:
        handles, builds = [(session, a) for a in participants], builds + 1
    return handles, builds


saved = [(1, 'Player'), (1, 'Gerdur')]
for fixed, want in ((True, True), (False, False)):
    sent, builds = start_model(['Player', 'Gerdur'], ['Player', 'Gerdur'], saved, session=2, fixed=fixed)
    check(f'{"fixed" if fixed else "old"} model: first talk after a load with both kept in Participants sends this '
          f'session\'s handles = {want}', all(s == 2 for s, _ in sent) == want and len(sent) == 2, sent)
sent, builds = start_model([], ['Player', 'Gerdur'], [], session=2)
check('normal start: built once', builds == 1 and len(sent) == 2, (sent, builds))

print('RESULT: ' + ('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'))
sys.exit(1 if FAILS else 0)
