"""Formal builds cannot turn open audits into distribution clearance."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import release_gate


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        (self.root/'VERSION').write_text('1.0.0\n',encoding='utf-8')
        body=b'fixture source and permission evidence'
        path=self.root/'proof.txt';path.write_bytes(body)
        self.proof=dict(path='proof.txt',sha256=hashlib.sha256(body).hexdigest())
        row=dict(path='tesseract.exe',bytes=7,sha256=hashlib.sha256(b'fixture').hexdigest())
        self.manifest=dict(installer=dict(sha256='a'*64),files=[row])
        self.ocr=dict(installer=dict(sha256='a'*64),files=[dict(row,distribution_status='verified',
            evidence=[dict(self.proof,role='license'),dict(self.proof,role='source_build')])])
        files=[]
        for name in release_gate.GAME_FILES:
            target=self.root/name;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(b'fixture catalogue')
            files.append(dict(path=name,sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                distribution_status='verified',evidence=[dict(self.proof,role='permission'),
                                                         dict(self.proof,role='source_build')]))
        self.game=dict(route='redistribute',files=files)
        self.write('scripts/ocr-runtime-manifest.json',self.manifest)
        self.write('docs/releases/ocr-runtime-audit.json',self.ocr)
        self.write('docs/releases/game-data-audit.json',self.game)

    def write(self,name,value):
        target=self.root/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(value),encoding='utf-8')

    def test_test_candidates_are_explicitly_local_and_stable_checks_materials(self):
        self.assertEqual(release_gate.validate(self.root,'1.0.0-test.5')['distribution'],'local_test_only')
        self.assertEqual(release_gate.validate(self.root,'1.0.0')['distribution'],'materials_verified')

    def test_public_beta_still_checks_materials_and_version(self):
        (self.root/'VERSION').write_text('1.0.0-beta.1',encoding='utf-8')
        self.assertEqual(release_gate.validate(self.root,'1.0.0-beta.1')['distribution'],'materials_verified')
        self.write('docs/releases/ocr-runtime-audit.json',dict(self.ocr,files=[]))
        with self.assertRaises(ValueError):release_gate.validate(self.root,'1.0.0-beta.1')

    def test_unlabelled_versions_and_source_mismatch_cannot_bypass_gate(self):
        for version in ('v1.0.0','1.0.0.0','release','1.0.0-unlabelled'):
            with self.assertRaises(ValueError):release_gate.validate(self.root,version)
        (self.root/'VERSION').write_text('1.0.0-test.5',encoding='utf-8')
        with self.assertRaises(ValueError):release_gate.validate(self.root,'1.0.0')

    def test_open_status_unchanged_label_or_wrong_hash_cannot_pass(self):
        for change in (dict(distribution_status='open'),dict(sha256='c'*64),dict(evidence=[])):
            audit=copy.deepcopy(self.ocr);audit['files'][0].update(change)
            self.write('docs/releases/ocr-runtime-audit.json',audit)
            with self.assertRaises(ValueError):release_gate.validate(self.root,'1.0.0')

    def test_missing_or_additional_closure_rows_are_rejected(self):
        for rows in ([],self.ocr['files']*2):
            audit=dict(self.ocr,files=rows)
            self.write('docs/releases/ocr-runtime-audit.json',audit)
            with self.assertRaises(ValueError):release_gate.validate(self.root,'1.0.0')

    def test_game_data_needs_its_own_source_and_permission_evidence(self):
        for route in ('local_generation_pending','unknown'):
            self.write('docs/releases/game-data-audit.json',dict(self.game,route=route))
            with self.assertRaises(ValueError):release_gate.validate_game_data(self.root)
        audit=copy.deepcopy(self.game);audit['files'][0]['evidence']=[dict(self.proof,role='source_build')]
        self.write('docs/releases/game-data-audit.json',audit)
        with self.assertRaises(ValueError):release_gate.validate_game_data(self.root)

    def test_evidence_content_changes_are_rejected(self):
        (self.root/'proof.txt').write_text('changed',encoding='utf-8')
        with self.assertRaises(ValueError):release_gate.validate(self.root,'1.0.0')

    def test_evidence_cannot_escape_workspace(self):
        audit=copy.deepcopy(self.ocr)
        audit['files'][0]['evidence'][0]['path']='../escape.txt'
        self.write('docs/releases/ocr-runtime-audit.json',audit)
        with self.assertRaises(ValueError):release_gate.validate(self.root,'1.0.0')

    def test_local_route_requires_validation_and_excludes_bundled_data(self):
        checks={name:'PASS' for name in ('startup_without_data','same_install_snapshot',
                'frozen_generation','upgrade_feedback','preserve_user_data')}
        audit=dict(route='local_generation',checks=checks,evidence=[dict(self.proof,role='validation')])
        self.write('docs/releases/game-data-audit.json',audit)
        with self.assertRaises(ValueError):release_gate.validate_game_data(self.root)
        audit['distribution_files'] = []
        self.write('docs/releases/game-data-audit.json',audit)
        self.assertEqual(release_gate.validate_game_data(self.root),'local_generation')
        self.assertEqual(release_gate.bundled_game_files(self.root), ())
        del checks['frozen_generation']
        self.write('docs/releases/game-data-audit.json',audit)
        with self.assertRaises(ValueError):release_gate.validate_game_data(self.root)

    def test_installer_payload_must_match_reviewed_bytes_and_approval(self):
        import shutil
        bundle=self.root/'bundle'
        (bundle/'ocr').mkdir(parents=True)
        (bundle/'ocr/tesseract.exe').write_bytes(b'fixture')
        for name in release_gate.GAME_FILES:
            target=bundle/'_internal'/name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(self.root/name,target)
        approval=release_gate.validate(self.root,'1.0.0')
        self.write('bundle/DISTRIBUTION.json',approval)
        self.assertEqual(release_gate.validate_bundle(self.root,'1.0.0',bundle),approval)
        (bundle/'ocr/tesseract.exe').write_bytes(b'changed')
        with self.assertRaises(ValueError):release_gate.validate_bundle(self.root,'1.0.0',bundle)
        (bundle/'ocr/tesseract.exe').write_bytes(b'fixture')
        self.write('bundle/DISTRIBUTION.json',dict(approval,distribution='local_test_only'))
        with self.assertRaises(ValueError):release_gate.validate_bundle(self.root,'1.0.0',bundle)
        self.write('bundle/DISTRIBUTION.json',approval)
        (bundle/'_internal'/release_gate.GAME_FILES[0]).write_bytes(b'changed')
        with self.assertRaises(ValueError):release_gate.validate_bundle(self.root,'1.0.0',bundle)


if __name__=='__main__':unittest.main()
