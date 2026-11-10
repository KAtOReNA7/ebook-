# Project boundaries

Read README.md and docs/DEVELOPMENT.md first. On an existing operational
checkout, also read the private RUNBOOK.md and config/runtime.json.

- The owner explicitly accepted production on 2026-11-10. Operational runners
  restore the approved private recipient roster from the v5 shared checkpoint;
  use config/runtime.json as authority, not names or cached IDs from old docs.
  Unaccepted/development deployments remain owner-only. Never expand the approved
  roster, replay historical outboxes, send direct IM, or send test messages.
  The two same-day production-launch reports are a one-time authorized resend;
  their completed guard must prevent repetition on another computer.
- Formal data is ebook-only. Audio and unknown service media stay excluded;
  a corresponding ebook ID does not admit an audio expense.
- Fanqie publication rising daily integration was authorized on 2026-11-06. Use
  scripts/collect_fanqie_rising.py and docs/FANQIE_RISING_HANDOFF.md. The daily
  integration requires complete 100-rank validation and a dry-run before writes. Use web sources only; do not
  start the App, Android tooling or an emulator, or create a new environment.
- Never guess platform IDs, treat internal expense-sheet numbers as IDs, copy
  reader metrics across platforms, invent zero for missing data, or backdate a
  sample. Work-level metadata requires a visible source.
- Never execute, restore or migrate the retired tracker/test Base. Historical
  local files are audit evidence only.
- Daily execution uses shared Base checkpoints and a global execution mutex.
  Either updated, authenticated computer may run the daily entry; never bypass
  the mutex or mix an old fixed-host version with the shared protocol. Local
  files are caches, not authority; no manual daily state transfer is required.
  `--shadow` still writes
  business data; only `--dry-run` prevents Base mutations in the daily entry.
- Read and reconcile unknown/incomplete mutation guards before any retry.
  Source edits/deletions are reviewed changes, not new marketing transactions.
- Keep live config, expense data, receipts, runtime state, credentials and
  private handoff archives out of Git. Do not reset/clean/force-push or restore
  historical stashes. Use mocked tests, then full readback for authorized writes.
