"""Production resource thresholds tested independently of shared-runner load."""
import unittest
from unittest.mock import patch
from resource_guard import ResourceGuard,ResourcePressure


class ResourceGuardTests(unittest.TestCase):
    def setUp(self):
        self.now=0
        self.sample={'cpu_total':100,'cpu_iowait':0,'available_bytes':1024**3,
                     'swap_used_bytes':0,'memory_full_avg10':0,'swap_in_pages':0,'swap_out_pages':0}
        sample=patch('resource_guard.counters',side_effect=lambda:dict(self.sample))
        clock=patch('resource_guard.time.monotonic',side_effect=lambda:self.now)
        sample.start();clock.start();self.addCleanup(sample.stop);self.addCleanup(clock.stop)
        self.guard=ResourceGuard()

    def tick(self,wait=0,swap=0):
        self.now+=1;self.sample['cpu_total']+=100;self.sample['cpu_iowait']+=wait
        self.sample['swap_in_pages']+=swap;self.guard.check()

    def test_healthy_synthetic_worker_never_depends_on_host_load(self):
        for _ in range(10):self.tick()

    def test_low_memory_stops_work(self):
        self.sample['available_bytes']=128*1024**2
        with self.assertRaisesRegex(ResourcePressure,'memory'):self.tick()

    def test_sustained_io_pressure_stops_work_but_one_spike_does_not(self):
        self.tick(wait=30);self.tick();self.tick(wait=30);self.tick(wait=30);self.tick(wait=30)
        with self.assertRaisesRegex(ResourcePressure,'I/O wait'):self.tick(wait=30)

    def test_sustained_swapping_stops_work(self):
        self.tick(swap=3000);self.tick(swap=3000);self.tick(swap=3000)
        with self.assertRaisesRegex(ResourcePressure,'swapping'):self.tick(swap=3000)
