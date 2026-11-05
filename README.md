# GPU Queue Panel 0.5.0

通过本机浏览器和 SSH 管理 Linux 服务器上的 GPU 训练队列。服务器常驻 tmux，持续保存最近 5 分钟显存与计算利用率曲线。Python 3.10+ 标准库后端，前端无需构建。

- 多用户登录，程序归属由服务器会话确定，普通用户管理本人任务，管理员管理全局设置。
- 自动／手动模式和领取开关。关闭领取后，运行程序继续完成。
- 等待任务可以切换 GPU、独立取消、拖拽排序，支持按卡与总队列查看。
- 自动并发窗口按每张 GPU 分别计算，CPU 独立计数，单卡阻塞时继续检查其他卡。
- 外部 GPU 进程只读置顶，显示 NVIDIA 提供的名称、PID、Python 路径与显存。
- `qrun` 一行登记，保留原解释器、argv、目录、日志、身份和退出码，失败后继续后续任务。

## 开始使用

两端需要 Python 3.10+ 与 SSH。服务器还需要 tmux、NVIDIA 驱动和 nvidia-smi。

1. 将源码分别放到本机和服务器，例如 `/XXX/gpu-queue-panel`。两端复制 `config.example.json` 为 `config.json`，填写 SSH 别名、服务器源码目录、Python 路径，保留其余默认值。真实配置已被 Git 忽略。
2. 在服务器项目目录创建管理员和用户，密码交互输入，至少 12 个字符。

   ```bash
   python users.py bootstrap operator
   python users.py add alice
   python users.py add bob
   ```

3. 服务器选择一种运行方式。

   ```bash
   bash start-server.sh             # 监控和采样
   bash start-server.sh --execute   # 执行已授权、已登记的任务
   ```

   已有同名 tmux 时脚本会保留其运行方式。账号和空队列由 bootstrap 初始化，不重复导入正在运行的训练。
4. 本机双击 `Start-Queue.cmd`，或运行 `python launch.py`，打开 `http://127.0.0.1:8789/` 后登录。

默认数据目录 `runtime-data-rc`、会话 `gpu-queue-panel-rc` 和 CLI 会话目录 `~/.gpu-queue-panel-rc` 沿用预发布版命名，以兼容已有配置。可以在 config.json 中自定义端口、数据目录和 tmux 名。多用户 RC 用户升级时保留自己的配置和账号库，先等待执行器空闲再更新程序。0.4.x 的单用户数据缺少账号归属，本版会拒绝直接接管，需在独立目录创建新多用户队列。

## 一行登记训练

先在服务器终端登录，随后从训练项目目录提交。

```bash
python /XXX/gpu-queue-panel/users.py login alice
python /XXX/gpu-queue-panel/qrun --gpu 0 -- python train.py --epochs 20
CUDA_VISIBLE_DEVICES=0 python /XXX/gpu-queue-panel/qrun python train.py --epochs 20
```

同一 Unix 账号使用多个面板账号时，为各账号指定独立会话文件。

```bash
python users.py --session-file /XXX/alice.session.json login alice
python qrun --session-file /XXX/alice.session.json --gpu 0 -- python train.py
```

网页登录与终端登录各有独立会话。默认任务 ID 带用户名，显式 ID 全局唯一。相同 ID 与内容重复提交返回原任务，失败或取消后仍不会自动重复训练。

更多参数、原脚本 `python train.py --queue` 接入和日志读取见 [CLI_USAGE.md](CLI_USAGE.md)。

## 网页操作

自动模式按各卡的等待顺序和资源保护领取。窗口默认 **每卡 2 项**，例如设为 3 时，GPU0 和 GPU1 可分别最多运行 3 项。CPU 使用独立队列和同一窗口值。总队列提供统一排序视图，卡内顺序由它派生。某张卡满窗、显存不足或被资源门阻塞时，其他卡仍可领取各自队首。

手动模式等待用户逐项点击“启动”，可以选择非队首任务。手动启动跳过自动显存、并发窗口、占用和启动间隔检查，由操作者决定共用时机，服务器继续校验账号权限、等待状态及目标 GPU 存在。

管理员可切换模式和“关闭领取／开启领取”。关闭领取或切换模式会撤销尚未领取的手动请求，运行中的程序继续自然收尾。

等待 GPU 任务的设备栏可以选择目标卡，后台实时查询 UUID，保留任务归属和全局位置。切卡后的命令通过统一入口同步已登记 config.json 中的 `gpu`、`gpu_uuid`、`authorized_physical_gpu`，保存原配置备份，再执行原解释器和参数。程序应遵循 `CUDA_VISIBLE_DEVICES`，内部使用逻辑 GPU 编号。运行中任务和只读进程不提供切卡操作。

普通用户的重排只交换本人任务占据的位置，其他用户位置保持。取消仅作用于所选等待项。

## 账号与权限

| 操作 | 普通用户 | 管理员 |
| --- | --- | --- |
| 查询任务和日志 | 本人 | 全部 |
| 取消、排序、切卡和手动启动 | 本人等待项 | 全部等待项 |
| 全局模式、窗口与资源设置 | 查看 | 修改 |
| 创建账号、重置密码、分配等待任务 | 无 | 服务器 CLI |
| 外部 GPU 进程 | NVIDIA 元信息，只读 | NVIDIA 元信息，只读 |

```bash
python users.py list
python users.py password alice
python users.py disable alice
python users.py assign TASK_ID alice
```

会话有效期 8 小时。退出、重置密码、停用账号会撤销相应会话。账号停用或无归属的任务会阻塞该设备队列，管理员可分配或取消等待项，其他设备继续接续。

密码使用独立盐值与 scrypt，服务端保存会话令牌摘要。浏览器使用 HttpOnly / SameSite=Strict Cookie，写操作校验 CSRF。接口在读取任务文件及修改队列前校验归属。所有真实配置、认证库、会话和运行数据留在各自机器上。

面板账号提供应用权限管理。训练仍由配置的 Unix 账号执行，共享同一 Unix 账号的人和任意训练代码具有其文件权限。需要操作系统级隔离时，采用独立 Unix 账号或容器。

## 资源保护与记录

自动模式默认保留 8 GiB 显存，显存上限 90%，每任务预计峰值 24 GiB，计算利用率默认 `-1`，同卡启动间隔 60 秒。所有限制逐项支持 `-1`。缩小窗口只影响补位，已有运行任务继续。资源设置控制启动时机，运行期显存峰值由训练程序决定。

服务器采样持续保存最近 300 秒曲线，重新打开网页即可加载已有缓存。首次启动需要积累真实采样，数据断档留空。外部 GPU 进程只读取 NVIDIA 元信息，未知进程不读取 argv、cwd、环境或日志。自动模式遇到未纳管占用会等待，手动模式可由用户选择共用。

每项命令的 stdout/stderr、启动身份和退出回执分别保存在数据目录的 `jobs/<ID>/output.log`、`launch.json`、`receipt.json`。训练进度可通过 `--status-file` 与 `--updates` 接入。普通退出码 0 与科学验收结果应分别解释。

## 检查

```bash
python -m unittest discover -v
node --check web/app.js
python qa_multiuser.py            # 需要 Playwright 与 Chromium
```

检查在临时队列运行，包含真实 CPU 子进程和模拟 NVIDIA 信息。版本检查结果见 [VALIDATION.md](VALIDATION.md)。
