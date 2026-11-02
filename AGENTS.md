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
