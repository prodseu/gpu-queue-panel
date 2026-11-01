"""One ordered personal queue shared by Codex, the viewer and its existing worker."""
import contextlib
import json
import os
import time
from pathlib import Path


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
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
                         metadata=metadata or {}, updated=time.time(), last_change='导入当前已授权队列')
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

    def cancel(self, task_id, revision=None):
        def update(value):
            task = value['tasks'][task_id]
            if task['state'] != 'waiting':
                raise ValueError('只可取消等待任务；正在运行的任务保持执行')
            task.update(state='canceled', finished=time.time())
            value['order'].remove(task_id)
            value['last_change'] = '取消等待项：' + task['name']
        return self.change(update, revision)

    def reorder(self, ids, revision=None):
        def update(value):
            if not isinstance(ids, list) or len(ids) != len(value['order']) or set(ids) != set(value['order']):
                raise ValueError('请按新顺序提供全部等待项；不能包含运行项或已取消项')
            value['order'] = ids
            value['last_change'] = '更新等待顺序'
        return self.change(update, revision)

    def claim(self,expected_id=None,revision=None):
        """Only the existing-queue worker calls this, after its current child exits."""
        def update(value):
            if value['running']:
                raise ValueError('当前任务仍在执行')
            if not value['order']:
                return None
            task_id = value['order'].pop(0)
            task = value['tasks'][task_id]
            task.update(state='running', started=time.time())
            value['running'] = task_id
            value['last_change'] = '开始既有队列任务：' + task['name']
            return dict(task)
        # None must stay None for an empty queue, rather than returning the whole state.
        with self.locked():
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if revision is not None and value['revision']!=revision:
                return None
            if expected_id is not None and (not value['order'] or value['order'][0]!=expected_id):
                return None
            task = update(value)
            if task is not None:
                value['revision'] += 1
                value['updated'] = time.time()
                atomic(self.path, value)
            return task

    def claim_if(self,task_id,revision):
        return self.claim(task_id,revision)

    def finish(self, task_id, success, note=''):
        def update(value):
            if value['running'] != task_id or value['tasks'][task_id]['state'] != 'running':
                raise ValueError('结束任务身份不匹配')
            value['tasks'][task_id].update(state='completed' if success else 'failed',
                                           finished=time.time(), note=note)
            value['running'] = None
            value['last_change'] = ('完成：' if success else '失败：') + value['tasks'][task_id]['name']
        return self.change(update)
