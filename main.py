from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import time
import os
import uuid
from rules import review_diff
import asyncio
import json
from fastapi.responses import StreamingResponse
import hashlib
from collections import deque
from fastapi.exceptions import RequestValidationError
from llm_provider import llm_review_diff

app = FastAPI()
START_TIME = time.time()

# error envelope helper
def error_response(status_code: int, code: str, message: str):
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}}
    )

# --- request/response models ---
class ReviewOptions(BaseModel):
    provider: str = "mock"
    maxFindings: int = 100

class ReviewRequest(BaseModel):
    diff: str
    options: Optional[ReviewOptions] = ReviewOptions()

# --- in-memory job store ---
JOBS = {}  # jobId -> job dict
CACHE = {}       # content_hash -> jobId
IDEMPOTENCY = {} # idempotency_key -> {"body_hash": ..., "jobId": ...}
MAX_CONCURRENT_JOBS = 4
job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)


RATE_LIMIT_PER_MINUTE = 30
RATE_LIMIT_WINDOW_SECONDS = 60

request_log = {}  # token -> deque of timestamps

# Turn review processing into a background async function
async def process_review_job(job_id: str):
    async with job_semaphore:
        JOBS[job_id]["status"] = "running"
        JOBS[job_id]["events"].append({"event": "status", "data": {"status": "running"}})

        try:
            job = JOBS[job_id]
            provider = job["options"].get("provider", "mock")

            if provider == "llm":
                findings, total_findings, num_chunks = llm_review_diff(job["diff"], job["options"]["maxFindings"])
            else:
                findings, total_findings, num_chunks = review_diff(job["diff"], job["options"]["maxFindings"])

            for finding in findings:
                JOBS[job_id]["events"].append({"event": "finding", "data": finding})

            JOBS[job_id]["findings"] = findings
            JOBS[job_id]["usage"]["chunks"] = num_chunks
            JOBS[job_id]["status"] = "done"

            JOBS[job_id]["events"].append({"event": "status", "data": {"status": "done"}})
            JOBS[job_id]["events"].append({
                "event": "done",
                "data": {"total": total_findings, "usage": JOBS[job_id]["usage"]}
            })

        except Exception as e:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = {"code": "internal", "message": str(e)}
            JOBS[job_id]["events"].append({"event": "status", "data": {"status": "failed"}})
            
# bearer token authentication
MY_BEARER_TOKEN = os.environ.get("SERVICE_BEARER_TOKEN", "changeme-dev-token")

def require_auth(authorization: str = Header(default=None)):
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Missing or malformed Authorization header"})
    token = authorization.removeprefix("Bearer ")
    if token != MY_BEARER_TOKEN:
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Invalid bearer token"})

def check_rate_limit(token: str):
    now = time.time()

    if token not in request_log:
        request_log[token] = deque()

    timestamps = request_log[token]

    while timestamps and timestamps[0] <= now - RATE_LIMIT_WINDOW_SECONDS:
        timestamps.popleft()

    if len(timestamps) >= RATE_LIMIT_PER_MINUTE:
        retry_after = int(RATE_LIMIT_WINDOW_SECONDS - (now - timestamps[0])) + 1
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "message": "Too many requests, please slow down"},
            headers={"Retry-After": str(retry_after)}
        )

    timestamps.append(now)

def compute_content_hash(diff: str, options: dict) -> str:
    payload = json.dumps({"diff": diff, "options": options}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

async def event_generator(job_id: str):
    index = 0
    while True:
        job = JOBS.get(job_id)
        if job is None:
            break

        events = job["events"]
        while index < len(events):
            ev = events[index]
            yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'])}\n\n"
            index += 1

        if job["status"] in ("done", "failed"):
            break

        await asyncio.sleep(0.2)

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        code = exc.detail.get("code", "internal")
        message = exc.detail.get("message", "An error occurred")
    else:
        code = "internal"
        message = str(exc.detail)

    response = JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": message}}
    )
    if exc.headers:
        for key, value in exc.headers.items():
            response.headers[key] = value
    return response

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()

    is_json_decode_error = any(err.get("type") == "json_invalid" for err in errors)

    if is_json_decode_error:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_json", "message": "Request body is not valid JSON"}}
        )

    return JSONResponse(
        status_code=422,
        content={"error": {"code": "invalid_diff", "message": "Request body does not match the expected shape"}}
    )

