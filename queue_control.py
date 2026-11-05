"""Persistent automatic/manual admission controls shared by RPC and dispatcher."""
import time

from auth import Accounts, allowed_task


def controls(value):
    saved = value.get('metadata', {}).get('queue_control', {})
    mode = saved.get('mode', 'auto')
    paused = saved.get('paused', False)
    if mode not in ('auto', 'manual') or type(paused) is not bool:
        raise ValueError('队列模式配置无效')
    return dict(mode=mode, paused=paused)


def configure(store, mode, paused, revision, actor):
    if not actor.admin:
        raise PermissionError('仅管理员可切换全局模式或暂停领取')
    if mode not in ('auto', 'manual') or type(paused) is not bool:
        raise ValueError('请提供有效模式和暂停状态')

    def update(value):
        value['metadata']['queue_control'] = dict(mode=mode, paused=paused)
        # A mode/pause change revokes requests that have not yet been claimed.
        for task in value['tasks'].values():
            task.pop('manual_start_request', None)
        value['last_change'] = '更新自动/手动模式与领取开关'

    return store.change(update, revision)


def request_start(store, task_id, revision, actor):
    def update(value):
        task = allowed_task(value, task_id, actor)
        control = controls(value)
        if control['mode'] != 'manual' or control['paused']:
            raise ValueError('请先切换手动模式并开启领取')
        if task['state'] != 'waiting' or task_id not in value['order']:
            raise ValueError('只可启动等待任务')
        task['manual_start_request'] = dict(by=actor.username, time=time.time())
        value['last_change'] = '请求手动启动：' + task['name']

    return store.change(update, revision)


def manual_candidate(value):
    return next((value['tasks'][key] for key in value['order']
                 if value['tasks'][key].get('manual_start_request')), None)


def manual_admission(task, directory):
    """Validate account and device identity, leaving capacity to the operator."""
    import gpu

    if not task.get('owner_id') or not Accounts(directory).enabled(task['owner_id']):
        return False, '任务账号未分配或已停用'
    if task.get('gpu') is not None or task.get('gpu_uuid'):
        info = gpu.snapshot({}, telemetry=True)
        if not info['ok']:
            return False, '无法查询目标GPU，请稍后再试'
        if not task.get('gpu_uuid') or not any(
                device['uuid'] == task['gpu_uuid'] for device in info['gpus']):
            return False, '目标GPU当前不存在'
    return True, '手动启动已授权，跳过自动资源检查'
