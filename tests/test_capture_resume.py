"""Receive-source recovery without a desktop, user data or network."""
import threading
import unittest
from unittest.mock import Mock, patch

from app.recognition.region import RegionCapture


class CaptureResumeTests(unittest.TestCase):
    def exercise_recovery(self, failure):
        state = {'failure': False}
        calls = []
        def screen():
            if state['failure']:
                state['failure'] = False
                if failure == 'lookup':
                    raise ValueError('Controlled transient client lookup failure')
                return (1920, 1080)
            return (2560, 1440)
        control = RegionCapture(lambda: {'capture_preset': 'auto'}, screen)
        def factory(**kw):
            calls.append(kw['screen_size'])
            kw['capture_ready']()
            if len(calls) == 1:
                yield 'before-switch'
                state['failure'] = True
                self.assertTrue(kw['capture_changed']())
                # The client geometry is healthy again before the outer loop
                # checks why the source ended. This must still create a source.
                return
            self.assertTrue(control.current())
            yield 'after-switch'
        self.assertEqual(list(control.iter_source(factory, threading.Event())),
                         ['before-switch', 'after-switch'])
        self.assertEqual(len(calls), 2)
        self.assertFalse(control.current())

    def test_transient_client_lookup_failure_restarts_source(self):
        self.exercise_recovery('lookup')

    def test_geometry_changes_back_before_outer_poll_still_restarts(self):
        self.exercise_recovery('resize')

    def test_finite_source_is_not_restarted(self):
        control = RegionCapture(lambda: {}, lambda: (2560, 1440))
        calls = []
        def factory(**kw):
            calls.append(True)
            kw['capture_ready']()
            yield 'last-message'
        self.assertEqual(list(control.iter_source(factory, threading.Event())), ['last-message'])
        self.assertEqual(len(calls), 1)

    def test_unavailable_geometry_waits_without_starting_capture(self):
        stop = Mock()
        stop.is_set.side_effect = [False, False, False, True]
        factory = Mock()
        screen = Mock(side_effect=ValueError('Controlled unavailable client'))
        control = RegionCapture(lambda: {}, screen)
        self.assertEqual(list(control.iter_source(factory, stop)), [])
        self.assertEqual(stop.wait.call_count, 3)
        stop.wait.assert_called_with(.25)
        factory.assert_not_called()

    def test_real_ocr_iterator_is_recreated_after_transient_geometry_loss(self):
        import numpy as np
        from app.recognition import capture, live, ocr
        stop = threading.Event()
        state = [False]
        frames = []
        def screen():
            if state[0]:
                state[0] = False
                raise ValueError('Controlled transient client lookup failure')
            return (1920, 1080)
        control = RegionCapture(lambda: {}, screen)
        def detect(image, **kwargs):
            frames.append(image.shape)
            if len(frames) == 1:
                state[0] = True
            else:
                stop.set()
            return []
        def factory(**geometry):
            return capture.iter_ocr_source(interval=0, glossary={}, only_in_game=False,
                verbose=False, stop_event=stop,
                frame_provider=lambda: np.full((1080,1920,3),255,dtype=np.uint8), **geometry)
        with patch.object(ocr,'set_tesseract',return_value='fixture'), \
                patch.object(ocr,'set_user_words',return_value=''), \
                patch.object(ocr,'find_bubbles',side_effect=detect), \
                patch.object(live,'is_paused',return_value=False), \
                patch.dict(live._LIVE,{},clear=True):
            self.assertEqual(list(control.iter_source(factory,stop)),[])
        self.assertEqual(len(frames),2)

    def test_stop_after_invalidation_does_not_start_a_new_source(self):
        stop = threading.Event()
        state = [False]
        def screen():
            if state[0]:
                raise ValueError('Controlled unavailable client')
            return (2560, 1440)
        control = RegionCapture(lambda: {}, screen)
        calls = []
        def factory(**kw):
            calls.append(True)
            kw['capture_ready']()
            state[0] = True
            self.assertTrue(kw['capture_changed']())
            stop.set()
            yield from ()
        self.assertEqual(list(control.iter_source(factory, stop)), [])
        self.assertEqual(len(calls), 1)
