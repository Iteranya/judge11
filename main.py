import asyncio
import queue
import subprocess
import tempfile
import os
import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
import uvicorn

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

POOL_SIZE = 10
container_pool = queue.Queue()

# SECURITY FIX: Output truncated at source (head -c), SIGKILL (-k) used to prevent unkillable hangs
RUNNER_SCRIPT = """\
#!/bin/sh
ulimit -f 10000

START_C=$(date +%s%3N)
# -k 1s forces a SIGKILL if the process ignores the standard timeout
timeout -k 1s 5s g++ -std=c++17 test.cpp -o test 2>/tmp/cerr
COMPILE_EXIT=$?
END_C=$(date +%s%3N)
echo "COMPILE_MS:$((END_C - START_C))" >&2

if [ $COMPILE_EXIT -ne 0 ]; then
    # 124 is standard timeout, 137 is SIGKILL timeout
    if [ $COMPILE_EXIT -eq 124 ] || [ $COMPILE_EXIT -eq 137 ]; then
        echo "TIMEOUT_ERROR:COMPILE" >&2
    else
        # Truncate stderr to prevent RAM exhaustion on host
        head -c 10000 /tmp/cerr >&2
    fi
    exit $COMPILE_EXIT
fi

START_R=$(date +%s%3N)
# Pipe stdout directly to head -c to prevent a C++ infinite print loop from crashing the Python server
timeout -k 0.5s 1s ./test | head -c 10000
RUN_EXIT=$?
END_R=$(date +%s%3N)
echo "RUN_MS:$((END_R - START_R))" >&2

if [ $RUN_EXIT -eq 124 ] || [ $RUN_EXIT -eq 137 ]; then
    echo "TIMEOUT_ERROR:RUN" >&2
fi

exit $RUN_EXIT
"""

def init_pool():
    for i in range(POOL_SIZE):
        name = f"judge_worker_{i}"
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run([
            "docker", "run", "-d",
            "--name", name,
            "--network=none",
            "--memory=128m",
            "--cpus=0.5",
            "--pids-limit=64",
            "--log-driver=none",
            "--read-only",             # SECURITY: Prevents tampering with system files (g++, headers)
            "--tmpfs", "/tmp:exec",    # SECURITY: Ephemeral RAM space for compilation artifacts
            "--tmpfs", "/code:exec",   # SECURITY: Ephemeral RAM space for user code
            "--workdir", "/code",      
            "--user", "1000:1000",     # SECURITY: Runs as guest. Prevents breaking the container for the next user.
            "gcc-pch",                 # Your custom image with PCH
            "sleep", "infinity"
        ], check=True)
        container_pool.put(name)
    print(f"[judge] Pool of {POOL_SIZE} containers ready.")

def cleanup_pool():
    for i in range(POOL_SIZE):
        subprocess.run(["docker", "rm", "-f", f"judge_worker_{i}"], capture_output=True)
    print("[judge] Pool cleaned up.")

@app.on_event("startup")
async def on_startup():
    await run_in_threadpool(init_pool)

@app.on_event("shutdown")
async def on_shutdown():
    await run_in_threadpool(cleanup_pool)

@app.get("/", response_class=HTMLResponse)
async def playground():
    return FileResponse("index.html")

class TestCode(BaseModel):
    code: str

def run_judge(body: TestCode):
    container = container_pool.get()  # blocks until a container is free
    t0 = time.time()
    try:
        # 1. Pipe user code directly from Python RAM into Container RAM (Bypasses docker cp completely)
        subprocess.run(
            ["docker", "exec", "-i", container, "sh", "-c", "cat > /code/test.cpp"],
            input=body.code, text=True, check=True
        )
        
        # 2. Pipe runner script directly into Container RAM
        subprocess.run(
            ["docker", "exec", "-i", container, "sh", "-c", "cat > /code/run.sh"],
            input=RUNNER_SCRIPT, text=True, check=True
        )

        # 3. Execute the code (Output is captured directly into Python RAM)
        result = subprocess.run(
            ["docker", "exec", container, "sh", "/code/run.sh"],
            capture_output=True, text=True, timeout=10
        )
        elapsed_ms = round((time.time() - t0) * 1000, 1)

        stderr_lines = result.stderr.splitlines()
        compile_ms = next((int(l.split(":")[1]) for l in stderr_lines if l.startswith("COMPILE_MS:")), None)
        run_ms     = next((int(l.split(":")[1]) for l in stderr_lines if l.startswith("RUN_MS:")), None)

        is_compile_timeout = any(l == "TIMEOUT_ERROR:COMPILE" for l in stderr_lines)
        is_run_timeout     = any(l == "TIMEOUT_ERROR:RUN"     for l in stderr_lines)

        clean_stderr = "\n".join(
            l for l in stderr_lines
            if not l.startswith(("COMPILE_MS:", "RUN_MS:", "TIMEOUT_ERROR:"))
        )
        stdout_clean = result.stdout

        if is_compile_timeout:
            return {"status": "compile_timeout", "message": "Compilation exceeded 5 seconds.", "compile_ms": compile_ms}
        if result.returncode != 0 and run_ms is None:
            return {"status": "compile_error", "stderr": clean_stderr, "compile_ms": compile_ms}
        if is_run_timeout:
            return {"status": "timeout", "message": "Execution exceeded 1 second.", "stdout": stdout_clean, "stderr": clean_stderr, "compile_ms": compile_ms, "run_ms": run_ms, "elapsed_ms": elapsed_ms}
        if result.returncode != 0:
            return {"status": "runtime_error", "message": f"Exited with code {result.returncode}", "stdout": stdout_clean, "stderr": clean_stderr, "compile_ms": compile_ms, "run_ms": run_ms, "elapsed_ms": elapsed_ms}

        return {"status": "ok", "stdout": stdout_clean, "stderr": clean_stderr, "exit_code": result.returncode, "compile_ms": compile_ms, "run_ms": run_ms, "elapsed_ms": elapsed_ms}

    except subprocess.TimeoutExpired:
        # Host timed out, aggressively kill container processes
        subprocess.run(["docker", "exec", "-u", "0", container, "sh", "-c", "killall -9 test g++ cc1plus || true"], capture_output=True)
        return {"status": "system_error", "message": "Container timed out.", "elapsed_ms": round((time.time() - t0) * 1000, 1)}

    finally:
        # Wipe the RAM disk completely clean for the next request
        subprocess.run(["docker", "exec", "-u", "0", container, "sh", "-c", "rm -rf /code/* /tmp/*"], capture_output=True)
        container_pool.put(container)
@app.post("/test")
async def test_judge(body: TestCode):
    return await run_in_threadpool(lambda: run_judge(body))

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=5893, reload=False)