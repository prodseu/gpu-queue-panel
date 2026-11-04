# 一行命令托管的实现方案调研

本文件为包装方式比较。0.5.0-rc.1 的所有提交需要先登录，账号归属由服务器会话决定；RC 安装与安全边界以 README/CLI_USAGE 为准。

推荐采用命令包装器。当前已实现qrun、继承单卡CUDA_VISIBLE_DEVICES和可选train.py --queue接入，不需要改Python解释器或全局注入启动钩子。

| 方案 | 示例 | 是否改训练代码 | 实现与适用性 |
| --- | --- | --- | --- |
| 命令包装器（已实现） | `qrun --gpu 2 -- python train.py ...` | 不需要 | 像screen一样增加命令前缀，但仅登记argv/cwd/环境；常驻队列之后执行，统一管理顺序/窗口/日志 |
| 环境变量＋包装器（已实现） | `CUDA_VISIBLE_DEVICES=2 qrun python train.py ...` | 不需要 | 包装器读取环境变量并解析UUID；worker在真正执行前设置CUDA可见设备 |
| 训练脚本参数（已实现可选接入） | `python train.py --queue --queue-gpu 2 ...` | 一次小接入 | 在解析训练参数和初始化CUDA前检测--queue，登记去掉托管参数的子命令后退出 |
| 只设环境变量 | `CUDA_VISIBLE_DEVICES=2 python train.py ...` | 无排队功能 | CUDA仅筛选可见GPU，不负责登记、延迟启动、取消和排队；要托管仍需程序/包装器读取约定 |
| sitecustomize/PYTHONPATH注入 | `QUEUE=1 python train.py ...` | 脚本可不改，但需注入环境 | Python site启动钩子可在脚本前运行；影响同环境所有Python启动，-S可绕过，模块执行/子进程行为也要适配，不作为本工具默认方案 |
| screen/tmux原生命令 | `screen -dmS train python train.py` | 不需要 | 提供持久终端/脱离SSH，启动命令本身；没有本工具所需的个人统一队列和显存门，外包一层qrun才能统一领取 |
| GPU Task Spooler | `ts -G 1 python train.py ...` | 不需要 | 现成GPU/CPU排队工具，-S调并发；需要迁移现有清单、资源策略和网页，不宜与当前工具同时领取同一任务 |
| Slurm | `sbatch --gres=gpu:1 --wrap='python train.py'` | 不需要 | 集群调度器可统一申请GPU资源，适合管理员部署的多用户集群；当前个人SSH工具无需新增控制平面 |

## 为什么任意 train.py 不能天然识别 --queue

Python把脚本名之后的参数交给脚本本身。`--queue`不是Python或CUDA标准开关，未修改的脚本可能直接报未知参数，也不会通知当前队列。可行的透明方案是命令前缀，或者为脚本/解释器环境加入接入钩子。为保持现有训练环境，当前选前缀包装器作为默认。

包装器直接使用argv数组和subprocess.Popen，不经隐式shell；提交时解析当前python入口和GPU UUID，保存工作目录及少量执行环境，再通过同一QueueStore锁追加。真正执行者只有原常驻Dispatcher，因而排序、取消、窗口及-1保护设置继续共享一份状态。它不会把已运行的陌生进程转为可操作任务。

## 参考资料

- [Python命令行与脚本参数](https://docs.python.org/3/using/cmdline.html)：脚本后的参数交给sys.argv。
- [NVIDIA CUDA环境变量](https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/environment-variables.html)：CUDA_VISIBLE_DEVICES控制设备可见性及顺序。
- [GNU Screen命令](https://www.gnu.org/software/screen/manual/html_node/Screen-Command.html)：在终端窗口中启动指定程序。
- [Python site启动钩子](https://docs.python.org/3/library/site.html)：sitecustomize/usercustomize及-S行为。
- [GPU Task Spooler源码与用法](https://github.com/justanhduc/task-spooler)：GPU任务及并发槽位设置。
- [Slurm sbatch](https://slurm.schedmd.com/sbatch.html) 与 [GRES GPU调度](https://slurm.schedmd.com/gres.html)：集群资源请求和包装命令。

调研只读文档，未安装其他调度器、替换系统python、注入sitecustomize或更改训练环境。
