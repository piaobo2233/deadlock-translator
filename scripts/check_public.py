"""Offline baseline checks for the public source clone; no application imports."""
import argparse
import ast
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []

    def handle_starttag(self, tag, attrs):
        self.targets.extend(value for name, value in attrs
                            if name in {'href', 'src'} and value)


def check_syntax(root):
    files = [p for folder in ('app', 'gui', 'scripts', 'tests', 'verify', 'tools')
             for p in (root / folder).rglob('*.py')]
    for path in files:
        ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    print(f'PASS public syntax: {len(files)} Python files')


def check_links(root):
    root = root.resolve()
    documents = sorted(set(root.glob('*.md')) |
                       set((root / 'docs').rglob('*.md')) |
                       set((root / '.github').rglob('*.md')))
    checked = 0
    errors = []
    for document in documents:
        text = document.read_text(encoding='utf-8-sig')
        text = re.sub(r'(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$', '', text)
        parser = Links()
        parser.feed(text)
        targets = re.findall(r'!?\[[^\]\n]*\]\(([^)\n]+)\)', text)
        targets += re.findall(r'(?m)^\s*\[[^\]\n]+\]:\s*(\S+)', text)
        for target in targets + parser.targets:
            target = target.strip().split(' "', 1)[0].strip('<>')
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            path = unquote(url.path)
            local = (root / path.lstrip('/') if path.startswith('/')
                     else document.parent / path).resolve()
            if not local.is_relative_to(root) or not local.exists():
                errors.append(f'{document.relative_to(root)}: {target}')
            checked += 1
    if errors:
        raise ValueError('Invalid local document targets:\n' + '\n'.join(errors))
    print(f'PASS local document targets: {checked} links in {len(documents)} Markdown files (anchors/external URLs excluded)')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--syntax', action='store_true')
    parser.add_argument('--links', action='store_true')
    args = parser.parse_args()
    if not args.syntax and not args.links:
        parser.error('Choose --syntax and/or --links')
    if args.syntax:
        check_syntax(ROOT)
    if args.links:
        check_links(ROOT)


if __name__ == '__main__':
    main()
