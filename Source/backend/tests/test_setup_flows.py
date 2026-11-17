"""Offline tests: Setup Backend also builds the OStim flows when Mantella Living World - OStim Expansion is installed.
Run with a Python 3.11: test_setup_flows.py <repo Source/backend dir>
Uses a made-up Mod Organizer 2 instance in a temp folder. The real flowgen.py is taken from the OStim Expansion
source next to this repo when it is there; those checks are skipped otherwise."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

BACKEND = Path(sys.argv[1]).resolve()
ROOT = BACKEND.parent.parent
SETUP = ROOT / 'setup_backend.py'
TOOL = Path('tools/Mantella Living World - OStim Expansion')
FLOWS_MOD = 'Mantella Living World - OStim Expansion - Flows'

FAILS = []
def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (('  -> ' + str(detail)) if not cond else ''))
    if not cond:
        FAILS.append(name)


def real_flowgen():
    for candidate in (ROOT.parent / 'Mantella-Living-World-OStim-Expansion/tools/flowgen.py',  # separate repos
                      ROOT.parent / 'OStim Expansion/tools/flowgen.py'):                        # All in One repo
        if candidate.is_file():
            return candidate
    return None


SCENE = {'actors': [{'intendedSex': 'male'}, {'intendedSex': 'female', 'tags': ['lyingback']}],
         'actions': [{'type': 'vaginalsex'}], 'tags': ['missionary']}


def make_instance(base, ostim_tool=None, ini=True, setup_in=None):
    """A small MO2 instance: one animation pack with two linked scenes, this setup as a mod, and optionally OStim
    Expansion's tool folder holding the given flowgen.py. Returns the setup mod folder."""
    base = Path(base)
    mods = base / 'mods'
    scenes = mods / 'Sample Pack/SKSE/Plugins/OStim/scenes/SamplePack'
    scenes.mkdir(parents=True)
    for i, nxt in ((1, 'SampleMissionary2'), (2, None)):
        scene = dict(SCENE, name=f'Sample Missionary {i}', navigations=[{'destination': nxt}] if nxt else [])
        (scenes / f'SampleMissionary{i}.json').write_text(json.dumps(scene), encoding='utf-8')
    setup_mod = mods / 'Setup Mod'
    setup_mod.mkdir()
    shutil.copy2(SETUP, setup_mod / 'setup_backend.py')
    order = ['Setup Mod', 'Sample Pack']
    if ostim_tool:
        tool = (setup_in or mods / 'OStim Mod') / TOOL
        tool.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ostim_tool, tool / 'flowgen.py')
        if not setup_in:
            order.insert(0, 'OStim Mod')
    if ini:
        (base / 'ModOrganizer.ini').write_text('[General]\nselected_profile=@ByteArray(Default)\n', encoding='utf-8')
    (base / 'profiles/Default').mkdir(parents=True)
    (base / 'profiles/Default/modlist.txt').write_text(''.join('+' + m + '\n' for m in order), encoding='utf-8')
    return setup_mod


# Runs the copied setup's main() with the exe build replaced (no Mantella.exe needed), as Setup Backend.cmd would.
HARNESS = r'''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import setup_backend as S
S.find_base = lambda root=None: Path('Mantella.exe')
S.build = lambda base, output, **kw: {'base_name': 'Mantella 0.14'}
if len(sys.argv) > 2:
    S.FLOWS_TIMEOUT = int(sys.argv[2])
    S.build_flows.__defaults__ = (S.ROOT, S.FLOWS_TIMEOUT)
sys.argv = ['setup_backend.py']
try:
    S.main()
except (ValueError, OSError, RuntimeError) as error:
    print('Setup stopped: ' + str(error), file=sys.stderr)
    sys.exit(1)
'''


