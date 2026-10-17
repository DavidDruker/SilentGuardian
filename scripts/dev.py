"""One command: python scripts/dev.py. Installs/builds on first launch only."""
import os
import subprocess
import sys
import hashlib
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
os.chdir(ROOT)
PYTHON=ROOT/('.venv/Scripts/python.exe' if os.name=='nt' else '.venv/bin/python')
NPM='npm.cmd' if os.name=='nt' else 'npm'


def run(command, cwd=ROOT):
    subprocess.run([str(s) for s in command],cwd=cwd,check=True)


if not PYTHON.exists():
    run([sys.executable,'-m','venv','.venv'])
requirements_hash = hashlib.sha256((ROOT/'backend/requirements.txt').read_bytes()).hexdigest()
marker = ROOT/'.venv/guardian-dependencies.sha256'
if not marker.exists() or marker.read_text() != requirements_hash:
    run([PYTHON,'-m','pip','install','-r','backend/requirements.txt'])
    marker.write_text(requirements_hash)
if not (ROOT/'web/node_modules').exists():run([NPM,'ci'],ROOT/'web')
if not (ROOT/'web/public/models/pose_landmarker_lite.task').exists():run([NPM,'run','assets'],ROOT/'web')
if not (ROOT/'web/dist/index.html').exists() or '--build' in sys.argv:
    run([PYTHON,'-m','scripts.export_openapi'])
    run([NPM,'run','types'],ROOT/'web')
    run([NPM,'run','build'],ROOT/'web')
print('Silent Guardian: http://localhost:8000 — Ctrl+C stops the server.',flush=True)
try:
    run([PYTHON,'-m','uvicorn','backend.app.main:app','--host','127.0.0.1','--port','8000','--workers','1'])
except KeyboardInterrupt:
    pass
