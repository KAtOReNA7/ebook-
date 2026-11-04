# Project boundaries

Read README.md and docs/DEVELOPMENT.md first. On an existing operational
checkout, also read the private RUNBOOK.md and config/runtime.json.

- This system is in debugging until its owner explicitly accepts production.
  Only the verified owner may receive debug notifications. Do not expand the
  recipients, replay historical outboxes, send direct IM, or send test messages.
- Formal data is ebook-only. Audio and unknown service media stay excluded;
  a corresponding ebook ID does not admit an audio expense.
- Fanqie publication rising ranking remains paused. Use web sources only; do
  not start the App, Android tooling or an emulator, or create a new environment.
- Never guess platform IDs, treat internal expense-sheet numbers as IDs, copy
  reader metrics across platforms, invent zero for missing data, or backdate a
  sample. Work-level metadata requires a visible source.
- Never execute, restore or migrate the retired tracker/test Base. Historical
  local files are audit evidence only.
- The daily pipeline and notification registries are single-writer state. Never
  run two computers against the same Base concurrently. `--shadow` still writes
  business data; only `--dry-run` prevents Base mutations in the daily entry.
- Read and reconcile unknown/incomplete mutation guards before any retry.
  Source edits/deletions are reviewed changes, not new marketing transactions.
- Keep live config, expense data, receipts, runtime state, credentials and
  private handoff archives out of Git. Do not reset/clean/force-push or restore
  historical stashes. Use mocked tests, then full readback for authorized writes.
