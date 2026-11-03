"""Admission controls and one persistent, bounded concurrent dispatcher.

Only admission is controlled: running jobs are never paused, killed or resized.
GPU-Util is a launch gate, not a hardware utilization limit.
"""
import json
import math
import threading
import time
import traceback
from pathlib import Path

import gpu
import process_monitor
from queue_store import atomic

DEFAULTS = dict(window=2, reserve_mib=8192, memory_limit_percent=90,
                default_peak_mib=24576, utilization_limit=-1,
                stable_seconds=30, launch_interval_seconds=60)


def running_ids(value):
    return [key for key, task in value['tasks'].items() if task['state'] == 'running']


def validate(settings):
    limits = dict(window=(1, 8), reserve_mib=(1024, 1048576),
                  memory_limit_percent=(10, 95), default_peak_mib=(1024, 1048576),
                  utilization_limit=(1, 95), stable_seconds=(15, 300),
                  launch_interval_seconds=(30, 600))
    if not isinstance(settings, dict) or set(settings) != set(DEFAULTS):
        raise ValueError('请提供完整的调度设置')
    for key, (low, high) in limits.items():
        n = settings[key]
        if type(n) is not int or (n != -1 and not low <= n <= high):
            raise ValueError(f'{key} 必须为 -1（不限制）或 {low}–{high} 的整数')
    return dict(settings)


def policy(value):
    return validate(dict(DEFAULTS, **value.get('metadata', {}).get('scheduling', {})))


def memory_needed(task, settings):
    n = (task.get('reserved_peak_memory_mib') if task.get('state')=='running' else None)
    if n is None:
        n = task.get('peak_memory_mib', settings['default_peak_mib'])
    if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or (n != -1 and n < 1024):
        raise ValueError('任务峰值显存预算须为 -1（不预留预算）或至少1024 MiB')
    return n


class Admission:
    def __init__(self, directory, rules=()):
        self.directory = Path(directory)
        self.rules = rules
        self.samples = {}

    def check(self, task, value, now=None):
        now = time.time() if now is None else now
        settings = policy(value)
        active = running_ids(value)
        if settings['window'] != -1 and len(active) >= settings['window']:
            return False, '并发窗口已满；缩窗不影响运行项'
        uuid = task.get('gpu_uuid')
        if not uuid:
            if task.get('gpu') is not None:
                return False, 'GPU任务缺少UUID，暂停领取；不能按CPU任务绕过保护'
            return True, 'CPU任务'
        # Fresh NVIDIA totals plus scoped identities, never inspect unknown argv.
        info = gpu.snapshot({}, telemetry=True)
        if not info['ok']:
            return False, 'GPU查询失败，暂停领取'
        devices = {g['uuid']: g for g in info['gpus']}
        for key, device in devices.items():
            samples = self.samples.setdefault(key, [])
            samples.append(dict(t=now, compute=device.get('utilization')))
            self.samples[key] = [s for s in samples if s['t'] >= now - 310]
        device = devices.get(uuid)
        if device is None:
            return False, '目标GPU未检测到'
        inventory = process_monitor.collect(value, info['gpus'], self.directory)
        scoped = gpu.snapshot({'hold_rules': self.rules})
        if inventory['error'] or not scoped['ok']:
            return False, 'GPU进程查询失败，暂停领取'
        holds = {p['pid'] for g in scoped['gpus'] if g['uuid'] == uuid
                 for p in g['processes'] if p['hold']}
        managed = {p['pid'] for rows in inventory['tasks'].values() for p in rows}
        # Check the latest process query too, including processes not in inventory.
        if any(p['pid'] not in managed | holds for g in scoped['gpus'] if g['uuid'] == uuid for p in g['processes']):
            return False, '目标GPU存在未纳管进程，仅监控，不共享或操作'
        same = [value['tasks'][key] for key in active if value['tasks'][key].get('gpu_uuid') == uuid]
        last_start = max([t.get('started', now) for t in same] +
                         [t.get('started', 0) for t in value['tasks'].values() if t.get('gpu_uuid') == uuid])
        if settings['launch_interval_seconds'] != -1 and last_start and now - last_start < settings['launch_interval_seconds']:
            return False, '启动冷却中，等待显存和计算稳定'
        if settings['utilization_limit'] != -1:
            if settings['stable_seconds'] == -1:
                points = self.samples[uuid][-1:]
            else:
                points = [s for s in self.samples.get(uuid, []) if s['t'] >= now - settings['stable_seconds'] - 5]
                if (not points or points[0]['t'] > now - settings['stable_seconds'] or
                        any(b['t'] - a['t'] > 10 for a, b in zip(points, points[1:]))):
                    return False, '正在积累连续GPU采样，暂不领取'
            if any(not isinstance(p['compute'], (int, float)) or not math.isfinite(p['compute']) or
                   p['compute'] >= settings['utilization_limit'] for p in points):
                return False, '近期GPU计算利用率达到保护阈值，暂不领取'
        if settings['reserve_mib'] == -1 and settings['memory_limit_percent'] == -1:
            return True, '资源检查通过（显存限额未启用）'
        used, total = device.get('memory_used_mib'), device.get('memory_total_mib')
        if any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in (used, total)) or total <= 0:
            return False, '显存数据不可用，暂停领取'
        unallocated = 0
        try:
            for t in same:
                rows = inventory['tasks'].get(t['id'], [])
                if any(p.get('memory_used_mib') is None for p in rows):
                    measured = 0
                else:
                    measured = sum(p['memory_used_mib'] for p in rows)
                unallocated += max(0, max(0, memory_needed(t, settings)) - measured)
            projected = used + unallocated + max(0, memory_needed(task, settings))
        except ValueError as exc:
            return False, str(exc)
        limits = []
        if settings['reserve_mib'] != -1:
            limits.append(total - settings['reserve_mib'])
        if settings['memory_limit_percent'] != -1:
            limits.append(total * settings['memory_limit_percent'] / 100)
        limit = min(limits)
        if projected > limit:
            return False, f'显存保护：预计{projected / 1024:.1f} GiB，允许{limit / 1024:.1f} GiB（含运行项未分配预算）'
        return True, '资源检查通过'


