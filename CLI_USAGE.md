# 多用户 RC 的一行托管

先在服务器 RC 目录初始化账号并启动唯一的 RC 执行器，见 [README.md](README.md)。仅已授权任务可以登记；网页登录不会自动登录终端。

## 登录与一行命令

```bash
python /XXX/gpu-queue-panel-rc/users.py login alice
python /XXX/gpu-queue-panel-rc/qrun --gpu 0 -- python train.py --epochs 20
CUDA_VISIBLE_DEVICES=0 python /XXX/gpu-queue-panel-rc/qrun python train.py --epochs 20
python /XXX/gpu-queue-panel-rc/qrun --cpu -- python prepare_data.py
```

可自行将 RC 的 qrun 放进个人 PATH；试用时优先用绝对路径，避免替换正式版同名入口。包装器记录当前目录、解释器、argv 与选定环境，不启动 GPU 或另建调度器。程序保持前台运行，不套 nohup/screen/tmux/后台 `&`。

账号来自登录会话，客户端不能指定 owner；自动 ID 带用户名，显式 ID 全局唯一。重复同 ID/内容只返回已有任务，不自动重跑；新一轮需明确授权与新 ID。

## 会话和参数

```bash
python users.py --session-file /XXX/alice.session.json login alice
python qrun --session-file /XXX/alice.session.json --gpu GPU-XXX \
  --cwd /XXX/project --id alice-exp-001 --name "实验 A" \
  --peak-memory-mib 24576 --status-file run/STATUS.json \
  --progress-key updates --updates 5000 --epochs 20 \
  --env OMP_NUM_THREADS=2 -- python train.py --epochs 20
```

- `--gpu`：编号或完整 UUID；可继承单卡 CUDA_VISIBLE_DEVICES。每任务单卡，CPU 用 `--cpu`。
- `--cwd`：默认当前目录；解释器从当前环境解析，保留虚拟环境入口。
- `--peak-memory-mib`：训练和验证预计峰值，默认由全局设置决定，`-1`不额外预留。
- `--status-file/--progress-key/--updates/--epochs`：可选进度；不提供时不伪造百分比。
- `--completion-file`：可选，退出码 0 后再检查 JSON 的 `complete: true`。
- `--env KEY=VALUE`：显式环境，可重复；默认只捕获 PATH/PYTHONPATH/LD_LIBRARY_PATH/CONDA_PREFIX/VIRTUAL_ENV。设备和队列身份不能覆盖。
- `--data`：RC 数据目录，默认 config.json 的 runtime-data-rc；旧版数据会拒绝。
- `--session-file` 或环境变量 `GPU_QUEUE_SESSION`：终端会话文件；默认 `~/.gpu-queue-panel-rc/session.json`。不同面板账号需使用各自会话文件。

会话文件属于凭据。Linux `0600` 仅限制其他 Unix 账号，不能限制共享同一系统账号的人或训练代码。

## 同行参数：python train.py --queue

脚本开头、重型导入/参数解析/CUDA 初始化之前接入一次：

```python
from queue_submit import enqueue_if_requested
if __name__ == '__main__':
    enqueue_if_requested()
```

```bash
export PYTHONPATH="/XXX/gpu-queue-panel-rc${PYTHONPATH:+:$PYTHONPATH}"
python train.py --queue --queue-gpu 0 --queue-session-file /XXX/alice.session.json --epochs 20
```

所有托管参数以 `--queue-` 为前缀。登记后启动器退出，执行器使用去除托管参数的命令；不传 `--queue` 时保持原行为。

## 查询、排序、取消与日志

以下在服务器 RC 目录执行；也可用同一套命令经自己的 SSH 终端调用：

```bash
python cli.py --session-file /XXX/alice.session.json status
python cli.py --session-file /XXX/alice.session.json reorder alice-task-c alice-task-a
python cli.py --session-file /XXX/alice.session.json cancel alice-task-a
python cli.py --session-file /XXX/alice.session.json logs alice-task-c --cursor 0
python users.py --session-file /XXX/alice.session.json logout
```

排序需包含当前账号可管理的**全部等待 ID**；其他用户的位置保留。日志按字节游标、最多 32768 字节/次返回，只读已登记任务自己的 output.log，不接受任意路径。网页不提供任意命令提交，SSH RPC 的 submit 仅供已登录 CLI 使用。

Codex 托管流程：先查看 RC 状态，保持授权预算和稳定 ID，用有效账号会话登记并复核；不重复启动、不自动重试失败项、不擅自接管现有训练。退出码 0 不等于科学验收通过，原有审计应保留在任务入口中。