@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "0.1.0",
        "uptimeSeconds": int(time.time() - START_TIME)
    }

@app.get("/spec")
def spec():
    return {
        "specVersion": "1.0",
        "providers": ["mock", "llm"],
        "limits": {
            "maxPayloadBytes": 1048576,
            "chunkBytes": 65536,
            "maxConcurrentJobs": 4,
            "rateLimitPerMinute": 30
        }
    }

@app.post("/v1/reviews", status_code=202)
async def create_review(
    body: ReviewRequest,
    auth=Depends(require_auth),
    idempotency_key: str = Header(default=None, alias="Idempotency-Key"),
):
    check_rate_limit(MY_BEARER_TOKEN)

    diff_bytes = body.diff.encode("utf-8")

    if len(diff_bytes) > 1_048_576:
        raise HTTPException(
            status_code=413,
            detail={"code": "payload_too_large", "message": "diff exceeds 1 MiB limit"}
        )

    if not body.diff.strip():
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_diff", "message": "diff is empty"}
        )

    if "+++" not in body.diff or "---" not in body.diff:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_diff", "message": "diff does not appear to be a valid unified diff"}
        )

    if "@@" not in body.diff:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_diff", "message": "diff has no hunk headers"}
        )

    if "+++" not in body.diff and "---" not in body.diff:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_diff", "message": "diff does not appear to be a valid unified diff"}
        )

    options_dict = body.options.dict()
    content_hash = compute_content_hash(body.diff, options_dict)

    if idempotency_key:
        existing = IDEMPOTENCY.get(idempotency_key)
        if existing:
            if existing["content_hash"] == content_hash:
                job_id = existing["jobId"]
                JOBS[job_id]["usage"]["cacheHit"] = True
                return {"jobId": job_id, "status": JOBS[job_id]["status"]}
            else:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "idempotency_conflict", "message": "Idempotency-Key reused with a different body"}
                )

    if content_hash in CACHE:
        job_id = CACHE[content_hash]
        JOBS[job_id]["usage"]["cacheHit"] = True
        if idempotency_key:
            IDEMPOTENCY[idempotency_key] = {"content_hash": content_hash, "jobId": job_id}
        return {"jobId": job_id, "status": JOBS[job_id]["status"]}

    if content_hash in CACHE:
        job_id = CACHE[content_hash]
        if idempotency_key:
            IDEMPOTENCY[idempotency_key] = {"content_hash": content_hash, "jobId": job_id}
        return {"jobId": job_id, "status": JOBS[job_id]["status"]}

    job_id = str(uuid.uuid4())

    JOBS[job_id] = {
        "jobId": job_id,
        "status": "queued",
        "findings": [],
        "events": [{"event": "status", "data": {"status": "queued"}}],
        "usage": {
            "inputBytes": len(diff_bytes),
            "chunks": 0,
            "cacheHit": False,
        },
        "diff": body.diff,
        "options": options_dict,
    }

    CACHE[content_hash] = job_id
    if idempotency_key:
        IDEMPOTENCY[idempotency_key] = {"content_hash": content_hash, "jobId": job_id}

    asyncio.create_task(process_review_job(job_id))

    return {"jobId": job_id, "status": "queued"}


@app.get("/v1/reviews/{job_id}")
@app.get("/v1/reviews/{job_id}")
def get_review(job_id: str, auth=Depends(require_auth)):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"No job found with id {job_id}"}
        )

    response = {
        "jobId": job["jobId"],
        "status": job["status"],
        "findings": job["findings"],
        "usage": job["usage"],
    }

    if job["status"] == "failed" and "error" in job:
        response["error"] = job["error"]

    return response

@app.get("/v1/reviews/{job_id}/stream")
async def stream_review(job_id: str, auth=Depends(require_auth)):
    if job_id not in JOBS:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"No job found with id {job_id}"}
        )

    return StreamingResponse(event_generator(job_id), media_type="text/event-stream")