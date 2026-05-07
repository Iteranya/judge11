from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import subprocess
import tempfile
import os

app = FastAPI()

# -------------------------------------------------------
# 🎨 Playground page at /
# -------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def playground():
    return FileResponse("index.html")

# -------------------------------------------------------
# 🧪 Test judge endpoint
# -------------------------------------------------------
class TestCode(BaseModel):
    code: str

@app.post("/test")
async def test_judge(body: TestCode):
    with tempfile.TemporaryDirectory() as tmpdir:
        cpp_file = os.path.join(tmpdir, "test.cpp")
        with open(cpp_file, "w") as f:
            f.write(body.code)

        # Compile
        compile_result = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{tmpdir}:/code", "-w", "/code",
             "--network=none", "--memory=128m",
             "gcc:latest", "g++", "-std=c++17", "test.cpp", "-o", "test"],
            capture_output=True, text=True, timeout=10,
        )

        if compile_result.returncode != 0:
            return {"status": "compile_error", "stderr": compile_result.stderr}

        # Run
        try:
            run_result = subprocess.run(
                ["docker", "run", "--rm",
                 "-v", f"{tmpdir}:/code", "-w", "/code",
                 "--network=none", "--memory=128m",
                 "gcc:latest", "./test"],
                capture_output=True, text=True, timeout=5,
            )
            return {
                "status": "ok",
                "stdout": run_result.stdout,
                "stderr": run_result.stderr,
                "exit_code": run_result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "message": "Took too long~"}
