"""
FastAPI-based web service to browse server-side videos, preview them in browser, and cut clips quickly.

Features
- List videos from a configured directory
- In-browser preview with seeking (HTTP Range support)
- Pick in/out timecodes with UI and cut using ffmpeg
- Fast copy (`-c copy`) for keyframe-aligned cuts; optional re-encode for frame-accurate cuts
- Background job so the API responds immediately; simple job status polling

How to run
1) Install deps:  
   pip install fastapi uvicorn[standard] jinja2 python-multipart pydantic
2) Ensure ffmpeg is installed and on PATH.
3) Put your source videos under the VIDEO_DIR (default: ./videos). Create it if missing.
4) Start:  
   uvicorn app:app --host 0.0.0.0 --port 8000 --reload
5) Open http://localhost:8000

Tested with .mp4/.mov/.mkv. Add more extensions in ALLOWED_EXTS if needed.
"""

import os
import io
import json
import mimetypes
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, BackgroundTasks, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

# ------------------ Configuration ------------------
BASE_DIR = Path(__file__).parent.resolve()
VIDEO_DIR = BASE_DIR / "videos"
OUTPUT_DIR = BASE_DIR / "clips"
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
ALLOWED_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
VIDEO_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# ------------------ Utilities ------------------

def ffmpeg_exists() -> bool:
    return shutil.which("ffmpeg") is not None


def sanitize_name(name: str) -> str:
    # Prevent path traversal and weird chars
    safe = os.path.basename(name).replace("..", "").strip()
    return safe


def seconds_to_timestamp(sec: float) -> str:
    # Format seconds as HH:MM:SS.mmm
    msec = int(round((sec - int(sec)) * 1000))
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{msec:03d}"


# ------------------ Data Models ------------------
class SliceJob(BaseModel):
    id: str
    source: str
    start: float
    end: float
    mode: str = Field(description="copy or reencode")
    status: str = Field(default="queued")
    out_path: Optional[str] = None
    error: Optional[str] = None


# simple in-memory job store
JOBS: dict[str, SliceJob] = {}


# ------------------ FastAPI App ------------------
app = FastAPI(title="Video Slicer", version="1.0")

# Setup Jinja templates and static assets
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATE_DIR)


# ------------------ HTTP Range streaming ------------------

def iter_file_range(path: Path, start: int = 0, end: Optional[int] = None, chunk_size: int = 1024 * 1024):
    with path.open('rb') as f:
        f.seek(start)
        remaining = None if end is None else end - start + 1
        while True:
            read_size = chunk_size if remaining is None else min(chunk_size, remaining)
            data = f.read(read_size)
            if not data:
                break
            if remaining is not None:
                remaining -= len(data)
                if remaining <= 0:
                    yield data
                    break
            yield data


@app.get("/api/videos")
async def list_videos() -> List[dict]:
    files = []
    for p in VIDEO_DIR.iterdir():
        if p.is_file() and p.suffix.lower() in ALLOWED_EXTS:
            files.append({
                "name": p.name,
                "size": p.stat().st_size,
                "modified": int(p.stat().st_mtime)
            })
    files.sort(key=lambda x: x["name"].lower())
    return files


