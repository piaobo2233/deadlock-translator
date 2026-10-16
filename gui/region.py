"""Read-only capture diagnostics; the panel is hidden before capturing."""
import ctypes

from app.recognition.region import supported_screen, primary_game_screen

CAPTURE_DELAY_MS = 3000


def screen_size():
    return (ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1))


class CaptureError(RuntimeError):
    pass


def capture_game_frame():
    from app.main import game_in_foreground
    from PIL import ImageGrab
    if not game_in_foreground():
        raise CaptureError('未检测到游戏在前台；请重新点击，并在倒计时结束前切回游戏。')
    try:screen = primary_game_screen()
    except ValueError as exc:raise CaptureError(str(exc)) from exc
    supported_screen(screen)
    image = ImageGrab.grab(bbox=(0,0,*screen))
    if not game_in_foreground() or primary_game_screen() != screen or image.size != screen:
        raise CaptureError('截图期间窗口或分辨率发生变化，请重新截取游戏画面。')
    if all(high <= 1 for low,high in image.convert('RGB').getextrema()):
        raise CaptureError('截图为空，请检查游戏显示模式后重新截取；原区域未改变。')
    return image

