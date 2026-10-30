"""A local thumbnail of the exact screenshot used for saved calibration."""
from io import BytesIO
import json
from pathlib import Path

from PIL import Image, PngImagePlugin
from app.storage import store


def save_preview(root, image, calibration):
    try:
        thumbnail=image.convert('RGB')
        thumbnail.thumbnail((1600,900),Image.Resampling.LANCZOS)
        metadata=PngImagePlugin.PngInfo()
        metadata.add_text('calibration',json.dumps(calibration,ensure_ascii=False,sort_keys=True))
        buffer=BytesIO()
        thumbnail.save(buffer,format='PNG',pnginfo=metadata)
        return store.atomic_write_bytes(store.data_path(root,'ui/region-preview.png'),buffer.getvalue())
    except (OSError,ValueError):
        return False


def load_preview(root, calibration):
    if not calibration:return None
    path=Path(store.data_path(root,'ui/region-preview.png'))
    try:
        if path.stat().st_size>20*1024*1024:return None
        with Image.open(path) as image:
            if image.width>1600 or image.height>900:return None
            if json.loads(image.info.get('calibration','null'))!=calibration:return None
            return image.convert('RGB')
    except (OSError,ValueError,Image.DecompressionBombError):
        return None
