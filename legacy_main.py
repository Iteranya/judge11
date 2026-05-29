import time
import subprocess
import tempfile
import os
import uuid

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", response_class=HTMLResponse)
async def playground():
    return FileResponse("index.html")

class TestCode(BaseModel):
    code: str

# Added Linux "timeout" commands for precise limits on compile and run times
RUNNER_SCRIPT = """\
#!/bin/sh
ulimit -f 10000 

START_C=$(date +%s%3N)
# 1. Enforce 5-second compilation limit
timeout 5s g++ -std=c++17 test.cpp -o test 2>/tmp/cerr
COMPILE_EXIT=$?
END_C=$(date +%s%3N)
echo "COMPILE_MS:$((END_C - START_C))" >&2

if [ $COMPILE_EXIT -ne 0 ]; then
    # Exit code 124 means 'timeout' killed the process
    if [ $COMPILE_EXIT -eq 124 ]; then
        echo "TIMEOUT_ERROR:COMPILE" >&2
    else
        cat /tmp/cerr >&2
    fi
    exit $COMPILE_EXIT
fi

START_R=$(date +%s%3N)
# 2. Enforce 1-second runtime limit
timeout 1s ./test
RUN_EXIT=$?
END_R=$(date +%s%3N)
echo "RUN_MS:$((END_R - START_R))" >&2

if [ $RUN_EXIT -eq 124 ]; then
    echo "TIMEOUT_ERROR:RUN" >&2
fi

exit $RUN_EXIT
"""

@app.post("/test")
async def test_judge(body: TestCode):
    container_name = f"judge_{uuid.uuid4().hex}"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "test.cpp"), "w") as f:
            f.write(body.code)
        with open(os.path.join(tmpdir, "run.sh"), "w") as f:
            f.write(RUNNER_SCRIPT)

        t0 = time.time()
        try:
            # We set Python's timeout to 10 seconds as a "last resort" safety net.
            # The script inside docker will finish in at most 6 seconds (5s compile + 1s run).
            result = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "--name", container_name,
                    "-v", f"{tmpdir}:/code", 
                    "-w", "/code",
                    "--network=none", 
                    "--memory=128m", 
                    "--cpus=0.5",                    
                    "--pids-limit=64",               
                    "--log-driver=none",             
                    "gcc:latest", "sh", "run.sh"
                ],
                capture_output=True, text=True, timeout=10,
            )
            elapsed_ms = round((time.time() - t0) * 1000, 1)

            # Parse timing and custom markers out of stderr
            stderr_lines = result.stderr.splitlines()
            compile_ms = next((int(l.split(":")[1]) for l in stderr_lines if l.startswith("COMPILE_MS:")), None)
            run_ms     = next((int(l.split(":")[1]) for l in stderr_lines if l.startswith("RUN_MS:")), None)
            
            is_compile_timeout = any(l == "TIMEOUT_ERROR:COMPILE" for l in stderr_lines)
            is_run_timeout     = any(l == "TIMEOUT_ERROR:RUN" for l in stderr_lines)

            # Filter out our hidden variables so the user doesn't see them
            clean_stderr = "\n".join(
                l for l in stderr_lines 
                if not l.startswith(("COMPILE_MS:", "RUN_MS:", "TIMEOUT_ERROR:"))
            )

            # Truncate massive outputs
            stdout_clean = result.stdout[:10000] + ("\n...[truncated]" if len(result.stdout) > 10000 else "")

            # 1. Handle Compilation Timeout
            if is_compile_timeout:
                return {
                    "status": "compile_timeout",
                    "message": "Compilation exceeded the 5-second limit.",
                    "compile_ms": compile_ms
                }

            # 2. Handle Normal Compilation Error
            if result.returncode != 0 and run_ms is None:
                return {
                    "status": "compile_error",
                    "stderr": clean_stderr,
                    "compile_ms": compile_ms,
                }

            # 3. Handle Runtime Timeout (Time Limit Exceeded - TLE)
            if is_run_timeout:
                return {
                    "status": "timeout",
                    "message": "Execution exceeded the 1-second limit.",
                    "stdout": stdout_clean,
                    "stderr": clean_stderr,
                    "compile_ms": compile_ms,
                    "run_ms": run_ms,
                    "elapsed_ms": elapsed_ms,
                }
            
            # 4. Handle Runtime Error (Segfaults, dividing by zero, etc.)
            if result.returncode != 0:
                return {
                    "status": "runtime_error",
                    "message": f"Process exited with error code {result.returncode}",
                    "stdout": stdout_clean,
                    "stderr": clean_stderr,
                    "compile_ms": compile_ms,
                    "run_ms": run_ms,
                    "elapsed_ms": elapsed_ms,
                }

            # 5. Success
            return {
                "status": "ok",
                "stdout": stdout_clean,
                "stderr": clean_stderr,
                "exit_code": result.returncode,
                "compile_ms": compile_ms,
                "run_ms": run_ms,
                "elapsed_ms": elapsed_ms,
            }
            
        except subprocess.TimeoutExpired:
            # This will only hit if Docker itself freezes.
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
            return {
                "status": "system_error",
                "message": "Docker container unexpectedly locked up.",
                "elapsed_ms": round((time.time() - t0) * 1000, 1),
            }

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=5893, reload=False)