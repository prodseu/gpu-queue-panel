# GPU Queue Panel

一个轻量的个人GPU任务队列面板。Windows本机打开网页，通过SSH查看Linux服务器；服务器不开放网页端口。后端仅用Python标准库，前端无需构建。

功能：自动识别GPU、点击显卡查看该卡个人队列、总队列、显存/计算利用率、服务器持续缓存的5分钟曲线、取消单个等待项、拖拽排序、空队列持续待命。所有示例中的服务器信息均为`XXX`，真实配置和运行数据不进入Git。

## 快速使用

环境：两端Python 3.10+和SSH；服务器安装tmux、NVIDIA驱动/nvidia-smi。Windows端可直接双击启动脚本。

1. 把源码放到本机及服务器。两端都复制`config.example.json`为`config.json`，替换`XXX`：

   ```json
   {
     "ssh_host": "XXX",
     "remote_dir": "/XXX/gpu-queue-panel",
     "remote_python": "/XXX/bin/python"
   }
   ```

   以上是要修改的主要字段；其余字段保留模板值。`ssh_host`填写自己已配置免密登录的SSH别名，`remote_dir`填写服务器源码目录。`config.json`已被Git忽略。

2. 在服务器复制`examples/queue.example.json`为`queue.local.json`，填写每项已授权任务的工作目录、命令参数、GPU UUID、进度字段和实际总更新数，再导入：

   ```bash
   python3 cli.py init queue.local.json
   ```

   导入不会执行命令，也不会接管已有进程。不要把正在运行的训练重复导入。任务ID保持稳定；参数使用JSON数组，命令保持前台运行，不再套tmux/nohup。CPU任务设`gpu: null`、`gpu_uuid: ""`。可选`completion_file`存在时要求其JSON包含`complete: true`。

3. 在服务器启动专用tmux：

   ```bash
   bash start-server.sh                 # 仅监控，不启动任务
   bash start-server.sh --execute       # 明确执行刚导入的任务
   ```

   两种模式二选一；已有同名会话时脚本不会再启动或切换模式。也可直接运行`python3 server_agent.py run --execute`。执行器按最新清单顺序运行，一项结束后再取下一项；GPU有其他计算进程时等待。不会结束他人任务或自动重跑失败任务。训练程序应遵循`CUDA_VISIBLE_DEVICES`，使用逻辑GPU编号。

4. 本机双击`Start-Queue.cmd`。也可在终端运行：

   ```powershell
   python launch.py
   python cli.py console
   ```

   图形界面仅监听本机`127.0.0.1`。服务器持续采样并保存5分钟曲线，关掉本机仍记录，重新打开直接加载。首次部署先积累5分钟真实数据；断档留空。绿线为GPU-Util，蓝线为显存占用比例，均为0—100%；GPU-Util不是逐SM硬件occupancy。

启动后根据服务器快照自动显示实际显卡数量。点击任意显卡卡片，查看自己在该卡运行和等待的任务；点击“总队列”查看全部当前任务，包括CPU任务。卡片显示运行/等待数量，选择在刷新后保留，按GPU UUID关联任务。

按卡查看时，上下移动或拖拽只交换该卡任务占据的等待位置，其他卡和CPU任务的位置保持；“总序”是其全局等待序号。总队列中可以统一调整所有等待项的顺序。以上是同一份队列的不同视图，执行仍为顺序执行。

执行模式没有领取截止时间，队列跑空后持续待命。之后已授权任务经同一QueueStore锁事务加入即可接续，无需重启；不自动新增训练、重跑失败项或增加训练轮数。

## Codex / 命令行

先查看实际ID和顺序，修改后再查看确认：

```powershell
python cli.py status
python cli.py cancel task-b
python cli.py reorder task-c task-a
```

服务器对应命令为`python3 cli.py status/cancel/reorder`。排序必须给出全部剩余等待ID。取消B只移除B，不影响A/C；运行中的项不能在这里终止或重排。文件锁和原子写入保证图形界面、Codex和执行器使用同一队列。

## 范围与验证

这是**单服务器顺序队列**，不同任务可指定不同GPU；尚未提供多GPU并行调度、显存共享或跨服务器调度。未知/占用GPU不会被自动抢占；默认失败后停止后续执行，等待项保留，`stop_on_failure: false`可改为继续独立任务。重启发现旧运行项时不会自动重跑，需要先人工检查。

```bash
python -m unittest discover -v
node --check web/app.js
```

可选界面检查需安装Playwright及Chromium，运行`python qa_gpu_views.py`。检查使用隔离清单和本机临时端口，验证按卡/总队列、重排保留其他卡位置、取消、GPU数量与编号变化，不访问真实SSH或GPU。

`runtime-data/`、`client-data/`、本机配置、令牌和日志都应留在各自机器上。自定义数据目录时也应加入`.gitignore`。训练脚本、模型、数据、实验专用收尾代码及运行截图不随源码发布。
