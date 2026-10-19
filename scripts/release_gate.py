"""Fail closed on unverified distribution inputs; private prereleases remain explicitly labelled."""
import hashlib
import json
from pathlib import Path
import re

if __package__:
    from .ocr_runtime import validate as validate_runtime, source_identity
else:
    from ocr_runtime import validate as validate_runtime, source_identity

GAME_FILES = ('app/translation/game_terms.json', 'app/recognition/quick_chat.json')


def _read(root, relative):
    try:
        return json.loads((root / relative).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Missing or invalid distribution evidence: ' + relative) from exc


def _evidence(root, entries, required_roles):
    if not isinstance(entries, list) or not entries:
        raise ValueError('Distribution approval needs hashed evidence files')
    roles = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            raise ValueError('Invalid distribution evidence entry')
        relative = Path(entry['path'])
        path = root / relative
        if (relative.is_absolute() or '..' in relative.parts or ':' in entry['path']
                or path.resolve() != path.absolute() or not path.is_file()):
            raise ValueError('Unsafe or missing distribution evidence file')
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry.get('sha256'):
            raise ValueError('Distribution evidence SHA-256 changed: ' + entry['path'])
        roles.add(entry.get('role'))
    if not required_roles <= roles:
        raise ValueError('Incomplete distribution evidence roles: ' + ', '.join(sorted(required_roles - roles)))


def validate_game_data(root, *, require_frozen=True):
    """New public source and stable binaries must use an evidenced data route."""
    root = Path(root)
    audit = _read(root, 'docs/releases/game-data-audit.json')
    route = audit.get('route')
    if route == 'local_generation':
        if audit.get('distribution_files') != []:
            raise ValueError('Local generation must explicitly exclude game catalogues from distribution')
        required = {'startup_without_data', 'same_install_snapshot', 'frozen_generation',
                    'upgrade_feedback', 'preserve_user_data'}
        if not require_frozen:
            required.remove('frozen_generation')
        checks = audit.get('checks', {})
        if not isinstance(checks, dict) or any(checks.get(name) != 'PASS' for name in required):
            raise ValueError('Local game-data generation has not passed its required checks')
        _evidence(root, audit.get('evidence'), {'validation'})
    elif route == 'redistribute':
        rows = audit.get('files', [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError('Invalid game-data audit')
        indexed = {row.get('path'): row for row in rows}
        if len(rows) != len(GAME_FILES) or set(indexed) != set(GAME_FILES):
            raise ValueError('Both derived game catalogues need distribution evidence')
        for name in GAME_FILES:
            row = indexed[name]
            if row.get('distribution_status') != 'verified':
                raise ValueError('Game-data distribution basis remains unconfirmed: ' + name)
            if not (root / name).is_file() or hashlib.sha256((root / name).read_bytes()).hexdigest() != row.get('sha256'):
                raise ValueError('Game catalogue changed after distribution review: ' + name)
            _evidence(root, row.get('evidence'), {'permission', 'source_build'})
    else:
        raise ValueError('Game-data distribution route is not ready')
    return route


def bundled_game_files(root):
    """Selection is separate from clearance; private developer originals remain untouched."""
    audit = _read(Path(root), 'docs/releases/game-data-audit.json')
    return GAME_FILES if audit.get('route') == 'redistribute' else ()


def validate(root, version):
    root = Path(root)
    public_beta = bool(re.fullmatch(r'\d+\.\d+\.\d+-beta\.\d+', version))
    if not public_beta and re.fullmatch(r'\d+\.\d+\.\d+-(?:test|rc|alpha|beta)(?:[.-][A-Za-z0-9]+)*', version):
        return dict(distribution='local_test_only', version=version)
    if not public_beta and not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Use a stable X.Y.Z or explicitly labelled test/rc/alpha/beta version')
    if (root / 'VERSION').read_text(encoding='utf-8').strip() != version:
        raise ValueError('Stable build version does not match source VERSION')
    manifest = _read(root, 'scripts/ocr-runtime-manifest.json')
    audit = _read(root, 'docs/releases/ocr-runtime-audit.json')
    if source_identity(audit) != source_identity(manifest):
        raise ValueError('OCR audit does not match the selected runtime source')
    rows = audit.get('files', [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Invalid OCR distribution audit')
    indexed = {row.get('path'): row for row in rows}
    if len(rows) != len(indexed) or set(indexed) != {row['path'] for row in manifest['files']}:
        raise ValueError('OCR audit does not cover the selected runtime closure')
    for expected in manifest['files']:
        row = indexed[expected['path']]
        if any(row.get(key) != expected[key] for key in ('bytes', 'sha256')):
            raise ValueError('OCR audit byte identity changed: ' + expected['path'])
        if row.get('distribution_status') != 'verified':
            raise ValueError('OCR distribution materials remain open: ' + expected['path'])
        _evidence(root, row.get('evidence'), {'license', 'source_build'})
    route = validate_game_data(root, require_frozen=not public_beta)
    return dict(distribution='materials_verified', version=version, game_data_route=route)


def validate_bundle(root, version, bundle):
    """The installer's actual payload must match reviewed inputs, not just its own file list."""
    approval = validate(root, version)
    if approval['distribution'] == 'local_test_only':
        return approval
    validate_game_data(root)
    root, bundle = Path(root), Path(bundle)
    validate_runtime(bundle / 'ocr', _read(root, 'scripts/ocr-runtime-manifest.json'))
    recorded = _read(bundle, 'DISTRIBUTION.json')
    if recorded != approval:
        raise ValueError('Installer payload does not match its material approval')
    for name in GAME_FILES:
        path = bundle / '_internal' / name
        if approval['game_data_route'] == 'local_generation':
            if path.exists():
                raise ValueError('Installer carries game data despite the local generation route')
        elif (not path.is_file()
              or hashlib.sha256(path.read_bytes()).hexdigest() != hashlib.sha256((root / name).read_bytes()).hexdigest()):
            raise ValueError('Installer game data differs from the reviewed catalogue')
    return approval