class Dispatcher:
    """A single owner mutates queue state; independent job threads execute commands."""
    def __init__(self, store, make_worker, phase, admission, interval=3,
                 stop_on_failure=lambda task: True, on_idle=None):
        self.store, self.make_worker, self.phase = store, make_worker, phase
        self.admission, self.interval = admission, interval
        self.stop_on_failure, self.on_idle = stop_on_failure, on_idle
        self.active = {}
        self.halted = False

    def launch(self, task):
        result = {}
        def execute():
            try:
                worker = self.make_worker(task)
                worker.execute(task)
                result['success'] = True
            except BaseException:
                result.update(success=False, error=traceback.format_exc())
        thread = threading.Thread(target=execute, name='queue-' + task['id'], daemon=False)
        self.active[task['id']] = (task, thread, result)
        thread.start()

    def tick(self):
        for key, (task, thread, result) in list(self.active.items()):
            if thread.is_alive():
                continue
            thread.join()
            self.store.finish(key, result['success'], result.get('error', ''))
            del self.active[key]
            if not result['success'] and self.stop_on_failure(task):
                self.halted = True
        value = self.store.read()
        settings = policy(value)
        reason = '暂停领取：任务失败；其余运行项继续，等待项保留' if self.halted else '等待队列任务'
        if not self.halted and value['order']:
            candidate = value['tasks'][value['order'][0]]
            allowed, reason = self.admission.check(candidate, value)
            if allowed:
                task = self.store.claim_if(candidate['id'], value['revision'])
                if task:
                    self.launch(task)
                    reason = '任务已启动，等待资源稳定后再判断下一项'
        value = self.store.read()
        ids = running_ids(value)
        atomic(self.store.directory / 'scheduler_status.json', dict(time=time.time(),
            running=ids, window=settings['window'], reason=reason, halted=self.halted))
        if not ids and not value['order']:
            self.phase('队列为空，持续待命；新任务加入后自动接续')
        else:
            window = '不限制' if settings['window'] == -1 else settings['window']
            self.phase(f"运行 {len(ids)}/{window} · {reason}")
        return value

    def run(self):
        if running_ids(self.store.read()):
            raise RuntimeError('存在先前运行项；保留状态，不自动重跑，请核对进程后恢复')
        idle_revision = None
        while True:
            value = self.tick()
            if not running_ids(value) and not value['order'] and idle_revision != value['revision']:
                if self.on_idle:
                    self.on_idle()
                idle_revision = value['revision']
            if self.halted and not self.active:
                return
            time.sleep(self.interval)
