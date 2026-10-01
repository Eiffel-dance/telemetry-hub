import json
import unittest

import app
from app import Telemetry


class SmokeTest(unittest.TestCase):
    def test_import(self):
        self.assertTrue(app)


class CompatTest(unittest.TestCase):
    def test_calls_without_service_still_work(self):
        t = Telemetry(lambda: 1.0)
        t.inc('requests', labels=(('route', '/api'),))
        t.observe('latency_ms', 12.5)
        t.start('request')
        t.finish('request')
        snap = t.snapshot()
        self.assertEqual(snap['counters'][0]['value'], 1)
        self.assertEqual(snap['counters'][0]['service'], '')
        self.assertEqual(snap['samples'][0]['values'], [12.5])
        self.assertEqual(snap['spans'][0]['service'], '')
        self.assertEqual(snap['spans'][0]['start'], 1.0)
        self.assertEqual(snap['spans'][0]['end'], 1.0)
        self.assertIsNone(snap['spans'][0]['error'])
        json.loads(t.json())


class ServiceTest(unittest.TestCase):
    def test_same_name_labels_different_service_aggregate_separately(self):
        t = Telemetry()
        t.inc('requests')
        t.inc('requests', service='web')
        t.inc('requests', service='web')
        t.observe('latency', 1.0)
        t.observe('latency', 2.0, service='web')
        snap = t.snapshot()
        counters = {(c['service'], c['name']): c['value'] for c in snap['counters']}
        self.assertEqual(counters, {('', 'requests'): 1, ('web', 'requests'): 2})
        samples = {(s['service'], s['name']): s['values'] for s in snap['samples']}
        self.assertEqual(samples, {('', 'latency'): [1.0], ('web', 'latency'): [2.0]})

    def test_service_must_be_non_empty_string(self):
        t = Telemetry()
        for bad in ('', 0, 1, b'web', object()):
            with self.assertRaises(ValueError):
                t.inc('x', service=bad)
            with self.assertRaises(ValueError):
                t.observe('x', 1.0, service=bad)
            with self.assertRaises(ValueError):
                t.start('s', service=bad)
            with self.assertRaises(ValueError):
                t.finish('s', service=bad)
        self.assertEqual(t.snapshot(), {'counters': [], 'samples': [], 'spans': []})

    def test_span_located_by_service_and_id(self):
        clock = iter([1.0, 2.0, 3.0, 4.0]).__next__
        t = Telemetry(clock)
        t.start('op')
        t.start('op', service='web')
        t.finish('op', service='web', error='boom')
        spans = {s['service']: s for s in t.snapshot()['spans']}
        self.assertIsNone(spans['']['end'])
        self.assertEqual(spans['web']['end'], 3.0)
        self.assertEqual(spans['web']['error'], 'boom')


class LabelTest(unittest.TestCase):
    def test_labels_normalized_by_key_order(self):
        t = Telemetry()
        t.inc('x', labels=(('b', 2), ('a', 1)))
        t.inc('x', labels=(('a', 1), ('b', 2)))
        snap = t.snapshot()
        self.assertEqual(len(snap['counters']), 1)
        self.assertEqual(snap['counters'][0]['value'], 2)
        self.assertEqual(snap['counters'][0]['labels'], [('a', 1), ('b', 2)])
        self.assertEqual(json.loads(t.json())['counters'][0]['labels'], [['a', 1], ['b', 2]])

    def test_duplicate_label_keys_rejected_without_mutation(self):
        t = Telemetry()
        t.inc('x', labels=(('a', 1),))
        with self.assertRaises(ValueError):
            t.inc('x', labels=(('a', 1), ('a', 2)))
        with self.assertRaises(ValueError):
            t.observe('y', 1.0, labels=(('k', 1), ('k', 2)))
        snap = t.snapshot()
        self.assertEqual(snap['counters'][0]['value'], 1)
        self.assertEqual(snap['samples'], [])

    def test_unserializable_labels_rejected_without_mutation(self):
        t = Telemetry()
        with self.assertRaises(ValueError):
            t.inc('x', labels=(('a', object()),))
        with self.assertRaises(ValueError):
            t.observe('y', 1.0, labels=(('a', {1, 2}),))
        self.assertEqual(t.snapshot()['counters'], [])
        self.assertEqual(t.snapshot()['samples'], [])


