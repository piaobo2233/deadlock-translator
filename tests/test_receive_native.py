"""Actual native helper and production generator; desktop frames are replayed."""

import app.translation.builtins as translation_builtins_module

import app.recognition.capture as recognition_capture
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
import app.recognition.ocr as ocr
from app.recognition.resident import ResidentReader, native_library
from app.runtime.pipeline import TranslationPipeline

ROOT=Path(__file__).resolve().parents[1]


class ResidentTests(unittest.TestCase):
    def setUp(self):
        executable=ocr.find_tesseract()
        if not executable or not any((Path(executable).parent/name).is_file()
                                     for name in ('libtesseract-5.5.dll', 'libtesseract-5.dll')):
            self.skipTest('Installed native Tesseract DLL unavailable')
        self.executable=executable
        self.models=ROOT/'app/recognition/models'

    def test_native_worker_watchdog_kills_instead_of_hanging(self):
        reader=ResidentReader(self.executable,self.models,'eng')
        process=reader.process
        self.addCleanup(reader.close)
        with patch.object(reader._responses,'get',side_effect=queue.Empty):
            with self.assertRaises(queue.Empty):
                reader.read(np.full((40,150),255,dtype=np.uint8),7,timeout=.02)
        self.assertIsNotNone(process.poll())
        self.assertTrue(reader.stop())

    def test_pipeline_exit_reaps_registered_native_reader(self):
        reader=ResidentReader(self.executable,self.models,'eng')
        self.addCleanup(reader.close)
        process=reader.process
        pipeline=TranslationPipeline(lambda: iter(()), lambda value: value)
        self.assertTrue(pipeline.register_capture_owner(reader))
        self.assertFalse(pipeline.finished)
        self.assertFalse(reader.finished)
        pipeline.stop(timeout=2)
        self.assertTrue(pipeline.finished)
        self.assertTrue(reader.finished)
        self.assertIsNotNone(process.poll())
        self.assertTrue(pipeline.stop(timeout=0))

    def test_zero_wait_does_not_drop_ownership_before_child_is_reaped(self):
        reader=ResidentReader(self.executable,self.models,'eng')
        self.addCleanup(reader.close)
        process=reader.process
        with patch.object(process,'wait',side_effect=subprocess.TimeoutExpired('ocr',0)):
            self.assertFalse(reader.stop(timeout=0))
        self.assertIs(reader.process,process)
        self.assertTrue(reader.stop(timeout=2))
        self.assertTrue(reader.finished)

    def test_packaged_entry_route_runs_only_worker_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([sys.executable,str(ROOT/'scripts/run.py'),'--ocr-worker',
                str(native_library(self.executable)),str(self.models),'eng',''],
                input='',capture_output=True,text=True,encoding='utf-8',cwd=directory,timeout=8)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),'{"ready": true}')
            self.assertFalse((Path(directory)/'data').exists())

    def test_three_actual_ocr_calls_reach_generator_and_close_owned_worker(self):
        part=ocr.imread_unicode(str(ROOT/'verify/truth/body/portrait_go_context.png'))
        frame=np.full((1080,1920,3),(30,25,32),np.uint8)
        frame[140:235,490:610]=part
        frames=iter([frame.copy() for _ in range(8)])
        owners=[]
        set_words=ocr.set_user_words
        previous_words=ocr._USER_WORDS
        def own(owner):
            owners.append(owner)
            return owner.start()
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(ocr,'set_user_words',side_effect=lambda words,**kwargs:
                    set_words(words,path=str(Path(folder)/'words.txt'))):
            source=recognition_capture.iter_ocr_source(interval=0,threshold=3,glossary=translation_builtins_module.DEFAULT_GLOSSARY,
                verbose=False,only_in_game=False,allow_popup=False,message_snapshots=True,
                frame_provider=lambda:next(frames,None),screen_size=(1920,1080),
                multilingual=True,capture_owner=own)
            started=time.monotonic()
            try:
                message=next(source)
                self.assertEqual(message.text.casefold(),'go')
                self.assertTrue(message.evidence.verified,message.evidence)
                self.assertLess(message.identity.first_seen_at,message.identity.confirmed_at)
                self.assertLess(time.monotonic()-message.identity.first_seen_at,4)
                self.assertEqual(len(owners),1)
                self.assertIsInstance(owners[0],ResidentReader)
            finally:
                source.close()
                ocr.configure_receive_languages(False,resident=False)
                ocr._USER_WORDS=previous_words
            self.assertIsNone(owners[0].process)


if __name__=='__main__':unittest.main()
