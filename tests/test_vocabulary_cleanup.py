"""Migration preserves user data, decisions and partial-commit retry boundaries."""
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from app.storage import store
from tools import audit_legacy_vocabulary as cleanup

class VocabularyCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'program';(self.root/'data').mkdir(parents=True)
        (self.root/'app/translation').mkdir(parents=True)
        (self.root/'app/main.py').write_text("DEFAULT_GLOSSARY={'gg':'打得好'}")
        (self.root/'app/translation/game_terms.json').write_text('{"terms":[]}')
        store.save_glossary_file(str(self.root/'data/glossary.json'),{
            'gg':dict(zh='打得好',source='user',custom={'keep':True}),
            'personal':dict(zh='我确认的译文',source='user',quality='human_reviewed'),
            'automatic':dict(zh='旧的自动译文',source='api',original='automatic'),
            'accepted':dict(zh='已确认译文',source='api',original='accepted')})
        store.save_collection(str(self.root/'data/pending.json'),{
            'accepted':dict(original='accepted',translation='已确认译文',status='accepted',decision_id='human-decision'),
            'ignored':dict(original='ignored',translation='旧译文',status='rejected')})
        self.before={p.name:p.read_bytes() for p in (self.root/'data').glob('*.json')}
        self.output=Path(self.temp.name)/'plan'
    def prepare(self):
        result=cleanup.plan(self.root,self.output)
        self.assertEqual(result['status'],'planned')
        self.assertEqual({p.name:p.read_bytes() for p in (self.root/'data').glob('*.json')},self.before)
        return self.output/'manifest.json'
    def test_cleanup_quarantines_only_unconfirmed_auto_preserves_decisions_and_exact_rollback(self):
        path=self.prepare();cleanup.apply(path)
        rows=store.load_glossary_file(str(self.root/'data/glossary.json'))
        self.assertEqual(rows['personal']['zh'],'我确认的译文')
        self.assertEqual(rows['gg']['custom'],{'keep':True})
        self.assertNotIn('quarantine_reason',rows['accepted'])
        self.assertFalse(rows['automatic']['enabled']);self.assertEqual(rows['automatic']['zh'],'旧的自动译文')
        queue=store.load_collection(str(self.root/'data/pending.json'))
        self.assertEqual(queue['accepted']['decision_id'],'human-decision')
        self.assertEqual(queue['ignored']['status'],'rejected')
        self.assertEqual(sum(r.get('status')=='pending' for r in queue.values()),1)
        candidate=next(r for r in queue.values() if r.get('status')=='pending')
        self.assertEqual(candidate['request_count'],0);self.assertEqual(candidate['request_ids'],[])
        cleanup.rollback(path)
        self.assertEqual({p.name:p.read_bytes() for p in (self.root/'data').glob('*.json')},self.before)
    def test_concurrent_change_rejects_all_writes_and_rollback_does_not_clobber_edits(self):
        path=self.prepare();store.save_collection(str(self.root/'data/pending.json'),{'new':dict(original='new',translation='新草稿',status='pending')})
        changed={p.name:p.read_bytes() for p in (self.root/'data').glob('*.json')}
        with self.assertRaises(ValueError):cleanup.apply(path)
        self.assertEqual({p.name:p.read_bytes() for p in (self.root/'data').glob('*.json')},changed)
        with self.assertRaises(ValueError):cleanup.rollback(path)
    def test_queue_first_partial_commit_can_retry_without_duplicate_or_false_success(self):
        path=self.prepare();save=store.atomic_write_json
        def fail_formal(target,obj,**kwargs):
            return False if Path(target).name=='glossary.json' else save(target,obj,**kwargs)
        with patch.object(store,'atomic_write_json',side_effect=fail_formal):
            with self.assertRaises(OSError):cleanup.apply(path)
        self.assertEqual((self.root/'data/glossary.json').read_bytes(),self.before['glossary.json'])
        queue=store.load_collection(str(self.root/'data/pending.json'))
        self.assertEqual(sum(r.get('status')=='pending' for r in queue.values()),1)
        cleanup.apply(path)
        self.assertEqual(store.load_collection(str(self.root/'data/pending.json')),queue)
        cleanup.rollback(path)

if __name__=='__main__':unittest.main()
