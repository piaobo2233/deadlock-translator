"""Render every desktop page with disposable demo data, never real user files."""


from pathlib import Path
import argparse
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.runtime.input_scope as input_scope


def main():
    from PIL import ImageGrab
    from app.storage import store
    from gui import app as app_mod, pages

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cost-cases", action="store_true", help="Render complete/partial/empty/broken consumption states")
    parser.add_argument("--size", default="820x650")
    parser.add_argument("--scaling", type=float, help="Override Tk point scaling for layout inspection")
    parser.add_argument('--interactive', action='store_true', help='Keep the isolated demo window open')
    parser.add_argument('--page', default='home', help='Initial section for interactive preview')
    parser.add_argument('--evidence', action='store_true', help='Also capture row help and the region modal with a synthetic image')
    options = parser.parse_args()

    output = ROOT / ".temp" / "ui-review"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deadlock-ui-", dir=ROOT / '.temp') as folder:
        root = Path(folder)
        config = store.data_path(folder, "config.json")
        store.atomic_write_json(config, {"price_in": .8, "price_out": 3.2,
                                        "translate_api": False, "api_key": ""})
        store.save_glossary_file(store.data_path(folder, "glossary.json"), {
            "wait me": store.glossary_entry("等等我", source="api"),
            "together": store.glossary_entry("一起行动", source="api"),
            "push mid": store.glossary_entry("推进中路", source="user"),
            "old phrase": store.glossary_entry("旧译文", enabled=False),
        })
        pending_rows={
            'can you help me':dict(original='can you help me', translation='你能来帮我吗？', source='api', status='pending'),
            'awesome duel':dict(original='awesome duel', translation='精彩的对决', source='api', status='pending'),
            'push together':dict(original='push together', translation='一起推进', source='api', status='accepted'),
        }
        from app.storage.requests import fingerprint
        from app.translation.query import QueryKey
        from app.translation import policy as chat_policy
        for name,row in pending_rows.items():
            if row['status']!='pending':continue
            row.update(quality_checks=chat_policy._translation_quality_report(name,row['translation'],{}),
                request_ids=['demo-'+name],
                version_id=fingerprint(dict(query=QueryKey.from_text(name).as_dict(),translation=row['translation'])),
                display_receipt=dict(session_id='a'*32,identity=['a'*32,1,1],request_id='demo-'+name,at=1))
        store.save_collection(store.data_path(folder,'pending.json'),pending_rows)
        today = time.strftime("%Y-%m-%d")
        bucket = {"glossary_hits": 120, "memory_hits": 15, "cache_hits": 8}
        days = {}
        for offset in range(7):
            stamp = time.strftime("%Y-%m-%d", time.localtime(time.time() - offset * 86400))
            days[stamp] = dict(bucket)
        store.atomic_write_json(store.data_path(folder, "stats.json"),
                                {"days": days, "sessions": [bucket]})
        from app.storage.requests import RequestJournal
        from app.translation.client import ApiResponse
        from app.translation.parameters import request_parameters
        journal = RequestJournal(str(Path(store.data_path(folder, 'stats.json')).parent/'requests'))
        for offset in range(7):
            journal._now = lambda offset=offset: time.time()-offset*86400
            for model, multiplier in (('deepseek-flash',1),('deepseek-v4-pro',2)):
                params = dict(request_parameters('https://invalid.example',model).snapshot(),
                    model=model,endpoint_fingerprint='a'*64,prompt_fingerprint='b'*64)
                original=f'demo-{offset}-{model}'
                rid=journal.prepare(original,params,dict(input=.8,output=3.2),session_id='00000000-0000-4000-8000-000000000001')
                tokens=dict(prompt_tokens=(1400+offset*350)*multiplier,
                            completion_tokens=210*multiplier,total_tokens=(1610+offset*350)*multiplier)
                journal.response(original,rid,ApiResponse(text='演示译文',usage=tokens))
        errors = []
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "", "DEADLOCK_TEST_NO_NETWORK": "1"}), \
                patch('socket.socket.connect', side_effect=RuntimeError('UI preview: network disabled')), \
                patch('socket.socket.connect_ex', side_effect=RuntimeError('UI preview: network disabled')), \
                patch('app.game_data.service.application_root', return_value=folder), \
                patch.object(input_scope, "CONFIG_PATH", config), \
                patch.object(pages, "status_of", return_value=("未运行", "截图使用演示数据，未连接游戏或 API", pages.COLORS["muted"])):
            app = app_mod.App(folder, size=options.size)
            app.th.label(app.bar,'演示数据',bg=pages.COLORS['shell'],fg=pages.COLORS['muted'],font=app.th.font_small).pack(side='right',padx=12)
            app.root.geometry(options.size + "+60+60")
            if options.scaling:
                app.root.tk.call("tk", "scaling", options.scaling)
            app.root.report_callback_exception = lambda *args: errors.append(str(args))
            try:
                if options.interactive:
                    app.root.title('锁语 · 隔离预览')
                    app.open_page(options.page)
                    app.root.mainloop()
                    return
                cases = ("complete", "partial", "empty", "broken") if options.cost_cases else ('home','send','api','ingame','cost','dictionary','dictionary-review','dictionary-processed','settings','settings-update','onboarding')
                for case in cases:
                    key = "cost" if options.cost_cases else 'dictionary' if case.startswith('dictionary') else 'settings' if case.startswith('settings') else case
                    from app.platform.updates import Release
                    app.updater.release = Release('9.9.9', '演示发行说明：仅核对按钮排版，未检查或下载更新。', today,
                                                  'https://invalid.example') if case == 'settings-update' else None
                    app.updater.repaint()
                    if options.cost_cases:
                        from app.storage.usage import empty_bucket
                        item = dict(bucket)
                        if case == "partial":
                            journal._now=time.time
                            original='demo-missing-usage'
                            rid=journal.prepare(original,params,dict(input=.8,output=3.2),
                                session_id='00000000-0000-4000-8000-000000000001')
                            journal.response(original,rid,ApiResponse(text='用量缺失的演示回执'))
                        if case == "empty":
                            item = empty_bucket()
                        # Receipt-free root produces a real empty state without deleting fixtures.
                        case_root=folder if case not in {'empty','broken'} else str(root/case)
                        doc = {"days": {today: item}, "sessions": [item]}
                        stats_path = store.data_path(case_root, "stats.json")
                        Path(stats_path).parent.mkdir(parents=True,exist_ok=True)
                        if case == "broken":
                            Path(stats_path).write_bytes(b"{broken demo ledger")
                        else:
                            store.atomic_write_json(stats_path, doc)
                        app.root.destroy()
                        app = app_mod.App(case_root, size=options.size)
                        app.root.report_callback_exception = lambda *args: errors.append(str(args))
                    if not app.open_page(key):
                        raise RuntimeError(f"Page failed: {key}")
                    if case.startswith('dictionary'):
                        dictionary = app.dictionary
                        dictionary.pick({'dictionary-review':'review','dictionary-processed':'processed'}.get(case,'formal'))
                        if dictionary.rows:
                            index = next((i for i,row in enumerate(dictionary.rows) if row['key']=='push mid'),0)
                            dictionary.list.reveal(dictionary.rows[index]['key']).invoke()
                    app.root.update()
                    app.root.after(350, app.root.quit)
                    app.root.mainloop()
                    # Capture our own Tk client window, not whichever app is in front.
                    image = ImageGrab.grab(window=app.root.winfo_id())
                    background = tuple(int(pages.COLORS["bg"][i:i + 2], 16) for i in (1, 3, 5))
                    colors = image.getcolors(image.width * image.height) or []
                    if sum(count for count, color in colors if color == background) < image.width * image.height * .05:
                        raise RuntimeError("Preview capture does not contain the expected UI background")
                    suffix = ("-" + options.size + (f"-scale{options.scaling:g}" if options.scaling else ""))
                    path = output / (f"cost-{case}{suffix}.png" if options.cost_cases else f"{case}{suffix}.png")
                    image.save(path)
                    print(path)
                if errors:
                    raise RuntimeError("Tk callbacks failed: " + "; ".join(errors))
                if options.evidence:
                    # A known desktop substitute makes compositor evidence private and repeatable.
                    backdrop=app.th.tk.Tk()
                    backdrop.overrideredirect(True)
                    backdrop.configure(bg='#193A52')
                    backdrop.geometry(f'{app.root.winfo_width()}x{app.root.winfo_height()}+{app.root.winfo_rootx()}+{app.root.winfo_rooty()}')
                    backdrop.attributes('-topmost',True)
                    backdrop.update()
                    app.open_page('recognition')
                    app.root.attributes('-topmost',True)
                    app.root.lift()
                    app.root.focus_force()
                    app.root.update()
                    backdrop.lower(app.root)
                    app.root.after(250,app.root.quit)
                    app.root.mainloop()
                    def descendants(widget):
                        for child in widget.winfo_children():
                            yield child
                            yield from descendants(child)
                    scale=next(w for w in descendants(app.workspace.frames['recognition']) if w.winfo_class()=='TScale')
                    app.workspace.reveal(scale)
                    app.interactions.update(scale)
                    app.root.after(220,app.root.quit)
                    app.root.mainloop()
                    app.interactions.show_hint()
                    if app.interactions.hint is not None:
                        app.interactions.hint.attributes('-topmost',True)
                        app.interactions.hint.lift(app.root)
                    app.root.update()
                    def capture_client(name):
                        app.root.update()
                        time.sleep(.2)  # Let the desktop compositor draw owned windows.
                        x,y=app.root.winfo_rootx(),app.root.winfo_rooty()
                        path=output/f'{name}-{options.size}.png'
                        captured=ImageGrab.grab(bbox=(x,y,x+app.root.winfo_width(),y+app.root.winfo_height()))
                        colors=captured.getcolors(captured.width*captured.height) or []
                        panel=tuple(int(pages.COLORS['panel'][i:i+2],16) for i in (1,3,5))
                        # The real compositor blend moves an opaque color by up to 9 levels.
                        if sum(count for count,color in colors if any(max(abs(a-b) for a,b in zip(color,base))<=10 for base in (background,panel)))<captured.width*captured.height*.02:
                            raise RuntimeError('Evidence window was occluded; capture not saved')
                        captured.save(path)
                        print(path)
                    capture_client('recognition-hover')
                    app.interactions.update(None)
                    app.open_page('api')
                    app.root.update()
                    box=app.api_controls['box']
                    app.root.tk.call('ttk::combobox::Post',box._w)
                    capture_client('api-dropdown')
                    app.root.tk.call('ttk::combobox::Unpost',box._w)
                    app.open_page('send')
                    app.root.update()
                    button=next(w for w in descendants(app.workspace.frames['send']) if w.winfo_class()=='Button' and w.cget('text')=='翻译')
                    frames=[]
                    for index in range(36):
                        if index==3:button.event_generate('<Enter>')
                        if index==24:button.event_generate('<Leave>')
                        app.root.after(33,app.root.quit);app.root.mainloop()
                        x,y=button.winfo_rootx(),button.winfo_rooty()
                        frames.append(ImageGrab.grab(bbox=(x-6,y-6,x+button.winfo_width()+6,y+button.winfo_height()+6)))
                    frames[0].save(output/f'liquid-hover-{options.size}.gif',save_all=True,append_images=frames[1:],duration=45,loop=0)
                    nav=app.nav_buttons['api']['button']
                    nav.event_generate('<Enter>');capture_client('nav-hover');nav.event_generate('<Leave>')
                    app.open_page('recognition')
                    app.interactions.update(None)
                    capture_client('backdrop-blue')
                    backdrop.configure(bg='#A86B37');capture_client('backdrop-warm')
                    from PIL import Image, ImageDraw
                    from gui.region_editor import RegionEditor
                    fixture=Image.new('RGB',(1920,1080),'#243129')
                    draw=ImageDraw.Draw(fixture)
                    for y in range(0,1080,90):draw.line((0,y,1920,y),fill='#36483A',width=2)
                    for x in range(0,1920,120):draw.line((x,0,x,1080),fill='#36483A',width=2)
                    draw.text((270,180),'Isolated image / no game capture',fill='#E5E5D0')
                    editor=RegionEditor(app.page,app,fixture,'imported_screenshot')
                    editor.shade.attributes('-topmost',True)
                    editor.window.attributes('-topmost',True)
                    editor.shade.lift(app.root)
                    editor.window.lift(editor.shade)
                    app.root.update()
                    capture_client('region-editor')
                    editor.close()
                    backdrop.destroy()
                    if errors:raise RuntimeError('Tk callbacks failed: '+ '; '.join(errors))
            finally:
                try:
                    if app.root.winfo_exists():
                        app.root.destroy()
                except app.th.tk.TclError:
                    pass


if __name__ == "__main__":
    main()
