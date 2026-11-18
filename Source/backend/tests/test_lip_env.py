"""Offline test: src/utils.py removes NoDefaultCurrentDirectoryInExePath so FaceFXWrapper can be found.
Run with any Python 3 on Windows: test_lip_env.py <repo Source/backend dir> [Processing folder with FaceFXWrapper.exe + FonixData.cdf] [16-bit mono wav]
The optional part runs FaceFXWrapper the way Mantella 0.14 does (bat file, cwd = Processing folder, bare exe name)."""
import importlib.util, os, shutil, subprocess, sys, tempfile, types
from pathlib import Path

BACKEND = Path(sys.argv[1])
FAILS = []
def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (('  -> ' + str(detail)) if not cond else ''))
    if not cond:
        FAILS.append(name)

def stub(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m; return m
stub('charset_normalizer', detect=lambda *a, **k: {})
stub('src'); stub('src.telemetry')
stub('src.telemetry.telemetry', get_telemetry_manager=lambda: None, create_span_from_thread=lambda *a, **k: None)

def run_like_mantella(processing: Path, wav_src: Path, work: Path) -> bool:
    folder = work / 'My Games' / 'voicelines'
    folder.mkdir(parents=True, exist_ok=True)
    wav = f'{folder}/out.wav'; lip = wav.replace('.wav', '.lip'); r_wav = wav.replace('.wav', '_r.wav')
    shutil.copy(wav_src, wav)
    command = ' '.join(['FaceFXWrapper.exe', 'Skyrim', 'USEnglish', 'FonixData.cdf', f'"{wav}"', f'"{r_wav}"', f'"{lip}"', '" Well, hello there. "'])
    bat = processing / 'run_mantella_command.bat'
    bat.write_text(f'@echo off\n{command} >nul 2>&1', encoding='utf-8')
    subprocess.run(bat, cwd=processing, creationflags=subprocess.CREATE_NO_WINDOW)
    return os.path.exists(lip)

os.environ['NoDefaultCurrentDirectoryInExePath'] = '1'
spec = importlib.util.spec_from_file_location('src.utils', BACKEND / 'src/utils.py')
utils = importlib.util.module_from_spec(spec); spec.loader.exec_module(utils)
check('importing src.utils removes NoDefaultCurrentDirectoryInExePath', 'NoDefaultCurrentDirectoryInExePath' not in os.environ)

if len(sys.argv) >= 4:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        processing = tmp / 'My Modlist' / 'Processing'
        processing.mkdir(parents=True)
        for f in ('FaceFXWrapper.exe', 'FonixData.cdf'):
            shutil.copy(Path(sys.argv[2]) / f, processing / f)
        check('FaceFXWrapper writes a .lip after the import', run_like_mantella(processing, Path(sys.argv[3]), tmp / 'a'))
        os.environ['NoDefaultCurrentDirectoryInExePath'] = '1'
        check('control: with the variable set, no .lip is written', not run_like_mantella(processing, Path(sys.argv[3]), tmp / 'b'))
        os.environ.pop('NoDefaultCurrentDirectoryInExePath', None)

print('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED')
sys.exit(1 if FAILS else 0)
