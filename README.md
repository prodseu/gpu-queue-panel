# GPU tmux dispatch v0.6.0

预存训练命令，分发到固定 tmux 终端或新建任务窗口。用终端面板查看 GPU 显存、计算利用率、只读进程与当前命令清单。

新版使用一个 `current.json` 保存命令、设置和运行状态。标准库即可运行，依赖 Linux、Python 3.10+、tmux 与 NVIDIA 驱动的 `nvidia-smi`。

## 启动

在服务器操作，将 `XXX` 替换成自己的路径。

```bash
tmux new-session -d -s zhp1
cd /XXX/gpu-queue-tmux
python3 tmux_dispatch.py init --root /XXX/zhp --session zhp1
python3 tmux_dispatch.py start
tmux attach -t zhp1:gpu-monitor
```

已有 `zhp1` 时跳过第一行。`start` 在该会话中新建 `train` 固定训练终端和 `gpu-monitor` 面板。默认手动模式。训练命令从配置的 `zhp` 根目录出发，支持 `cd project && /XXX/env/bin/python train.py ...`。tmux 按 Ctrl+B 再按 D 可离开并保留面板运行。

`current.json` 是唯一持久 JSON。两个空锁文件用于并发写入和单实例约束。GPU 数据仅在内存中刷新，训练输出保留在任务 tmux 终端。程序重启保留当前清单与终态，`clear` 可清除已结束项。旧版 v0.5.0 的网页登录、历史记录与科学任务适配器留在原 Git 标签中。

## 预存并分发命令

用引号包住完整的 shell 命令。保存后返回 ID，命令由面板领取。

```bash
# 默认分发到固定 train 终端
python3 tmux_dispatch.py add --gpu 1 --peak-mib 16384 \
  'cd project/model && /XXX/env/bin/python train.py --epochs 20'

# 每个任务独立新开小窗口，适合多卡并行
python3 tmux_dispatch.py add --gpu 2 --target new --peak-mib 8192 \
  'cd project/other && /XXX/env/bin/python train.py --epochs 10'

# 新开空终端，返回工具管理的 pane ID，例如 %8
python3 tmux_dispatch.py new-shell
python3 tmux_dispatch.py edit ID --target %8

python3 tmux_dispatch.py dispatch ID
python3 tmux_dispatch.py mode auto
python3 tmux_dispatch.py mode manual
python3 tmux_dispatch.py pause on
python3 tmux_dispatch.py pause off
```

手动模式按 Enter 或 `dispatch ID` 请求分发。自动模式按清单顺序检查各 GPU 的首条等待命令，某张卡受限时可以检查其他卡。固定终端串行复用，独立新窗允许按每卡窗口并行。固定终端有活动命令时会保留新命令等待，由操作者在手动模式请求或由自动模式接续。

两种模式都检查每卡窗口、GPU 存在和显存准入设置。切换模式或暂停会清除未领取的手动请求。退出面板 `q` 后停止继续分发，已经打开的训练终端继续执行。

## 面板按键

|按键|操作|
|---|---|
|c|保存命令并选择 GPU、预计峰值显存|
|Enter|分发选中的等待命令|
|a / p|切换自动/手动，暂停/恢复分发|
|j / k、方向键|选择命令|
|g / m / e / t|修改等待命令的 GPU、峰值预算、命令文本、目标终端|
|l|修改全局资源设置|
|J / K|调整等待命令顺序|
|x|取消选中的等待命令|
|n|新建空 tmux 小窗口，返回 pane ID|
|d|清除已结束、失败、取消或中断项|
|q|退出监控与后续分发|

长命令推荐通过 CLI 预存。面板按 `t` 输入 `new` 可切为独立任务窗，输入工具创建的 `%编号` 可切为固定终端。默认固定终端的编号保存在 `current.json` 的 `default_target` 中。

## GPU 与显存设置

```bash
python3 tmux_dispatch.py edit ID --gpu 2 --peak-mib 24576
python3 tmux_dispatch.py up ID
python3 tmux_dispatch.py down ID
python3 tmux_dispatch.py cancel ID
python3 tmux_dispatch.py limits --window 2 --reserve-mib 8192 --memory-percent 90 --utilization -1
python3 tmux_dispatch.py show
python3 tmux_dispatch.py clear
```

GPU 编号在保存/修改时解析为 UUID。执行环境的 `CUDA_VISIBLE_DEVICES` 设置为该 UUID，程序内部使用逻辑 GPU 0。选择 `--gpu cpu` 时隐藏 CUDA 设备。训练代码须遵循环境中的设备选择，请同步修改命令文本中自带的 GPU/config 参数。

`window` 按每张卡计算，CPU 独立计数。`peak-mib` 是命令预计峰值预算，`reserve-mib` 是每卡预留空间，`memory-percent` 是整卡显存使用上限，`utilization` 是计算利用率准入阈值。每项都允许 `-1` 关闭对应限制。现有任务尚未分配的峰值预算也计入准入计算。

显存设置控制启动时机。任意 shell/Python 命令的运行期显存需由程序本身控制，准入阈值无法强制限制分配器。手动与自动模式采用相同检查。缩小窗口后在已有命令结束、容量满足时继续分发。

外部进程仅读取 NVIDIA 提供的 PID、名称、显存，并标为只读。面板基于当前 SSH/Unix 账户运行。只有工具创建并记录身份的固定终端可以接收命令。失败、中断的命令保留终态，后续项可接续。重启发现遗留进程时显示状态并保留资源预算，需要操作者核对。

## 检查

```bash
python3 -m unittest test_dispatch -v
```

检查在临时目录和随机命名的独立 tmux 会话中执行 CPU 短命令，覆盖固定终端、新窗口、模式、取消/排序、GPU 环境、资源限制、一次性分发和并发写入。
