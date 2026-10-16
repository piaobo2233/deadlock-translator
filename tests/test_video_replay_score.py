"""Selected-case denominators, repeated deliveries and timing bounds."""
import unittest
from tools.score_video_replay import score


class ReplayScoreTests(unittest.TestCase):
    def test_duplicate_delivery_does_not_inflate_unique_coverage(self):
        truth=dict(scope='selected only',messages=[dict(id=1,text='go!',split='validation',
            first_reviewed_readable=14,match_from=10,match_until=25,
            readable_onset_interval=[12,14],boundary_case=True)],
            edge_cases=[dict(text='video ends')])
        result=dict(start=0,seconds=30,records=[
            dict(stage='confirmed',at=15,text='Go'),
            dict(stage='dequeued',at=16,text='GO!',successful=True),
            dict(stage='dequeued',at=17,text='go',successful=True),
            dict(stage='dequeued',at=18,text='other real chat',successful=True)])
        value=score(truth,result)
        self.assertEqual(value['totals']['validation']['delivered_messages'],1)
        self.assertEqual(value['totals']['validation']['duplicate_dequeues'],1)
        self.assertEqual(value['totals']['validation']['outside_10s_boundary_guard']['labeled_messages'],0)
        self.assertEqual(value['messages'][0]['dequeue_delay_interval'],[2,4])
        self.assertEqual(len(value['unmatched_needing_visual_review']),1)
        self.assertNotIn('false_positives',value)

    def test_a_different_word_or_out_of_window_read_cannot_match(self):
        truth=dict(scope='selected',messages=[dict(id=1,text='go left',split='tune',
            first_reviewed_readable=14,match_from=10,match_until=25)])
        result=dict(start=0,seconds=30,records=[dict(stage='confirmed',at=15,text='go right'),
            dict(stage='dequeued',at=29,text='go left',successful=True)])
        value=score(truth,result)
        self.assertEqual(value['totals']['all']['confirmed_messages'],0)
        self.assertEqual(value['totals']['all']['delivered_messages'],0)


if __name__=='__main__':
    unittest.main()
