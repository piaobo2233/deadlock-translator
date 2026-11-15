"""Long-body geometry and deliberate outgoing-language choice regressions."""
from pathlib import Path
import hashlib
import json
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from app.vision.body import locate_body


class DenseBodyTests(unittest.TestCase):
    def test_detached_script_parts_do_not_invent_extra_text_rows(self):
        image=np.full((100,330,3),30,np.uint8)
        cv2.rectangle(image,(20,10),(310,88),(220,230,235),-1)
        for y in (25,52):
            for x in (35,60,85):
                cv2.rectangle(image,(x,y),(x+13,y+20),(20,20,20),-1)
            for x in (115,140,165,190):
                cv2.rectangle(image,(x,y),(x+13,y+7),(20,20,20),-1)
                cv2.rectangle(image,(x,y+12),(x+13,y+20),(20,20,20),-1)
        body=locate_body(image,(15,5,300,90))
        self.assertTrue(body.accepted,body)
        self.assertEqual(body.line_count,2)
        self.assertGreaterEqual(body.text_rect[3],48)

    def paragraph(self):
        image=np.full((160,360,3),30,np.uint8)
        cv2.rectangle(image,(35,15),(325,132),(220,230,235),-1)
        for y in (31,55,79,103):
            for x in range(47,306,18):
                cv2.rectangle(image,(x,y),(x+15,y+16),(25,25,25),-1)
        return image

    def test_dense_rows_keep_all_four_lines(self):
        body=locate_body(self.paragraph(),(30,10,305,130),scale=1)
        self.assertTrue(body.accepted,body)
        self.assertEqual(body.line_count,4)
        self.assertGreaterEqual(body.text_rect[3],88)

    def test_dark_gutter_does_not_merge_separate_panels(self):
        image=self.paragraph();image[72:78,35:326]=30
        body=locate_body(image,(30,10,305,130),scale=1)
        self.assertTrue(body.accepted,body)
        self.assertLessEqual(body.line_count,2)
        self.assertLess(body.rect[3],70)

    def test_neutral_compressed_paragraph_requires_multiple_aligned_lines(self):
        image=self.paragraph()
        pale=image.min(axis=2)>160
        image[pale]=(230,230,230)
        body=locate_body(image,(30,10,305,130),scale=1)
        self.assertTrue(body.accepted,body)
        self.assertEqual(body.line_count,4)

    def test_partial_tint_cannot_truncate_a_complete_neutral_paragraph(self):
        image=self.paragraph()
        bottom=image[72:]
        bottom[bottom.min(axis=2)>160]=(230,230,230)
        body=locate_body(image,(30,10,305,130),scale=1)
        self.assertTrue(body.accepted,body)
        self.assertEqual(body.line_count,4)
        self.assertGreaterEqual(body.text_rect[3],88)

    def test_neutral_single_line_does_not_establish_a_chat_body(self):
        from tests.test_ocr_body import synthetic_chat
        image=synthetic_chat()
        image[image.min(axis=2)>160]=(230,230,230)
        body=locate_body(image,(0,0,*image.shape[1::-1]))
        self.assertFalse(body.accepted,body)

    def test_quality_fallback_has_fixed_source_license_and_identity(self):
        from app.recognition.resident import script_data_name
        folder=Path(__file__).resolve().parents[1]/'app/recognition/models'
        manifest=json.loads((folder/'quality-provenance.json').read_text())
        for name,row in manifest['files'].items():
            data=(folder/name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(),row['sha256'])
            self.assertEqual(len(data),row['bytes'])
        self.assertEqual(script_data_name('jpn',folder),'jpn_quality')
        self.assertEqual(script_data_name('rus',folder),'rus')

    def test_only_japanese_intercharacter_spacing_is_normalized(self):
        from app.recognition.languages import clean_ocr_spacing
        self.assertEqual(clean_ocr_spacing('一 緒 に\n攻 撃 しよう'),'一緒に攻撃しよう')
        for text in ('같이 공격해요','do not go','Подождите меня'):
            self.assertEqual(clean_ocr_spacing(text),text)

    def test_source_retry_keeps_full_agreement_and_confidence_requirements(self):
        from app.recognition import ocr
        from tests.test_ocr_body import synthetic_chat
        image=synthetic_chat();body=ocr.locate_body(image,(0,0,*image.shape[1::-1]))
        def tsv(text,confidence):
            return ('level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
                    +f'5\t1\t1\t1\t1\t1\t0\t0\t80\t20\t{confidence}\t{text}\n')
        for reading,confidence,verified in (('敵が来た',90,True),('敵が来ない',90,False),('敵が来た',20,False)):
            reader=Mock();reader.read.side_effect=[tsv('英が来た',10),tsv(reading,confidence),tsv(reading,confidence)]
            with patch.object(ocr,'_RESIDENT',reader),patch.object(ocr,'_RECEIVE_MODEL','chi_sim+eng+jpn'):
                evidence=ocr.source_evidence(image,body,'敵が来た')
            self.assertEqual(evidence.verified,verified)
            self.assertEqual(reader.read.call_count,3 if confidence < 65 else 2)
            if confidence < 65:
                self.assertEqual(reader.read.call_args.kwargs['language'],'jpn_standard')


if __name__=='__main__':unittest.main()
