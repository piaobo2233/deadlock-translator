"""Regressions from a match with no visible translations; no network or input."""
import ast
from pathlib import Path
import unittest
from unittest.mock import patch

import app.main as dt
from app.recognition.selection import select_candidate
from app.recognition.tracking import Consensus
from app.recognition.visual import Presence, VisualObservation
from app.runtime.messages import DisplayMessage, VisibleMessage
from tests.test_presentation import overlay


def prune_history(history, now):
    # Exercise the nested production pump's actual eviction code without
    # starting capture, hotkeys, Tk, or the application's data stores.
    tree = ast.parse(Path(dt.__file__).read_text(encoding='utf-8'))
    pump = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == 'pump_body')
    index = next(i for i, n in enumerate(pump.body) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'fresh' for t in n.targets))
    code = ast.Module(body=pump.body[index:index + 2], type_ignores=[])
    scope = dict(history=history, now=now, live_message_box=dt.live_message_box)
    exec(compile(code, dt.__file__, 'exec'), scope)


class VisibilityRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.tracker = Consensus(threshold=2, clock=lambda: self.now)
        self.box = (100, 200, 180, 35)
        self.tracker.feed('wait for me', box=self.box)
        self.tracker.feed('wait for me', box=self.box)
        self.identity = self.tracker.confirmed[self.tracker.last_id]
        self.tracker.bind_visual(self.identity, self.now)
        self.message = DisplayMessage('', 'wait for me', '等我', 'team', 10., self.box,
                                      self.identity.bubble_id, '词汇库', True, self.identity)
        self.history = [VisibleMessage(self.message, 40.)]
        self.view = overlay()

    def render(self):
        with patch.object(dt, 'live_message_box',
                          side_effect=lambda message: self.tracker.box_for(message.identity)):
            prune_history(self.history, self.now)
            self.view.render([item.message for item in self.history])

    def test_stale_coordinates_hide_then_same_identity_can_draw_again(self):
        self.render()
        self.assertEqual(self.view._draw_bubble.call_count, 1)
        self.now = 10.30
        self.assertIsNone(self.tracker.box_for(self.identity))
        self.render()
        self.assertEqual(self.view.label_rects, [])
        self.assertEqual(self.view._draw_bubble.call_count, 1)
        self.assertEqual(len(self.history), 1)
        self.now = 10.31
        self.tracker.observe_visual(VisualObservation(self.identity, self.now, Presence.PRESENT, self.box))
        self.render()
        self.assertEqual(self.view._draw_bubble.call_count, 2)
        self.assertEqual(self.history[0].expires_at, 40.)

    def test_retired_identity_never_draws_even_when_old_history_is_retained(self):
        self.tracker.observe_visual(VisualObservation(self.identity, self.now, Presence.CHANGED))
        self.render()
        self.view._draw_bubble.assert_not_called()
        self.assertEqual(self.view.label_rects, [])
        self.now = 40.
        self.render()
        self.assertEqual(self.history, [])


class ShortChatAdmissionTests(unittest.TestCase):
    def test_single_letter_words_do_not_make_real_two_word_chat_a_lone_word(self):
        for text in ('i disagree', 'a miracle', 'u scared', 'i panicked',
                     'i misclicked', 'u lagging'):
            with self.subTest(text=text):
                result = select_candidate([('body7', text)], dt.DEFAULT_GLOSSARY,
                                          dt.recognition_rules())
                self.assertTrue(result.accepted, result)
                self.assertEqual(result.text, text)
        for text in ('disagree', 'miracle', 'scared', 'panicked', 'misclicked', 'lagging',
                     'Shift', 'thank q', 'ee u', 'q z'):
            with self.subTest(rejected=text):
                self.assertFalse(select_candidate([('body7', text)], dt.DEFAULT_GLOSSARY,
                                                  dt.recognition_rules()).accepted)

    def test_known_short_chat_is_accepted_without_rewriting_original(self):
        for text in ('thank u', 'THANK U', 'can u', 'i agree', 'r u ready'):
            with self.subTest(text=text):
                result = select_candidate([('body7', text)], dt.DEFAULT_GLOSSARY,
                                          dt.recognition_rules())
                self.assertTrue(result.accepted, result)
                self.assertEqual(result.text, text)

    def test_lone_words_and_noise_are_still_rejected(self):
        for text in ('thank', 'thank q', 'ee u', 'q z',
                     'good i a)', 'aoe | mame omw | exer', 'of =f. gauw'):
            with self.subTest(text=text):
                self.assertFalse(select_candidate([('body7', text)], dt.DEFAULT_GLOSSARY,
                                                  dt.recognition_rules()).accepted)


if __name__ == '__main__':
    unittest.main()
