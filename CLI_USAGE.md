# 一行托管训练：qrun 与 --queue

推荐不改训练代码，像screen一样在原命令前加`qrun`。在服务器当前训练环境、项目目录内运行：

```bash
qrun --gpu 2 -- python train.py --epochs 20 --batch-size 16
# 或沿用熟悉的单卡环境变量写法
CUDA_VISIBLE_DEVICES=2 qrun python train.py --epochs 20 --batch-size 16
```

两者都立即登记并返回任务ID；训练由现有常驻调度器按队列顺序、窗口和显存保护执行。提交进程不占CUDA、不新建调度器；关闭SSH不会停止已入队任务。GPU编号在提交时解析为UUID，训练内部使用逻辑GPU0。当前仅支持每任务一张GPU或CPU，多卡CUDA_VISIBLE_DEVICES需明确指定一张或使用其他调度方案。

## 一次安装

服务器先按README配置并初始化队列，启动`start-server.sh --execute`。已有队列不要重复init；可以用包含`{"tasks": [], "order": []}`的JSON初始化空队列。只监控模式不会执行新任务。

```bash
chmod +x /XXX/gpu-queue-panel/qrun
mkdir -p "$HOME/.local/bin"
ln -s /XXX/gpu-queue-panel/qrun "$HOME/.local/bin/qrun"
export PATH="$HOME/.local/bin:$PATH"
```

最后一行可加入自己的shell配置。只安装本人PATH入口，不替换系统python。也可以不安装，直接用绝对路径：

```bash
python /XXX/gpu-queue-panel/queue_submit.py --gpu 2 -- python train.py --epochs 20
# 等价统一CLI入口
python /XXX/gpu-queue-panel/cli.py submit --gpu 2 -- python train.py --epochs 20
```

`--`之前为托管参数，之后完整保留原训练参数、空格和引号；不用把整条训练命令拼成shell字符串。

## 常用参数

```bash
qrun --gpu 2 --id exp-a-001 --name "实验 A" \
  --peak-memory-mib 24576 --status-file run/STATUS.json \
  --progress-key updates --updates 5000 --epochs 20 \
  -- python train.py --epochs 20

qrun --cpu -- python prepare_data.py
qrun --gpu GPU-XXX --cwd /XXX/project --env OMP_NUM_THREADS=2 -- python train.py
```

- `--gpu`：NVIDIA物理编号或完整UUID；未指定时可继承单卡`CUDA_VISIBLE_DEVICES`。`--cpu`显式选择CPU。
- `--cwd`：工作目录，默认当前目录；命令的python路径从当前训练环境解析为绝对路径，保留虚拟环境的解释器入口。
- `--id`：稳定任务ID。默认由命令、目录、GPU及执行设置生成；重复提交返回原任务，不重复排队，已完成/取消/失败也不自动重跑。有意再训练需显式给新ID。
- `--peak-memory-mib`：该任务预计训练/验证峰值显存，MiB；不填沿用网页默认值，`-1`不额外预留预算。
- `--status-file`、`--progress-key`、`--updates`、`--epochs`：可选进度信息；普通程序不提供也能排队，但不会凭空生成准确训练百分比。
- `--completion-file`：可选退出后额外验证JSON的`complete: true`；不指定时以退出码0完成。
- `--env KEY=VALUE`：显式添加训练环境变量，可重复。默认仅保留PATH/PYTHONPATH/LD_LIBRARY_PATH/CONDA_PREFIX/VIRTUAL_ENV，不把全部shell环境写进清单。
- `--data`：覆盖队列数据目录；默认使用配置中的data_dir，现用私有工具默认personal-data。

命令必须前台运行。不要再套nohup、screen、tmux或`&`让父命令提前退出。stdout/stderr保存在队列数据目录的`jobs/<ID>/output.log`，启动身份与退出码分别在`launch.json`、`receipt.json`。取消/排序只操作等待项。提交只限服务器CLI，网页和SSH状态RPC仍不提供任意命令提交入口。

## 直接 python train.py --queue（可选）

如果希望参数就接在原训练脚本后面，一次性在脚本开头加入（放在`__future__`导入之后、torch导入/参数解析/CUDA初始化之前）：

```python
from queue_submit import enqueue_if_requested

if __name__ == "__main__":
    enqueue_if_requested()
```

让该训练环境能找到模块，再一行提交：

```bash
export PYTHONPATH="/XXX/gpu-queue-panel${PYTHONPATH:+:$PYTHONPATH}"
python train.py --queue --queue-gpu 2 --epochs 20 --batch-size 16
# 同样可继承显卡环境变量
CUDA_VISIBLE_DEVICES=2 python train.py --queue --epochs 20
```

队列参数统一带`--queue-`前缀（如`--queue-id`、`--queue-data`、`--queue-peak-memory-mib`），避免与训练脚本原有`--gpu`/`--epochs`冲突。启动器登记后退出；托管子进程会移除这些参数并按原参数运行，防止循环入队。不加`--queue`时函数不修改原参数。无需改训练代码时直接选qrun。

## Codex默认流程

用户已授权的后续训练，默认先查队列和GPU状态，准备独立目录/有限预算与稳定ID，再用qrun/submit追加到同一清单；核对返回ID和status。不直接nohup/train.py绕过队列，不另开等待器、不重复登记或恢复失败/取消项。实验有CPU审计/科学验收时，应让原前台训练入口完整执行这些步骤或保留已有专用任务适配器；通用命令退出码0不等于科学收益通过。

默认托管不代表自行增加训练预算、轮数、seed、占用其他人的GPU或新增未经授权任务。已有训练不会自动迁移或被重复接管。
