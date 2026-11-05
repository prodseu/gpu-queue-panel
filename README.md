# GPU Queue Panel — 0.5.0-rc.2

多用户登录预发布版。每个程序归属于提交它的账号；普通用户查看、排序、取消自己的等待任务，管理员查看全部任务并设置全局并发和资源保护。仍通过本机网页 + SSH 工作，服务器不开放 HTTP 端口。

**本版使用独立目录和数据，不迁移、不接管当前队列。不要覆盖正在使用的工具或复用其配置。** 默认数据目录 `runtime-data-rc`、本机端口 `8789`、tmux 会话 `gpu-queue-panel-rc`。未标记为多用户 RC 的旧队列会被拒绝。

## 权限范围

| 能力 | 普通用户 | 管理员 |
| --- | --- | --- |
| 查看任务与登记日志 | 本人 | 全部 |
| 取消、拖拽、上下调整 | 本人等待项 | 全部等待项 |
| 修改全局并发窗口、资源保护 | 只读 | 可以 |
| 创建/停用账号、重置密码、分配等待任务 | 不可以 | 服务器 CLI |
| 未纳管 GPU 进程 | NVIDIA 名称/PID/显存，只读置顶 | 同样只读 |
| 终止运行任务、操作外部进程 | 不提供 | 不提供 |

归属与权限在服务器端校验。用户重排只交换其任务占据的位置，其他用户的全局位置保持。客户端不能指定或伪造任务归属。无归属任务不会自动归给第一个登录者，也不会启动。

**这是应用/API 层隔离，不是操作系统隔离。** 训练仍由配置的同一个 Unix 账号执行；有该系统账号权限的人可以访问其文件，任意训练代码也拥有该账号权限。仅适合可信团队的任务管理。面对不受信任的代码/用户，应先部署独立 Unix 账号、容器或 Slurm；本 RC 不宣称解决这类隔离。

## 独立试用

要求：Python 3.10+；Linux 执行端具备 SSH、tmux、NVIDIA 驱动/nvidia-smi；前端无需构建。服务运行仅用 Python 标准库。

1. 将本包分别解压到本机和服务器的**新目录**，例如服务器 `/XXX/gpu-queue-panel-rc`。两端复制 `config.example.json` 为 `config.json`，填写 SSH 别名、RC 目录、Python 路径。保留 RC 数据目录、端口及会话名，不引用生产配置。
2. 在服务器 RC 目录初始化管理员，密码交互输入（至少 12 字符，无默认账号密码）：

   ```bash
   python users.py bootstrap operator
   python users.py add alice
   python users.py add bob
   ```

   `bootstrap` 只允许空账号库，并创建空 RC 队列。不要复制生产运行数据或认证库。管理员 CLI 会话默认保存在 `~/.gpu-queue-panel-rc/session.json`。
3. 服务器启动专用监控：

   ```bash
   bash start-server.sh             # 仅监控，不执行任务
   ```

   新队列准备好且明确要执行后，选择 `bash start-server.sh --execute`。已有同名 tmux 不会再启动或切换模式；不要为了试用重启正式会话。
4. 本机双击 `Start-Queue.cmd`，或运行 `python launch.py`，打开 `http://127.0.0.1:8789/` 后登录。每位用户在自己的电脑配置 SSH 连接，使用管理员分配的面板账号。浏览器不提供新建训练或任意命令入口。

登录后自动识别 GPU，支持按卡和总队列查看；全卡共享 NVIDIA 利用率与显存信息。服务器持续保留最近 300 秒曲线，重新打开即可加载，首次部署需要积累真实采样，缺口不补造。未纳管进程置顶并标“不可操作”；其他用户的任务名、进度、工作目录、日志不会通过个人视图返回，其他用户 GPU 进程只显示 NVIDIA 元信息。

## 登录后提交程序

服务器终端的登录独立于浏览器。账号归属从有效会话产生，不使用 `--owner`：

```bash
python /XXX/gpu-queue-panel-rc/users.py login alice
python /XXX/gpu-queue-panel-rc/qrun --gpu 0 -- python train.py --epochs 20
python /XXX/gpu-queue-panel-rc/cli.py status
```

