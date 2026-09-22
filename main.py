from fastapi import FastAPI
from datetime import datetime, timezone

app = FastAPI(
    title="GH External Brain",
    version="0.1.0",
    description="External workspace for GH."
)

@app.get("/")
def root():
    return {
        "system": "GH External Brain",
        "version": "0.1.0",
        "status": "online"
    }

@app.get("/health")
def health():
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/capabilities")
def capabilities():
    return {
        "capabilities": [
            {
                "id": "system.observe",
                "effect": "READ_ONLY",
                "description": "Observe the External Brain status"
            },
            {
                "id": "memory.read",
                "effect": "READ_ONLY",
                "description": "Read External Brain memory"
            },
            {
                "id": "memory.write",
                "effect": "REVERSIBLE",
                "description": "Store information in External Brain memory"
            }
        ]
    }
