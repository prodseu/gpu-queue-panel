# Multiuser RC working rules

- This branch is 0.5.0-rc.2, an isolated preview. Never deploy/restart/modify the current production queue or push this branch to stable main implicitly.
- Use new source/data/config directories, port 8789 and tmux gpu-queue-panel-rc. Refuse non-RC data; do not migrate production jobs automatically.
- Every authorized training uses authenticated qrun/submit or the early --queue hook; preserve finite budgets, validation and stable IDs. Do not run a real training to test enrollment.
- Server RPC must authenticate and check ownership before reading progress, process identities or logs and before changing queues. Never trust client owner/role fields.
- Ordinary users reorder only their own slots; preserve all foreign slots. Cancel only own waiting tasks. Admin manages all waiting items and global settings; nobody signals running or external jobs.
- External GPU processes are read-only, NVIDIA metadata only, visually first. Unknown processes, even same UID, must not have argv/cwd/env/logs/files read.
- Application accounts do not isolate Unix processes/files. Shared Unix-account execution is for trusted operators/code only; document this boundary clearly.
- No web arbitrary-command submission. CLI submission derives owner from a valid revocable session. Auth tokens must not appear in URLs, JS storage, logs or public artifacts.
- Keep config, auth.sqlite, session files, runtime data, screenshots and machine details out of Git/Zip. Use XXX in shared server examples.
- Preserve failure-continues, independent waiting tasks, resource gates and per-field -1 semantics. Shrinking a window never stops running jobs. No automatic retries or extra budget.
- Run meaningful isolated account/RPC/HTTP/CPU and browser checks. Record Linux/GPU checks not run; never claim production validation from fixtures.

- Waiting GPU reassignment must authenticate ownership and validate revision inside the queue lock. Resolve the target from fresh NVIDIA UUIDs. Preserve global order, budgets, and all other tasks. A stale claim must fail after reassignment. Scientific config changes happen only in the registered worker before launch, with a saved original and no run directory.
