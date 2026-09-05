---
name: Parallel trading runtimes
description: Safety rules when workspace and published bot processes can run concurrently
---

If workspace and published runtimes are both active, treat them as independent local ledgers even when their external exchange account appears to be the same. Do not infer data provenance from the published process; repository analyses normally read the workspace SQLite file.

**Why:** Local SQLite files, in-memory cooldowns, duplicate guards, and exposure calculations are not shared across isolated runtimes. Similar external equity/exposure snapshots can strongly indicate one exchange account, but secret values must remain undisclosed and platform metadata may only expose secret existence.

**How to apply:** Before publishing live trading changes, establish which runtime owns Telegram input and order submission, verify the external account identity using non-secret runtime evidence, and label every analysis by its source ledger. Keep one canonical trading runtime until cross-process coordination exists.

When a published VM uses a local SQLite snapshot, equal `MAX(id)` values do not prove that workspace and published histories are identical. Independent writers can allocate the same next ID after a snapshot while their timestamps and rows diverge; a republish can also replace the VM-local file and discard rows written since the snapshot.

**Why:** A live/published local file is not a durable shared database. Comparing both the top ID and its timestamp can expose divergence that an ID-only comparison hides.

**How to apply:** For every production forensic comparison, label the source runtime and compare `(id, ts)` together. Treat published SQLite history as non-authoritative across republish until it is backed by durable shared storage or an exported snapshot.

When comparing live positions across independent ledgers, keep an explicit orphan class for exchange positions absent from both ledgers. Use two sequential, timestamped snapshots for the operational stop decision: a stable `workspace-only` set blocks stopping; an empty set does not.

**Why:** The exchange account is shared while each runtime's SQLite history is not. A position can therefore be real and unmanaged by either current ledger, and non-atomic reads can otherwise misclassify a workspace-only position during a live update.

**How to apply:** Record read times for workspace SQLite, published status, and the exchange API. Classify only positions older than the earliest read; label newer or divergent positions `in-flight` and do not use them to block or justify stopping.

An open-position count transition in a runtime status endpoint is not proof of an exchange close: a published count can briefly drop and return while the Bybit position remains open and no closed-PnL event exists.

**Why:** The status count is ledger-derived and has no transaction boundary with the exchange snapshot; transient polling or reconciliation state can change it independently.

**How to apply:** Confirm a close with exchange `closed-pnl`/execution evidence or a zero remote position size before attributing PnL or declaring a position orphaned.

For orphan-position provenance, compare the exchange position `createdTime` (and, when available, `openTime`) with the VM publication boundary. Values before publication support inherited pre-publish state rather than a post-publish ledger-write failure.

**Why:** A missing row alone cannot distinguish lost historical SQLite state from a current runtime submitting without recording its intent.

**How to apply:** State the absolute UTC timestamps and boundary explicitly; do not claim the exact historical SQLite file was recovered unless it is actually found.

Republishing a VM with local SQLite can orphan every live position whose owner ledger exists only in the outgoing runtime; this is an operational continuity failure, not merely analytics-history loss.

**Why:** The new VM inherits a file snapshot, while the outgoing process may have opened positions after that snapshot. Those rows do not follow the position to the replacement runtime, so SL/TP monitoring and close polling disappear.

**How to apply:** Before any republish, establish one canonical runtime and transfer or durably share all unfinished ledger rows with idempotent verification against the exchange. Keep cleanup-only endpoint removal separate from this migration.

An infrastructure-level Replit runtime restart can relaunch the workspace bot even when its workflow was manually stopped; manual process termination is not a durable trading-isolation control.

**Why:** The environment process manager restarted and launched a new Gunicorn instance, while no separate application supervisor or active composite workflow could be confirmed as the trigger.

**How to apply:** Make workspace Bybit trading fail closed through a persisted development environment gate, and guard both scheduler jobs and order-submission side effects. Keep the published owner explicitly enabled only after verification.