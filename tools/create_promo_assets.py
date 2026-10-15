"""Capture only our own real Tk UI using disposable, labelled demo content."""
from pathlib import Path
import os
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from PIL import ImageGrab
    import app.main as dt
    from app.storage import store
    from app.translation.client import ApiResponse
    from gui.app import App
    from gui import pages
    output=ROOT/'docs/promo/assets'
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='promo-ui-') as folder:
        store.atomic_write_json(store.data_path(folder,'config.json'),
            dict(api_key='demonstration-only-invalid-key',translate_api=True))
        store.atomic_write_json(Path(folder)/'.input-scope.json',{})
        store.save_glossary_file(store.data_path(folder,'glossary.json'),
            {word:store.glossary_entry(value,source='user') for word,value in
             {'push mid':'推进中路','need help':'需要支援','group up':'集合','wait for me':'等等我'}.items()})
        errors=[]
        with patch.dict(os.environ,DEEPSEEK_API_KEY=''), \
                patch.object(pages,'status_of',return_value=('演示预览','示例数据，未连接游戏或 API',pages.COLORS['muted'])), \
                patch.object(dt,'translator_is_running',return_value=False), \
                patch('urllib.request.urlopen',side_effect=AssertionError('No live HTTP in publicity captures')):
            app=App(folder,size='1210x850')
            app.root.geometry('1210x850+50+50')
            app.root.report_callback_exception=lambda *args:errors.append(repr(args))
            def capture(name):
                app.root.update()
                app.root.after(180,app.root.quit)
                app.root.mainloop()
                ImageGrab.grab(window=app.root.winfo_id()).save(output/name)
            try:
                app.open_page('home')
                capture('app-overview.png')
                composer=app.composer
                composer['source'].insert('1.0','等我一下，我马上到。')
                app.root.update()
                with patch('app.translation.client.ChatCompletionClient.request',return_value=ApiResponse(
                        text="Wait for me, I'll be right there.",finish_reason='stop',
                        usage=dict(prompt_tokens=10,completion_tokens=10))):
                    composer['translate'].invoke()
                    until=time.monotonic()+5
                    while composer['state']['busy'] and time.monotonic()<until:
                        app.root.update();time.sleep(.01)
                    assert composer['state']['translation']
                composer['status'].configure(text='演示内容：英文预览需确认后复制；本截图未调用真实 API。')
                y=composer['source'].winfo_rooty()-app.page.winfo_rooty()-100
                app.canvas.yview_moveto(max(0,y)/max(1,app.page.winfo_height()))
                capture('app-compose.png')
                app.open_page('dictionary')
                capture('app-dictionary.png')
                assert not errors,errors
            finally:
                app.root.destroy()
    print('Created 3 real-window demo captures; no live API or real user data')


if __name__=='__main__':main()
