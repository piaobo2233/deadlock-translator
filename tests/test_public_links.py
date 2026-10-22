"""Project links remain checked while verbatim third-party notices keep upstream references."""
from pathlib import Path
import tempfile
import unittest

from scripts.check_public import check_links


class PublicLinkTests(unittest.TestCase):
    def test_templates_resolve_links_at_their_exported_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / 'docs/releases/ARCHITECTURE-public.md'
            template.parent.mkdir(parents=True)
            template.write_text('[development](DEVELOPMENT.md)', encoding='utf-8')
            (root / 'docs/DEVELOPMENT.md').write_text('Development', encoding='utf-8')
            check_links(root)
            (root / 'docs/DEVELOPMENT.md').unlink()
            # A same-named file beside the template cannot hide a missing export target.
            (root / 'docs/releases/DEVELOPMENT.md').write_text('Wrong location', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Invalid local document targets'):
                check_links(root)

    def test_upstream_readme_links_do_not_require_vendoring_upstream_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notice = root / 'docs/licenses/upstream/README.md'
            notice.parent.mkdir(parents=True)
            notice.write_text('[source](src/component.c)', encoding='utf-8')
            (root/'README.md').write_text('[notice](docs/licenses/upstream/README.md)', encoding='utf-8')
            check_links(root)

    def test_project_document_link_errors_are_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'README.md').write_text('[missing](docs/missing.md)', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Invalid local document targets'):
                check_links(root)
