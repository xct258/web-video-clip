import os
import shutil
import subprocess
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Body
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

# ------------------ 配置 ------------------
BASE_DIR = Path(__file__).parent.resolve()
VIDEO_DIR = BASE_DIR / "videos"
OUTPUT_DIR = BASE_DIR / "clips"
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
ALLOWED_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

for d in [VIDEO_DIR, OUTPUT_DIR, TEMPLATE_DIR, STATIC_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ------------------ 工具函数 ------------------
def ffmpeg_exists() -> bool:
    return shutil.which("ffmpeg") is not None

def sanitize_name(name: str) -> Path:
    path = (VIDEO_DIR / name).resolve()
    if not str(path).startswith(str(VIDEO_DIR.resolve())):
        raise HTTPException(400, "非法路径")
    return path

def iter_file_range(path: Path, start: int = 0, end: Optional[int] = None, chunk_size: int = 1024*1024):
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

# ------------------ 数据模型 ------------------
class SliceRequest(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)

class VideoClip(BaseModel):
    name: str
    clips: List[SliceRequest]

class MultiVideoRequest(BaseModel):
    videos: List[VideoClip]
    out_basename: Optional[str] = None
    username: str  # 新增字段

class SliceJob(BaseModel):
    id: str
    source: str
    status: str = Field(default="queued")
    out_path: Optional[str] = None
    error: Optional[str] = None

# ------------------ FastAPI 应用 ------------------
app = FastAPI(title="Multi-Video Slicer", version="1.4")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATE_DIR)
JOBS: dict[str, SliceJob] = {}

# ------------------ 视频列表与流 ------------------
@app.get("/api/videos")
async def list_videos() -> List[dict]:
    files = []
    for p in VIDEO_DIR.rglob("*"):
        if p.is_file() and p.suffix.lower() in ALLOWED_EXTS:
            rel_path = p.relative_to(VIDEO_DIR)
            files.append({
                "name": str(rel_path),
                "size": p.stat().st_size,
                "modified": int(p.stat().st_mtime)
            })
    files.sort(key=lambda x: x["name"].lower())
    return files

@app.get("/api/video/{name:path}")
async def stream_video(name: str, request: Request):
    path = sanitize_name(name)
    if not path.exists():
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
            raise HTTPException(416, "Invalid Range header")
        headers.update({
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1)
        })
        return StreamingResponse(
            iter_file_range(path, start, end),
            media_type=mimetypes.guess_type(str(path))[0] or "application/octet-stream",
            status_code=206,
            headers=headers
        )
    headers.update({'Accept-Ranges': 'bytes', 'Content-Length': str(file_size)})
    return StreamingResponse(
        iter_file_range(path),
        media_type=mimetypes.guess_type(str(path))[0] or "application/octet-stream",
        headers=headers
    )

# ------------------ 视频目录树 ------------------
@app.get("/api/tree")
async def video_tree():
    def build_tree(path: Path):
        tree = []
        for p in sorted(path.iterdir(), key=lambda x: x.name.lower()):
            if p.is_dir():
                tree.append({
                    "type": "dir",
                    "name": p.name,
                    "children": build_tree(p)
                })
            elif p.is_file() and p.suffix.lower() in ALLOWED_EXTS:
                tree.append({
                    "type": "file",
                    "name": str(p.relative_to(VIDEO_DIR))
                })
        return tree
    return build_tree(VIDEO_DIR)

