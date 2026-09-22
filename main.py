import hmac
import os
from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from openai import OpenAI
from pydantic import BaseModel, Field


DATABASE_URL = os.environ.get("DATABASE_URL")
EXTERNAL_BRAIN_API_KEY = os.environ.get("EXTERNAL_BRAIN_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-astra")
OPENAI_MAX_OUTPUT_TOKENS = int(os.environ.get("OPENAI_MAX_OUTPUT_TOKENS", "3000"))

app = FastAPI(
    title="GH External Brain",
    version="0.5.0",
    description="Persistent external memory workspace for GH."
)


# ---------- Models ----------

class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=50000)
    title: Optional[str] = Field(default=None, max_length=500)
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(default=5, ge=1, le=10)


class MemoryUpdate(BaseModel):
    content: Optional[str] = Field(default=None, min_length=1, max_length=50000)
    title: Optional[str] = Field(default=None, max_length=500)
    tags: Optional[list[str]] = None
    importance: Optional[int] = Field(default=None, ge=1, le=10)


class TaskCreate(BaseModel):
    instruction: str = Field(min_length=1, max_length=50000)
    title: Optional[str] = Field(default=None, max_length=500)
    priority: int = Field(default=5, ge=1, le=10)
    requires_approval: bool = True


class TaskUpdate(BaseModel):
    status: Optional[
        Literal["queued", "running", "completed", "failed", "cancelled"]
    ] = None
    title: Optional[str] = Field(default=None, max_length=500)
    instruction: Optional[str] = Field(default=None, min_length=1, max_length=50000)
    priority: Optional[int] = Field(default=None, ge=1, le=10)
    requires_approval: Optional[bool] = None
    result: Optional[str] = Field(default=None, max_length=50000)
    error: Optional[str] = Field(default=None, max_length=10000)


# ---------- Authentication ----------

def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    if not EXTERNAL_BRAIN_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="EXTERNAL_BRAIN_API_KEY is not configured",
        )

    if x_api_key is None or not hmac.compare_digest(
        x_api_key.encode("utf-8"),
        EXTERNAL_BRAIN_API_KEY.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
        )


# ---------- Database ----------

def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=10,
    )


def initialize_database():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id UUID PRIMARY KEY,
                    title TEXT,
                    content TEXT NOT NULL,
                    tags TEXT[] NOT NULL DEFAULT '{}',
                    importance INTEGER NOT NULL DEFAULT 5
                        CHECK (importance BETWEEN 1 AND 10),
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id UUID PRIMARY KEY,
                    title TEXT,
                    instruction TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK (
                            status IN (
                                'queued',
                                'running',
                                'completed',
                                'failed',
                                'cancelled'
                            )
                        ),
                    priority INTEGER NOT NULL DEFAULT 5
                        CHECK (priority BETWEEN 1 AND 10),
                    requires_approval BOOLEAN NOT NULL DEFAULT TRUE,
                    result TEXT,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    started_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ
                )
                """
            )

            cur.execute(
                "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS openai_response_id TEXT"
            )
            cur.execute(
                "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS model TEXT"
            )
            cur.execute(
                "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ"
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_queue
                ON tasks (status, priority DESC, created_at ASC)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_created_at
                ON memories (created_at DESC)
                """
            )

        conn.commit()


@app.on_event("startup")
def startup():
    initialize_database()


# ---------- System ----------

@app.get("/")
def root():
    return {
        "system": "GH External Brain",
        "version": "0.5.0",
        "status": "online"
    }


@app.get("/health")
def health():
    db_status = "unknown"

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()

        db_status = "connected"

    except Exception:
        db_status = "unavailable"

    return {
        "status": "healthy" if db_status == "connected" else "degraded",
        "database": db_status,
        "openai": "configured" if OPENAI_API_KEY else "not_configured",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@app.get("/capabilities")
def capabilities():
    return {
        "capabilities": [
            {
                "id": "memory.write",
                "effect": "REVERSIBLE",
                "description": "Store persistent information"
            },
            {
                "id": "memory.read",
                "effect": "READ_ONLY",
                "description": "Retrieve persistent information"
            },
            {
                "id": "memory.search",
                "effect": "READ_ONLY",
                "description": "Search persistent information"
            },
            {
                "id": "memory.update",
                "effect": "REVERSIBLE",
                "description": "Update stored information"
            },
            {
                "id": "memory.delete",
                "effect": "DESTRUCTIVE",
                "description": "Delete stored information"
            },
            {
                "id": "task.create",
                "effect": "REVERSIBLE",
                "description": "Add work to the external task queue"
            },
            {
                "id": "task.read",
                "effect": "READ_ONLY",
                "description": "Read queued work and results"
            },
            {
                "id": "task.update",
                "effect": "REVERSIBLE",
                "description": "Update task status and results"
            },
            {
                "id": "task.execute",
                "effect": "CONSEQUENTIAL",
                "description": "Approve and execute a queued task with OpenAI"
            }
        ]
    }


# ---------- Memory ----------

@app.post("/memories", status_code=201, dependencies=[Depends(require_api_key)])
def create_memory(memory: MemoryCreate):
    memory_id = uuid4()
    now = datetime.now(timezone.utc)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memories
                    (id, title, content, tags, importance, created_at, updated_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    memory_id,
                    memory.title,
                    memory.content,
                    memory.tags,
                    memory.importance,
                    now,
                    now,
                ),
            )

            result = cur.fetchone()

        conn.commit()

    return result


