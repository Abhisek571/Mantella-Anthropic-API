"""Offline test for the XTTS accent guard in src/tts/xtts.py.
Run with any Python 3: test_xtts_accent.py <repo Source/backend dir>"""
import importlib.util, logging, sys, types
from pathlib import Path

BACKEND = Path(sys.argv[1])
FAILS = []
def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (('  -> ' + str(detail)) if not cond else ''))
    if not cond:
        FAILS.append(name)

# stand-ins for the Mantella modules and third-party packages xtts.py imports
warnings = []
class Logger:
    def warning(self, msg): warnings.append(msg)
    def log(self, *a, **k): pass
    def error(self, *a, **k): pass
    def info(self, *a, **k): pass
def stub(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m; return m
class TTSable:
    def __init__(self, config): self._language = config.language
    def _sanitize_voice_name(self, v): return v.replace(' ', '').lower() if isinstance(v, str) else ''
class GameEnum: FALLOUT4 = 'fo4'
utils = stub('src.utils', get_logger=lambda: Logger(), time_it=lambda f: f)
stub('src'); sys.modules['src'].utils = utils
stub('src.config'); stub('src.config.config_loader', ConfigLoader=object)
stub('src.config.definitions'); stub('src.config.definitions.game_definitions', GameEnum=GameEnum)
stub('src.tts'); stub('src.tts.ttsable', TTSable=TTSable)
stub('src.tts.synthesization_options', SynthesizationOptions=object)
stub('requests'); stub('soundfile'); stub('numpy')

spec = importlib.util.spec_from_file_location('xtts', BACKEND / 'src/tts/xtts.py')
xtts = importlib.util.module_from_spec(spec); spec.loader.exec_module(xtts)

def make(language='en', accent_on=True):
    obj = xtts.XTTS.__new__(xtts.XTTS)
    obj._language = language
    obj._XTTS__xtts_accent = accent_on
    obj._XTTS__voice_accent = language
    obj._XTTS__available_models = []
    obj._XTTS__last_model = 'main'
    obj._XTTS__official_model_list = ['main']
    obj._XTTS__available_speakers = ['femaleyoungeager']
    obj._game = types.SimpleNamespace(base_game='skyrim')
    obj._loglevel = 20
    return obj

o4 = make('en', accent_on=False)
o4.change_voice('FemaleYoungEager', voice_accent='pt')
check('accent option off (default): English sent', o4._XTTS__voice_accent == 'en')
check('accent option off: no warning', len(warnings) == 0, warnings)
o = make('en')
o.change_voice('FemaleYoungEager', voice_accent='pt')
check('accent option on: player choice honoured (pt sent)', o._XTTS__voice_accent == 'pt', o._XTTS__voice_accent)
check('accent option on: one warning logged', len(warnings) == 1 and "'pt'" in warnings[0] and 'static' in warnings[0], warnings)
o.change_voice('FemaleYoungEager', voice_accent='fr')
check('second accent: fr sent, no second warning', o._XTTS__voice_accent == 'fr' and len(warnings) == 1, warnings)
o.change_voice('FemaleYoungEager', voice_accent='en')
check('matching accent kept', o._XTTS__voice_accent == 'en')
o.change_voice('FemaleYoungEager', voice_accent='')
check('empty accent -> language', o._XTTS__voice_accent == 'en')
o3 = make('zh-cn')
o3.change_voice('FemaleYoungEager', voice_accent='zh')
check('zh maps to zh-cn', o3._XTTS__voice_accent == 'zh-cn')
src = (BACKEND / 'src/config/definitions/tts_definitions.py').read_text(encoding='utf-8')
check('setting label warns', '"XTTS Accent (can cause static)"' in src)
check('setting description starts with WARNING', 'description = """WARNING:' in src)
check('setting still off by default', '"XTTS Accent (can cause static)", description, False' in src)

print('ALL PASS' if not FAILS else f'{len(FAILS)} FAILED')
sys.exit(1 if FAILS else 0)
