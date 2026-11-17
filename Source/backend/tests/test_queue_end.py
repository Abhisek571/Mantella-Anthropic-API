"""Offline regression test: a reply that ends without sentences must not leave the sentence queue waiting. Stock
ChatManager.generate_response returns at once when no NPC is in the conversation, without ending the reply; the
request then waited in SentenceQueue.get_next_sentence forever and the whole server froze.
The helpers are taken out of conversation.py, so none of Mantella's imports are needed. Every wait has a timeout,
and a watchdog stops the run if something still hangs.
Run with a Python 3.11: test_queue_end.py <repo Source/backend dir>"""
import ast, logging, os, queue, sys, threading, traceback
from pathlib import Path

BACKEND = Path(sys.argv[1])
TIMEOUT = 5

watchdog = threading.Timer(60, lambda: (print('FAIL watchdog: the test hung'), sys.stdout.flush(), os._exit(1)))
watchdog.daemon = True
watchdog.start()

FAILS = []
def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (('  -> ' + str(detail)) if not cond else ''))
    if not cond:
        FAILS.append(name)


class SentenceQueue:
    """Same waiting rule as Mantella 0.14's src/llm/sentence_queue.py: get blocks while is_more_to_come is set."""
    def __init__(self):
        self.q = queue.Queue()
        self.is_more_to_come = False
    def get_next_sentence(self):
        if self.q.qsize() > 0 or self.is_more_to_come:
            return self.q.get()
        return None
    def put(self, sentence):
        self.q.put(sentence)

class SentenceContent:
    def __init__(self, speaker, text, sentence_type, is_system_generated_sentence=False, actions=None):
        self.speaker, self.text, self.is_system_generated_sentence = speaker, text, is_system_generated_sentence
        self.actions = actions or []
class Sentence:
    def __init__(self, content, voice_file, duration, error_message=None):
        self.content, self.text, self.actions = content, content.text, content.actions
        self.is_system_generated_sentence = content.is_system_generated_sentence
class SentenceTypeEnum:
    SPEECH = 'speech'

errors = []
class Collect(logging.Handler):
    def emit(self, record):
        errors.append(record.getMessage())
log = logging.getLogger('queue-end-test'); log.addHandler(Collect())

rel = 'src/conversation/conversation.py'
source = (BACKEND / rel).read_text(encoding='utf-8')
tree = ast.parse(source)
names = {'_end_sentence_queue', '_generate_into_queue'}
keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
check('helpers present', {n.name for n in keep} == names, [n.name for n in keep])
space = {'Sentence': Sentence, 'SentenceContent': SentenceContent, 'SentenceTypeEnum': SentenceTypeEnum,
         'logger': log, 'traceback': traceback}
exec(compile(ast.Module(body=keep, type_ignores=[]), rel, 'exec'), space)
generate_into_queue = space['_generate_into_queue']


def reader_result(sentences, start_generation):
    """A request already waiting in get_next_sentence, then the generation runs: what does the request get?"""
    got = []
    reader = threading.Thread(target=lambda: got.append(sentences.get_next_sentence()), daemon=True)
    reader.start()
    start_generation()
    reader.join(TIMEOUT)
    return reader.is_alive(), got


def early_return():
    pass  # what stock generate_response does when no NPC is in the conversation

def raises():
    raise RuntimeError('model connection lost')

def ends_itself(sentences):
    def run():
        sentences.put(Sentence(SentenceContent('Lydia', 'Hello.', 'speech'), 'a.wav', 1.0))
        sentences.is_more_to_come = False
        sentences.put(Sentence(SentenceContent('Lydia', '', 'speech', True), '', 0))
    return run


# the bug: an early return with is_more_to_come still set
sentences = SentenceQueue(); sentences.is_more_to_come = True
hung, got = reader_result(sentences, lambda: generate_into_queue(early_return, sentences, None))
check('early return: waiting request is released', not hung)
check('early return: it gets the empty end sentence', len(got) == 1 and got[0] is not None and got[0].text == ''
      and got[0].is_system_generated_sentence, got)
check('early return: queue says nothing more is coming', sentences.is_more_to_come is False)
check('early return: next read returns None at once', sentences.get_next_sentence() is None)

# an error inside the generation: released and logged with the traceback
errors.clear()
sentences = SentenceQueue(); sentences.is_more_to_come = True
hung, got = reader_result(sentences, lambda: generate_into_queue(raises, sentences, 'Lydia'))
check('error: waiting request is released', not hung)
check('error: logged with traceback', any('model connection lost' in e and 'Traceback' in e for e in errors), errors)
check('error: end sentence has the NPC as speaker', got and got[0].content.speaker == 'Lydia', got)

# a normal reply ends the queue itself: no second end sentence is added
sentences = SentenceQueue(); sentences.is_more_to_come = True
generate_into_queue(ends_itself(sentences), sentences, 'Lydia')
texts = []
while (s := sentences.get_next_sentence()) is not None:
    texts.append(s.text)
check('normal reply: unchanged, one end sentence', texts == ['Hello.', ''], texts)

# the generation thread and the no-NPC check in __start_generating_npc_sentences use these helpers
start = source[source.index('def __start_generating_npc_sentences'):source.index('def __offered_keyword_actions')]
check('start: no NPC ends the queue before a thread starts',
      'if not npcs.last_added_character:' in start and '_end_sentence_queue(self.__sentences' in start
      and start.index('_end_sentence_queue(self.__sentences') < start.index('is_more_to_come = True'))
check('start: generation runs through _generate_into_queue', '_generate_into_queue(lambda: self.__output_manager.generate_response(' in start)

watchdog.cancel()
print(f'\n{len(FAILS)} failed' if FAILS else '\nall passed')
sys.exit(1 if FAILS else 0)
