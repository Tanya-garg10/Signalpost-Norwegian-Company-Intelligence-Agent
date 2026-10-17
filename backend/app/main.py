import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .research import (
    RequestLimiter,
    db_connection,
    init_db,
    normalize_org_number,
    research,
    research_many,
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Signalpost Norwegian Company Intelligence", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class Lookup(BaseModel):
    org_number: str
    refresh: bool = False


class Batch(BaseModel):
    org_numbers: list[str] = Field(min_length=1, max_length=1000)
    refresh: bool = False


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/company")
async def company(query: Lookup) -> dict[str, Any]:
    return await research(query.org_number, refresh=query.refresh)


@app.post("/api/batch")
async def batch(query: Batch) -> dict[str, Any]:
    limiter = RequestLimiter(interval_seconds=0.2)
    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=False,
        headers={"User-Agent": "SignalpostStudentAgent/1.0"},
    ) as client:
        results = await research_many(
            query.org_numbers,
            client,
            refresh=query.refresh,
            concurrency=5,
            limiter=limiter,
        )
    complete = len(results) == len(query.org_numbers)
    return {
        "input_count": len(query.org_numbers),
        "result_count": len(results),
        "complete": complete,
        "distinct_input_count": len(
            {number for number in map(normalize_org_number, query.org_numbers) if number}
        ),
        "results": results,
    }


@app.get("/api/company/{org_number}/history")
def history(org_number: str) -> list[dict[str, Any]]:
    org = normalize_org_number(org_number)
    if org is None:
        raise HTTPException(status_code=422, detail="Invalid Norwegian organisation number.")
    init_db()
    with db_connection() as connection:
        rows = connection.execute(
            """SELECT retrieved_at, digest, changes FROM source_snapshots
               WHERE org=? ORDER BY id DESC LIMIT 50""",
            (org,),
        ).fetchall()
    return [
        {
            "retrieved_at": row[0],
            "sha256": row[1],
            "changes": json.loads(row[2]),
        }
        for row in rows
    ]
