# Queue operations

- Keep server/user-specific values in ignored config.json. Use XXX in shared examples.
- Never commit runtime data, task logs, credentials, screenshots or local machine paths.
- Inspect status before and after cancel/reorder; use stable task IDs.
- Only waiting tasks may be canceled or reordered. Do not signal running jobs.
- Every execution reads the current queue; do not add a second fixed-order launcher.
- Do not add implicit dependencies between neighboring tasks.
- Keep the viewer local-only and use SSH for server access.
- GPU cards filter the same queue by UUID; per-GPU reordering must merge all waiting IDs and preserve the slots of other GPUs/CPU tasks. These views do not imply parallel execution.
- The execution worker stays idle when the queue is empty; later explicitly authorized entries may be appended via QueueStore.change without restarting it.
- Observed GPU processes are read-only runtime rows, never executable queue entries. Inspect command lines only for explicitly registered, own-UID identities matching PID/start_ticks/boot. Unknown processes, including same-UID processes, use NVIDIA metadata only; never inspect their directories, environment, logs or Python installations.

- 2026-11-03用户授权并发窗口默认2、网页调节和资源保护，覆盖此前仅顺序执行约定。仅一个Dispatcher领取；running兼容字段为一个活动ID，完整活动集以tasks中state=running为准，所有视图使用完整活动集。缩窗不发信号、不重跑、不改模型/预算。未知进程仍阻塞目标卡，峰值预算与利用率仅作启动保护，不保证运行期硬限制。
- 用户最新已明确授权完成命令入口/文档后push，本轮允许提交并同步正式Git发布版；保持所有服务器信息脱敏。

- 用户后续要求计算利用率默认不限制、每项限制以-1关闭。所有配置逐项判断；MiB/GiB转换不得把-1变成-1024；窗口默认2不变。利用率-1也跳过稳定历史门，其余显存/未知进程规则独立生效。Git发布按用户本轮明确授权执行。

- Default workflow: submit every future user-authorized training through qrun/queue_submit (or the optional early --queue hook) into the same QueueStore. Inspect live status first; retain finite budgets, original scientific validation and stable IDs. Do not launch standalone train.py/nohup/screen loops or duplicate schedulers unless the user explicitly requests an exception.
- CLI enrollment happens on the server only. Keep arbitrary-command submission out of the browser/state RPC. Preserve argv/cwd/selected interpreter; registered command tasks use their own foreground runner/log/identity/exit receipt. Do not auto-adopt existing or foreign processes.
- Repeated identical submissions return the same job. Use a new explicit ID only for a newly authorized run; default queue management grants no extra epochs/seeds/retries/GPU access. Never submit a real training merely to test enrollment.