class SampleStatsTest(unittest.TestCase):
    def test_stats(self):
        t = Telemetry()
        for v in (3, 1.5, 2):
            t.observe('latency', v)
        s = t.snapshot()['samples'][0]
        self.assertEqual(s['values'], [3, 1.5, 2])
        self.assertEqual(s['count'], 3)
        self.assertEqual(s['sum'], 6.5)
        self.assertEqual(s['minimum'], 1.5)
        self.assertEqual(s['maximum'], 3.0)
        self.assertEqual(s['mean'], 6.5 / 3)
        self.assertEqual(t.snapshot()['samples'][0], s)  # stable across reads

    def test_nan_and_inf_rejected_without_mutation(self):
        t = Telemetry()
        t.observe('x', 1.0)
        for bad in (float('nan'), float('inf'), float('-inf')):
            with self.assertRaises(ValueError):
                t.observe('x', bad)
        s = t.snapshot()['samples'][0]
        self.assertEqual(s['values'], [1.0])
        self.assertEqual(s['count'], 1)


class SpanQueryTest(unittest.TestCase):
    def make(self):
        clock = iter([1.0, 2.0, 3.0, 4.0, 5.0]).__next__
        t = Telemetry(clock)
        t.start('a')                      # open, default service
        t.start('b', service='web')       # error
        t.finish('b', service='web', error='boom')
        t.start('c', service='web')       # finished cleanly
        t.finish('c', service='web')
        return t

    def test_open_and_error(self):
        t = self.make()
        self.assertEqual([s['span'] for s in t.spans('open')], ['a'])
        errors = t.spans('error')
        self.assertEqual(len(errors), 1)
        e = errors[0]
        self.assertEqual(e['span'], 'b')
        self.assertEqual(e['service'], 'web')
        self.assertIsNone(e['parent'])
        self.assertEqual(e['start'], 2.0)
        self.assertEqual(e['end'], 3.0)
        self.assertEqual(e['error'], 'boom')

    def test_invalid_status_and_empty_result(self):
        t = self.make()
        for bad in ('closed', 'ok', '', None):
            with self.assertRaises(ValueError):
                t.spans(bad)
        t2 = Telemetry()
        t2.start('x')
        t2.finish('x')
        self.assertEqual(t2.spans('open'), [])
        self.assertEqual(t2.spans('error'), [])

    def test_parent_preserved(self):
        t = Telemetry(lambda: 0.0)
        t.start('child', parent='parent-id')
        s = t.snapshot()['spans'][0]
        self.assertEqual(s['parent'], 'parent-id')


class SnapshotTest(unittest.TestCase):
    def test_sorting(self):
        clock = iter([3.0, 1.0, 2.0]).__next__
        t = Telemetry(clock)
        t.inc('m', service='b')
        t.inc('m')
        t.inc('a', service='b')
        t.observe('m', 1.0, service='b')
        t.observe('m', 1.0)
        t.start('s2', service='a')   # start=3.0
        t.start('s1', service='b')   # start=1.0
        t.start('s0', service='a')   # start=2.0
        snap = t.snapshot()
        self.assertEqual([(c['service'], c['name']) for c in snap['counters']],
                         [('', 'm'), ('b', 'a'), ('b', 'm')])
        self.assertEqual([(s['service'], s['name']) for s in snap['samples']],
                         [('', 'm'), ('b', 'm')])
        self.assertEqual([(s['service'], s['span']) for s in snap['spans']],
                         [('a', 's0'), ('a', 's2'), ('b', 's1')])

    def test_json_compact_and_stable_keys(self):
        t = Telemetry(lambda: 1.0)
        t.inc('x')
        text = t.json()
        self.assertNotIn(' ', text)
        self.assertEqual(text, json.dumps(json.loads(text), sort_keys=True,
                                          separators=(',', ':')))
        self.assertEqual(text, t.json())


if __name__ == '__main__':
    unittest.main()