def run_setup(setup_mod, tmp, timeout_arg=None, keep_stdin_open=False, wait=120):
    """Returns (exit code or 'hung', output)."""
    harness = Path(tmp) / 'harness.py'
    harness.write_text(HARNESS, encoding='utf-8')
    env = dict(os.environ, LOCALAPPDATA=str(Path(tmp) / 'appdata'), PYTHONIOENCODING='utf-8')
    log = Path(tmp) / 'out.txt'
    args = [sys.executable, str(harness), str(setup_mod)] + ([str(timeout_arg)] if timeout_arg else [])
    with open(log, 'w', encoding='utf-8') as out:
        # an open pipe that never sends anything: a child reading it would wait forever
        proc = subprocess.Popen(args, stdin=subprocess.PIPE if keep_stdin_open else subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT, env=env, cwd=tmp)
        try:
            code = proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            code = 'hung'
        finally:
            if proc.stdin:
                proc.stdin.close()
    return code, log.read_text(encoding='utf-8')


flowgen = real_flowgen()

# (a) OStim Expansion installed as its own mod: the setup builds the flows into the Flows mod, without asking
if flowgen:
    with tempfile.TemporaryDirectory() as tmp:
        setup_mod = make_instance(Path(tmp) / 'instance', ostim_tool=flowgen)
        code, out = run_setup(setup_mod, tmp)
        flows = Path(tmp) / 'instance/mods' / FLOWS_MOD
        check('separate mod: setup succeeds', code == 0, (code, out[-800:]))
        check('separate mod: heading shown', 'Building OStim flows...' in out, out[-800:])
        check('separate mod: flowgen output shown', 'Flows built: 1 sets from 1 packs' in out, out[-800:])
        check('separate mod: sequence written', (flows / 'SKSE/Plugins/OStim/sequences/MAEF_S1.json').is_file())
        check('separate mod: catalogue written', (flows / 'SKSE/Plugins/StorageUtilData/MantellaAdultExpansion/flows.json').is_file())
        check('separate mod: report written', (flows / 'flows-report.txt').is_file())
        check('separate mod: no warning', 'Warning' not in out, out[-800:])
        check('backend lines come first', out.find('backend ready') < out.find('Building OStim flows...'), out[-800:])

    # (a) All in One: the tool sits inside the setup's own mod folder
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / 'instance'
        setup_mod = make_instance(base, ostim_tool=flowgen, setup_in=base / 'mods/Setup Mod')
        code, out = run_setup(setup_mod, tmp)
        check('all in one: flows built', code == 0 and (base / 'mods' / FLOWS_MOD / 'SKSE/Plugins/OStim/sequences/MAEF_S1.json').is_file(),
              (code, out[-800:]))
else:
    print('SKIP flows built with the real flowgen.py (OStim Expansion source not found next to this repo)')

# (b) no OStim Expansion: nothing runs and nothing about flows is printed
with tempfile.TemporaryDirectory() as tmp:
    setup_mod = make_instance(Path(tmp) / 'instance')
    code, out = run_setup(setup_mod, tmp)
    check('not installed: setup succeeds', code == 0, (code, out))
    check('not installed: nothing about flows', 'flow' not in out.lower(), out)
    check('not installed: output ends with the backend lines', out.rstrip().endswith('Your original files and settings were not changed.'), out)
    check('not installed: no Flows mod', not (Path(tmp) / 'instance/mods' / FLOWS_MOD).exists())

# (c) a failing flow build: warning, backend still reported ready, setup exit code stays success
with tempfile.TemporaryDirectory() as tmp:
    failing = Path(tmp) / 'failing.py'
    failing.write_text("import sys\nprint('No usable two-person OStim scenes found.')\nsys.exit(1)\n", encoding='utf-8')
    setup_mod = make_instance(Path(tmp) / 'instance', ostim_tool=failing)
    code, out = run_setup(setup_mod, tmp)
    check('failing flowgen: exit code 0', code == 0, (code, out))
    check('failing flowgen: its message shown', 'No usable two-person OStim scenes found.' in out, out)
    check('failing flowgen: warning names Build Flows.cmd and its folder',
          'Warning: the OStim flows were not built (see the message above). The backend is built and ready. Run Build Flows.cmd in ' in out
          and str(Path(tmp) / 'instance/mods/OStim Mod' / TOOL) in out, out)