@app.get("/memories/search", dependencies=[Depends(require_api_key)])
def search_memories(
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=10, ge=1, le=50),
):
    pattern = f"%{q}%"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM memories
                WHERE
                    content ILIKE %s
                    OR COALESCE(title, '') ILIKE %s
                    OR EXISTS (
                        SELECT 1
                        FROM unnest(tags) AS tag
                        WHERE tag ILIKE %s
                    )
                ORDER BY importance DESC, updated_at DESC
                LIMIT %s
                """,
                (pattern, pattern, pattern, limit),
            )

            results = cur.fetchall()

    return {
        "query": q,
        "count": len(results),
        "memories": results
    }


@app.get("/memories", dependencies=[Depends(require_api_key)])
def list_memories(
    limit: int = Query(default=20, ge=1, le=100),
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM memories
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (limit,),
            )

            results = cur.fetchall()

    return {
        "count": len(results),
        "memories": results
    }


@app.get("/memories/{memory_id}", dependencies=[Depends(require_api_key)])
def get_memory(memory_id: UUID):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM memories WHERE id = %s",
                (memory_id,),
            )

            result = cur.fetchone()

    if result is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    return result


@app.patch("/memories/{memory_id}", dependencies=[Depends(require_api_key)])
def update_memory(memory_id: UUID, memory: MemoryUpdate):
    changes = memory.model_dump(exclude_unset=True)

    if not changes:
        raise HTTPException(status_code=400, detail="No changes supplied")

    allowed = {"title", "content", "tags", "importance"}

    assignments = []
    values = []

    for field, value in changes.items():
        if field not in allowed:
            continue

        assignments.append(f"{field} = %s")
        values.append(value)

    assignments.append("updated_at = %s")
    values.append(datetime.now(timezone.utc))
    values.append(memory_id)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE memories
                SET {", ".join(assignments)}
                WHERE id = %s
                RETURNING *
                """,
                values,
            )

            result = cur.fetchone()

        conn.commit()

    if result is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    return result


@app.delete("/memories/{memory_id}", dependencies=[Depends(require_api_key)])
def delete_memory(memory_id: UUID):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM memories
                WHERE id = %s
                RETURNING id
                """,
                (memory_id,),
            )

            result = cur.fetchone()

        conn.commit()

    if result is None:
        raise HTTPException(status_code=404, detail="Memory not found")

    return {
        "deleted": True,
        "id": result["id"]
    }


# ---------- Tasks ----------

TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}


def get_openai_client():
    if not OPENAI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY is not configured",
        )

    return OpenAI(api_key=OPENAI_API_KEY)


def refresh_task_execution(task_id: UUID):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
            task = cur.fetchone()

    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if (
        task["status"] in TERMINAL_TASK_STATUSES
        or not task.get("openai_response_id")
    ):
        return task

    try:
        response = get_openai_client().responses.retrieve(
            task["openai_response_id"]
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not refresh OpenAI response: {exc}",
        ) from exc

    response_status = getattr(response.status, "value", response.status)
    now = datetime.now(timezone.utc)
    assignments = ["updated_at = %s"]
    values = [now]

    if response_status == "completed":
        assignments.extend(
            ["status = 'completed'", "result = %s", "error = NULL", "completed_at = %s"]
        )
        values.extend([response.output_text or "", now])
    elif response_status in {"failed", "incomplete", "cancelled"}:
        error = getattr(response, "error", None)
        error_message = getattr(error, "message", None)
        if not error_message:
            incomplete = getattr(response, "incomplete_details", None)
            error_message = str(incomplete or response_status)

        final_status = "cancelled" if response_status == "cancelled" else "failed"
        assignments.extend(
            ["status = %s", "error = %s", "completed_at = %s"]
        )
        values.extend([final_status, error_message[:10000], now])
    else:
        assignments.append("status = 'running'")

    values.append(task_id)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE tasks
                SET {", ".join(assignments)}
                WHERE id = %s
                RETURNING *
                """,
                values,
            )
            task = cur.fetchone()
        conn.commit()

    return task


