"""Extracted desktop responsibilities."""
from app import brand
from app.platform.single_instance import acquire_instance, instance_running
import ctypes
import os

def _enable_dpi_awareness() -> bool:
    """必须在创建任何窗口之前调用。

    屏幕 2560x1440 + 125% 缩放。不声明 DPI aware 的话：
      1. GetSystemMetrics 返回逻辑尺寸 2048x1152，定位全部错位
      2. 窗口被 Windows 按 125% 位图拉伸 —— 文字发糊，
         那「字体和游戏一致」就白说了
    """
    try:
        return bool(ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except Exception:
        return False


DPI_AWARE = False
_INITIALIZED = False

def initialize_desktop():
    """Apply Windows identity and DPI before creating any window."""
    global DPI_AWARE, _INITIALIZED
    if not _INITIALIZED:
        brand.apply_process_identity()
        DPI_AWARE = _enable_dpi_awareness()
        _INITIALIZED = True
    return DPI_AWARE


GAME_EXE = "deadlock.exe"


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _foreground_exe() -> str:
    """返回当前前台窗口对应的可执行文件名（小写）。取不到就返回空串。"""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def game_in_foreground() -> bool:
    """前台窗口是不是《异锁》。

    [!] 为什么必须有这个判断：屏幕 OCR 是"看屏幕上有什么"，不是"看游戏里有什么"。
        实测：博士切出去看网页时，抓到的全是网页上的白底文字块（'LLL' / '2.' /
        'were Fat'），既白跑 CPU，又可能被误判成聊天送去翻译 —— **那就是纯烧钱**。
        所以只在前台是游戏时才抓：一举两得，既省钱又不误判。
    """
    return _foreground_exe() == GAME_EXE


def single_instance_guard():
    """Compatibility facade; guard failure is (None, False), never allowed."""
    return acquire_instance()


def translator_is_running() -> bool:
    """有没有另一个翻译器进程在跑 —— 用**它自己那个命名互斥体**判断。

    [!] 为什么复用互斥体、而不是去枚举进程比对命令行：
        同名互斥体只有翻译器会创建，不会误判；枚举命令行容易被别的
        Python 脚本误伤（本机就有好几个跑 deadlock 字样的脚本）。

    [!] 自己创建成功时必须**立刻关掉句柄** ——
        否则"数据中心"一开就会把互斥体占住，
        真正的翻译器启动时反而以为自己重复了。

    [!] 2026-10-07 开源整改：原来是 `if not exists: CloseHandle(handle)` ——
        **只在「自己创建成功」时才关**。这是错的，后果很具体：
        命名互斥体只要还有**任何一个**句柄，对象就一直存在。
        所以「翻译器正在跑」时来探测一次，就会在**探测方**留下一个永不释放的句柄——
        用户随后关掉翻译器，那个句柄还在 → 再启动翻译器时 CreateMutexW
        仍然报 ERROR_ALREADY_EXISTS → **"已有实例在运行"、起不来**，
        必须先把控制面板也关掉才行。
        （控制面板刷新状态时会反复调这个函数，真机上很容易撞到。）
        修法：**任何情况下都立刻关掉我们自己那个句柄** ——
        关自己的句柄不影响别人持有的，语义完全正确。
    """
    return instance_running()


def process_running(exe_name: str) -> bool:
    """目标进程在不在（不区分大小写）。不依赖 psutil —— 它不在 requirements 里。"""
    target = (exe_name or "").strip().lower()
    if not target:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),   # ULONG_PTR
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        # [!] 结构体布局错了会读出垃圾名字（还不报错）。先验尺寸再干活。
        if ctypes.sizeof(PROCESSENTRY32) != 304:
            return False

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == -1:
            return False
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        found = False
        try:
            ok = k.Process32First(snap, ctypes.byref(entry))
            while ok:
                if entry.szExeFile.decode("mbcs", "replace").lower() == target:
                    found = True
                    break
                ok = k.Process32Next(snap, ctypes.byref(entry))
        finally:
            k.CloseHandle(snap)
        return found
    except Exception:
        return False


def has_console() -> bool:
    """这个进程有没有控制台窗口。

    [!] 用它来判断「用户能不能自己看到/关掉这个程序」。
        无窗口启动（pythonw / exe 双击）时没有控制台 —— 这时如果托盘也起不来，
        用户就**完全看不见也关不掉**这个进程（只能去任务管理器里杀）。
        那是唯一必须弹窗的情况。
    判断不了就返回 True（当作有控制台），宁可少弹一次窗，也别乱弹。
    """
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return True


def _warn_window(title: str, message: str) -> None:
    """弹一个必须被看到的提示框。**只用在"没有别的办法通知用户"时。**"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x30)   # 0x30 = 警告图标
    except Exception:
        pass


def _notify_user(message: str, title: str = brand.DIALOG_TITLE,
                 allow_popup: bool = True) -> None:
    """需要用户**看得见**的一次性提示（OCR 起不来 / 到费用上限……）。

    [!] 2026-10-07 开源整改。这条路径原来只 print 几行就 return，
        采集线程就此永久结束（不再重试）。用 --console 打包时用户至少能在
        黑窗口里看到；一旦改成 --windowed（无控制台），那几行只进 run.log，
        用户屏幕上**一个字都没有** —— 比原来更糟。
        所以：有控制台就照旧（不重复打扰），没控制台才补一条弹窗。
        --headless / --simulate 这类自动化路径由 allow_popup 关掉，不能弹窗卡住。
    """
    print(message)
    if not allow_popup or has_console():
        return
    try:
        import ctypes
        # [!] 必须用带超时的 MessageBoxTimeoutW，不能图省事用 MessageBoxW ——
        #     它是**模态**的，会一直等到有人点「确定」。这条路径跑在**采集线程**里，
        #     游戏里没人去点那个框，那个线程就永久挂在对话框上。
        #     项目里「已有实例在运行」那条提示早就踩过这个坑（见 main 里对应的注释），
        #     这里沿用同一写法：60 秒自动关；老系统没这个 API 就直接不弹，
        #     绝不退回阻塞版本（打印已经进 run.log 了）。
        ctypes.windll.user32.MessageBoxTimeoutW(
            0, message, title, 0x30, 0, 60000)
    except Exception:
        pass


def _open_path(path: str) -> bool:
    """用系统默认程序打开一个文件/目录（托盘菜单用）。失败只打一行提示。"""
    try:
        if not os.path.exists(path):
            print(f"[warn] 文件还不存在：{path}")
            return False
        os.startfile(path)
        return True
    except Exception as exc:
        print(f"[warn] 打不开 {path}：{exc}")
        return False
