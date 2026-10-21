"""Optional public stable/Beta releases; full verified installers, no Tk or API key."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

REPOSITORY = 'https://github.com/piaobo2233/deadlock-translator'
RELEASES_URL = REPOSITORY + '/releases'
LATEST_URL = 'https://api.github.com/repos/piaobo2233/deadlock-translator/releases/latest'
LIST_URL = 'https://api.github.com/repos/piaobo2233/deadlock-translator/releases?per_page=100'
CHECK_INTERVAL = 24 * 60 * 60
MAX_SETUP_BYTES = 512 * 1024 * 1024
DOWNLOAD_HOSTS = {'github.com', 'api.github.com', 'release-assets.githubusercontent.com',
                  'objects.githubusercontent.com'}


class UpdateError(Exception):
    pass


def version_key(value):
    match = re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?', str(value))
    if not match:
        raise UpdateError('版本号无法识别，请打开官方下载页查看')
    identifiers = (match[4] or '').split('.') if match[4] else []
    if any(not part or (part.isdigit() and len(part) > 1 and part.startswith('0')) for part in identifiers):
        raise UpdateError('版本号无法识别，请打开官方下载页查看')
    prerelease = tuple((0, int(part)) if part.isdigit() else (1, part) for part in identifiers)
    return (*map(int, match.group(1, 2, 3)), not bool(match[4]), prerelease)


def beta_channel(version):
    version_key(version)
    return bool(re.fullmatch(r'v?\d+\.\d+\.\d+-beta\.(?:0|[1-9]\d*)', version))


def trusted_url(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.hostname in DOWNLOAD_HOSTS
                and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except (TypeError, ValueError):
        return False


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not trusted_url(newurl):
            raise UpdateError('下载跳转不是受信任的 GitHub 地址')
        return super().redirect_request(request, fp, code, message, headers, newurl)


def open_url(url):
    if not trusted_url(url):
        raise UpdateError('更新地址无效')
    request = Request(url, headers={'User-Agent': 'LockLingo-updater',
                                   'Accept': 'application/vnd.github+json' if url.startswith('https://api.github.com/') else '*/*'})
    return build_opener(GitHubRedirects()).open(request, timeout=20)


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    published: str
    page: str
    setup_url: str = ''
    setup_size: int = 0
    sha256: str = ''
    checksum_url: str = ''


def parse_release(raw, *, allow_beta=False):
    if not isinstance(raw, dict) or raw.get('draft') is not False or type(raw.get('prerelease')) is not bool:
        raise UpdateError('该版本不是公开发行版本')
    tag = raw.get('tag_name', '')
    stable = version_key(tag)[3]
    if not stable and not (allow_beta and beta_channel(tag) and raw['prerelease']):
        raise UpdateError('该版本不属于当前公开更新通道')
    if stable and raw['prerelease']:
        raise UpdateError('稳定版本标记异常')
    version = tag.removeprefix('v')
    page = REPOSITORY + '/releases/tag/' + tag
    if raw.get('html_url') != page:
        raise UpdateError('版本信息不属于本项目')
    assets = raw.get('assets', [])
    if not isinstance(assets, list):
        raise UpdateError('发行附件信息异常')
    name = f'LockLingo-{version}-setup.exe'
    prefix = REPOSITORY + '/releases/download/' + tag + '/'
    selected = {}
    for asset in assets:
        if not isinstance(asset, dict) or asset.get('name') not in {name, name + '.sha256'}:
            continue
        if asset['name'] in selected or asset.get('browser_download_url') != prefix + asset['name']:
            raise UpdateError('发行附件地址异常')
        selected[asset['name']] = asset
    setup = selected.get(name, {})
    size = setup.get('size', 0)
    if setup and (type(size) is not int or not 0 < size <= MAX_SETUP_BYTES):
        raise UpdateError('安装包大小异常')
    digest = setup.get('digest') or ''
    sha = digest.removeprefix('sha256:') if isinstance(digest, str) else ''
    if sha and not re.fullmatch('[0-9a-fA-F]{64}', sha):
        raise UpdateError('发行附件校验信息异常')
    checksum = selected.get(name + '.sha256', {}).get('browser_download_url', '')
    return Release(version, str(raw.get('body') or '本版本未提供更新概述。')[:20000],
                   str(raw.get('published_at') or '')[:32], page,
                   setup.get('browser_download_url', ''), size, sha.lower(), checksum)


def fetch_latest(current_version='1.0.0'):
    try:
        beta = beta_channel(current_version)
        candidates = []
        # Exact public beta.N tags exclude local/fasttrack suffixes. Network
        # work is capped at five pages and one MiB per response.
        for page in range(1, 6):
            url = LIST_URL + f'&page={page}' if beta else LATEST_URL
            with open_url(url) as response:
                body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise UpdateError('版本信息过大')
            raw = json.loads(body)
            if not beta:
                return parse_release(raw)
            if not isinstance(raw, list):
                raise UpdateError('发行列表信息异常')
            for item in raw:
                try:
                    release = parse_release(item, allow_beta=True)
                except UpdateError:
                    continue
                if version_key(release.version) > version_key(current_version):
                    candidates.append(release)
            if len(raw) < 100:
                break
        return max(candidates, key=lambda release: version_key(release.version), default=None)
    except HTTPError as exc:
        if exc.code == 404:
            return None  # The project has not published a stable release yet.
        if exc.code in {403, 429}:
            raise UpdateError('GitHub 暂时限制访问，请稍后重试或打开官方下载页') from exc
        raise UpdateError('GitHub 版本查询失败，请重试或打开官方下载页') from exc
    except UpdateError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise UpdateError('无法连接 GitHub 或读取版本信息，请检查网络后重试') from exc


def release_record(release):
    if not release:
        return None
    name = f'LockLingo-{release.version}-setup.exe'
    assets = []
    if release.setup_url:
        assets.append(dict(name=name, browser_download_url=release.setup_url, size=release.setup_size,
                           digest='sha256:' + release.sha256 if release.sha256 else None))
    if release.checksum_url:
        assets.append(dict(name=name + '.sha256', browser_download_url=release.checksum_url))
    return dict(tag_name=release.page.rsplit('/', 1)[1], body=release.notes, published_at=release.published,
                html_url=release.page, draft=False, prerelease=not version_key(release.version)[3], assets=assets)


def fetch_cached(root, *, force=False):
    from app.storage import store
    from app.storage.locking import config_lock
    path = Path(root) / '.temp/updates/check.json'
    try:
        current = (Path(root) / 'VERSION').read_text(encoding='utf-8-sig').strip()
    except FileNotFoundError:
        current = '1.0.0'
    beta = beta_channel(current)
    if path.parent.resolve() != path.parent.absolute():
        raise UpdateError('更新缓存目录异常')
    try:
        with config_lock(path, timeout=.1):
            cached = store.read_json(path, {})
            cached = cached if isinstance(cached, dict) else {}
            checked = cached.get('checked_at', 0)
            if (not force and cached.get('current_version') == current and type(checked) in (int, float)
                    and 0 <= time.time() - checked < CHECK_INTERVAL):
                if cached.get('error'):
                    raise UpdateError(str(cached['error']))
                return parse_release(cached['release'], allow_beta=beta) if cached.get('release') else None
            try:
                release = fetch_latest(current)
            except UpdateError as exc:
                store.atomic_write_json(path, dict(checked_at=time.time(), current_version=current, error=str(exc)))
                raise
            if not store.atomic_write_json(path, dict(checked_at=time.time(), current_version=current, release=release_record(release))):
                raise UpdateError('无法保存更新检查结果，请检查目录权限')
            return release
    except TimeoutError as exc:
        raise UpdateError('另一个窗口正在检查更新，请稍后重试') from exc


def start_background(root, stop):
    """Keep checking while the panel is closed to the tray; never show a popup."""
    import threading
    def worker():
        if stop.wait(5):
            return
        while not stop.is_set():
            try:
                fetch_cached(root)
            except Exception:
                pass  # Manual checks expose failures; the daily task stays quiet.
            stop.wait(60)
    threading.Thread(target=worker, name='release-check', daemon=True).start()


def checksum_for(release):
    if release.sha256:
        return release.sha256
    if not release.checksum_url:
        raise UpdateError('该版本没有安装包校验信息，请打开官方下载页')
    with open_url(release.checksum_url) as response:
        body = response.read(4097)
    if len(body) > 4096:
        raise UpdateError('校验文件异常')
    name = f'LockLingo-{release.version}-setup.exe'
    match = re.fullmatch(r'([0-9a-fA-F]{64})\s+\*?' + re.escape(name) + r'\s*', body.decode('ascii'))
    if not match:
        raise UpdateError('安装包校验文件不匹配')
    return match[1].lower()


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def download(release, directory, progress=lambda done, total: None):
    if not release.setup_url:
        raise UpdateError('该版本没有安装包，请打开官方下载页')
    directory = Path(directory)
    # Each attempt owns a new directory, so retries never replace an older artifact.
    directory.mkdir(parents=True, exist_ok=False)
    partial = directory / 'setup.part'
    target = directory / f'LockLingo-{release.version}-setup.exe'
    try:
        expected = checksum_for(release)
        received = 0
        deadline = time.monotonic() + 15 * 60
        with open_url(release.setup_url) as response, partial.open('xb') as stream:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > release.setup_size or time.monotonic() > deadline:
                    raise UpdateError('下载大小异常或已超时，请重试')
                stream.write(chunk)
                progress(received, release.setup_size)
            stream.flush()
            os.fsync(stream.fileno())
        if received != release.setup_size or file_digest(partial) != expected:
            raise UpdateError('安装包不完整或校验失败，旧版本未更改，请重试')
        partial.replace(target)
        return target, expected
    except Exception as exc:
        partial.unlink(missing_ok=True)
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError('下载失败，请检查网络、磁盘空间和目录权限后重试') from exc