@app.get("/api/video/{name}")
async def stream_video(name: str, request: Request):
    safe = sanitize_name(name)
    path = (VIDEO_DIR / safe)
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Video not found")

    file_size = path.stat().st_size
    headers = {}
    range_header = request.headers.get('Range')
    if range_header:
        try:
            units, rng = range_header.split("=")
            start_s, end_s = rng.split("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1
            end = min(end, file_size - 1)
            if start > end or start < 0:
                raise ValueError
        except Exception:
            raise HTTPException(status_code=416, detail="Invalid Range header")

        content_length = end - start + 1
        headers.update({
            'Content-Range': f'bytes {start}-{end}/{file_size}',
            'Accept-Ranges': 'bytes',
            'Content-Length': str(content_length),
        })
        return StreamingResponse(
            iter_file_range(path, start, end),
            media_type=mimetypes.guess_type(str(path))[0] or 'application/octet-stream',
            status_code=206,
            headers=headers,
        )

    # no range
    headers.update({'Accept-Ranges': 'bytes', 'Content-Length': str(file_size)})
    return StreamingResponse(
        iter_file_range(path, 0, None),
        media_type=mimetypes.guess_type(str(path))[0] or 'application/octet-stream',
        headers=headers,
    )


class SliceRequest(BaseModel):
    name: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    mode: str = Field(default="copy", description="copy|reencode")
    out_basename: Optional[str] = None


@app.post("/api/slice")
async def slice_video(body: SliceRequest, bg: BackgroundTasks):
    if not ffmpeg_exists():
        raise HTTPException(500, "ffmpeg not found in PATH")

    safe = sanitize_name(body.name)
    src = VIDEO_DIR / safe
    if not src.exists():
        raise HTTPException(404, "Source video not found")

    if body.end <= body.start:
        raise HTTPException(400, "end must be greater than start")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = body.out_basename or f"{src.stem}_{int(body.start)}-{int(body.end)}_{body.mode}_{ts}"
    out_path = OUTPUT_DIR / f"{base}{src.suffix}"

    job_id = f"job_{ts}_{os.getpid()}_{len(JOBS)+1}"
    job = SliceJob(
        id=job_id,
        source=safe,
        start=body.start,
        end=body.end,
        mode=body.mode,
        status="queued",
        out_path=str(out_path.name)
    )
    JOBS[job_id] = job

    def run_job():
        job.status = "running"
        try:
            # Build ffmpeg command
            duration = body.end - body.start
            if body.mode == "copy":
                # fast, keyframe-aligned trims
                cmd = [
                    "ffmpeg", "-hide_banner", "-y",
                    "-ss", f"{body.start}",
                    "-i", str(src),
                    "-t", f"{duration}",
                    "-c", "copy",
                    str(out_path)
                ]
            else:
                # frame-accurate (re-encode)
                cmd = [
                    "ffmpeg", "-hide_banner", "-y",
                    "-ss", f"{body.start}",
                    "-i", str(src),
                    "-t", f"{duration}",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    "-c:a", "aac", "-b:a", "192k",
                    str(out_path)
                ]
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            job.status = "done"
        except subprocess.CalledProcessError as e:
            job.status = "error"
            job.error = e.stderr.decode(errors='ignore')[-800:]
        except Exception as e:  # noqa
            job.status = "error"
            job.error = str(e)

    bg.add_task(run_job)
    return {"job_id": job_id, "out_file": job.out_path}


@app.get("/api/job/{job_id}")
async def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.model_dump()


@app.get("/clips/{name}")
async def get_clip(name: str):
    safe = sanitize_name(name)
    path = OUTPUT_DIR / safe
    if not path.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(path)


# ------------------ Minimal UI ------------------
INDEX_HTML = TEMPLATE_DIR / "index.html"
if not INDEX_HTML.exists():
    INDEX_HTML.write_text(
        """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>视频切片工具</title>
  <link rel="stylesheet" href="/static/style.css" />
</head>
<body>
  <div class="container">
    <h1>服务器视频切片</h1>

    <div class="panel">
      <label>选择视频：</label>
      <select id="videoSelect"></select>
      <button id="refreshBtn">刷新</button>
    </div>

    <video id="player" controls preload="metadata" style="width:100%; max-height:50vh; background:#000;">
      您的浏览器不支持视频播放
    </video>

    <div class="panel grid">
      <div>
        <label>开始 (秒)：</label>
        <input id="startInput" type="number" min="0" step="0.1" />
        <button id="markIn">取当前为开始</button>
      </div>
      <div>
        <label>结束 (秒)：</label>
        <input id="endInput" type="number" min="0" step="0.1" />
        <button id="markOut">取当前为结束</button>
      </div>
      <div>
        <label>模式：</label>
        <select id="mode">
          <option value="copy">快速（不重编码）</option>
          <option value="reencode">精确（重编码）</option>
        </select>
      </div>
      <div>
        <label>输出文件名（可选）：</label>
        <input id="outBase" type="text" placeholder="不填则自动生成" />
      </div>
    </div>

    <div class="panel">
      <button id="sliceBtn">开始切片</button>
      <span id="status"></span>
    </div>

    <div id="result"></div>
  </div>

<script>
const videoSelect = document.getElementById('videoSelect');
const player = document.getElementById('player');
const startInput = document.getElementById('startInput');
const endInput = document.getElementById('endInput');
const statusEl = document.getElementById('status');

async function loadVideos() {
  videoSelect.innerHTML = '';
  const res = await fetch('/api/videos');
  const list = await res.json();
  list.forEach(v => {
    const opt = document.createElement('option');
    opt.value = v.name;
    opt.textContent = `${v.name} (${(v.size/1024/1024).toFixed(1)} MB)`;
    videoSelect.appendChild(opt);
  });
  if (list.length) {
    videoSelect.value = list[0].name;
    setVideoSrc(list[0].name);
  }
}

function setVideoSrc(name) {
  player.src = `/api/video/${encodeURIComponent(name)}`;
  player.load();
}

videoSelect.addEventListener('change', () => setVideoSrc(videoSelect.value));

document.getElementById('refreshBtn').addEventListener('click', loadVideos);

document.getElementById('markIn').addEventListener('click', () => {
  startInput.value = player.currentTime.toFixed(3);
});

document.getElementById('markOut').addEventListener('click', () => {
  endInput.value = player.currentTime.toFixed(3);
});

async function createSlice() {
  const name = videoSelect.value;
  const start = parseFloat(startInput.value || '0');
  const end = parseFloat(endInput.value || '0');
  const mode = document.getElementById('mode').value;
  const outBase = document.getElementById('outBase').value || null;

  if (!(end > start)) {
    alert('结束时间必须大于开始时间');
    return;
  }

  statusEl.textContent = '提交切片任务…';

  const res = await fetch('/api/slice', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, start, end, mode, out_basename: outBase })
  });

  if (!res.ok) {
    const err = await res.text();
    alert('创建任务失败：' + err);
    statusEl.textContent = '';
    return;
  }

  const data = await res.json();
  pollJob(data.job_id, data.out_file);
}

document.getElementById('sliceBtn').addEventListener('click', createSlice);

async function pollJob(jobId, outFile) {
  statusEl.textContent = '处理中…';
  const res = await fetch(`/api/job/${jobId}`);
  const job = await res.json();
  if (job.status === 'done') {
    statusEl.textContent = '完成！';
    document.getElementById('result').innerHTML = `
      <p>输出： <a href="/clips/${encodeURIComponent(outFile)}" target="_blank">${outFile}</a></p>
      <video controls style="width:100%; max-height:40vh; background:#000;" src="/clips/${encodeURIComponent(outFile)}"></video>
    `;
  } else if (job.status === 'error') {
    statusEl.textContent = '出错';
    document.getElementById('result').innerHTML = `<pre>${job.error || 'Unknown error'}</pre>`;
  } else {
    setTimeout(() => pollJob(jobId, outFile), 800);
  }
}

loadVideos();
</script>
</body>
</html>
        """,
        encoding="utf-8",
    )

# Minimal CSS
(STATIC_DIR / "style.css").write_text(
    """
:root { --bg:#0b1324; --fg:#eef2ff; --muted:#94a3b8; --card:#111827; --accent:#60a5fa; }
*{ box-sizing:border-box; }
body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto; background:var(--bg); color:var(--fg); }
.container { max-width: 980px; margin: 24px auto; padding: 0 16px; }
.panel { background: var(--card); border-radius: 16px; padding: 12px 16px; margin: 12px 0; box-shadow: 0 6px 24px rgba(0,0,0,.25); }
.grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
label { display:block; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
input, select, button { width: 100%; padding: 8px 10px; border-radius: 10px; border: 1px solid #263041; background: #0f172a; color: var(--fg); }
button { cursor: pointer; }
h1 { font-size: 24px; margin: 8px 0 12px; }
pre { white-space: pre-wrap; background:#0f172a; padding:12px; border-radius:12px; }
""",
    encoding="utf-8",
)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
