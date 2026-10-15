"""Render every desktop page with disposable demo data, never real user files."""

from pathlib import Path
import argparse
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from PIL import ImageGrab
    import app.main as dt
    from app.storage import store
    from gui import app as app_mod, pages

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cost-cases", action="store_true", help="Render complete/partial/empty/broken consumption states")
    parser.add_argument("--size", default="820x650")
    parser.add_argument("--scaling", type=float, help="Override Tk point scaling for layout inspection")
    options = parser.parse_args()

    output = ROOT / ".temp" / "ui-review"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deadlock-ui-") as folder:
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
        store.save_collection(store.data_path(folder, 'pending.json'), {
            'can you help me':dict(original='can you help me', translation='你能来帮我吗？', source='api', status='pending'),
            'awesome duel':dict(original='awesome duel', translation='精彩的对决', source='api', status='pending'),
            'push together':dict(original='push together', translation='一起推进', source='api', status='accepted'),
        })
        today = time.strftime("%Y-%m-%d")
        bucket = {"api_ok": 34, "api_fail": 2, "api_calls": 36,
                  "glossary_hits": 120, "memory_hits": 15, "cache_hits": 8,
                  "prompt_tokens": 14000, "completion_tokens": 2100}
        days = {}
        for offset in range(7):
            stamp = time.strftime("%Y-%m-%d", time.localtime(time.time() - offset * 86400))
            days[stamp] = dict(bucket, prompt_tokens=14000 + offset * 5000)
        store.atomic_write_json(store.data_path(folder, "stats.json"),
                                {"days": days, "sessions": [bucket]})
        errors = []
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}), \
                patch.object(dt, "CONFIG_PATH", config), \
                patch.object(pages, "status_of", return_value=("等待游戏", "截图使用演示数据，未连接游戏或 API", pages.COLORS["muted"])):
            app = app_mod.App(folder, size=options.size)
            app.root.geometry(options.size + "+60+60")
            if options.scaling:
                app.root.tk.call("tk", "scaling", options.scaling)
            app.root.report_callback_exception = lambda *args: errors.append(str(args))
            try:
                cases = ("complete", "partial", "empty", "broken") if options.cost_cases else ('home','ingame','cost','dictionary','dictionary-review','dictionary-processed','settings')
                for case in cases:
                    key = "cost" if options.cost_cases else 'dictionary' if case.startswith('dictionary') else case
                    if options.cost_cases:
                        from app.storage.usage import empty_bucket
                        item = dict(bucket, api_tracked_tasks=36, api_attempts=40, api_usage_missing=0)
                        if case == "partial":
                            item["api_usage_missing"] = 4
                        if case == "empty":
                            item = empty_bucket()
                        doc = {"days": {today: item}, "sessions": [item]}
                        stats_path = store.data_path(folder, "stats.json")
                        if case == "broken":
                            Path(stats_path).write_bytes(b"{broken demo ledger")
                        else:
                            store.atomic_write_json(stats_path, doc)
                        app.open_page("home")
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
            finally:
                app.root.destroy()


if __name__ == "__main__":
    main()
