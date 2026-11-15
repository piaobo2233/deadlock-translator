"""An in-flight response survives a brief lease gap, never absence/replacement."""
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from app.recognition.messages import ConfirmedMessage
from app.runtime.events import RuntimeHealth
from app.runtime.messages import ChatCandidate
from app.runtime.pipeline import TranslationPipeline
from app.runtime.processor import TranslationProcessor
from app.translation.client import ApiResponse
from app.translation.desktop_engine import Translator
from app.translation.learning import LearnedGlossary
from app.translation.query import QueryKey


class VisualResponseResumeTests(unittest.TestCase):
    def test_response_is_delivered_only_to_the_same_current_unexpired_identity(self):
        for state in ('restored', 'absent', 'replaced', 'deadline'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as folder:
                text = 'Wait for me near the bridge.'
                identity = ConfirmedMessage('a' * 32, 1, 1, text, QueryKey.from_text(text),
                    time.monotonic(), 'monotonic')
                candidate = ChatCandidate('', text, 'team', (10, 20, 150, 40), 1,
                                          identity=identity)
                current, entered, release = [identity], threading.Event(), threading.Event()
                engine = Translator('fixture', 'https://invalid.example', 'fixture', False,
                    allow_popup=False, glossary={},
                    learning=LearnedGlossary(str(Path(folder) / 'glossary.json')))
                def request(**kwargs):
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError('HTTP fixture was not released')
                    return ApiResponse(text='在桥边等我。', finish_reason='stop')
                engine.client.request = Mock(side_effect=request)
                processor = TranslationProcessor(engine, is_chinese=lambda _: False,
                    chatlog=Mock(), matchlog=Mock(), stats={}, health=RuntimeHealth(),
                    log=lambda _: None)
                pipe = TranslationPipeline(lambda: iter((candidate,)), processor,
                    is_current=lambda item: item.identity == current[0],
                    task_timeout=.25 if state == 'deadline' else 3, log=lambda _: None)
                pipe.start()
                try:
                    self.assertTrue(entered.wait(2))
                    current[0] = None
                    # Cross multiple deadline-coordinator ticks while in flight.
                    time.sleep(.12 if state != 'deadline' else .3)
                    release.set()
                    time.sleep(.12)
                    if state in ('restored', 'deadline'):
                        current[0] = identity
                    elif state == 'replaced':
                        current[0] = replace(identity, bubble_id=2)
                    if state == 'restored':
                        displayed = pipe.outbox.get(timeout=2)
                        self.assertTrue(displayed.successful)
                        self.assertEqual(displayed.identity, identity)
                        self.assertEqual(displayed.translated, '在桥边等我。')
                    until = time.monotonic() + 2
                    while not pipe.finished and time.monotonic() < until:
                        time.sleep(.02)
                    self.assertTrue(pipe.finished)
                    self.assertTrue(pipe.outbox.empty())
                    self.assertEqual(engine.client.request.call_count, 1)
                    self.assertEqual(engine.journal.read(text)['state'], 'validated')
                finally:
                    release.set()
                    self.assertTrue(pipe.stop(timeout=2))


if __name__ == '__main__':
    unittest.main()
