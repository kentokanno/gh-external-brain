import os
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field


DATABASE_URL = os.environ.get("DATABASE_URL")

app = FastAPI(
    title="GH External Brain",
    version="0.2.0",
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
        "version": "0.2.0",
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
            }
        ]
    }


# ---------- Memory ----------

@app.post("/memories", status_code=201)
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


@app.get("/memories/search")
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


@app.get("/memories")
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


@app.get("/memories/{memory_id}")
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


@app.patch("/memories/{memory_id}")
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


@app.delete("/memories/{memory_id}")
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
