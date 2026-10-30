import unittest
from unittest.mock import patch
from resource_guard import ResourceGuard,ResourcePressure


def sample(total=0,wait=0,available=1024*1024*1024,pressure=0):
    return {'cpu_total':total,'cpu_iowait':wait,'available_bytes':available,
            'swap_used_bytes':0,'memory_full_avg10':pressure,'swap_in_pages':0,'swap_out_pages':0}


class ResourceTests(unittest.TestCase):
    def test_sustained_high_iowait_stops_work(self):
        with patch('resource_guard.counters',side_effect=[sample()]+[sample(i*100,i*30) for i in range(1,5)]), patch('resource_guard.time.monotonic',side_effect=[0,1,2,3,4]):
            guard=ResourceGuard()
            for _ in range(3):guard.check()
            self.assertRaises(ResourcePressure,guard.check)

    def test_isolated_spike_does_not_stop_work(self):
        with patch('resource_guard.counters',side_effect=[sample(),sample(100,30),sample(200,30),sample(300,30)]), patch('resource_guard.time.monotonic',side_effect=[0,1,2,3]):
            guard=ResourceGuard()
            for _ in range(3):guard.check()
            self.assertEqual(guard.summary()['max_iowait_percent'],30)
            self.assertIsNone(guard.high_io_since)

    def test_severe_memory_pressure_stops_immediately(self):
        for bad in [sample(100,0,available=100*1024*1024),sample(100,0,pressure=12)]:
            with patch('resource_guard.counters',side_effect=[sample(),bad]),patch('resource_guard.time.monotonic',side_effect=[0,1]):
                guard=ResourceGuard();self.assertRaises(ResourcePressure,guard.check)

    def test_sustained_heavy_swapping_stops_work(self):
        values=[sample()]
        for i in range(1,5):
            value=sample(i*100,0);value['swap_out_pages']=i*3000;values.append(value)
        with patch('resource_guard.counters',side_effect=values),patch('resource_guard.time.monotonic',side_effect=[0,1,2,3,4]):
            guard=ResourceGuard()
            for _ in range(3):guard.check()
            self.assertRaises(ResourcePressure,guard.check)
