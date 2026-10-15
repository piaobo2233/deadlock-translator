"""Compare per-frame OCR and independent source evidence, never call translation APIs.

Existing real crops are a counterexample set, not a complete match denominator.
Generated language samples exercise deployment only, not game recognition accuracy.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import statistics
import sys
import time
import tempfile


def working_set_mb(process):
    import ctypes
    class Memory(ctypes.Structure):
        _fields_=[('size',ctypes.c_uint32),('faults',ctypes.c_uint32)]+[
            (name,ctypes.c_size_t) for name in ('peak','working','quota_peak','quota','nonpaged_peak',
                                              'nonpaged','pagefile','pagefile_peak')]
    counters=Memory()
    counters.size=ctypes.sizeof(counters)
    api=ctypes.WinDLL('psapi').GetProcessMemoryInfo
    api.argtypes=[ctypes.c_void_p,ctypes.POINTER(Memory),ctypes.c_uint32]
    if not api(int(process._handle),ctypes.byref(counters),ctypes.sizeof(counters)):
        return None
    return round(counters.working/1024/1024,1)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(output, only=None):
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    import app.main as dt
    import app.recognition.ocr as ocr
    ocr.set_tesseract('')
    cases = json.loads((ROOT/'verify/truth/body_cases.json').read_text(encoding='utf-8'))
    samples = []
    for name, case in cases.items():
        image = ocr.imread_unicode(str(ROOT/'verify/truth/body'/name))
        if image is None:
            raise ValueError('Missing truth: ' + name)
        samples.append((name, image, case.get('expect'), case.get('screen',(1920,1080))))
    font = ImageFont.truetype(r'C:\Windows\Fonts\arialbd.ttf',22)
    for language, value in (('en','wait for me'),('es','necesito ayuda'),('pt','não agora'),
                            ('fr','où êtes vous'),('de','Wir brauchen Hilfe')):
        image = Image.new('RGB',(max(280,round(font.getlength(value))+70),90),(25,25,25))
        draw = ImageDraw.Draw(image)
        draw.rectangle((30,25,image.width-3,87),fill=(216,214,205))
        draw.text((38,35),value,font=font,fill=(12,12,12))
        samples.append(('synthetic_'+language,cv2.cvtColor(np.asarray(image),cv2.COLOR_RGB2BGR),value,(2560,1440)))
    profiles = []
    words = tempfile.TemporaryDirectory()
    previous_words = ocr._USER_WORDS
    ocr.set_user_words(dt.DEFAULT_GLOSSARY, path=Path(words.name)/'words.txt')
    try:
        for name, multi, native in (('cli_english',False,False),('cli_five_languages',True,False),
                                     ('resident_five_languages',True,True)):
            if only and name != only:
                continue
            started = time.perf_counter()
            ocr.configure_receive_languages(multi,resident=native)
            startup_ms = round((time.perf_counter()-started)*1000)
            rows=[]
            for sample, image, expected, screen in samples:
                started = time.perf_counter()
                box=(0,0,image.shape[1],image.shape[0])
                body=ocr.locate_body(image,box,scale=screen[1]/ocr.REF_H)
                cached=[]
                def verify(text):
                    evidence=ocr.source_evidence(image,body,text,scale=screen[1]/ocr.REF_H)
                    cached.append(evidence)
                    return evidence
                decision=dt.select_candidate(ocr.ocr_variants(image,box,screen=screen,region=body),
                    dt.DEFAULT_GLOSSARY,dt.recognition_rules(),verify_body=verify if native else None)
                ocr_ms=round((time.perf_counter()-started)*1000)
                from app.recognition.source import for_text
                evidence=(for_text(cached[0],decision.text) if cached else
                    ocr.source_evidence(image,body,decision.text,scale=screen[1]/ocr.REF_H)) if decision.accepted else None
                rows.append(dict(sample=sample,expected=expected,actual=decision.text,ocr_ms=ocr_ms,
                    body=body.reason,admitted=decision.accepted,
                    strict=dt.normalize_query(decision.text)==dt.normalize_query(expected) if expected else not decision.accepted,
                    source_verified=evidence.verified if evidence else None,
                    source_reason=evidence.reason if evidence else '',source_ms=evidence.elapsed_ms if evidence else 0))
            active=[r for r in rows if r['admitted']]
            profiles.append(dict(name=name,startup_ms=startup_ms,resident_active=ocr._RESIDENT is not None,
                resident_working_set_mb=working_set_mb(ocr._RESIDENT.process) if ocr._RESIDENT is not None else None,
                strict=sum(r['strict'] for r in rows),total=len(rows),
                admitted=len(active),false_positive=sum(r['admitted'] and not r['expected'] for r in rows),
                ocr_median_ms=statistics.median(r['ocr_ms'] for r in active) if active else None,
                source_median_ms=statistics.median(r['source_ms'] for r in active) if active else None,rows=rows))
            print(json.dumps({k:v for k,v in profiles[-1].items() if k!='rows'}),flush=True)
    finally:
        ocr.close_receive_backend()
        ocr._USER_WORDS = previous_words
        words.cleanup()
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(dict(scope=__doc__,profiles=profiles),ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--profile',choices=('cli_english','cli_five_languages','resident_five_languages'))
    args=parser.parse_args()
    run(args.output,args.profile)
