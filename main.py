from fastapi import FastAPI
import time

app = FastAPI()
START_TIME = time.time()

@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "0.1.0",
        "uptimeSeconds": int(time.time() - START_TIME)
    }