同一 Unix 账号同时使用多个面板账号时，必须为各自会话指定不同文件，避免覆盖：

```bash
python users.py --session-file /XXX/alice.session.json login alice
python qrun --session-file /XXX/alice.session.json --gpu 0 -- python train.py
```

会话文件就是凭据，不能共享、提交 Git 或放进发行包。CLI 会话文件在 POSIX 上以 `0600` 创建；同一 Unix 账号仍可以读取它，文件名不是隔离措施。Windows 上保护取决于本机 ACL。默认自动任务 ID 添加用户名；显式 ID 全局唯一。同一用户重复提交同一 ID 和内容不重复排队，失败/取消项也不自动重跑。

更多一行命令、原脚本 `--queue` 接入和日志读取见 [CLI_USAGE.md](CLI_USAGE.md)。管理员可用 `python users.py assign TASK_ID alice` 分配新 RC 中已有的等待项；不支持改变运行项归属。停用账号或无归属的队首会阻塞后续 FIFO，由管理员分配或取消该项，工具不静默跳过。

## 登录安全与调度

密码以独立盐值 + scrypt 保存；服务端只保存会话令牌摘要；会话 8 小时过期，退出、重置密码、停用账号会撤销对应会话。连续 5 次登录失败锁定该账号 60 秒。浏览器使用 HttpOnly / SameSite=Strict Cookie，写操作校验 CSRF，令牌不放 URL、localStorage 或 sessionStorage。本机仅监听回环，凭据通过 SSH 到服务器；不要将回环 HTTP 服务暴露给公网。

全局窗口默认 2，管理员可设 1–8 或 `-1`；显存、计算阈值、冷却等设置各自支持 `-1`。计算利用率默认不限；未知进程占卡时仍阻塞领取。缩窗不发信号，运行项自然结束；默认失败记录本项后继续领取，不自动重试。资源保护仅控制新启动，不是 GPU 硬配额，显存峰值需由任务提交者如实预估。

## 检查与预发布限制

```bash
python -m unittest discover -v
node --check web/app.js
# 可选浏览器检查（需 Playwright + Chromium）
python qa_multiuser.py
```

检查使用临时数据、模拟 NVIDIA 和本机临时端口，包含真实 CPU 子进程；不会连接服务器或启动训练。当前本地 Windows 已通过后端/HTTP 与三用户 Chromium 检查，Linux 专用命令入口/真实 tmux 和实际 GPU 集成仍需在独立 Linux 环境验证。检查结果见 [PRE_RELEASE.md](PRE_RELEASE.md)。

本版不提供注册、找回密码、SSO、运行项强停、用户配额/公平调度或 OS 沙箱。管理员在服务器用 `users.py password USER` 重置密码。账号停用后其等待任务保留，运行任务自然完成。不要将 RC 直接覆盖部署到当前操作目录。

## 等待任务切换 GPU

等待 GPU 任务的设备栏提供 GPU 下拉框，选择目标卡后立即保存。后端查询当前 NVIDIA 设备并按 UUID 关联，任务保留原有全局位置和所属账号。按卡查看时，移走的任务会从当前卡列表消失，可在目标卡或总队列查看。普通用户只能切换自己的等待项，管理员可以管理全部等待项。CPU任务及运行/取消/完成任务不能在这里转换设备。

```bash
python cli.py move-gpu TASK_ID GPU-XXX
```

切换后仍需通过目标卡的显存、并发、外部进程和启动间隔检查。程序应遵循 CUDA_VISIBLE_DEVICES，训练内部使用逻辑卡号。通用 argv 包装器会设置所选 GPU UUID。私有科学执行器可在启动前调用 gpu_transfer.prepare_scientific_config，更新已登记任务配置中的 gpu/gpu_uuid/authorized_physical_gpu，并保存原配置备份。提交参数、预算、模型与等待序保持。

本次 Linux 隔离集成 40 项检查通过，包含 qrun、CLI、train.py --queue、登录权限、GPU切换竞态与真实CPU任务。三用户 Chromium 检查覆盖等待任务切换后按卡刷新。实际 GPU 训练切卡仍取决于各训练程序是否遵循设备约定。
