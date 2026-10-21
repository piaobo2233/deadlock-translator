"""Check a personal ROI against independent human boxes, never detector output.

Reads an original image, a version-1 calibration record and private annotations.
Prints geometry/counts only; never saves pixels, text, paths or app configuration.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.recognition.region import validate_calibration
from gui.region import import_game_image


def coverage_report(record, truth):
    record = validate_calibration(record)
    if not record or not isinstance(truth, dict) or truth.get('screen') != record['screen']:
        raise ValueError('标注与区域必须来自同尺寸原图')
    boxes = truth.get('chat_boxes')
    if not isinstance(boxes, list):
        raise ValueError('标注必须提供 chat_boxes 列表；无聊天时使用空列表')
    margin = truth.get('margin', 0)
    if type(margin) is not int or margin < 0:
        raise ValueError('margin 须为非负原图整数像素')
    x0,y0,x1,y1 = record['rect']; w,h = record['screen']
    missed = []
    for index, box in enumerate(boxes):
        if (not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box)
                or not 0 <= box[0] < box[2] <= w or not 0 <= box[1] < box[3] <= h):
            raise ValueError('chat_boxes 须为原图坐标 [x0,y0,x1,y1]，右下不包含')
        if not (x0 <= box[0]-margin and y0 <= box[1]-margin and
                x1 >= box[2]+margin and y1 >= box[3]+margin):
            missed.append(index)
    return dict(screen=record['screen'], rect=record['rect'], annotated=len(boxes),
                missed_indices=missed, margin=margin,
                coverage='unverified_no_chat' if not boxes else 'missed' if missed else 'visible_boxes_covered',
                roi_screen_fraction=round((x1-x0)*(y1-y0)/(w*h),4),
                future_chat='unverified', real_game_performance='unverified')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--calibration', type=Path, required=True, help='单份个人区域 JSON（非整个 config.json）')
    parser.add_argument('--truth', type=Path, required=True)
    args = parser.parse_args()
    try:
        image, rotated = import_game_image(args.image)
        record = validate_calibration(json.loads(args.calibration.read_text(encoding='utf-8-sig')))
        if rotated or not record or list(image.size) != record['screen']:
            raise ValueError('需要与校准尺寸相同、无旋转标记的完整原始图片')
        report = coverage_report(record,json.loads(args.truth.read_text(encoding='utf-8-sig')))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        # Exceptions from IO may contain personal paths, so only report class.
        print('验证未完成：'+type(exc).__name__, file=sys.stderr)
        return 2
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 1 if report['missed_indices'] else 0


if __name__ == '__main__':
    sys.exit(main())
