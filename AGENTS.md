# Terminal command dispatch

- v0.7.0 uses tmux_dispatch.py and one current.json. Use the user's existing Unix account and configured root/session.
- web_view.py is a loopback-only SSH client, web_bridge.py reuses the same command functions. The terminal monitor remains the sole dispatcher. Browser mutations check revision under the JSON lock and use same-origin/CSRF protections. Terminal output access is restricted to tool-created recorded panes. Preserve terminal/CLI operation and single-JSON storage.
- Save only user-authorized commands with finite training budgets. Use add, edit, dispatch and mode through this tool. No implicit import, replay or retry of historical experiments.
- Fixed targets must be tool-created, identity-checked idle shells. Existing user panes are preserved. Unknown GPU processes are NVIDIA metadata only and read-only.
- Preserve independent pending cancel/reorder, UUID device selection, per-GPU capacity and per-field -1 semantics. Both modes use admission checks. Memory thresholds are admission budgets, not allocator caps.
- current.json is authoritative. All mutations use its file lock and atomic replacement. Each dispatch is consumed once, including after failures and restarts. Keep active/orphaned reservations until processes end or the operator resolves them.
- Commands run in visible tmux terminals. Closing the monitor stops future dispatch. Never signal existing training or external processes while changing the panel.
- Do not publish current.json, private paths, credentials, datasets or checkpoints. Public examples use XXX. v0.5.0 history is available by Git tag.
- Validate with isolated CPU commands and dedicated temporary tmux sessions. Never run a real training to test the tool.
