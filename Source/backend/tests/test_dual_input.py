"""Offline tests for src/dual_input.py: one Speak Reply capture with a fake Transcriber.
Run with any Python 3: test_dual_input.py <repo Source/backend dir>"""
import importlib.util, logging, sys
from pathlib import Path

BACKEND = Path(sys.argv[1])
logging.basicConfig(level=logging.CRITICAL)
spec = importlib.util.spec_from_file_location('dual_input', BACKEND / 'src/dual_input.py')
dual = importlib.util.module_from_spec(spec); spec.loader.exec_module(dual)

FAILS = []
def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (('  -> ' + str(detail)) if not cond else ''))
    if not cond:
        FAILS.append(name)


class FakeTranscriber:
    def __init__(self, text='hello there', fail_on=None):
        self.calls, self.text, self.fail_on = [], text, fail_on
        self.is_listening = False
    def start_listening(self, prompt):
        self.calls.append('start')
        if self.fail_on == 'start':
            raise OSError('Error querying device -1')  # what sounddevice raises with no input device
        self.is_listening = True
    def get_latest_transcription(self, silence_timeout):
        self.calls.append('read')
        if self.fail_on == 'read':
            raise RuntimeError('stream died')
        return self.text
    def stop_listening(self):
        self.calls.append('stop')
        self.is_listening = False


# ---------- capture ----------
t = FakeTranscriber()
check('capture returns the transcript', dual.capture_dual_input_once(t, '', 1.0) == 'hello there')
check('capture opens, reads, closes', t.calls == ['start', 'read', 'stop'], t.calls)

t = FakeTranscriber(fail_on='start')
try:
    value = dual.capture_dual_input_once(t, '', 1.0)
    check('no microphone counts as no speech', value is None, value)
except Exception as e:
    check('no microphone counts as no speech', False, repr(e))
check('microphone closed after a failed start', t.calls[-1] == 'stop', t.calls)

t = FakeTranscriber(fail_on='read')
try:
    check('a failed read counts as no speech', dual.capture_dual_input_once(t, '', 1.0) is None)
except Exception as e:
    check('a failed read counts as no speech', False, repr(e))

# ---------- lazy model ----------
made = []
def factory():
    made.append(1)
    return FakeTranscriber()
lazy = dual.LazyTranscriber(factory)
check('no model loaded at talk start', made == [])
lazy.stop_listening()
check('stopping before any capture loads nothing', made == [] and not lazy.is_listening)
check('first Speak Reply loads the model', dual.capture_dual_input_once(lazy, '', 1.0) == 'hello there' and made == [1], made)
dual.capture_dual_input_once(lazy, '', 1.0)
check('model loaded once', made == [1], made)

def broken():
    raise RuntimeError('Whisper model not downloaded')
lazy = dual.LazyTranscriber(broken)
try:
    check('a model that fails to load counts as no speech', dual.capture_dual_input_once(lazy, '', 1.0) is None)
except Exception as e:
    check('a model that fails to load counts as no speech', False, repr(e))

print(f'{len(FAILS)} failed' if FAILS else 'all passed')
sys.exit(1 if FAILS else 0)
