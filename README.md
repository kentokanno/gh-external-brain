# GH External Brain

Persistent memory and background task execution for a Custom GPT.

## Environment variables

- `DATABASE_URL`: PostgreSQL connection URL
- `EXTERNAL_BRAIN_API_KEY`: API key expected in the `X-API-Key` header
- `OPENAI_API_KEY`: OpenAI project API key
- `OPENAI_MODEL`: Optional model override; defaults to `gpt-6-astra`
- `OPENAI_MAX_OUTPUT_TOKENS`: Optional output limit; defaults to `3000`

## Task workflow

1. `POST /tasks` creates a task.
2. Tasks with `requires_approval: true` remain queued.
3. `POST /tasks/{task_id}/execute` explicitly approves and starts the task.
4. `GET /tasks/{task_id}` refreshes the OpenAI background response and stores the final result.

Tasks created with `requires_approval: false` start immediately. The execution
engine only returns research, analysis, plans, summaries, or drafts; external
side effects require a separate human-approved integration.
