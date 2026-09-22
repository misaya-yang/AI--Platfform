# Managed Runtime compatibility at upstream 279ba894

The upstream Codex App Server owns the only agent loop. This crate owns
PostgreSQL persistence, immutable Gateway capability ceilings and private HTTP
adapters. The host-only start/resume options install `ToolPolicy` before Core
captures extension data. Ordinary turns may narrow their authenticated snapshot;
they cannot widen the original thread ceiling. Configless verification reuses
that ceiling and does not reconfigure native search. Legacy rows without a
ceiling need an explicit authenticated policy before resume.

## Persistence

Thread membership, JSONB creation projection and canonical session metadata are
committed in one PostgreSQL transaction. Rollout, metadata and platform receipt
writes await commit. There is no background write queue. Any failed history, metadata or receipt write poisons
the thread in the current Runtime process: subsequent persist/flush/shutdown,
turn admission and tool dispatch fail. Terminal admission also checks this
fence: recovered PostgreSQL can persist an explicit failed terminal; an ongoing
outage produces the existing non-durable failed terminal, never success. The host-only Core durability marker
propagates flush failures before ordinary or compaction model sends. Recover by
reloading committed history; do not clear the fence after a successful read.

New creator and workspace fields are stored in existing JSONB fields. The
creator user comes from the authenticated platform membership; a platform tenant
is never a ChatGPT account. Creator/originator metadata is fill-only. Initial root startup data is staged only after durable root authorization and
committed with the creation projection; resumed/forked/child startup resolves the
original ceiling and canonical catalog through exact PostgreSQL membership.
Legacy flat dynamic-tool metadata is normalized when decoded. Pending
host metadata remains in memory until a successful metadata update. Discard only
releases staged metadata and preserves committed history.

Deletes are idempotent tombstones. A root delete hides its full membership;
subagent deletes hide their descendants. Writers and deletes share the root row
lock, including lifecycle and V1 receipts. Append-only history and platform
artifact/attachment ACL records remain retained for audit. New native Codex
thread attachment/project/section APIs retain `Unsupported`; existing platform
attachments and Worker capabilities remain supported.

## Native history and rollback

The history format is the complete native upstream `RolloutItemWire` at
`279ba894152b2c01c5294cc0723b463b209bdca4`. No target events are filtered or converted
to user messages. New `token_usage_record` and `retained_context` variants are not
readable by the old `94cbbdda` decoder. Therefore a return to that binary is
**stop-write, backup-and-restore required**; an image-only rollback against a
database written by the target is unsupported. Preserve the target database and
restore the frozen baseline into a separate database before any old-runtime
validation. The release coordinator owns the complete service/database restore
receipt and must not label same-database N-1 compatibility as passed.

## Hosted feature profile

The isolated Runtime home receives the full upstream bundled model catalog with
`use_responses_lite=false`, supplied through `model_catalog_json` so reload and
children use the same Gateway-supported Responses wire. Unknown model fallback
already uses standard Responses. Native Code Mode, reasoning overrides,
Guardian automation, hooks, Apps, plugins, realtime conversations, daemon
startup, worktree creation, system proxy fallback and implicit TTY selection are
not enabled by this integration. Platform Worker tools and Codex collaboration
remain governed by their existing authenticated catalog.

Dynamic-tool ownership uses the immutable thread catalog with namespace/name
identity. Child capability authorization resolves the exact durable
`TurnContextItem.root_turn_id`, checks the root snapshot and live lease, and
qualifies execution call IDs to avoid sibling collisions. Codex replies and
child lifecycle receipts retain their original native identity; root cancellation
propagates through child execution tokens. Spawn-tool completion is acceptance;
actual subagent terminal activity is projected separately.

## Verification

Rust unit coverage includes policy omission/narrowing/widening, namespace
identity, native search authority, Gateway model catalog mode, subagent activity
and child execution identity. The PostgreSQL contract test injects an append
failure, restores healthy database reads and requires every durability barrier
to continue failing. These tests require the coordinated Docker/hosted Rust
build; source inspection and `git diff --check` alone are not execution evidence.
