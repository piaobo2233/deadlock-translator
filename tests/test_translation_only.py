"""Actual Tk display and real isolated translation routing; no paid requests."""

import gui.overlay as overlay_ui

import app.recognition.live as live_state

import app.platform.game_fonts as game_fonts
import app.translation.desktop_engine as desktop_translation
import inspect
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

import app.main as dt
from app.config.defaults import default_config
from app.config.repository import ConfigRepository
from app.config.specification import BY_KEY
from app.presentation.receipts import DisplayMonitor,DrawEntry,DrawFrame,DisplayState
from app.runtime.events import RuntimeHealth,PipelineStage
from app.runtime.messages import DisplayMessage
from app.translation.learning import LearnedGlossary
from gui.workspace import display_feedback


class UserKeyRoutingTests(unittest.TestCase):
    def test_other_tool_key_path_and_default_are_absent(self):
        self.assertFalse(hasattr(dt,'_key_from_claude_settings'))
        self.assertFalse(hasattr(dt,'CLAUDE_SETTINGS'))
        self.assertNotIn('key_fallback',inspect.signature(ConfigRepository.load).parameters)
        self.assertNotIn('dev_key_fallback',default_config())
        self.assertNotIn('dev_key_fallback',BY_KEY)
        self.assertEqual(default_config()['api_key'],'')

    def test_local_first_and_online_miss_durable_translation(self):
        with tempfile.TemporaryDirectory() as root:
            path=str(Path(root)/'glossary.json')
            repo=LearnedGlossary(path)
            repo.learn('hold here','守住这里')
            engine=desktop_translation.Translator('fixture-key','https://invalid.local','fixture',False,
                                 learning=repo,allow_popup=False)
            engine.client.request=Mock(side_effect=AssertionError('Real HTTP forbidden'))
            with patch.object(engine,'_call',return_value='一起前进') as call:
                local=engine.translate_result('hold here')
                self.assertTrue(local.successful);self.assertEqual(local.text,'守住这里')
                call.assert_not_called()
                online=engine.translate_result('move together')
                self.assertTrue(online.successful);self.assertEqual(online.text,'一起前进')
                call.assert_called_once()
            restarted=desktop_translation.Translator('','https://invalid.local','fixture',True,
                                    learning=LearnedGlossary(path),allow_popup=False)
            with patch.object(restarted,'_call',side_effect=AssertionError('Paid request forbidden')):
                self.assertEqual(restarted.translate_result('move together').text,'一起前进')
            engine.client.request.assert_not_called()

    def test_disabled_missing_key_and_failed_api_do_not_become_translation(self):
        for key,offline,expected in (('fixture-key',True,'pending'),('',False,'none')):
            with self.subTest(key_present=bool(key),offline=offline):
                engine=desktop_translation.Translator(key,'https://invalid.local','fixture',offline,glossary={},allow_popup=False)
                with patch.object(engine,'_call',side_effect=AssertionError('Paid request forbidden')):
                    result=engine.translate_result('move together')
                    self.assertFalse(result.successful);self.assertEqual(result.reason_code,expected)
        engine=desktop_translation.Translator('fixture-key','https://invalid.local','fixture',False,glossary={},allow_popup=False)
        with patch.object(engine,'_call',return_value='[翻译失败 HTTP 429]'):
            self.assertFalse(engine.translate_result('move together').successful)

    def test_panel_reason_counts_validate_and_expire(self):
        state=dict(at=100,translation_unavailable={'pending':1,'none':2,'budget':1},ui_status='视觉采集异常')
        feedback=display_feedback(state,running=True,now=101)[1]
        for label in ('在线翻译未启用','未配置密钥（翻译接口）','费用保护','视觉采集异常'):
            self.assertIn(label,feedback)
        self.assertEqual(len(feedback.splitlines()),4)
        self.assertEqual(display_feedback(state,running=True,now=131)[1],'')
        self.assertEqual(display_feedback(state,running=False,now=101),('',''))
        for bad in ({'none':True},{'none':-1},[],{'none':'1'}):
            self.assertIn('记录异常',display_feedback({**state,'translation_unavailable':bad},running=True,now=101)[0])


class TranslationOnlyTkTests(unittest.TestCase):
    def setUp(self):
        with patch.object(game_fonts,'register_game_font',return_value=[]):
            self.view=overlay_ui.Overlay(dict(width=300,font_size=18,max_messages=1))
        self.health=RuntimeHealth();self.monitor=DisplayMonitor(self.health)
        self.view._display_observer=self.monitor.observe
        self.errors=[]
        self.view.root.report_callback_exception=lambda *args:self.errors.append(str(args))
        self.box=(100,200,160,35)

    def tearDown(self):
        self.view.root.destroy()
        self.assertFalse(self.errors)

    def render(self,messages,current=True):
        with patch.object(live_state,'live_message_box',return_value=self.box if current else None), \
             patch.object(live_state,'live_box',return_value=self.box if current else None), \
             patch.object(live_state,'live_obstacles',return_value=()):
            self.view.render(messages);self.view.root.update()

    def test_failed_messages_have_no_canvas_bubbles_and_do_not_take_translation_slots(self):
        now=time.time()
        failed=[DisplayMessage('',code,'not a translation','all',now,None,i,successful=False,reason_code=code)
                for i,code in enumerate(('pending','none','budget','api_failed'),2)]
        self.render(failed)
        self.assertFalse(self.view.canvas.find_all())
        self.assertFalse(self.view.root.winfo_viewable())
        self.assertEqual(self.view.label_rects,[])
        self.assertEqual(self.monitor.unavailable_reasons,dict(pending=1,none=1,budget=1,api_failed=1))
        self.assertTrue(all(event.state is DisplayState.HIDDEN for event in self.monitor.events))
        self.assertEqual(self.health.count(PipelineStage.DISPLAY,'translation_drawn'),0)
        translated=DisplayMessage('','hold here','守住这里','all',now,None,1)
        self.render([translated,*failed])
        texts=[self.view.canvas.itemcget(item,'text') for item in self.view.canvas.find_all()
               if self.view.canvas.type(item)=='text']
        self.assertEqual(texts,['守住这里'])
        self.assertEqual(self.health.count(PipelineStage.DISPLAY,'translation_drawn'),1)
        self.assertEqual(self.health.count(PipelineStage.DISPLAY,'reason_drawn'),0)
        self.assertEqual(len(self.view.label_rects),1)

    def test_same_message_success_clears_panel_reason_and_expired_source_does_not_revive(self):
        item=DisplayMessage('','hold here','','all',time.time(),None,1,successful=False,reason_code='none')
        self.render([item]);self.assertEqual(self.monitor.unavailable_reasons,{'none':1})
        translated=item._replace(translated='守住这里',successful=True,reason_code='')
        self.render([translated]);self.assertEqual(self.monitor.unavailable_reasons,{})
        self.assertTrue(self.view.root.winfo_viewable())
        self.render([translated],current=False)
        self.assertFalse(self.view.root.winfo_viewable())
        self.assertEqual(self.view.label_rects,[])
        self.assertEqual(self.health.count(PipelineStage.DISPLAY,'translation_drawn'),1)

    def test_capture_notice_and_blank_legacy_result_never_draw_game_content(self):
        from app.presentation.notices import FailureNotice
        self.view._notice=FailureNotice('visual_capture','isolated fault')
        self.render([])
        self.assertFalse(self.view.canvas.find_all())
        self.render([('','hold here','','all',self.box,1)])
        self.assertFalse(self.view.canvas.find_all())
        self.assertFalse(self.view.root.winfo_viewable())


if __name__=='__main__': unittest.main()
