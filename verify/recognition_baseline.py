"""Score fixed synthetic chat crops using the real OCR and source-evidence path."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, __version__ as pillow_version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.recognition import ocr
from app.recognition.capture import recognition_rules
from app.recognition.selection import select_candidate
from app.translation.builtins import DEFAULT_GLOSSARY

SAMPLES = {
    'eng': ('go', 'GG', 'wait for me', "don't push yet", 'I have 1 urn',
            'defend left\nthen push mid', 'can you help at the bridge'),
    'jpn': ('一緒に攻撃しよう', '左を守ってください'),
    'kor': ('같이 공격해요', '왼쪽을 지켜요'),
    'rus': ('Ждите меня', 'Защищайте слева'),
    'negative': ('', '100 / 120'),
}
FONTS = {'jpn': 'msgothic.ttc', 'kor': 'malgunbd.ttf'}


def samples(font_dir):
    for language, texts in SAMPLES.items():
        font_path = font_dir / FONTS.get(language, 'arialbd.ttf')
        for index, text in enumerate(texts, 1):
            size = 18 if index % 2 == 0 else 22
            font = ImageFont.truetype(str(font_path), size)
            bottom = 113 if '\n' in text else 75
            width = max(210, round(max(font.getlength(line) for line in text.split('\n'))) + 58)
            image = Image.new('RGB', (width, bottom + 3), (32, 25, 30))
            draw = ImageDraw.Draw(image)
            draw.rectangle((30, 25, width - 3, bottom), fill=(216, 214, 205))
            draw.rectangle((0, 25, 28, 49), fill=(75, 120, 140))
            draw.multiline_text((38, 30), text, font=font, fill=(12, 12, 12))
            expected = '' if language == 'negative' else text
            yield f'{language}-{index:02}', language, size, expected, cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)


def read_sample(image):
    box = (0, 0, image.shape[1], image.shape[0])
    body = ocr.locate_body(image, box)
    if not body.accepted:
        return ''
    variants = ocr.ocr_variants(image, box, region=body, max_workers=1)
    return select_candidate(variants, DEFAULT_GLOSSARY, recognition_rules(),
                            verify_body=lambda text: ocr.source_evidence(image, body, text)).text or ''


def summarize(rows):
    count = len(rows)
    missed = sum(bool(row['expected']) and not row['selected'] for row in rows)
    wrong = sum(bool(row['selected']) and not row['exact'] for row in rows)
    correct_times = sorted(row['ocr_ms'] for row in rows if row['exact'] and row['selected'])
    return dict(reads=count, missed=missed, wrong=wrong, miss_rate=missed / count,
                wrong_rate=wrong / count, correct_timing_reads=len(correct_times),
                ocr_p50_ms=correct_times[math.ceil(len(correct_times) * .5) - 1] if correct_times else None,
                ocr_p95_ms=correct_times[math.ceil(len(correct_times) * .95) - 1] if correct_times else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tesseract', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--mode', choices=('english', 'multilingual', 'both'), default='both')
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    if args.rounds < 1 or not args.tesseract.is_file() or args.output.exists():
        parser.error('Use a positive round count, an existing OCR executable and a new output path')
    ocr.set_tesseract(str(args.tesseract.resolve()))
    font_dir = Path(os.environ['WINDIR']) / 'Fonts'
    corpus = list(samples(font_dir))
    modes = ('english', 'multilingual') if args.mode == 'both' else (args.mode,)
    rows = []
    for repeat in range(args.rounds):
        for mode in modes if repeat % 2 == 0 else reversed(modes):
            ocr.configure_receive_languages(mode == 'multilingual')
            try:
                for sample_id, language, font_px, expected, image in corpus:
                    start = time.perf_counter()
                    selected = read_sample(image)
                    elapsed = round((time.perf_counter() - start) * 1000, 2)
                    rows.append(dict(id=sample_id, language=language, font_px=font_px, mode=mode,
                                     repeat=repeat + 1, expected=expected, selected=selected,
                                     exact=ocr.comparable(expected) == ocr.comparable(selected),
                                     ocr_ms=elapsed))
            finally:
                ocr.close_receive_backend()
    groups = {mode: {language: summarize([row for row in rows if row['mode'] == mode
                                         and row['language'] == language]) for language in SAMPLES}
              for mode in modes}
    report = dict(version=(ROOT / 'VERSION').read_text().strip(), system=platform.platform(),
                  input='synthetic crops, Windows fonts at 18/22 px, no physical display',
                  tesseract_sha256=hashlib.sha256(args.tesseract.read_bytes()).hexdigest(),
                  pillow=pillow_version, opencv=cv2.__version__, samples=len(corpus),
                  rounds=args.rounds, groups=groups, reads=rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(groups, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
