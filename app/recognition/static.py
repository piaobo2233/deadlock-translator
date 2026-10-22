"""Extracted static responsibilities."""
from app.storage import store
import app.recognition.live as live_state
import app.runtime.input_scope as input_scope

def _load_static_ui(trusted_only: bool = False):
    """读「已确认是游戏 UI 的位置」名单 **以及它是按哪个画面尺寸记的**。

    [!] `trusted_only=True` 时**只认人工确认过的名单**：文件里必须有
        `confirmed_by: "human"`，否则当作没有名单（返回空集）。
        CLI（`--static-ui`）用默认 `False`，这样它仍能看到并审查旧名单。

    [!] 判定逻辑**不在这一层** —— 这一层只负责别失忆。
        Consensus 用「同位置同文本持续 25 秒」判定静态 UI（实测调出来的），
        判定结果是 `(round(cx/25), round(cy/25))` 这个位置 key。

    [!] 2026-10-11：格子是**绝对像素 / 25**，所以名单**与分辨率绑死** —— 实测同一个
        气泡在 1440p 是 `(39,11)`、在 1080p 是 `(29,8)`。换分辨率/窗口化后每个格子
        都指错地方：既在错误位置抑制聊天，又不再抑制原本要抑制的 HUD。
        所以把画面尺寸一起记下来，**交给知道当前尺寸的调用方去核对**
        （核对点在 `iter_ocr_source`，那里才有 `sw, sh`）。

    返回 `(positions, screen)`。`screen` 为 `None` = 没有尺寸信息的旧文件
    （照旧加载，但调用方应提示一次）。读不出来返回 `(set(), None)` ——
    绝不让一个坏文件挡住程序启动。
    """
    raw = store.read_json(input_scope.STATIC_UI_PATH, None)
    out = set()
    if not isinstance(raw, dict):
        return out, None
    for item in raw.get("positions") or []:
        try:
            out.add((int(item[0]), int(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
    screen = None
    try:
        if raw.get("screen"):
            screen = (int(raw["screen"][0]), int(raw["screen"][1]))
    except (TypeError, ValueError, IndexError, KeyError):
        screen = None
    if trusted_only and out and raw.get("confirmed_by") != "human":
        # [!] 2026-10-11：**没经过人工确认的名单一律不套用**。
        #     这份名单是自动学习攒出来的，实测会吃真聊天（博士那一局 4/4：
        #     `that has gotta be the worst pocket i have ever seen`、`lmao`、`> go`
        #     的格子全在名单里 → 静默丢弃）。而且旧实例还会每 60 秒把内存快照写回
        #     文件 —— 清空之后它自己长回 38 格。加了这道标记，名单就不可能再
        #     "自己长回来"并偷偷抑制聊天。
        print(f"[warn] 静态位置名单里有 {len(out)} 个格子，但**没有人工确认标记** —— "
              f"本次不套用（自动学出来的名单被实测证明会吃掉真聊天）。"
              f" 想恢复某个格子请显式确认：`--static-ui` 查看、`--promote-static 列,行` 提升。")
        return set(), screen
    return out, screen


def _save_static_ui(positions, screen=None, *, human=False) -> bool:
    """把**确认**名单写盘（走 store 的原子写）。空名单也写 —— 那是「确认没有」的事实。

    `screen` 是这份名单所属的画面尺寸 `(w, h)`。带上它，换分辨率时才不会拿错位的
    格子去抑制聊天（见 `_load_static_ui` 的说明）。

    `human=True` 表示这是**人工确认**的结果，会写上 `confirmed_by: "human"` ——
    只有带这个标记的名单才会被套用（见 `_load_static_ui`）。
    """
    try:
        rows = sorted([int(a), int(b)] for a, b in positions)
    except Exception:
        return False
    payload = {"version": 3, "positions": rows}
    try:
        if screen:
            payload["screen"] = [int(screen[0]), int(screen[1])]
    except (TypeError, ValueError, IndexError):
        pass
    if human:
        payload["confirmed_by"] = "human"
    return store.atomic_write_json(input_scope.STATIC_UI_PATH, payload)


def _load_static_candidates() -> dict:
    """读自动学到的静态位置候选（**不参与抑制**）。"""
    raw = store.read_json(input_scope.STATIC_UI_CANDIDATES_PATH, None)
    out = {}
    if not isinstance(raw, dict):
        return out
    for item in raw.get("candidates") or []:
        try:
            position = (int(item["position"][0]), int(item["position"][1]))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        out[position] = {"text": str(item.get("text", ""))[:64]}
    return out


def _save_static_candidates(candidates) -> bool:
    """把本会话自动学到的候选写到**单独文件**。

    [!] 为什么必须单独一个文件：确认名单参与抑制且跨会话；候选没经过人工确认。
        原来两者是同一个 set，自动学习于是直接写进了用户数据 —— 实测
        data/static_ui.json 的 38 格里有 11 格压在真气泡出现过的地方，
        等于把这些位置的聊天静默封掉。分离之后，自动候选仅供审查，不参与抑制。
    """
    try:
        rows = [{"position": [int(a), int(b)], "text": str(info.get("text", ""))[:64]}
                for (a, b), info in sorted(candidates.items())]
    except Exception:
        return False
    return store.atomic_write_json(input_scope.STATIC_UI_CANDIDATES_PATH,
                                   {"version": 1, "candidates": rows})


def _live_static_candidates() -> dict:
    """取当前运行实例本会话学到的候选；没有实例就返回空。"""
    con = live_state._LIVE.get("con")
    if con is None or not callable(getattr(con, "learned_static", None)):
        return {}
    try:
        return con.learned_static()
    except Exception:
        return {}


def static_ui_cli(action="list", cell=""):
    """静态位置名单的人工审查入口。

    确认名单（static_ui.json）参与抑制、跨会话；
    候选（static_ui_candidates.json）由程序自动学到、**不参与抑制**，
    必须由人确认后才能提升 —— 这样自动学习就不可能单方面封掉聊天位置。
    """
    confirmed, recorded_screen = _load_static_ui()
    candidates = _load_static_candidates()
    if action == "list":
        screen_note = recorded_screen if recorded_screen else "（旧文件，未记录尺寸）"
        print(f"确认名单（参与抑制、跨会话）：{len(confirmed)} 个位置｜按画面 {screen_note} 记")
        for position in sorted(confirmed):
            print(f"   {position}")
        print(f"自动候选（**不参与抑制**，需人工确认）：{len(candidates)} 个位置")
        for position, info in sorted(candidates.items()):
            mark = "（已在确认名单）" if position in confirmed else ""
            print(f"   {position}  文本={info.get('text', '')!r} {mark}")
        print()
        print("  确认要抑制     → --promote-static 列,行")
        print("  从确认名单移除 → --forget-static 列,行")
        return 0
    try:
        col, row = (int(value) for value in cell.replace(" ", "").split(","))
    except Exception:
        print("[error] 位置格式应为 列,行（例如 15,7）")
        return 2
    position = (col, row)
    if action == "promote":
        if position not in candidates:
            print(f"[warn] {position} 不在自动候选里 —— 仍按你的要求提升，"
                  "但请先确认它不是聊天位置")
        confirmed.add(position)
    else:
        confirmed.discard(position)
    # 保留原来的尺寸标注：CLI 只改成员，不改这份名单属于哪个画面尺寸。
    # `human=True` 会写上 `confirmed_by: "human"` —— **只有带这个标记的名单才被套用**。
    if not _save_static_ui(confirmed, recorded_screen, human=True):
        print("[error] 写盘失败，名单未改变")
        return 1
    print(f"[ok] 确认名单现有 {len(confirmed)} 个位置（{action} {position}）")
    return 0
