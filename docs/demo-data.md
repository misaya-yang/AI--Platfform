# Demo Data

AI Gateway includes deterministic demo records for local open-source evaluation.
The seed covers a knowledge dataset, a public conversation share, a public quiz,
and a published exam record. It is intended for local development databases only.

## Preview

Preview the SQL and routes without connecting to PostgreSQL:

```bash
make seed-demo
```

Equivalent direct command:

```bash
scripts/new/seed-demo-data.sh --dry-run
```

## Apply Locally

Start a local stack and run migrations first:

```bash
cp .env.example .env
make quickstart
```

Then load the demo records:

```bash
make seed-demo-apply
```

To use a non-default local env file:

```bash
ENV_FILE=.env.local make seed-demo-apply
```

The seed script uses `scripts/new/common.sh` and writes through `psql` or the
local PostgreSQL container selected by the env file.

## Route Smoke Without A Backend

The frontend also has a mocked open-source route smoke for these demo IDs:

```bash
pnpm -C web e2e:opensource
```

This starts only the Vite frontend and uses Playwright request mocks for the
demo dataset, share, quiz, and exam responses.

## Demo Routes

After applying the seed to a local stack, use these routes:

```text
/knowledge/demo-kb-ai-gateway
/share/demo-share
/quiz/demo-quiz
/exams/00000000-0000-4000-8000-000000000044
```

## Scope

The demo seed does not configure live model providers or embedding providers.
You still need to set the required provider keys in `.env` before testing real
chat, embedding, or retrieval calls.

The seed file is idempotent:

```text
examples/demo-data/open-source-demo.sql
```

All IDs are deterministic and use `ON CONFLICT` upserts so contributors can
rerun the seed while iterating on local UI and route checks.