# (c) a flow build that runs too long is stopped with the same warning
with tempfile.TemporaryDirectory() as tmp:
    slow = Path(tmp) / 'slow.py'
    slow.write_text('import time\ntime.sleep(60)\n', encoding='utf-8')
    setup_mod = make_instance(Path(tmp) / 'instance', ostim_tool=slow)
    code, out = run_setup(setup_mod, tmp, timeout_arg=3, wait=45)
    check('slow flowgen: stopped, exit code 0', code == 0, (code, out))
    check('slow flowgen: warning says it was stopped', 'took longer than 3 seconds and was stopped' in out and 'Build Flows.cmd' in out, out)

# (d) stdin is an open pipe, not a terminal, and flowgen cannot find MO2 (it would ask where it is): no hang
if flowgen:
    with tempfile.TemporaryDirectory() as tmp:
        setup_mod = make_instance(Path(tmp) / 'instance', ostim_tool=flowgen, ini=False)
        code, out = run_setup(setup_mod, tmp, keep_stdin_open=True, wait=60)
        check('no terminal: does not hang', code != 'hung', out[-800:])
        check('no terminal: exit code 0', code == 0, (code, out[-800:]))
        check("no terminal: flowgen's question and its own stop message shown",
              'Paste the folder that holds ModOrganizer.ini' in out and 'No folder given.' in out, out[-800:])
        check('no terminal: warning shown', 'Warning: the OStim flows were not built' in out, out[-800:])
else:
    print('SKIP no-terminal check with the real flowgen.py (OStim Expansion source not found next to this repo)')

# the same question with a fake flowgen that only asks: still no hang without the real tool
with tempfile.TemporaryDirectory() as tmp:
    asking = Path(tmp) / 'asking.py'
    asking.write_text("import sys\ntry:\n    input('> ')\nexcept EOFError:\n    sys.exit('No answer.')\n", encoding='utf-8')
    setup_mod = make_instance(Path(tmp) / 'instance', ostim_tool=asking)
    code, out = run_setup(setup_mod, tmp, keep_stdin_open=True, wait=60)
    check('asking flowgen, no terminal: no hang, exit code 0', code == 0, (code, out))

# (e) B13: a copy of OStim Expansion that is off in the profile read builds nothing; the next copy that is on is used,
# and the profile read is named (it is the one last selected in Mod Organizer 2)
if flowgen:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / 'instance'
        setup_mod = make_instance(base, ostim_tool=flowgen)
        off_tool = base / 'mods/An Old OStim Copy' / TOOL  # sorts before "OStim Mod"
        off_tool.mkdir(parents=True)
        shutil.copy2(flowgen, off_tool / 'flowgen.py')
        modlist = base / 'profiles/Default/modlist.txt'
        modlist.write_text(modlist.read_text(encoding='utf-8') + '-An Old OStim Copy\n', encoding='utf-8')
        code, out = run_setup(setup_mod, tmp)
        check('copy off in the profile: skipped, the enabled copy builds the flows',
              code == 0 and 'is not enabled in profile "Default"' in out and 'Flows built: 1 sets from 1 packs' in out
              and (base / 'mods' / FLOWS_MOD / 'SKSE/Plugins/OStim/sequences/MAEF_S1.json').is_file(), (code, out[-1200:]))
        check('the profile read is named as the one last selected in Mod Organizer 2',
              'Profile: Default (the profile last selected in Mod Organizer 2' in out, out[-1200:])
        check('copy off in the profile: no warning', 'Warning' not in out, out[-1200:])
        # only a copy that is off: nothing built, a plain note, the setup still succeeds
        shutil.rmtree(base / 'mods' / 'OStim Mod')
        shutil.rmtree(base / 'mods' / FLOWS_MOD)
        modlist.write_text(''.join(l + '\n' for l in modlist.read_text(encoding='utf-8').splitlines() if 'OStim Mod' not in l), encoding='utf-8')
        code, out = run_setup(setup_mod, tmp)
        check('only a copy that is off: no flows, a note naming why, setup succeeds',
              code == 0 and 'not enabled in the profile named above' in out and not (base / 'mods' / FLOWS_MOD).exists()
              and 'Warning' not in out, (code, out[-1200:]))
else:
    print('SKIP disabled-copy check with the real flowgen.py (OStim Expansion source not found next to this repo)')

print('RESULT: ' + ('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'))
sys.exit(1 if FAILS else 0)
