"""Collect exact source packages and missing license texts for a staged MSYS2 OCR closure.

Does not adopt a runtime or approve distribution. Verifies recipe hashes without
executing recipes. Corresponding source archives remain separate release assets.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import urllib.request

if __package__:
    from .prepare_msys2_ocr import archive_bytes, digest
else:
    from prepare_msys2_ocr import archive_bytes, digest


def metadata(path):
    result = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if ' = ' in line:
            key, value = line.split(' = ', 1)
            result.setdefault(key, []).append(value)
    return result


def members(body):
    with tarfile.open(fileobj=io.BytesIO(body), mode='r:*') as archive:
        for member in archive:
            if member.isfile():
                yield member.name, archive.extractfile(member).read()


def collect(candidate, lock_path, cache, output, sevenzip):
    if output.exists():
        raise ValueError('Choose a new source evidence output directory')
    report = json.loads((candidate / 'candidate.json').read_text(encoding='utf-8'))
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    index = {row['name']: row for row in lock['packages']}
    owners = {row['package'] for row in report['files'] if row['path'].lower().endswith(('.dll', '.exe'))}
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True)
    sources = {}
    packages = []
    for owner in sorted(owners):
        package = index[owner]
        url = package['source_url']
        name = url.rsplit('/', 1)[-1]
        if not url.startswith('https://repo.msys2.org/mingw/sources/') or Path(name).name != name:
            raise ValueError('Unexpected source package URL')
        path = cache / name
        if not path.exists():
            temporary = path.with_name(path.name + '.part')
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open('xb') as target:
                received = 0
                while block := response.read(1024 * 1024):
                    received += len(block)
                    if received > 256 * 1024 * 1024:
                        raise ValueError('Source archive exceeds collection bound')
                    target.write(block)
            temporary.rename(path)
        build = metadata(candidate / 'evidence' / owner / '.BUILDINFO')
        expected = build['pkgbuild_sha256sum'][0]
        if name not in sources:
            records = list(members(archive_bytes(path, sevenzip)))
            recipes = [(member, body) for member, body in records if Path(member).name == 'PKGBUILD']
            if len(recipes) != 1 or hashlib.sha256(recipes[0][1]).hexdigest() != expected:
                raise ValueError('Source recipe differs from binary BUILDINFO: ' + owner)
            # Preserve the complete archive as source asset; evidence records every input.
            rows = [dict(path=member, bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
                    for member, body in records]
            sources[name] = dict(filename=name, url=url, bytes=path.stat().st_size,
                                 sha256=digest(path), pkgbuild_sha256=expected, members=rows)
            # These packages omit installed license texts; take them from the exact sources.
            patterns = {
                'mingw-w64-x86_64-giflib': {'COPYING'},
                'mingw-w64-x86_64-libidn2': {'COPYING', 'COPYING.LESSERv3', 'COPYING.unicode', 'COPYINGv2'},
                'mingw-w64-x86_64-lz4': {'LICENSE'},
                'mingw-w64-x86_64-jbigkit': {'COPYING', 'jbig.c'},
            }
            if owner in patterns:
                target = output / 'supplemental' / owner
                found = set()
                for outer, compressed in records:
                    if not re.search(r'\.tar\.(?:gz|xz|bz2)$', outer):
                        continue
                    for member, body in members(compressed):
                        basename = Path(member).name
                        if basename not in patterns[owner]:
                            continue
                        relative = Path(member)
                        if relative.is_absolute() or '..' in relative.parts or ':' in member or '\\' in member:
                            raise ValueError('Unsafe supplemental license path')
                        destination = target / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(body)
                        found.add(basename)
                if not found:
                    raise ValueError('Missing supplemental license texts: ' + owner)
        elif sources[name]['pkgbuild_sha256'] != expected:
            raise ValueError('Shared source recipe hash differs: ' + owner)
        packages.append(dict(name=owner, version=package['version'], source=name,
                             binary_sha256=package['sha256'], binary_url=package['url'],
                             licenses=package['licenses'], pkgbuild_sha256=expected))
        print(json.dumps(dict(source=name, recipe_matches=True)), flush=True)
    result = dict(schema_version=1, clearance='unreviewed', sources=list(sources.values()), packages=packages,
                  package_lock_sha256=digest(lock_path))
    (output / 'source-evidence.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidate', 'lock', 'cache', 'output', 'sevenzip'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    result = collect(args.candidate, args.lock, args.cache, args.output, args.sevenzip)
    print(json.dumps(dict(source_packages=len(result['sources']),
                          bytes=sum(row['bytes'] for row in result['sources']), clearance='unreviewed')))


if __name__ == '__main__':
    main()
