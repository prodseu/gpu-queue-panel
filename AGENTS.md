# Queue operations

- Version 0.5.0 uses authenticated multiuser queues. Preserve existing account ownership, sessions, and configuration when updating.
- Existing single-user queues require explicit migration and owner assignment. Never infer ownership or adopt a live process automatically.
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

- Automatic/manual controls use metadata.queue_control. Admin alone changes global mode/pause, ordinary users request only their own waiting tasks. Manual requests bypass automatic capacity/admission gates but require an existing GPU and enabled owner. Every claim rechecks current mode, pause, revision and explicit request under the queue lock.
- Pause and mode switches revoke unclaimed manual requests, preserving running jobs. Manual mode never grants agents permission to launch newly submitted work autonomously. Use the existing single Dispatcher, with no extra admission loop.

- Command jobs use queued_command.py and keep the original executable/arguments in original_argv. Explicit GPU reassignment config sync must apply to command tasks as well as private scientific tasks. Keep entry wrapping idempotent, and preserve POSIX exec identity, logging and exit receipts. Regression checks must exercise the already-loaded legacy runner path.

- Automatic window capacity is per GPU UUID, with a separate CPU queue. Check each device head while preserving FIFO within the device. Claim must enforce the same per-device capacity under lock. A blocked/full GPU must not stall eligible heads on other GPUs.
