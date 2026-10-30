"""Current receive phases from isolated runtime data; no game or API needed."""
import tempfile
import time
import unittest
from unittest.mock import patch

from app.runtime.reporting import capture_diagnostics
from app.storage import store
from gui.pages import status_of
from gui.workspace import display_feedback


class RuntimeStatusUiTests(unittest.TestCase):
    def test_current_work_is_distinct_from_cumulative_history(self):
        with tempfile.TemporaryDirectory() as root, patch('gui.pages.desktop.translator_is_running',return_value=True):
            base=dict(at=time.time(),game=True,game_foreground=True,ocr_confirmed=200,
                      translated=199,recognition_capture=dict(ocr_active=False))
            for changes,word in (
                    ({},'等待聊天'),
                    (dict(recognition_capture=dict(ocr_active=True)),'正在识别'),
                    (dict(translate_requests=1),'正在翻译'),
                    (dict(queue_pending=1),'正在翻译'),
                    (dict(recognition_ocr=dict(backend='cli',last_failure='Empty')),'兼容识别'),
                    (dict(paused=True,translate_requests=1),'已暂停'),
                    (dict(game=False),'等待游戏'),
                    (dict(game_foreground=False),'已挂起'),
                    (dict(at=time.time()-40,translate_requests=1),'没有响应')):
                with self.subTest(changes=changes):
                    store.atomic_write_json(store.data_path(root,'runtime.json'),dict(base,**changes))
                    self.assertEqual(status_of(root)[0],word)

    def test_capture_snapshot_rejects_missing_private_and_nonfinite_values(self):
        self.assertEqual(capture_diagnostics(None),{})
        self.assertEqual(capture_diagnostics(dict(frames=12,no_ocr=float('nan'),
            garbage_filtered=-1,confirmed='secret',ocr_active=True,raw='private',box=[1,2,3,4])),
            dict(frames=12,ocr_active=True))

    def test_failed_screenshot_is_visible_without_inferring_missing_translation(self):
        counts,message=display_feedback(dict(at=time.time(),ocr_confirmed=4,translated=3,
            display_translation_drawn=2,recognition_evidence=dict(last_failure='PermissionError')),
            running=True,now=time.time())
        self.assertIn('已绘制译文 2',counts)
        self.assertIn('诊断截图保存失败',message)
        self.assertIn('手动选择截图',message)


if __name__=='__main__':unittest.main()
