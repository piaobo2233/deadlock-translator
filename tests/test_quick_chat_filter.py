"""Native quick-chat filtering, crop boundaries and bounded OCR recovery."""
import unittest
from unittest.mock import Mock, patch

import app.recognition.ocr as ocr
from app.recognition.quick_chat import QuickChat
from app.recognition.chat_area import candidate_inside, chat_body_sized, typing_indicator
from app.recognition.region import preset
from app.recognition.reuse import QuickChatPixels, QuickChatTemplates
from test_ocr_body import synthetic_chat
from app.vision.body import locate_body, BodyRegion


class QuickChatFilterTests(unittest.TestCase):
    def test_local_templates_require_manual_official_label_and_exact_glyphs(self):
        import base64,json,tempfile
        from pathlib import Path
        import numpy as np
        from PIL import Image,ImageDraw,ImageFont
        from app.vision.presence import visual_body
        image=Image.new('RGB',(200,80),(25,25,25))
        draw=ImageDraw.Draw(image)
        draw.rectangle((30,25,195,75),fill=(216,214,205))
        draw.text((40,30),'上了',font=ImageFont.truetype('C:/Windows/Fonts/msyhbd.ttc',22),fill=(10,10,10))
        array=np.asarray(image).copy()
        region=locate_body(array,(0,0,200,80))
        mask=visual_body(array,region).mask
        row=dict(text='上了',verified=True,shape=list(mask.shape),
            mask=base64.b64encode(np.packbits(mask).tobytes()).decode())
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'visual.json'
            self.assertFalse(QuickChatTemplates(path,QuickChat()).matches(array,region))
            path.write_text(json.dumps(dict(templates=[row])),encoding='utf-8')
            templates=QuickChatTemplates(path,QuickChat())
            self.assertTrue(templates.matches(array,region))
            for text in ('go','gj','ES','REM','omg','its uh'):
                english=synthetic_chat((text,))
                self.assertFalse(templates.matches(english,locate_body(english,(0,0,*english.shape[1::-1]))))
            row['verified']=False
            path.write_text(json.dumps(dict(templates=[row])),encoding='utf-8')
            self.assertFalse(QuickChatTemplates(path,QuickChat()).matches(array,region))

    def test_final_chinese_read_never_yields_a_local_glossary_guess(self):
        import numpy as np
        import app.main as main
        from app.recognition.source import SourceEvidence
        part=synthetic_chat(('go',))
        frame=np.zeros((1440,2560,3),np.uint8)
        frame[220:220+part.shape[0],500:500+part.shape[1]]=part
        frames=iter((frame,frame))
        stats=main.new_capture_stats()
        with patch.object(ocr,'set_tesseract',return_value='fixture'), patch.object(ocr,'set_user_words',return_value=''), patch.object(ocr,'configure_receive_languages'), patch.object(ocr,'close_receive_backend'), patch.object(ocr,'_RESIDENT',None), patch.object(ocr,'ocr_variants',side_effect=lambda *a,**kw:iter((('grayblock6','REM'),))), patch.object(ocr,'source_evidence',return_value=SourceEvidence(False,'native_chinese','灵瓮就在那里！')) as evidence:
            source=main.iter_ocr_source(interval=0,threshold=1,
                glossary=dict(main.DEFAULT_GLOSSARY,rem='雷姆'),verbose=False,
                only_in_game=False,allow_popup=False,message_snapshots=True,
                frame_provider=lambda:next(frames,None),screen_size=(2560,1440),stats=stats)
            self.assertEqual(list(source),[])
        evidence.assert_called_once()
        self.assertEqual(stats['native_chinese_filtered'],1)

    def test_typing_squares_are_filtered_but_ordinary_text_and_ellipsis_are_not(self):
        import numpy as np
        image=np.full((44,130,3),(210,214,220),np.uint8)
        for left in (50,66,82):
            image[16:27,left:left+11]=135
        self.assertTrue(typing_indicator(image,(0,0,130,44)))
        image[16:27,82:93]=(210,214,220)
        self.assertFalse(typing_indicator(image,(0,0,130,44)))
        for text in ('wait for me','go','...','mmm'):
            ordinary=synthetic_chat((text,))
            self.assertFalse(typing_indicator(ordinary,(0,0,*ordinary.shape[1::-1])))

    def test_ui_body_geometry_preserves_existing_recorded_chat(self):
        from verify.adaptive_ocr_probe import load_samples
        for name,image,expected,screen in load_samples():
            if expected:
                with self.subTest(name=name):
                    scale=screen[1]/1440
                    body=locate_body(image,(0,0,*image.shape[1::-1]),scale=scale)
                    self.assertTrue(chat_body_sized(body,scale=scale))
        self.assertFalse(chat_body_sized(BodyRegion(rect=(0,0,89,22),line_count=1)))
        self.assertFalse(chat_body_sized(BodyRegion(rect=(0,0,200,96),line_count=15)))

    def test_only_official_chinese_messages_and_known_dynamic_names_match(self):
        catalog = QuickChat()
        for text in ('谢了！', '上了', '撤退', '帮忙护送灵瓮', '代行敕令好了！'):
            with self.subTest(text=text):
                self.assertTrue(catalog.matches(text))
        for text in ('thanks', 'sorry', 'fair trade', 'coming green', '凭空造出的名字好了！'):
            with self.subTest(text=text):
                self.assertFalse(catalog.matches(text))

    def test_preset_covers_portrait_band_and_scales(self):
        self.assertEqual(preset((2560,1440))['rect'], [339,190,2220,766])
        self.assertEqual(preset((3840,2160))['rect'], [508,285,3330,1149])

    def test_clipped_feed_tail_is_rejected_before_ocr(self):
        self.assertFalse(candidate_inside((0,100,120,40),(576,1881,3)))
        self.assertFalse(candidate_inside((1800,100,81,40),(576,1881,3)))
        self.assertTrue(candidate_inside((100,100,120,40),(576,1881,3)))

    def test_pixel_reuse_never_adapts_or_extends_lifetime(self):
        first=synthetic_chat(('wait for me',))
        changed=synthetic_chat(('not for me',))
        region=locate_body(first,(0,0,first.shape[1],first.shape[0]))
        cache=QuickChatPixels()
        cache.remember(first,region,10.)
        self.assertTrue(cache.skip(first,region,10.2))
        self.assertFalse(cache.skip(changed,locate_body(changed,(0,0,changed.shape[1],changed.shape[0])),10.3))
        self.assertFalse(cache.skip(first,region,20.))

    def test_expired_source_task_does_not_kill_healthy_reader(self):
        reader=Mock()
        with patch.object(ocr,'_RESIDENT',reader), patch.object(ocr.time,'monotonic',return_value=10.):
            result=ocr._read_source_evidence(None,Mock(accepted=True),'go',scale=1.,deadline=9.)
        self.assertEqual(result.reason,'source_ocr_timeout')
        reader.read.assert_not_called()
        reader.close.assert_not_called()

    def test_recovery_is_explicit_and_rate_limited(self):
        reader=Mock()
        with patch.object(ocr,'_RESIDENT',None), patch.object(ocr,'_RESIDENT_SETTINGS',('cmd','dir','model','words')), patch.object(ocr,'_RESIDENT_RECOVERY_AT',30.), patch.object(ocr.time,'monotonic',return_value=29.), patch('app.recognition.resident.ResidentReader',return_value=reader) as factory:
            self.assertIsNone(ocr.recover_receive_backend())
            factory.assert_not_called()
            with patch.object(ocr.time,'monotonic',return_value=31.):
                self.assertIs(ocr.recover_receive_backend(),reader)
                self.assertIsNone(ocr.recover_receive_backend())
            factory.assert_called_once()


if __name__ == '__main__':
    unittest.main()