# ------------------ 批量切片并合并 ------------------
@app.post("/api/slice_merge_all")
async def slice_merge_all(body: MultiVideoRequest = Body(...), bg: BackgroundTasks = None):
    username = getattr(body, "username", None) or "user"
    if not ffmpeg_exists():
        raise HTTPException(500, "ffmpeg not found in PATH")
    
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    # 输出文件名改为：用户名-原本名字-时间戳.mp4
    out_basename = f"{username}-{body.out_basename or 'merged'}-{ts}"
    out_path = OUTPUT_DIR / f"{out_basename}.mp4"

    job_id = f"job_{ts}_{os.getpid()}"
    job = SliceJob(id=job_id, source="multiple", status="queued", out_path=str(out_path.name))
    JOBS[job_id] = job

    def run_merge(job: SliceJob, videos: List[VideoClip], out_path: Path):
        job.status = "running"
        temp_files = []
        try:
            for vid_idx, vid in enumerate(videos):
                src = sanitize_name(vid.name)
                if not src.exists():
                    raise FileNotFoundError(f"{vid.name} not found")
                for i, clip in enumerate(vid.clips):
                    if clip.end <= clip.start:
                        raise ValueError(f"Clip end <= start in {vid.name}")
                    tmp = OUTPUT_DIR / f"{job.id}_{vid_idx}_{i}{src.suffix}"
                    duration = clip.end - clip.start
                    cmd = ["ffmpeg","-hide_banner","-y","-ss",str(clip.start),"-i",str(src),"-t",str(duration),"-c","copy",str(tmp)]
                    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    temp_files.append(tmp)
            
            list_file = OUTPUT_DIR / f"{job.id}_list.txt"
            with list_file.open("w", encoding="utf-8") as f:
                for tmp in temp_files:
                    f.write(f"file '{tmp.name}'\n")

            cmd_concat = ["ffmpeg","-hide_banner","-y","-f","concat","-safe","0","-i",str(list_file),"-c","copy",str(out_path)]
            subprocess.run(cmd_concat, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            job.status = "done"
        except Exception as e:
            job.status = "error"
            job.error = str(e)
        finally:
            for tmp in temp_files + [list_file]:
                if tmp.exists(): tmp.unlink()
    
    bg.add_task(run_merge, job, body.videos, out_path)
    return {"job_id": job_id, "out_file": job.out_path}


@app.get("/api/job/{job_id}")
async def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.model_dump()

@app.get("/clips/{name}")
async def get_clip(name: str):
    path = OUTPUT_DIR / name
    if not path.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(path)

# ------------------ B站投稿（测试模式） ------------------
@app.post("/api/upload_bili")
async def upload_bili(data: dict = Body(...)):
    file_name = data.get("file")
    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    tags = data.get("tags", "").strip()

    if not file_name or not title:
        return {"success": False, "message": "文件名或标题不能为空"}

    video_path = OUTPUT_DIR / file_name
    if not video_path.exists():
        return {"success": False, "message": "切片视频不存在"}

    # 构建将要执行的命令
    cmd = [
        "apps/biliup-rs",
        "-u", "cookies/bilibili/cookies-烦心事远离.json",
        "upload",
        "--copyright", "2",
        "--source", "https://live.bilibili.com/1962720",
        "--tid", "17",
        "--title", title,
        "--desc", description,
        "--tag", tags,
        str(video_path)
    ]

    # 测试模式：不真正执行，返回命令字符串
    # return {
    #     "success": True,
    #     "message": "测试模式，不会实际上传",
    #     "cmd_preview": " ".join(cmd)
    # }

    # 真实上传模式（调试完再打开）
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return {"success": True, "output": result.stdout}
    except subprocess.CalledProcessError as e:
        return {"success": False, "error": e.stderr}

# ------------------ 前端 HTML & CSS ------------------
INDEX_HTML = TEMPLATE_DIR / "index.html"
INDEX_HTML.write_text("""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>切片</title>
<link rel="stylesheet" href="/static/style.css">

<!-- 内联默认强调色 -->
<style>
:root {
    --accent-color: rgb(79,158,255);
    --btn-bg: rgba(79,158,255,0.4);
    --btn-hover: rgba(79,158,255,0.6);
}
</style>
</head>
<body>

<!-- 加载层 -->
<div id="loadingOverlay" class="loading-overlay">加载中...</div>

<!-- 主内容 -->
<div class="container" id="mainContent" style="display:none;">
<h1>括弧笑直播切片工具</h1>

<div class="panel">
<label>选择视频文件：</label>
<div id="fileTree" class="file-tree"></div>
</div>

<div id="videoContainer" class="video-placeholder-container">
    <span id="videoPlaceholder" class="video-placeholder-text">请选择视频文件</span>
    <video id="player" controls preload="metadata" style="display:none;"></video>
    <button id="previewBtn" style="margin-top:10px;">预览</button>
</div>

<div class="panel grid">
<div>
<label>开始：</label>
<input id="startInput" type="text" placeholder="HH:MM:SS">
<button id="markIn">取当前为开始</button>
</div>
<div>
<label>结束：</label>
<input id="endInput" type="text" placeholder="HH:MM:SS">
<button id="markOut">取当前为结束</button>
</div>
</div>

<div class="panel">
<button id="addClip">添加片段</button>
<div id="clipList"></div>
</div>

<div class="panel">
    <label>请输入用户名：</label>
    <input id="usernameInput" type="text" placeholder="输入您的B站名称" style="width:100%; margin-bottom:8px;">
</div>

<div class="panel">
<button id="mergeAllBtn">开始切片合并</button>
<div id="mergeStatus"></div>
<div id="mergeResult"></div>
</div>

</div>

<script>
let currentVideoName = '';
let currentClips = [];
let videoTasks = [];
const player = document.getElementById('player');
const startInput = document.getElementById('startInput');
const endInput = document.getElementById('endInput');
const clipList = document.getElementById('clipList');
const mergeStatus = document.getElementById('mergeStatus');
const mergeResult = document.getElementById('mergeResult');
const fileTreeDiv = document.getElementById('fileTree');

const dynamicImageUrl = 'https://random-image.xct258.top/';

// ------------------ 时间转换 ------------------
function formatTime(seconds) {
    seconds = Math.floor(seconds);
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    const hh = hrs > 0 ? String(hrs).padStart(2,'0') + ':' : '';
    const mm = (hrs > 0 || mins > 0) ? String(mins).padStart(2,'0') + ':' : '';
    const ss = String(secs).padStart(2,'0');
    return hh + mm + ss;
}

function parseTime(str){
    const parts = str.split(':').map(Number);
    if(parts.length === 3) return parts[0]*3600 + parts[1]*60 + parts[2];
    if(parts.length === 2) return parts[0]*60 + parts[1];
    if(parts.length === 1) return parts[0];
    return 0;
}

// ------------------ 获取并设置强调色 ------------------
async function fetchThemeColor(url) {
    try {
        const res = await fetch(url, { method: 'HEAD' });
        const themeColor = res.headers.get('x-theme-color');
        return themeColor || '79,158,255';
    } catch (e) {
        console.warn('无法获取 x-theme-color', e);
        return '79,158,255';
    }
}

function setAccentColor(rgbString) {
    const rgb = rgbString.match(/\d+/g).join(',');
    document.documentElement.style.setProperty('--accent-color', `rgb(${rgb})`);
    document.documentElement.style.setProperty('--btn-bg', `rgba(${rgb},0.4)`);
    document.documentElement.style.setProperty('--btn-hover', `rgba(${rgb},0.6)`);
}

// ------------------ 背景图预加载 ------------------
function preloadImage(url) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.src = url;
        img.onload = resolve;
        img.onerror = reject;
    });
}

// ------------------ 页面初始化 ------------------
async function initPage() {
    const loadingOverlay = document.getElementById('loadingOverlay');
    const mainContent = document.getElementById('mainContent');

    try {
        const themeColor = await fetchThemeColor(dynamicImageUrl);
        setAccentColor(themeColor);
        await preloadImage(dynamicImageUrl);
        document.body.style.backgroundImage = `url(${dynamicImageUrl})`;
    } catch(e) {
        console.warn("背景图片加载失败，使用默认背景", e);
        document.body.style.backgroundColor = "rgba(10,14,28,0.95)";
    } finally {
        loadingOverlay.style.display = 'none';
        mainContent.style.display = 'block';
        loadFileTree();
        updateMarkButtons();  // 初始化按钮状态
    }
}

previewBtn.style.display = 'none'; // 初始化隐藏


const markInBtn = document.getElementById('markIn');
const markOutBtn = document.getElementById('markOut');

// 初始化状态
function updateMarkButtons() {
    if (!currentVideoName || player.style.display === 'none') {
        markInBtn.textContent = '请输入片段时间';
        markInBtn.disabled = true;
        markOutBtn.textContent = '请输入片段时间';
        markOutBtn.disabled = true;
    } else {
        markInBtn.textContent = '取当前为开始';
        markInBtn.disabled = false;
        markOutBtn.textContent = '取当前为结束';
        markOutBtn.disabled = false;
    }
}



// ------------------ 文件树 ------------------
async function loadFileTree() {
    const res = await fetch('/api/tree');
    const tree = await res.json();
    fileTreeDiv.innerHTML = '';

    function createTree(nodes) {
        const ul = document.createElement('ul');
        nodes.forEach(node => {
            const li = document.createElement('li');
            const span = document.createElement('span');
            span.textContent = node.name;
            if(node.type === 'dir') {
                span.className = 'dir';
                li.appendChild(span);
                if(node.children && node.children.length) li.appendChild(createTree(node.children));
                li.classList.add('collapsed');
                span.addEventListener('click', e => { e.stopPropagation(); li.classList.toggle('collapsed'); });
            } else {
                span.className = 'file';
                const previewBtn = document.getElementById('previewBtn');
                span.addEventListener('click', e => {
                    e.stopPropagation();

                    // 停止当前播放的视频
                    player.pause();
                    player.currentTime = 0;
                    player.style.display = 'none';
                    document.getElementById('videoPlaceholder').style.display = 'block';
                    document.getElementById('videoPlaceholder').textContent = '点击预览播放视频';
                    previewBtn.style.display = 'inline-block'; // 显示预览按钮

                    // 更新当前任务和片段
                    videoTasks = videoTasks.filter(v => v.clips.length > 0);
                    currentVideoName = node.name;
                    if(!videoTasks.find(v => v.name === currentVideoName)) {
                        videoTasks.push({name: currentVideoName, clips: []});
                    }
                    currentClips = videoTasks.find(v => v.name === currentVideoName).clips;
                    renderClipList();
                    updateMarkButtons(); // 更新按钮状态
                });


                li.appendChild(span);
            }
            ul.appendChild(li);
        });
        return ul;
    }
    fileTreeDiv.appendChild(createTree(tree));
}

// ------------------ 渲染片段列表 ------------------
function renderClipList(){
    clipList.innerHTML='';
    let globalIdx = 1;
    videoTasks.forEach(video=>{
        const div = document.createElement('div');
        div.innerHTML = `<strong>${video.name}</strong>`;
        video.clips.forEach((c, idx)=>{
            const d = document.createElement('div');
            d.style.marginLeft = '16px';
            const btn = document.createElement('button');
            btn.textContent = '删除';
            btn.style.marginLeft = '8px';
            btn.addEventListener('click', ()=>{ video.clips.splice(idx, 1); renderClipList(); });
            const startFmt = formatTime(c.start);
            const endFmt = formatTime(c.end);
            d.textContent = `片段 ${globalIdx}: ${startFmt} - ${endFmt}`;
            d.appendChild(btn);
            div.appendChild(d);
            globalIdx++;
        });
        clipList.appendChild(div);
    });
}

// ------------------ 标记按钮 ------------------
markInBtn.addEventListener('click', () => {
    if (currentVideoName) startInput.value = formatTime(player.currentTime);
});
markOutBtn.addEventListener('click', () => {
    if (currentVideoName) endInput.value = formatTime(player.currentTime);
});

// ------------------ 添加片段 ------------------
document.getElementById('addClip').addEventListener('click', () => {
    if (!currentVideoName) {   // 新增判断
        alert('请先选择一个视频文件');
        return;
    }

    const start = parseTime(startInput.value);
    const end = parseTime(endInput.value);
    if (end <= start) {
        alert('结束必须大于开始');
        return;
    }

    const task = videoTasks.find(v => v.name === currentVideoName);
    if (task) {
        const overlap = task.clips.some(c => !(end <= c.start || start >= c.end));
        if (overlap) {
            alert('片段时间重叠，请调整');
            return;
        }
        task.clips.push({ start, end });
    }
    renderClipList();
});


// ------------------ 提交任务 ------------------
document.getElementById('mergeAllBtn').addEventListener('click', async ()=> {
    if(videoTasks.length===0){ 
        alert('请至少添加一个视频片段'); 
        return; 
    }

    const username = document.getElementById('usernameInput').value.trim();
    if(!username) {
        alert('请输入用户名');
        return;
    }

    mergeStatus.textContent='提交任务...';

    // POST 时把 username 也传给后端
    const res = await fetch('/api/slice_merge_all', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({videos:videoTasks, username: username})
    });
    const data = await res.json();
    pollJob(data.job_id);
});

async function pollJob(jobId){
    const res = await fetch(`/api/job/${jobId}`);
    const job = await res.json();
    if(job.status === 'done'){
        mergeStatus.textContent = '完成';
        mergeResult.innerHTML = `
            <p>输出文件: ${job.out_path}</p>
            <div style="display:flex; gap:12px; align-items:center; margin-top:8px;">
                <a href="/clips/${encodeURIComponent(job.out_path)}" download>
                    <button>下载切片</button>
                </a>
                <button id="submitVideoBtn">烦心事远离账号投稿</button>
            </div>
            <div id="videoFormContainer" style="margin-top:12px;"></div>
        `;

        const submitBtn = document.getElementById('submitVideoBtn');
        submitBtn.addEventListener('click', () => {
            const container = document.getElementById('videoFormContainer');
            if(container.innerHTML.trim() !== '') return; // 已经生成过表单
            container.innerHTML = `
                <div id="uploadForm" style="margin-top:12px;">
                    <label>视频标题：</label><br>
                    <input id="videoTitle" type="text" placeholder="输入视频标题" style="width:100%; margin-bottom:8px;">
                    <label>视频简介：</label><br>
                    <input id="videoDesc" type="text" placeholder="输入视频简介" style="width:100%; margin-bottom:8px;">
                    <label>视频标签 (空格分隔)：</label><br>
                    <input id="videoTags" type="text" placeholder="标签1 标签2 ..." style="width:100%; margin-bottom:8px;">
                    <button id="uploadBiliBtn">投稿到B站</button>
                    <div id="uploadResult" style="margin-top:8px;"></div>
                </div>
            `;

            const uploadBtn = document.getElementById('uploadBiliBtn');
            const formDiv = document.getElementById('uploadForm');

            uploadBtn.addEventListener('click', async () => {
                const fileName = job.out_path;
                const title = document.getElementById('videoTitle').value.trim();
                const desc = document.getElementById('videoDesc').value.trim();
                let tags = document.getElementById('videoTags').value.trim();
                tags = tags.split(/\s+/).filter(t => t).join(',');

                // 必填验证
                if(!fileName || !title){
                    alert('标题不能为空');
                    return;
                }

                // 禁用表单，显示投稿提示 **仅在验证通过后**
                formDiv.querySelectorAll('input, button').forEach(el => el.disabled = true);
                uploadBtn.textContent = '投稿中...';
                const resultDiv = document.getElementById('uploadResult');
                resultDiv.innerHTML = `<p style="color:orange;">投稿过程中可能会因为网络原因出现异常提示，不用担心，投稿正在进行中，可以联系xct258获取帮助...</p>`;

                const userName = document.getElementById('usernameInput').value.trim();
                const fullDesc = `投稿用户：${userName}\n${desc}\n使用 Web 投稿工具切片投稿`;

                try {
                    const res = await fetch('/api/upload_bili', {
                        method: 'POST',
                        headers: {'Content-Type':'application/json'},
                        body: JSON.stringify({file:fileName, title, description:fullDesc, tags})
                    });
                    const data = await res.json();

                    if(data.success){
                        uploadBtn.textContent = '投稿成功！';
                        uploadBtn.disabled = true;
                        resultDiv.innerHTML = `<pre style="color:green;">投稿成功：\n${data.cmd_preview || data.output || data.message || "成功"}</pre>`;
                    } else {
                        uploadBtn.textContent = '投稿失败！';
                        formDiv.querySelectorAll('input, button').forEach(el => el.disabled = false);
                        resultDiv.innerHTML = `<pre style="color:red;">投稿失败：\n${data.error || data.message}</pre>`;
                    }
                } catch(e){
                    console.error(e);
                    uploadBtn.textContent = '投稿异常！';
                    formDiv.querySelectorAll('input, button').forEach(el => el.disabled = false);
                    resultDiv.innerHTML = `<pre style="color:red;">投稿异常：\n${e}</pre>`;
                }
            });
        }, {once:true});
    } else if(job.status === 'error'){
        mergeStatus.textContent = '出错';
        mergeResult.innerHTML = `<pre>${job.error || 'Unknown error'}</pre>`;
    } else {
        mergeStatus.textContent = '处理中...';
        setTimeout(() => pollJob(jobId), 800);
    }
}


// ------------------ 初始化页面 ------------------
initPage();



// 绑定预览按钮点击事件
previewBtn.addEventListener('click', () => {
    if(currentVideoName){
        player.src = `/api/video/${encodeURIComponent(currentVideoName)}`;
        player.style.display = 'block';
        document.getElementById('videoPlaceholder').style.display = 'none';
        player.play();
        previewBtn.style.display = 'none';
        updateMarkButtons(); // 重新更新按钮状态
    } else {
        alert('请先选择视频');
    }
});

</script>

</body>
</html>
""", encoding="utf-8")

(STATIC_DIR / "style.css").write_text("""
/* ------------------ 全局变量 ------------------ */
:root {
    --bg-color: rgba(10, 14, 28, 0.95);
    --bg-image: url('https://random-image.xct258.top/');
    --fg-color: #f0f0f0;
    --muted-color: #a0a0a0;

    --accent-color: #4f9eff;
    --accent-hover: #3b7fd4;

    --panel-bg: rgba(20, 25, 40, 0.6);
    --clip-bg: rgba(30, 35, 55, 0.6);
    --border-color: rgba(255, 255, 255, 0.1);

    --btn-bg: rgba(79, 158, 255, 0.4);
    --btn-hover: rgba(79, 158, 255, 0.6);

    --radius-lg: 16px;
    --radius-md: 12px;
    --radius-sm: 8px;
    --shadow-lg: 0 8px 20px rgba(0,0,0,0.5);
    --shadow-md: 0 6px 16px rgba(0,0,0,0.6);
    --shadow-sm: 0 4px 12px rgba(0,0,0,0.3);
}

/* ------------------ 全局基础 ------------------ */
* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
    font-family: 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;
    color: var(--fg-color);
    -webkit-tap-highlight-color: transparent;
    user-select: none;
}

body {
    background: var(--bg-color) no-repeat center center fixed;
    background-image: var(--bg-image);
    background-size: cover;
    min-height: 100vh;
    line-height: 1.6;
}

.container {
    max-width: 980px;
    margin: 20px auto;
    padding: 0 16px;
}

h1 {
    text-align: center;
    font-size: 32px;
    margin-bottom: 20px;
    color: var(--accent-color);
    text-shadow: 0 0 6px rgba(0,0,0,0.7);
}

/* ------------------ 毛玻璃统一样式 ------------------ */
.glass {
    background: var(--panel-bg);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-sm);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
}

/* ------------------ 面板 ------------------ */
.panel {
    padding: 20px;
    margin: 16px 0;
}
.panel, .video-placeholder-container, #clipList > div, pre {
    /* 应用统一毛玻璃样式 */
    background: var(--panel-bg);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-sm);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
}

/* ------------------ 输入区域 ------------------ */
.panel.grid {
    display: flex;
    gap: 20px;
    flex-wrap: wrap;
}

.panel.grid div {
    display: flex;
    flex-direction: column;
    gap: 8px; /* 输入和按键间距 */
    flex: 1;
}

input[type="text"], select {
    padding: 6px 10px;
    font-size: 13px;
    border-radius: var(--radius-sm);
    background: var(--clip-bg);
    border: 1px solid var(--border-color);
    color: var(--fg-color);
    outline: none;
    transition: all 0.2s;
}

input:focus, select:focus {
    border-color: var(--accent-color);
    box-shadow: 0 0 8px rgba(79,158,255,0.6);
}

/* ------------------ 按钮 ------------------ */
button {
    padding: 6px 10px;
    font-size: 13px;
    border-radius: var(--radius-sm);
    background: var(--btn-bg);
    color: #fff;
    font-weight: 600;
    border: 1px solid rgba(255,255,255,0.2);
    cursor: pointer;
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    box-shadow: 0 3px 8px rgba(0,0,0,0.25);
    transition: all 0.2s ease-in-out, transform 0.1s ease-in-out;
    align-self: flex-start;
}

button:hover {
    background: var(--btn-hover);
    transform: translateY(-1px);
    box-shadow: 0 4px 10px rgba(0,0,0,0.3);
}

button:active {
    background: rgba(79, 158, 255, 0.3);
    transform: translateY(0);
    box-shadow: 0 2px 6px rgba(0,0,0,0.2);
}

/* ------------------ 视频播放器 ------------------ */
.video-placeholder-container {
    width: 100%;
    max-height: 60vh;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 18px;
    font-weight: bold;
    position: relative;
    overflow: hidden;
}

.video-placeholder-text {
    text-align: center;
    padding: 16px;
    color: var(--muted-color);
}

#player {
    width: 100%;
    max-height: 60vh;
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-md);
}

/* ------------------ 文件树 ------------------ */
.file-tree {
    max-height: 400px;
    overflow-y: auto;
    background: var(--panel-bg);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-sm);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
}

.file-tree ul {
    list-style: none;
    padding-left: 20px;
}

.file-tree li {
    cursor: pointer;
    margin: 6px 0;
    padding: 4px 8px;
    border-radius: var(--radius-sm);
    transition: background 0.2s, transform 0.1s;
}

.file-tree li:hover {
    background: rgba(79,158,255,0.15);
}

.file-tree li span.dir::before { content: "📁 "; }
.file-tree li span.file::before { content: "🎬 "; }

.file-tree li.collapsed > ul {
    display: none;
}

/* ------------------ 片段列表 ------------------ */
#clipList {
    margin-top: 12px;
}

#clipList > div {
    padding: 10px 14px;
    margin-bottom: 10px;
    border-radius: var(--radius-md);
}

/* ------------------ 输出结果 ------------------ */
#mergeResult video {
    margin-top: 14px;
    border-radius: var(--radius-md);
    max-height: 220px;
    width: 100%;
}

pre {
    white-space: pre-wrap;
    padding: 14px;
    overflow-x: auto;
}

/* ------------------ 响应式 ------------------ */
@media (max-width: 768px) {
    h1 { font-size: 26px; }
    .panel { padding: 16px; }
    #player { max-height: 45vh; }
    .file-tree { max-height: 300px; }
    #mergeResult video { max-height: 160px; }
}

@media (max-width: 480px) {
    h1 { font-size: 22px; }
    #player { max-height: 35vh; }
    input, button, select { font-size: 14px; padding: 8px; }
    #mergeResult video { max-height: 130px; }
}

/* ------------------ loading 层 ------------------ */
.loading-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(10,14,28,0.95);
    display: flex;
    justify-content: center;
    align-items: center;
    font-size: 24px;
    color: #ffffff; /* 固定为白色 */
    z-index: 9999;
}

""", encoding="utf-8")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
