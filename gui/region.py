"""Read-only capture diagnostics; the panel is hidden before capturing."""
import ctypes
from pathlib import Path

from app.recognition.region import supported_screen, primary_game_screen

CAPTURE_DELAY_MS = 3000


def screen_size():
    return (ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1))


class CaptureError(RuntimeError):
    pass


def import_game_image(path):
    """Decode one bounded PNG/JPEG without retaining its path or changing it."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        with Path(path).open('rb') as stream:
            if stream.seek(0, 2) > 50 * 1024 * 1024:
                raise CaptureError('图片超过 50 MB，请选择完整原始 PNG/JPEG 截图。')
            stream.seek(0)
            with Image.open(stream) as opened:
                if opened.format not in ('PNG', 'JPEG'):
                    raise CaptureError('请选择 PNG 或 JPEG 完整原始截图。')
                w, h = opened.size
                if max(w, h) > 12000 or w*h > 40_000_000:
                    raise CaptureError('图片超过资源上限（4000 万像素、单边 12000），请选择游戏原图。')
                if getattr(opened, 'n_frames', 1) != 1:
                    raise CaptureError('不支持动画或多帧图片，请选择单张局内截图。')
                if w <= 31 or h <= 21:
                    raise CaptureError('图片太小，无法建立最小识别范围；请选择完整游戏原图。')
                orientation = opened.getexif().get(274, 1)
                image = ImageOps.exif_transpose(opened).convert('RGB')
                image.load()
                # Coordinates describe the normalized pixels, never metadata DPI.
                image.info.clear()
        return image, orientation != 1
    except CaptureError:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise CaptureError('图片无法解码，请重新选择完整原始 PNG/JPEG 截图。') from exc


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

