"""Optional resident Tesseract C API in a killable, hidden helper process.

Body reads and compatible serial fallbacks reuse one context; source verification
has independent language contexts. CLI remains the bounded fallback.
"""
import base64
import ctypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading


class ResidentReader:
    def __init__(self, executable, models, languages, words='', *, startup_timeout=5):
        self.process = None
        self._lock = threading.Lock()
        self._responses = queue.Queue()
        dll = Path(executable).parent / 'libtesseract-5.dll'
        if sys.platform != 'win32' or not dll.is_file():
            raise OSError('Resident C API unavailable')
        env = dict(os.environ, OMP_THREAD_LIMIT='1', PYTHONUTF8='1')
        # The GUI bootloader/pythonw has no standard streams. The hidden helper
        # needs a console-capable executable for its JSON pipe protocol.
        executable_path = Path(sys.executable)
        if getattr(sys, 'frozen', False):
            command = [str(executable_path.with_name('DeadlockTranslate.exe')), '--ocr-worker']
        else:
            if executable_path.name.lower() == 'pythonw.exe':
                executable_path = executable_path.with_name('python.exe')
            command = [str(executable_path), '-u', '-m', __name__]
        try:
            self.process = subprocess.Popen(command + [str(dll), str(models), languages, words],
                cwd=Path(__file__).resolve().parents[2], env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self._reader = threading.Thread(target=self._receive, args=(self.process.stdout,), name='dl-ocr-reader', daemon=True)
            self._reader.start()
            if self._responses.get(timeout=startup_timeout) != {'ready': True}:
                raise OSError('Resident C API startup failed')
        except Exception:
            self.close()
            raise

    def _receive(self, stream):
        try:
            for line in stream:
                self._responses.put(json.loads(line))
        except (OSError, ValueError):
            pass
        finally:
            self._responses.put({'error': 'worker_closed'})

    def start(self):
        return self.process is not None and self.process.poll() is None

    @property
    def finished(self):
        return self.process is None or self.process.poll() is not None

    def read(self, image, mode, *, source=False, timeout=2, language=''):
        # One helper owns both language contexts; no native thread touches Tk.
        with self._lock:
            if self.process is None or self.process.poll() is not None:
                raise OSError('Resident OCR stopped')
            try:
                height, width = image.shape[:2]
                payload = dict(width=width, height=height, mode=mode, source=source, language=language,
                    pixels=base64.b64encode(image.tobytes()).decode('ascii'))
                self.process.stdin.write(json.dumps(payload) + '\n')
                self.process.stdin.flush()
                result = self._responses.get(timeout=max(.001, timeout))
                if 'error' in result:
                    raise OSError(result['error'])
                return result['text']
            except Exception:
                self.close()
                raise

    def close(self, timeout=1):
        process = self.process
        if process is None:
            return True
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=max(0, timeout))
        except subprocess.TimeoutExpired:
            # Retain ownership until the child has actually ended. Tk polls
            # again instead of falsely declaring a live helper finished.
            return False
        for stream in (process.stdin, process.stdout):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass
        if self.process is process:
            self.process = None
        return True

    def stop(self, timeout=1):
        return self.close(timeout=timeout)


def worker(dll, models, languages, words):
    # The signatures follow tesseract/include/tesseract/capi.h; all owned text
    # pointers are explicitly released, and native code stays in this process.
    directory = os.add_dll_directory(str(Path(dll).parent))
    lib = ctypes.CDLL(dll)
    pointer, integer, string = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    signatures = {
        'TessBaseAPICreate': (pointer, []),
        'TessBaseAPIInit4': (integer, [pointer,string,string,integer,pointer,integer,
                                     pointer,pointer,ctypes.c_size_t,integer]),
        'TessBaseAPISetPageSegMode': (None,[pointer,integer]),
        'TessBaseAPISetImage': (None,[pointer,pointer,integer,integer,integer,integer]),
        'TessBaseAPIGetUTF8Text': (pointer,[pointer]),
        'TessBaseAPIGetTsvText': (pointer,[pointer,integer]),
        'TessBaseAPIClearAdaptiveClassifier': (None,[pointer]),
        'TessBaseAPIDelete': (None,[pointer]),
        'TessDeleteText': (None,[pointer]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, arguments
    contexts = []
    try:
        source_languages = '+'.join(dict.fromkeys(('chi_sim', *languages.split('+'))))
        def initialize(language, use_words=False):
            api = lib.TessBaseAPICreate()
            contexts.append(api)
            use_words = bool(words) and use_words
            names = (string * 1)(b'user_words_file') if use_words else None
            values = (string * 1)(words.encode('utf-8')) if use_words else None
            if not api or lib.TessBaseAPIInit4(api, models.encode('utf-8'), language.encode('ascii'),
                    1, None, 0, names, values, 1 if use_words else 0, 0):
                raise OSError('Native model initialization failed')
            return api
        defaults = [initialize(languages, True), initialize(source_languages)]
        english = None
        scripts = {}
        print(json.dumps({'ready': True}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            width, height = request['width'], request['height']
            pixels = base64.b64decode(request['pixels'], validate=True)
            if not 0 < width * height <= 16000000 or len(pixels) != width * height:
                raise ValueError('Invalid grayscale image')
            language = request.get('language', '')
            if language:
                if language not in ('jpn', 'kor', 'rus'):
                    raise ValueError('Invalid script model')
                key = (language, bool(request['source']))
                if key not in scripts:
                    # Keep one script pair, rather than retaining six more full
                    # model contexts after a multilingual conversation.
                    if scripts and next(iter(scripts))[0] != language:
                        for old in scripts.values():
                            lib.TessBaseAPIDelete(old)
                            contexts.remove(old)
                        scripts.clear()
                    scripts[key] = initialize(language + '+eng+chi_sim', not request['source'])
                api = scripts[key]
            elif request['source'] == 'english':
                if english is None:
                    english = initialize('chi_sim+eng')
                api = english
            else:
                api = defaults[bool(request['source'])]
            buffer = ctypes.create_string_buffer(pixels)
            lib.TessBaseAPISetPageSegMode(api, request['mode'])
            lib.TessBaseAPIClearAdaptiveClassifier(api)
            lib.TessBaseAPISetImage(api, buffer, width, height, 1, width)
            output = (lib.TessBaseAPIGetTsvText(api, 0) if request['source']
                      else lib.TessBaseAPIGetUTF8Text(api))
            try:
                value = ctypes.string_at(output).decode('utf-8') if output else ''
            finally:
                if output:
                    lib.TessDeleteText(output)
            print(json.dumps({'text': value}), flush=True)
    finally:
        for api in contexts:
            if api:
                lib.TessBaseAPIDelete(api)
        directory.close()


if __name__ == '__main__':
    worker(*sys.argv[1:])
