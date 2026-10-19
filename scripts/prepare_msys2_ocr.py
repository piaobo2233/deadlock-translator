"""Stage a pinned MSYS2 OCR candidate without installing packages or replacing the current runtime.

Package hashes come from a saved official repository snapshot. This inventories
licenses and build metadata; it does not declare redistribution clearance.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import urllib.request

if __package__:
    from . import ocr_runtime
else:
    import ocr_runtime

REPOSITORY = 'https://repo.msys2.org/mingw/mingw64/'
SOURCE_REPOSITORY = 'https://repo.msys2.org/mingw/sources/'
SEED = 'mingw-w64-x86_64-tesseract-ocr'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def archive_bytes(path, sevenzip):
    # 7-Zip only decompresses to stdout. No archive-controlled path is extracted.
    return subprocess.check_output([str(sevenzip), 'x', '-so', str(path)],
                                   stderr=subprocess.DEVNULL)


def repository_packages(body):
    packages = {}
    with tarfile.open(fileobj=io.BytesIO(body), mode='r:') as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith('/desc'):
                continue
            fields, key = {}, None
            for line in archive.extractfile(member).read().decode('utf-8').splitlines():
                if line.startswith('%') and line.endswith('%'):
                    key = line.strip('%'); fields[key] = []
                elif line and key:
                    fields[key].append(line)
            if fields.get('NAME'):
                name = fields['NAME'][0]
                if name in packages:
                    raise ValueError('Duplicate package in repository snapshot')
                packages[name] = fields
    return packages


def select_packages(packages, seed=SEED):
    providers = {}
    for name, fields in sorted(packages.items()):
        for alias in fields.get('PROVIDES', []):
            providers.setdefault(re.split(r'[<>=]', alias)[0], []).append(name)
    selected, pending = {}, [seed]
    while pending:
        requested = pending.pop()
        name = re.split(r'[<>=]', requested)[0]
        if name not in packages:
            choices = providers.get(name, [])
            if len(choices) != 1:
                raise ValueError('Missing or ambiguous dependency provider: ' + requested)
            name = choices[0]
        if name in selected:
            continue
        selected[name] = packages[name]
        pending.extend(packages[name].get('DEPENDS', []))
    return selected


def create_lock(database, sevenzip):
    selected = select_packages(repository_packages(archive_bytes(database, sevenzip)))
    rows = []
    for name, fields in sorted(selected.items()):
        filename = fields['FILENAME'][0]
        if not re.fullmatch(r'[A-Za-z0-9+_.-]+\.pkg\.tar\.zst', filename):
            raise ValueError('Unsafe package filename')
        version = fields['VERSION'][0]
        base = fields.get('BASE', [name])[0]
        rows.append(dict(name=name, base=base, version=version, filename=filename,
                         bytes=int(fields['CSIZE'][0]), sha256=fields['SHA256SUM'][0],
                         url=REPOSITORY + filename, licenses=fields.get('LICENSE', []),
                         dependencies=fields.get('DEPENDS', []),
                         source_url=SOURCE_REPOSITORY + base + '-' + version.split(':')[-1] + '.src.tar.zst'))
    return dict(schema_version=1, seed=SEED, repository=REPOSITORY,
                repository_sha256=digest(database), clearance='unreviewed', packages=rows)


def download_package(row, directory):
    path = directory / row['filename']
    if path.exists():
        if path.stat().st_size != row['bytes'] or digest(path) != row['sha256']:
            raise ValueError('Cached package differs from lock: ' + row['name'])
        return path
    temporary = path.with_name(path.name + '.part')
    if temporary.exists():
        raise ValueError('Partial download exists; preserve or review it before retrying: ' + str(temporary))
    with urllib.request.urlopen(row['url'], timeout=45) as response, temporary.open('xb') as target:
        received = 0
        while block := response.read(1024 * 1024):
            received += len(block)
            if received > row['bytes']:
                raise ValueError('Download exceeds locked size: ' + row['name'])
            target.write(block)
    if received != row['bytes'] or digest(temporary) != row['sha256']:
        raise ValueError('Package download failed hash verification: ' + row['name'])
    temporary.rename(path)
    return path


def stage(lock, cache, output, sevenzip, model):
    if lock.get('schema_version') != 1 or not isinstance(lock.get('packages'), list):
        raise ValueError('Invalid package lock')
    seen = set()
    for row in lock['packages']:
        if (not re.fullmatch(r'[A-Za-z0-9+_-]+', row['name']) or row['name'] in seen
                or not re.fullmatch(r'[A-Za-z0-9+_.-]+\.pkg\.tar\.zst', row['filename'])
                or row['url'] != REPOSITORY + row['filename']
                or not re.fullmatch(r'[0-9a-f]{64}', row['sha256'])
                or not isinstance(row['bytes'], int) or row['bytes'] <= 0):
            raise ValueError('Invalid package lock entry')
        seen.add(row['name'])
    provenance = json.loads(ocr_runtime.MODEL_PROVENANCE.read_text(encoding='utf-8'))
    if digest(model) != provenance['files']['eng.traineddata']['sha256']:
        raise ValueError('English model differs from pinned provenance')
    if output.exists():
        raise ValueError('Candidate output exists; use a new directory')
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True)
    binaries = output / 'package-binaries'; binaries.mkdir()
    owners, inventories = {}, []
    for row in lock['packages']:
        # Lock files are inputs as well: reject edited URLs and paths before any network/file action.
        if (not re.fullmatch(r'[A-Za-z0-9+_.-]+\.pkg\.tar\.zst', row['filename'])
                or row['url'] != REPOSITORY + row['filename']
                or not re.fullmatch(r'[0-9a-f]{64}', row['sha256'])):
            raise ValueError('Invalid package lock entry')
        path = download_package(row, cache)
        evidence = output / 'evidence' / row['name']; evidence.mkdir(parents=True)
        metadata, licenses = {}, []
        with tarfile.open(fileobj=io.BytesIO(archive_bytes(path, sevenzip)), mode='r:') as archive:
            for member in archive:
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or '..' in relative.parts or '\\' in member.name or ':' in member.name:
                    raise ValueError('Unsafe package member')
                if not member.isfile():
                    continue
                parts = relative.parts
                binary = (len(parts) == 3 and parts[:2] == ('mingw64', 'bin')
                          and (parts[-1].lower().endswith('.dll') or parts[-1] == 'tesseract.exe'))
                license_file = parts[:3] == ('mingw64', 'share', 'licenses')
                meta = relative.as_posix() in ('.PKGINFO', '.BUILDINFO')
                if not (binary or license_file or meta):
                    continue
                body = archive.extractfile(member).read()
                sha = hashlib.sha256(body).hexdigest()
                if binary:
                    name = parts[-1]
                    if name.casefold() in owners and owners[name.casefold()]['sha256'] != sha:
                        raise ValueError('Conflicting DLL bytes: ' + name)
                    (binaries / name).write_bytes(body)
                    owners[name.casefold()] = dict(package=row['name'], version=row['version'], sha256=sha)
                else:
                    target = evidence / relative
                    target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(body)
                    metadata[member.name] = sha
                    if license_file:
                        licenses.append(dict(path=target.relative_to(output).as_posix(), sha256=sha))
        package_info = (evidence / '.PKGINFO').read_text(encoding='utf-8')
        fields = {}
        for line in package_info.splitlines():
            if ' = ' in line:
                key, value = line.split(' = ', 1); fields.setdefault(key, []).append(value)
        if fields.get('pkgname') != [row['name']] or fields.get('pkgver') != [row['version']]:
            raise ValueError('Package metadata does not match lock: ' + row['name'])
        inventories.append(dict(name=row['name'], version=row['version'], metadata=metadata, licenses=licenses))
        print(json.dumps(dict(package=row['name'], verified=True)), flush=True)
    runtime = output / 'runtime'; runtime.mkdir()
    files = []
    for path in ocr_runtime._import_closure(binaries):
        shutil.copy2(path, runtime / path.name)
        files.append(dict(path=path.name, bytes=path.stat().st_size, sha256=digest(path),
                          package=owners[path.name.casefold()]['package']))
    (runtime / 'tessdata').mkdir()
    shutil.copy2(model, runtime / 'tessdata/eng.traineddata')
    files.append(dict(path='tessdata/eng.traineddata', bytes=model.stat().st_size, sha256=digest(model),
                      package='project-pinned-tessdata-fast'))
    report = dict(schema_version=1, clearance='unreviewed',
                  lock_sha256=hashlib.sha256(json.dumps(lock, sort_keys=True).encode()).hexdigest(),
                  files=files, packages=inventories,
                  remaining='Review actual closure licenses, exact source/build correspondence and compatibility before adoption')
    (output / 'candidate.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('lock', 'stage'))
    parser.add_argument('--database', type=Path)
    parser.add_argument('--lock', type=Path, required=True)
    parser.add_argument('--sevenzip', type=Path, required=True)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'lock':
        if not args.database or args.lock.exists():
            parser.error('Supply a database and a new lock file path')
        result = create_lock(args.database, args.sevenzip)
        args.lock.parent.mkdir(parents=True, exist_ok=True)
        args.lock.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(dict(packages=len(result['packages']), bytes=sum(r['bytes'] for r in result['packages']))))
    else:
        if not args.cache or not args.output:
            parser.error('Staging needs --cache and --output')
        result = stage(json.loads(args.lock.read_text(encoding='utf-8')), args.cache, args.output,
                       args.sevenzip, ocr_runtime.MODEL_SOURCE)
        print(json.dumps(dict(runtime_files=len(result['files']), clearance=result['clearance'])))


if __name__ == '__main__':
    main()
