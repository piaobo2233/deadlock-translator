"""Export names and provenance from the user's installed localization files."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.game_data.localization import tokens, extract
from app.storage import store

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = extract(args.directory)
    if not store.atomic_write_json(args.output, result):
        raise SystemExit('Could not save catalogue')
    print('Official terms:', len(result['terms']), '; source files:', len(result['sources']), '; rejected:', result['rejected'])
