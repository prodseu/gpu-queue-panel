"""Move a waiting task to a freshly discovered GPU under the queue lock."""
import time
import sys
from pathlib import Path

import gpu
from auth import allowed_task


def move(store, task_id, target_uuid, revision, actor):
    if type(revision) is not int:
        raise ValueError('请刷新队列后再切换GPU')
    if not isinstance(target_uuid, str) or not target_uuid:
        raise ValueError('请选择目标GPU')
    # Check ownership before querying any task-specific information.
    allowed_task(store.read(), task_id, actor)
    inventory = gpu.snapshot({}, telemetry=True)
    if not inventory['ok']:
        raise ValueError('GPU查询失败，请稍后再试')
    matches = [device for device in inventory['gpus'] if device['uuid'] == target_uuid]
    if len(matches) != 1:
        raise ValueError('目标GPU当前不存在，请刷新后重选')
    target = matches[0]

    def update(value):
        task = allowed_task(value, task_id, actor)
        if task['state'] != 'waiting' or task_id not in value['order']:
            raise ValueError('仅等待任务可以切换GPU')
        if task.get('gpu') is None and not task.get('gpu_uuid'):
            raise ValueError('CPU任务不能在这里改成GPU任务')
        previous = dict(index=task.get('gpu'), uuid=task.get('gpu_uuid'))
        task.update(gpu=target['index'], gpu_uuid=target['uuid'])
        task.pop('reserved_peak_memory_mib', None)
        task['gpu_assignment'] = dict(previous=previous, index=target['index'],
                                      uuid=target['uuid'], changed_by=actor.username,
                                      changed_at=time.time())
        wrap_command(task, store.directory)
        value['last_change'] = '切换等待任务GPU：' + task['name']

    return store.change(update, revision)


def wrap_command(task, directory):
    """An already loaded worker can launch this fresh entry after a GPU move.

    The entry execs the original argv in the same PID after syncing config, so
    the worker's log, identity and exit receipt still belong to the real command.
    """
    if task.get('kind') != 'command':
        return
    if 'original_argv' not in task:
        task['original_argv'] = list(task['argv'])
    task['argv'] = [sys.executable, str(Path(__file__).with_name('queued_command.py')),
                    '--data', str(directory), '--id', task['id']]


def prepare_scientific_config(task):
    """Apply an explicit queued move before the private scientific launcher runs.

    A crash before launch is safe to retry: the recorded target is idempotent.
    Only the task's already registered config is accessed by the worker.
    """
    if not task.get('gpu_assignment'):
        return
    import json
    from queue_store import atomic

    root = Path(task['root'])
    path = root / 'config.json'
    if not path.is_file():
        return
    config = json.loads(path.read_text(encoding='utf-8'))
    if not any(key in config for key in ('gpu', 'gpu_uuid', 'authorized_physical_gpu')):
        return
    if (root / 'run').exists():
        raise ValueError('已有运行目录，拒绝修改训练设备配置')
    backup = root / 'GPU_ASSIGNMENT_ORIGINAL_CONFIG.json'
    if not backup.exists():
        atomic(backup, config)
    for key in ('gpu', 'authorized_physical_gpu'):
        if key in config:
            config[key] = task['gpu']
    if 'gpu_uuid' in config:
        config['gpu_uuid'] = task['gpu_uuid']
    atomic(path, config)
