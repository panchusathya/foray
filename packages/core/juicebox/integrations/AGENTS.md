# Working in `integrations/`

Read the root `AGENTS.md` first for repo-wide conventions.

## Mail providers (`packages/core/juicebox/integrations`)

- **Adding a provider** means implementing `MailProvider` in `providers.py` and
  registering it there: OAuth, sending, and reading raw mailbox traffic as
  `MailboxEvent`s. Reply matching, intent classification, enrollment stops, the
  thread-sync privacy guard and reply-draft generation are shared — a provider
  must never need to touch `sequencer.py`.
- **Never let a provider's wire format past its adapter.** Graph returns ISO
  timestamps, a nested `from.emailAddress.address` and a `conversationId`; Zoho
  returns millisecond epochs, a plain `fromAddress` and a `threadId`. Both
  become `MailboxEvent` in `to_event`, and nothing downstream knows which one
  it came from.
- **`Message.provider_native_id` is the ingest idempotency key.** Dedupe, the
  reply-draft lookup, the sent-items guard and a partial unique index all read
  it. A provider that cannot supply its own message id cannot be idempotent, so
  a send that comes back without one fails rather than reporting success.
- **Running out of send capacity is not an error.** `process_enrollment` returns
  `"deferred"`, which requeues the same permanent idempotency key. Raising
  parks the job; advancing `next_send_at` makes the requeue return `"skipped"`,
  which marks the job succeeded and loses that step for good.
