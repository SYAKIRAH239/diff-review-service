from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import time
import os
import uuid
from rules import review_diff
import asyncio

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
MAX_CONCURRENT_JOBS = 4
job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

# Turn review processing into a background async function
async def process_review_job(job_id: str):
    async with job_semaphore:
        JOBS[job_id]["status"] = "running"

        try:
            job = JOBS[job_id]
            findings, total_findings = review_diff(job["diff"], job["options"]["maxFindings"])

            JOBS[job_id]["findings"] = findings
            JOBS[job_id]["usage"]["chunks"] = 1
            JOBS[job_id]["status"] = "done"

        except Exception as e:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = {"code": "internal", "message": str(e)}
            
# bearer token authentication
MY_BEARER_TOKEN = os.environ.get("SERVICE_BEARER_TOKEN", "changeme-dev-token")

def require_auth(authorization: str = Header(default=None)):
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Missing or malformed Authorization header"})
    token = authorization.removeprefix("Bearer ")
    if token != MY_BEARER_TOKEN:
        raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "Invalid bearer token"})

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        code = exc.detail.get("code", "internal")
        message = exc.detail.get("message", "An error occurred")
    else:
        code = "internal"
        message = str(exc.detail)
    return error_response(exc.status_code, code, message)

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
async def create_review(body: ReviewRequest, auth=Depends(require_auth)):
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

    if "+++" not in body.diff and "---" not in body.diff:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_diff", "message": "diff does not appear to be a valid unified diff"}
        )

    job_id = str(uuid.uuid4())

    JOBS[job_id] = {
        "jobId": job_id,
        "status": "queued",
        "findings": [],
        "usage": {
            "inputBytes": len(diff_bytes),
            "chunks": 0,
            "cacheHit": False,
        },
        "diff": body.diff,
        "options": body.options.dict(),
    }

    asyncio.create_task(process_review_job(job_id))

    return {"jobId": job_id, "status": "queued"}


@app.get("/v1/reviews/{job_id}")
def get_review(job_id: str, auth=Depends(require_auth)):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"No job found with id {job_id}"}
        )

    return {
        "jobId": job["jobId"],
        "status": job["status"],
        "findings": job["findings"],
        "usage": job["usage"],
    }