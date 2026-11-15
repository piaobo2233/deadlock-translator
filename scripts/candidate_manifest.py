"""Record candidate identity and check downloaded release attachments against it."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OCR_INPUTS = ('scripts/ocr-runtime-manifest.json', 'docs/releases/ocr-msys2-packages.lock.json')


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def assets(directory, version):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Candidate must have a stable X.Y.Z version')
    records = {}
    for suffix in ('setup.exe', 'windows-x64.zip'):
        path = directory / f'LockLingo-{version}-{suffix}'
        digest = sha256(path)
        checksum = path.with_name(path.name + '.sha256').read_text(encoding='ascii').split()
        if checksum != [digest, path.name]:
            raise ValueError(f'Checksum sidecar does not match {path.name}')
        records[path.name] = {'bytes': path.stat().st_size, 'sha256': digest}
    return records


def record(directory, commit):
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if head != commit:
        raise ValueError(f'Expected source {commit}, checked out {head}')
    status = subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True)
    if status:
        raise ValueError('Candidate requires a clean source checkout')
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    manifest = dict(version=version, source_commit=head, python=sys.version.split()[0],
                    ocr_inputs={path: sha256(ROOT / path) for path in OCR_INPUTS},
                    assets=assets(directory, version))
    with (directory / 'candidate.json').open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, indent=2)
        stream.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('record')
    create.add_argument('directory', type=Path)
    create.add_argument('--commit', required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('manifest', type=Path)
    verify.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'record':
            record(args.directory, args.commit)
        else:
            manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
            if assets(args.directory, manifest['version']) != manifest['assets']:
                raise ValueError('Installer or ZIP differs from the recorded candidate')
            print(f"PASS release attachments match candidate {manifest['source_commit']}")
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
