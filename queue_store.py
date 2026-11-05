"""One ordered personal queue shared by Codex, the viewer and its existing worker."""
import contextlib
import json
import os
import time
import uuid
from pathlib import Path


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding='utf-8')
    temp.replace(path)


class QueueStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'queue.json'

    @contextlib.contextmanager
    def locked(self):
        with (self.directory / 'queue.lock').open('a+b') as handle:
            handle.write(b'0')
            handle.flush()
            handle.seek(0)
            if os.name == 'posix':
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
            else:
                import msvcrt
                deadline = time.monotonic() + 5
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(.02)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'posix':
                    fcntl.flock(handle, fcntl.LOCK_UN)
                else:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def read(self):
        with self.locked():
            return json.loads(self.path.read_text(encoding='utf-8'))

    def initialize(self, tasks, order, running=None, metadata=None):
        with self.locked():
            if self.path.exists():
                raise ValueError('队列已存在，不重新导入或覆盖')
            ids = [t['id'] for t in tasks]
            if len(set(ids)) != len(ids) or set(order) - set(ids) or len(set(order)) != len(order):
                raise ValueError('任务ID或顺序不合法')
            jobs = {t['id']: dict(t, state='waiting') for t in tasks}
            if running:
                jobs[running]['state'] = 'running'
            if set(order) != {i for i in ids if i != running}:
                raise ValueError('等待顺序必须包含所有未运行任务')
            value = dict(version=2, revision=0, tasks=jobs, order=order, running=running,
                         metadata=dict(metadata or {},multiuser_rc=True), updated=time.time(), last_change='导入当前已授权队列')
            atomic(self.path, value)
            return value

    def change(self, fn, revision=None):
        with self.locked():
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if revision is not None and revision != value['revision']:
                raise ValueError('队列刚有变化，已刷新；请重新操作')
            result = fn(value)
            value['revision'] += 1
            value['updated'] = time.time()
            atomic(self.path, value)
            return result if result is not None else value

    def cancel(self, task_id, revision=None, *, actor):
        def update(value):
            from auth import allowed_task
            task = allowed_task(value,task_id,actor)
            if task['state'] != 'waiting':
                raise ValueError('只可取消等待任务；正在运行的任务保持执行')
            task.update(state='canceled', finished=time.time())
            value['order'].remove(task_id)
            value['last_change'] = '取消等待项：' + task['name']
        return self.change(update, revision)

    def reorder(self, ids, revision=None, *, actor):
        def update(value):
            from auth import visible
            owned=[key for key in value['order'] if visible(value['tasks'][key],actor)]
            if not isinstance(ids,list) or len(ids)!=len(owned) or len(set(ids))!=len(ids) or set(ids)!=set(owned):
                raise ValueError('请提供自己可管理的全部等待ID；不能包含其他用户/运行/已取消项')
            slots=set(owned);iterator=iter(ids)
            value['order']=[next(iterator) if key in slots else key for key in value['order']]
            value['last_change']='更新等待顺序'
        return self.change(update,revision)

    def configure(self, settings, revision=None, *, actor):
        if not actor.admin:raise PermissionError('仅管理员可修改全局调度设置')
        from scheduling import validate
        settings = validate(settings)
        def update(value):
            value['metadata']['scheduling'] = settings
            value['last_change'] = '更新并发窗口和资源保护；运行项保持执行'
        return self.change(update, revision)

    def claim(self, expected_id=None, revision=None, manual=False):
        from scheduling import policy, running_ids, running_in_queue, queue_heads, memory_needed
        with self.locked():
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if revision is not None and value['revision'] != revision:
                return None
            if not value['order']:
                return None
            from queue_control import controls
            control = controls(value)
            if control['paused'] or (control['mode'] == 'manual') != manual:
                return None
            if manual:
                if expected_id not in value['order']:
                    return None
                task_id = expected_id
                request = value['tasks'][task_id].get('manual_start_request')
                if not request:
                    return None
            else:
                heads = queue_heads(value)
                task_id = expected_id if expected_id is not None else heads[0]['id']
                if task_id not in {task['id'] for task in heads}:
                    return None
                window = policy(value)['window']
                if window != -1 and len(running_in_queue(value, value['tasks'][task_id])) >= window:
                    return None
            task = value['tasks'][task_id]
            from auth import Accounts
            if not task.get('owner_id') or not Accounts(self.directory).enabled(task['owner_id']):
                return None
            if task.get('gpu_uuid'):
                task['reserved_peak_memory_mib'] = memory_needed(task, policy(value))
            value['order'].remove(task_id)
            if manual:
                task['manual_start_authorization'] = task.pop('manual_start_request')
            task.update(state='running', started=time.time())
            value['running'] = running_ids(value)[0]  # compatibility: one active ID; tasks holds the full set
            value['last_change'] = '开始既有队列任务：' + task['name']
            value['revision'] += 1
            value['updated'] = time.time()
            atomic(self.path, value)
            return dict(task)

    def claim_if(self, task_id, revision, manual=False):
        return self.claim(task_id, revision, manual=manual)

    def finish(self, task_id, success, note=''):
        from scheduling import running_ids
        def update(value):
            if value['tasks'][task_id]['state'] != 'running':
                raise ValueError('结束任务身份不匹配')
            value['tasks'][task_id].update(state='completed' if success else 'failed',
                                         finished=time.time(), note=note)
            active = running_ids(value)
            value['running'] = active[0] if active else None
            value['last_change'] = ('完成：' if success else '失败：') + value['tasks'][task_id]['name']
        return self.change(update)
