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

# Added 'ulimit -f 10000' to prevent the C++ code from writing massive files to disk
RUNNER_SCRIPT = """\
#!/bin/sh
ulimit -f 10000 

START_C=$(date +%s%3N)
g++ -std=c++17 test.cpp -o test 2>/tmp/cerr
COMPILE_EXIT=$?
END_C=$(date +%s%3N)
echo "COMPILE_MS:$((END_C - START_C))" >&2

if [ $COMPILE_EXIT -ne 0 ]; then
    cat /tmp/cerr >&2
    exit $COMPILE_EXIT
fi

START_R=$(date +%s%3N)
./test
END_R=$(date +%s%3N)
echo "RUN_MS:$((END_R - START_R))" >&2
"""

@app.post("/test")
async def test_judge(body: TestCode):
    # Generate a unique name for this specific run
    container_name = f"judge_{uuid.uuid4().hex}"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "test.cpp"), "w") as f:
            f.write(body.code)
        with open(os.path.join(tmpdir, "run.sh"), "w") as f:
            f.write(RUNNER_SCRIPT)

        t0 = time.time()
        try:
            result = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "--name", container_name,        # Name the container so we can kill it later
                    "-v", f"{tmpdir}:/code", 
                    "-w", "/code",
                    "--network=none", 
                    "--memory=128m", 
                    "--cpus=0.5",                    # Limit CPU usage to 50% of one core
                    "--pids-limit=64",               # Prevent fork bombs (C++ system() calls)
                    "--log-driver=none",             # CRITICAL: Stops Docker from writing logs to your SSD
                    "gcc:latest", "sh", "run.sh"
                ],
                capture_output=True, text=True, timeout=15,
            )
            elapsed_ms = round((time.time() - t0) * 1000, 1)

            stderr_lines = result.stderr.splitlines()
            compile_ms = next((int(l.split(":")[1]) for l in stderr_lines if l.startswith("COMPILE_MS:")), None)
            run_ms     = next((int(l.split(":")[1]) for l in stderr_lines if l.startswith("RUN_MS:")), None)
            clean_stderr = "\n".join(l for l in stderr_lines if not l.startswith(("COMPILE_MS:", "RUN_MS:")))

            # Truncate massive outputs so they don't crash your FastAPI server memory
            stdout_clean = result.stdout[:10000] + ("\n...[truncated]" if len(result.stdout) > 10000 else "")

            if result.returncode != 0 and run_ms is None:
                return {
                    "status": "compile_error",
                    "stderr": clean_stderr,
                    "compile_ms": compile_ms,
                }

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
            # CRITICAL: Python stopped waiting, but Docker is still running. We MUST kill it.
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
            
            return {
                "status": "timeout",
                "message": "Execution timed out (15 seconds limit).",
                "elapsed_ms": round((time.time() - t0) * 1000, 1),
            }

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=5893, reload=False)