def start_task_execution(task_id: UUID, approved: bool):
    now = datetime.now(timezone.utc)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM tasks WHERE id = %s FOR UPDATE",
                (task_id,),
            )
            task = cur.fetchone()

            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")

            if task.get("openai_response_id"):
                return task

            if task["status"] != "queued":
                raise HTTPException(
                    status_code=409,
                    detail=f"Task cannot be executed from status {task['status']}",
                )

            if task["requires_approval"] and not approved:
                raise HTTPException(
                    status_code=409,
                    detail="Task requires explicit approval before execution",
                )

            cur.execute(
                """
                UPDATE tasks
                SET status = 'running',
                    started_at = COALESCE(started_at, %s),
                    approved_at = CASE WHEN %s THEN %s ELSE approved_at END,
                    updated_at = %s,
                    error = NULL
                WHERE id = %s
                RETURNING *
                """,
                (now, approved, now, now, task_id),
            )
            task = cur.fetchone()
        conn.commit()

    try:
        response = get_openai_client().responses.create(
            model=OPENAI_MODEL,
            background=True,
            max_output_tokens=OPENAI_MAX_OUTPUT_TOKENS,
            instructions=(
                "You are GH External Brain's safe task execution engine. "
                "Complete research, analysis, planning, summarization, and drafting tasks. "
                "Return a useful final deliverable, in Japanese unless the task requests another language. "
                "Do not claim to have sent, purchased, deleted, published, or changed anything in an external service. "
                "When an external side effect is requested, prepare the draft or action plan and clearly say that human approval is required."
            ),
            input=task["instruction"],
            metadata={"external_brain_task_id": str(task_id)},
        )
    except Exception as exc:
        failed_at = datetime.now(timezone.utc)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tasks
                    SET status = 'failed', error = %s,
                        updated_at = %s, completed_at = %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (str(exc)[:10000], failed_at, failed_at, task_id),
                )
                failed_task = cur.fetchone()
            conn.commit()
        return failed_task

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tasks
                SET openai_response_id = %s, model = %s, updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (response.id, OPENAI_MODEL, datetime.now(timezone.utc), task_id),
            )
            task = cur.fetchone()
        conn.commit()

    return task

@app.post("/tasks", status_code=201, dependencies=[Depends(require_api_key)])
def create_task(task: TaskCreate):
    task_id = uuid4()
    now = datetime.now(timezone.utc)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks (
                    id,
                    title,
                    instruction,
                    status,
                    priority,
                    requires_approval,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    task_id,
                    task.title,
                    task.instruction,
                    task.priority,
                    task.requires_approval,
                    now,
                    now,
                ),
            )
            result = cur.fetchone()

        conn.commit()

    if not task.requires_approval:
        return start_task_execution(task_id, approved=False)

    return result


@app.post(
    "/tasks/{task_id}/execute",
    dependencies=[Depends(require_api_key)],
)
def approve_and_execute_task(task_id: UUID):
    return start_task_execution(task_id, approved=True)


@app.get("/tasks", dependencies=[Depends(require_api_key)])
def list_tasks(
    status: Optional[
        Literal["queued", "running", "completed", "failed", "cancelled"]
    ] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            if status is None:
                cur.execute(
                    """
                    SELECT *
                    FROM tasks
                    ORDER BY priority DESC, created_at ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
            else:
                cur.execute(
                    """
                    SELECT *
                    FROM tasks
                    WHERE status = %s
                    ORDER BY priority DESC, created_at ASC
                    LIMIT %s
                    """,
                    (status, limit),
                )

            results = cur.fetchall()

    return {
        "count": len(results),
        "tasks": results,
    }


@app.get("/tasks/{task_id}", dependencies=[Depends(require_api_key)])
def get_task(task_id: UUID):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM tasks WHERE id = %s",
                (task_id,),
            )
            result = cur.fetchone()

    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if result["status"] == "running" and result.get("openai_response_id"):
        return refresh_task_execution(task_id)

    return result


@app.patch("/tasks/{task_id}", dependencies=[Depends(require_api_key)])
def update_task(task_id: UUID, task: TaskUpdate):
    changes = task.model_dump(exclude_unset=True)

    if not changes:
        raise HTTPException(status_code=400, detail="No changes supplied")

    allowed = {
        "status",
        "title",
        "instruction",
        "priority",
        "requires_approval",
        "result",
        "error",
    }
    assignments = []
    values = []

    for field, value in changes.items():
        if field not in allowed:
            continue

        assignments.append(f"{field} = %s")
        values.append(value)

    now = datetime.now(timezone.utc)

    if changes.get("status") == "running":
        assignments.append("started_at = COALESCE(started_at, %s)")
        values.append(now)

    if changes.get("status") in {"completed", "failed", "cancelled"}:
        assignments.append("completed_at = %s")
        values.append(now)

    assignments.append("updated_at = %s")
    values.append(now)
    values.append(task_id)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE tasks
                SET {", ".join(assignments)}
                WHERE id = %s
                RETURNING *
                """,
                values,
            )
            result = cur.fetchone()

        conn.commit()

    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")

    return result
