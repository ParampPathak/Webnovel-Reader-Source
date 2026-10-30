"""Lightweight Linux pressure sampling for backups and isolated checks."""
from collections import deque
import json
import time
from pathlib import Path


class ResourcePressure(RuntimeError):
    pass


def counters():
    fields = Path('/proc/stat').read_text().splitlines()[0].split()[1:9]
    values = list(map(int,fields))
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key,_,value = line.partition(':')
        memory[key] = int(value.strip().split()[0])*1024
    pressure = Path('/proc/pressure/memory').read_text().splitlines()
    full = next((line for line in pressure if line.startswith('full ')), 'full avg10=0')
    avg10 = float(dict(field.split('=') for field in full.split()[1:])['avg10'])
    vm = dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
    return {'cpu_total':sum(values),'cpu_iowait':values[4],
            'available_bytes':memory['MemAvailable'], 'swap_used_bytes':memory['SwapTotal']-memory['SwapFree'],
            'memory_full_avg10':avg10,'swap_in_pages':int(vm['pswpin']),'swap_out_pages':int(vm['pswpout'])}


class ResourceGuard:
    def __init__(self, interval=1, sustained_seconds=3):
        self.interval,self.sustained_seconds = interval,sustained_seconds
        self.previous = counters()
        self.previous_time = time.monotonic()
        self.high_io_since = None
        self.samples = deque(maxlen=3600)
        self.high_swap_since = None

    def check(self, force=False):
        now = time.monotonic()
        elapsed = now-self.previous_time
        if not force and elapsed<self.interval:
            return
        current = counters()
        total = current['cpu_total']-self.previous['cpu_total']
        wait = current['cpu_iowait']-self.previous['cpu_iowait']
        current.update(iowait_percent=round(max(0,wait)/total*100,2) if total>0 else 0,
                       swap_in_pages_per_second=round(max(0,current['swap_in_pages']-self.previous['swap_in_pages'])/max(elapsed,.01),2),
                       swap_out_pages_per_second=round(max(0,current['swap_out_pages']-self.previous['swap_out_pages'])/max(elapsed,.01),2))
        self.samples.append(current)
        self.previous,self.previous_time = current,now
        if current['available_bytes']<256*1024*1024 or current['memory_full_avg10']>=10:
            raise ResourcePressure('Severe memory pressure: intensive work stopped.')
        swapping=current['swap_in_pages_per_second']+current['swap_out_pages_per_second']
        if swapping>2048:
            if self.high_swap_since is None: self.high_swap_since=now
            if now-self.high_swap_since>=self.sustained_seconds:
                raise ResourcePressure('Sustained heavy swapping: intensive work stopped.')
        else:
            self.high_swap_since=None
        if current['iowait_percent']>20:
            if self.high_io_since is None:
                self.high_io_since = now
            if now-self.high_io_since>=self.sustained_seconds:
                raise ResourcePressure('Sustained I/O wait above 20%: intensive work stopped.')
        else:
            self.high_io_since = None

    def summary(self):
        return {'samples':len(self.samples),
                'max_iowait_percent':max((s['iowait_percent'] for s in self.samples),default=0),
                'min_available_mib':round(min((s['available_bytes'] for s in self.samples),default=self.previous['available_bytes'])/1024/1024),
                'max_memory_full_avg10':max((s['memory_full_avg10'] for s in self.samples),default=0),
                'max_swap_in_pages_per_second':max((s['swap_in_pages_per_second'] for s in self.samples),default=0),
                'max_swap_out_pages_per_second':max((s['swap_out_pages_per_second'] for s in self.samples),default=0)}
