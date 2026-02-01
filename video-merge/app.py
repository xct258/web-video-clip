import os
import json
import time
import re
import shutil
import subprocess
import mimetypes
import threading
from datetime import datetime
from uuid import uuid4
from pathlib import Path
from typing import List, Optional
from collections import deque
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Body
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

try:
    import pty  # type: ignore
except Exception:  # pragma: no cover
    pty = None

# ------------------ 配置 ------------------
BASE_DIR = Path(__file__).parent.resolve()
VIDEO_DIR = BASE_DIR / "videos"
OUTPUT_DIR = BASE_DIR / "clips"
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
ALLOWED_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

# 脚本内测试模式开关：设为 True 则上传进入测试模式（只返回命令预览），False正常上传
UPLOAD_TEST_MODE = False

for d in [VIDEO_DIR, OUTPUT_DIR, TEMPLATE_DIR, STATIC_DIR]:
    d.mkdir(parents=True, exist_ok=True)

MERGE_STATE_PATH = BASE_DIR / "merge_state.json"
_merge_state_lock = threading.Lock()

UPLOAD_STATE_PATH = BASE_DIR / "upload_state.json"
_upload_state_lock = threading.Lock()

# 取消合并：由于本服务一次只允许一个合并任务运行，这里用全局 event + 当前进程句柄即可。
_merge_cancel_event = threading.Event()
_merge_runtime_lock = threading.Lock()
_merge_runtime_job_id: Optional[str] = None
_merge_runtime_proc: Optional[subprocess.Popen] = None

_upload_runtime_lock = threading.Lock()
_upload_runtime_proc: Optional[subprocess.Popen] = None


# 目录树懒加载缓存：避免前端频繁展开/折叠导致重复扫描同一层目录
_tree_cache_lock = threading.Lock()
_tree_cache: dict[str, tuple[float, List[dict]]] = {}


class MergeCancelled(RuntimeError):
    pass


def _set_merge_runtime(job_id: Optional[str] = None, proc: Optional[subprocess.Popen] = None) -> None:
    global _merge_runtime_job_id, _merge_runtime_proc
    with _merge_runtime_lock:
        if job_id is not None:
            _merge_runtime_job_id = job_id
        _merge_runtime_proc = proc


def _terminate_current_merge_proc() -> bool:
    """尝试终止当前正在运行的 ffmpeg 进程。返回是否找到并触发了终止。"""
    proc: Optional[subprocess.Popen] = None
    with _merge_runtime_lock:
        proc = _merge_runtime_proc

    if proc is None:
        return False

    try:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            # 给一点时间优雅退出；不行再 kill
            for _ in range(20):
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        return True
    except Exception:
        return False


def _set_upload_runtime(proc: Optional[subprocess.Popen] = None) -> None:
    global _upload_runtime_proc
    with _upload_runtime_lock:
        _upload_runtime_proc = proc


def _terminate_current_upload_proc() -> bool:
    """尝试终止当前正在运行的 biliup 进程。返回是否找到并触发了终止。"""
    proc: Optional[subprocess.Popen] = None
    with _upload_runtime_lock:
        proc = _upload_runtime_proc

    if proc is None:
        return False

    try:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            for _ in range(30):
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        return True
    except Exception:
        return False


def _read_merge_state_unlocked() -> dict:
    if not MERGE_STATE_PATH.exists():
        return {"running": False}
    try:
        with MERGE_STATE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"running": False}
        if "running" not in data:
            data["running"] = False
        return data
    except Exception:
        return {"running": False}


def _write_merge_state_unlocked(state: dict) -> None:
    tmp_path = MERGE_STATE_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, MERGE_STATE_PATH)


def _get_merge_state() -> dict:
    with _merge_state_lock:
        return _read_merge_state_unlocked()


def _update_merge_state(**updates) -> dict:
    with _merge_state_lock:
        state = _read_merge_state_unlocked()
        state.update(updates)
        _write_merge_state_unlocked(state)
        return state


def _read_upload_state_unlocked() -> dict:
    if not UPLOAD_STATE_PATH.exists():
        return {"running": False}
    try:
        with UPLOAD_STATE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"running": False}
        if "running" not in data:
            data["running"] = False
        return data
    except Exception:
        return {"running": False}


def _write_upload_state_unlocked(state: dict) -> None:
    tmp_path = UPLOAD_STATE_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, UPLOAD_STATE_PATH)


def _get_upload_state() -> dict:
    with _upload_state_lock:
        return _read_upload_state_unlocked()


def _update_upload_state(**updates) -> dict:
    with _upload_state_lock:
        state = _read_upload_state_unlocked()
        state.update(updates)
        _write_upload_state_unlocked(state)
        return state


def _repair_stale_upload_state_on_startup() -> None:
    """服务重启时，之前的 biliup 进程不可能继续存在。

    若 upload_state.json 里仍是 running=true，则将其标记为已中断，避免前端永远显示“投稿中”。
    """
    try:
        with _upload_state_lock:
            state = _read_upload_state_unlocked()
            if not isinstance(state, dict):
                return
            if state.get("running") is not True:
                return

            logs = state.get("logs")
            if not isinstance(logs, list):
                logs = []
            logs.append("[server] 服务重启，投稿任务已中断")

            state.update(
                {
                    "running": False,
                    "status": "cancelled",
                    "cancel_requested": True,
                    "cancel_reason": "server_restart",
                    "error": "服务重启，投稿任务已中断",
                    "updated_at": int(time.time()),
                    "logs": logs[-1200:],
                }
            )
            _write_upload_state_unlocked(state)
    except Exception:
        # 不影响启动
        pass


def _upload_token_ok(token: Optional[str]) -> bool:
    t = (token or "").strip()
    if not t:
        return False
    state = _get_upload_state()
    return str(state.get("upload_token") or "").strip() == t


# 模块加载时修复“遗留 running=true 的投稿状态”
_repair_stale_upload_state_on_startup()


_BILIUP_PROGRESS_RE = re.compile(
    r"(?P<cur>\d+(?:\.\d+)?)\s*(?P<cur_u>KiB|MiB|GiB|TiB)\s*/\s*(?P<tot>\d+(?:\.\d+)?)\s*(?P<tot_u>KiB|MiB|GiB|TiB)"
)
_BILIUP_SPEED_ETA_RE = re.compile(r"\((?P<speed>[^,]+),\s*(?P<eta>[^\)]*)")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# 参考用户脚本：从任意进度行里抓“数字+单位”片段
_BILIUP_ANY_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGT]iB)(/s)?")
_BILIUP_ETA_RE = re.compile(r"(\d+[hms](?:\d+[ms])?)")


def _unit_to_bytes(value: float, unit: str) -> float:
    u = (unit or "").strip()
    mul = 1.0
    if u == "KiB":
        mul = 1024.0
    elif u == "MiB":
        mul = 1024.0 ** 2
    elif u == "GiB":
        mul = 1024.0 ** 3
    elif u == "TiB":
        mul = 1024.0 ** 4
    return float(value) * mul


def _parse_biliup_progress(text: str) -> dict:
    """从 biliup 输出中尽量解析进度/速度/ETA。解析失败则返回空 dict。"""
    s = (text or "").strip("\r\n")
    # biliup 可能带 ANSI 控制符/光标控制，先剔除再解析
    s = _ANSI_ESCAPE_RE.sub("", s)
    out: dict = {}

    m = _BILIUP_PROGRESS_RE.search(s)
    if m:
        cur = float(m.group("cur"))
        tot = float(m.group("tot"))
        cur_b = _unit_to_bytes(cur, m.group("cur_u"))
        tot_b = _unit_to_bytes(tot, m.group("tot_u"))
        out["transferred_bytes"] = cur_b
        out["total_bytes"] = tot_b
        if tot_b > 0:
            out["percent"] = max(0.0, min(1.0, cur_b / tot_b))

    m2 = _BILIUP_SPEED_ETA_RE.search(s)
    if m2:
        out["speed"] = m2.group("speed").strip()
        out["eta"] = m2.group("eta").strip()

    # 兜底：按“数字+单位”抓取 current/total/speed，提升匹配覆盖率
    if "percent" not in out or ("speed" not in out and "eta" not in out):
        parts = list(_BILIUP_ANY_UNIT_RE.finditer(s))
        if parts:
            # 顺序通常是：已传 / 总量 / 速度
            cur_b = None
            tot_b = None
            speed_str = None
            for m in parts:
                v = float(m.group(1))
                u = m.group(2)
                is_speed = bool(m.group(3))
                if is_speed and speed_str is None:
                    speed_str = f"{m.group(1)} {u}/s"
                    continue
                # 只拿前两个非 /s
                if cur_b is None:
                    cur_b = _unit_to_bytes(v, u)
                elif tot_b is None:
                    tot_b = _unit_to_bytes(v, u)
            if cur_b is not None:
                out.setdefault("transferred_bytes", cur_b)
            if tot_b is not None:
                out.setdefault("total_bytes", tot_b)
                if tot_b > 0:
                    out.setdefault("percent", max(0.0, min(1.0, float(cur_b or 0.0) / tot_b)))
            if speed_str:
                out.setdefault("speed", speed_str)
            if "eta" not in out or not str(out.get("eta") or "").strip():
                m_eta = _BILIUP_ETA_RE.search(s)
                if m_eta:
                    out["eta"] = m_eta.group(1)

    return out


def _start_biliup_upload_job(cmd: list[str], upload_token: str, meta: dict) -> None:
    """启动 biliup 上传进程并持续写入 upload_state.json（日志 + 进度）。"""
    # 初始化状态
    _update_upload_state(
        running=True,
        status="uploading",
        percent=0.0,
        speed="",
        eta="",
        transferred_bytes=0.0,
        total_bytes=0.0,
        progress_line="",
        logs=[],
        started_at=int(time.time()),
        updated_at=int(time.time()),
        upload_token=upload_token,
        cancel_requested=False,
        cancel_reason="",
        **(meta or {}),
    )

    def _worker() -> None:
        proc: Optional[subprocess.Popen] = None
        master_fd: Optional[int] = None
        slave_fd: Optional[int] = None
        try:
            # 关键：使用 PTY 让 biliup 以 TTY 模式输出进度条（\r 刷新）。
            if pty is not None:
                master_fd, slave_fd = pty.openpty()
                proc = subprocess.Popen(
                    cmd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    bufsize=0,
                )
                try:
                    os.close(slave_fd)
                except Exception:
                    pass
                slave_fd = None
            else:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                )
            _set_upload_runtime(proc)

            logs = deque(maxlen=1200)
            progress_line = ""
            buf = b""
            last_flush = 0.0
            last_prog: dict = {}

            while True:
                if master_fd is not None:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        break
                else:
                    assert proc.stdout is not None
                    chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                buf += chunk

                # 按 \n / \r 切分，\r 通常是进度刷新
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop() if parts else b""
                for p in parts:
                    if not p:
                        continue
                    try:
                        text = p.decode("utf-8", errors="replace")
                    except Exception:
                        continue

                    # 清理 ANSI 控制符，尽量保留“命令本身输出”可读性
                    clean = _ANSI_ESCAPE_RE.sub("", text).strip("\r\n")

                    # 解析进度（不一定每行都有）
                    prog = _parse_biliup_progress(clean)
                    if prog:
                        last_prog.update(prog)

                    # 进度刷新行（通常来自 \r）：只保留最新一条，避免刷屏但让用户能看到原始进度输出
                    is_progress_line = bool(prog) or bool(_BILIUP_ANY_UNIT_RE.search(clean)) or bool(_BILIUP_ETA_RE.search(clean))
                    line = clean.strip()
                    if line:
                        if is_progress_line:
                            progress_line = line
                        else:
                            logs.append(line)

                    now = time.time()
                    # 限频写状态（避免频繁 IO）
                    if now - last_flush >= 0.6:
                        updates = {
                            "logs": list(logs),
                            "progress_line": progress_line,
                            "updated_at": int(now),
                        }
                        # 使用 last_prog，避免“写入时刚好没解析到进度行”导致进度长时间不动
                        if last_prog:
                            updates.update(last_prog)
                        _update_upload_state(**updates)
                        last_flush = now

            # 处理残留 buffer（避免最后一段日志丢失）
            try:
                tail = _ANSI_ESCAPE_RE.sub("", buf.decode("utf-8", errors="replace")).strip()
                if tail:
                    tail_prog = _parse_biliup_progress(tail)
                    if tail_prog:
                        last_prog.update(tail_prog)
                    if bool(tail_prog) or bool(_BILIUP_ANY_UNIT_RE.search(tail)) or bool(_BILIUP_ETA_RE.search(tail)):
                        progress_line = tail
                    else:
                        logs.append(tail)
            except Exception:
                pass

            # 进程结束
            rc = proc.wait(timeout=5)
            st = _get_upload_state()
            cancel_requested = bool(st.get("cancel_requested"))
            cancel_reason = str(st.get("cancel_reason") or "").strip()
            status = "done" if rc == 0 and not cancel_requested else ("cancelled" if cancel_requested else "error")
            final_updates = {
                "running": False,
                "status": status,
                "exit_code": int(rc),
                "updated_at": int(time.time()),
                "logs": list(logs),
                "progress_line": progress_line,
            }
            if cancel_requested:
                final_updates["cancel_reason"] = cancel_reason or "user"
            # done 时补齐 percent
            if status == "done":
                final_updates["percent"] = 1.0
            _update_upload_state(**final_updates)
        except Exception as e:
            _update_upload_state(
                running=False,
                status="error",
                error=str(e),
                progress_line="",
                updated_at=int(time.time()),
            )
        finally:
            _set_upload_runtime(None)
            try:
                if proc and proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            try:
                if master_fd is not None:
                    os.close(master_fd)
            except Exception:
                pass
            try:
                if slave_fd is not None:
                    os.close(slave_fd)
            except Exception:
                pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def _format_eta_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    try:
        s = float(seconds)
    except Exception:
        return ""
    if s <= 0:
        return ""  # 已接近完成

    s_int = int(round(s))
    if s_int < 60:
        return f"约 {s_int} 秒"
    m = s_int // 60
    sec = s_int % 60
    if m < 60:
        return f"约 {m} 分 {sec} 秒"
    h = m // 60
    m2 = m % 60
    return f"约 {h} 小时 {m2} 分"


def _estimate_eta_seconds(state: dict) -> Optional[float]:
    """根据已处理时长/已用时间估算剩余时间（秒）。"""
    if not isinstance(state, dict):
        return None
    if state.get("running") is not True:
        return None
    try:
        total = float(state.get("total_seconds") or 0.0)
        processed = float(state.get("processed_seconds") or 0.0)
    except Exception:
        return None
    remaining = max(0.0, total - processed)
    if remaining <= 0:
        return 0.0

    try:
        started_at = int(state.get("started_at") or 0)
    except Exception:
        started_at = 0
    elapsed = float(max(0, int(time.time()) - started_at))
    # rate: “视频秒/真实秒”，例如 2.0 表示 1 秒真实处理 2 秒视频
    if elapsed <= 3.0 or processed <= 1.0:
        # 刚开始时波动大：先按 1x 粗估
        return remaining

    rate = processed / elapsed
    # 防止异常值导致 ETA 爆炸
    if rate <= 0.05:
        rate = 0.05
    if rate > 20.0:
        rate = 20.0
    return remaining / rate

# ------------------ 工具函数 ------------------
def ffmpeg_exists() -> bool:
    return shutil.which("ffmpeg") is not None

def sanitize_name(name: str) -> Path:
    path = (VIDEO_DIR / name).resolve()
    if not str(path).startswith(str(VIDEO_DIR.resolve())):
        raise HTTPException(400, "非法路径")
    return path


def sanitize_output_name(name: str) -> Path:
    path = (OUTPUT_DIR / name).resolve()
    if not str(path).startswith(str(OUTPUT_DIR.resolve())):
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


class CancelMergeRequest(BaseModel):
    job_id: Optional[str] = None
    merge_token: Optional[str] = None


class DeleteOutputRequest(BaseModel):
    file: str

# ------------------ FastAPI 应用 ------------------
app = FastAPI(title="Multi-Video Slicer", version="1.4")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATE_DIR)
JOBS: dict[str, SliceJob] = {}


@app.on_event("startup")
def _startup_merge_state_reconcile():
    # 服务重启后若发现上一次任务仍标记 running，自动置为 error，避免死锁
    with _merge_state_lock:
        state = _read_merge_state_unlocked()
        if state.get("running") is True:
            state["running"] = False
            state["status"] = "error"
            state.setdefault("started_at", int(time.time()))
            state["error"] = "服务重启，已自动将 running 任务标记为 error"
            state["finished_at"] = int(time.time())
            _write_merge_state_unlocked(state)


@app.get("/api/merge_status")
async def merge_status(merge_token: Optional[str] = None):
    state = _get_merge_state()
    token = (merge_token or "").strip()
    state_token = str(state.get("merge_token") or "").strip()
    authed = bool(token and state_token and token == state_token)

    # 未授权：不返回进度/输出/错误等信息（避免其他用户看到“合并进度条”等无意义信息）
    if not authed:
        return {"running": False}

    eta_seconds = _estimate_eta_seconds(state)
    eta_human = _format_eta_seconds(eta_seconds)

    full = dict(state)
    if "running" not in full:
        full["running"] = False
    full["eta_seconds"] = eta_seconds
    full["eta_human"] = eta_human
    return full


@app.post("/api/cancel_merge")
async def cancel_merge(body: CancelMergeRequest = Body(...)):
    """取消当前合并任务。

    约束：服务只允许单任务运行。
    """
    state = _get_merge_state()
    if state.get("running") is not True:
        return {"ok": True, "status": "idle"}

    req_job_id = (getattr(body, "job_id", None) or "").strip()
    req_token = (getattr(body, "merge_token", None) or "").strip()
    cur_job_id = str(state.get("job_id") or "").strip()
    cur_token = str(state.get("merge_token") or "").strip()

    if cur_token and (not req_token or req_token != cur_token):
        raise HTTPException(status_code=403, detail="只能取消自己发起的合并任务")
    if req_job_id and cur_job_id and req_job_id != cur_job_id:
        raise HTTPException(status_code=409, detail="任务已变更，请刷新后重试")

    _merge_cancel_event.set()
    _update_merge_state(
        status="cancelling",
        stage=state.get("stage") or "running",
        cancel_requested=True,
        cancel_requested_at=int(time.time()),
    )
    _terminate_current_merge_proc()
    return {"ok": True, "status": "cancelling"}

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
async def video_tree(path: str = ""):
    """按需（懒加载）返回 VIDEO_DIR 下某个目录的一层子节点。

    - path 为空时表示根目录。
    - 只扫描一层，不递归，避免一次性扫描全盘。
    - dir 节点返回 hasChildren，用于前端决定是否可展开。
    """

    def _dir_has_children_fast(dir_path: Path) -> bool:
        try:
            for entry in dir_path.iterdir():
                try:
                    if entry.is_dir():
                        return True
                    if entry.is_file() and entry.suffix.lower() in ALLOWED_EXTS:
                        return True
                except Exception:
                    continue
        except Exception:
            return False
        return False

    target_dir = sanitize_name(path)
    if not target_dir.exists() or not target_dir.is_dir():
        raise HTTPException(status_code=404, detail="目录不存在")

    cache_key = str(target_dir)
    now = time.time()
    with _tree_cache_lock:
        cached = _tree_cache.get(cache_key)
        if cached is not None:
            ts, data = cached
            # 短 TTL：平衡性能与目录实时性
            if now - ts <= 5.0:
                return data

    dirs: List[Path] = []
    files: List[Path] = []
    try:
        for p in target_dir.iterdir():
            try:
                if p.is_dir():
                    dirs.append(p)
                elif p.is_file() and p.suffix.lower() in ALLOWED_EXTS:
                    files.append(p)
            except Exception:
                continue
    except Exception:
        raise HTTPException(status_code=500, detail="无法读取目录")

    dirs.sort(key=lambda x: x.name.lower())
    files.sort(key=lambda x: x.name.lower())

    tree: List[dict] = []
    for d in dirs:
        rel = str(d.relative_to(VIDEO_DIR)).replace("\\", "/")
        tree.append(
            {
                "type": "dir",
                "name": d.name,
                "path": rel,
                "hasChildren": _dir_has_children_fast(d),
            }
        )

    for f in files:
        rel_file = str(f.relative_to(VIDEO_DIR)).replace("\\", "/")
        st = f.stat()
        tree.append(
            {
                "type": "file",
                "name": rel_file,
                "basename": f.name,  # 仅文件名，用于显示
                "size": st.st_size,
                "mtime": st.st_mtime,
                "duration": 0,
            }
        )

    with _tree_cache_lock:
        _tree_cache[cache_key] = (now, tree)
        # 简单限长，防止目录过多导致缓存无限增长
        if len(_tree_cache) > 512:
            # 删除最旧的一批
            oldest = sorted(_tree_cache.items(), key=lambda kv: kv[1][0])[:128]
            for k, _ in oldest:
                _tree_cache.pop(k, None)

    return tree

@app.get("/api/dir_durations")
async def get_dir_durations(path: str = ""):
    target_dir = sanitize_name(path)
    if not target_dir.exists() or not target_dir.is_dir():
        return {}

    res_map = {}
    for f in target_dir.iterdir():
        # 简单过滤，只处理常见视频格式
        if f.is_file() and f.suffix.lower() in ALLOWED_EXTS:
            try:
                cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(f)]
                # 设置超时，如果文件太多或太大，前端也是异步请求
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1.0)
                if res.returncode == 0:
                    val = res.stdout.strip()
                    if val and val != "N/A":
                        res_map[f.name] = float(val)
            except:
                pass
    return res_map

# ------------------ 批量切片并合并 ------------------
@app.post("/api/slice_merge_all")
async def slice_merge_all(body: MultiVideoRequest = Body(...), bg: BackgroundTasks = None):
    MAX_TOTAL_SECONDS = 20 * 60

    # 空片段校验：避免前端误提交导致 ffmpeg 报错
    total_clips = sum(len(v.clips) for v in (body.videos or []))
    if total_clips <= 0:
        raise HTTPException(400, "请至少添加一个视频片段")

    # 基础校验
    total_seconds = 0.0
    for vid in body.videos:
        for clip in vid.clips:
            if clip.end <= clip.start:
                raise HTTPException(400, f"片段结束时间必须大于开始时间: {vid.name}")
            total_seconds += float(clip.end - clip.start)

    # 总时长限制：只允许切片合并 20 分钟以内
    if total_seconds > MAX_TOTAL_SECONDS:
        raise HTTPException(400, f"最终合并总时长不能超过20分钟（当前约 {int(total_seconds)} 秒）")

    username = getattr(body, "username", None) or "user"
    if not ffmpeg_exists():
        raise HTTPException(500, "ffmpeg not found in PATH")
    
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    # 输出文件名改为：用户名-原本名字-时间戳.mp4
    out_basename_base = f"{username}-{body.out_basename or 'merged'}-{ts}"
    out_basename = out_basename_base
    out_path = OUTPUT_DIR / f"{out_basename}.mp4"
    # 避免覆盖同名文件（例如同一秒内连续提交）
    suffix = 1
    while out_path.exists():
        out_basename = f"{out_basename_base}-{suffix}"
        out_path = OUTPUT_DIR / f"{out_basename}.mp4"
        suffix += 1

    job_id = f"job_{ts}_{os.getpid()}_{uuid4().hex[:6]}"
    merge_token = uuid4().hex
    job = SliceJob(id=job_id, source="multiple", status="queued", out_path=str(out_path.name))
    JOBS[job_id] = job

    # ---- 全局并发限制 + 持久化写入 running 状态（同一把锁，避免竞争） ----
    with _merge_state_lock:
        cur_state = _read_merge_state_unlocked()
        if cur_state.get("running") is True:
            eta_human = _format_eta_seconds(_estimate_eta_seconds(cur_state))
            msg = "已有合并任务正在进行中，请稍后再试"
            if eta_human:
                msg = f"{msg}（预计剩余：{eta_human}）"
            raise HTTPException(status_code=409, detail=msg)
        state = {
            "running": True,
            "job_id": job_id,
            "merge_token": merge_token,
            "username": username,
            "status": "running",
            "started_at": int(time.time()),
            "out_file": str(out_path.name),
            "stage": "queued",
            "total_clips": int(total_clips),
            "done_clips": 0,
            "total_seconds": float(total_seconds),
            "processed_seconds": 0.0,
            "percent": 0.0,
        }
        _write_merge_state_unlocked(state)

    # 新任务启动：清理上一轮可能残留的取消标志/运行态
    _merge_cancel_event.clear()
    _set_merge_runtime(job_id=job_id, proc=None)

    def run_merge(job: SliceJob, videos: List[VideoClip], out_path: Path, total_seconds_all: float, total_clips_all: int):
        job.status = "running"
        temp_files = []
        list_file: Optional[Path] = None
        processed_seconds = 0.0
        done_clips = 0

        # 运行线程启动时也写入运行态（便于取消时校验/诊断）
        _set_merge_runtime(job_id=job.id, proc=None)

        def _safe_percent(processed: float) -> float:
            if not total_seconds_all or total_seconds_all <= 0:
                return 0.0
            p = processed / float(total_seconds_all)
            if p < 0:
                return 0.0
            if p > 1:
                return 1.0
            return float(p)

        def _run_ffmpeg_with_progress(cmd: list, clip_duration: float, current_label: str):
            """Run ffmpeg and periodically update merge_state.json with percent based on out_time_ms."""
            if _merge_cancel_event.is_set():
                raise MergeCancelled("用户取消合并")

            def _stop_proc(p: subprocess.Popen) -> None:
                try:
                    if p.poll() is None:
                        try:
                            p.terminate()
                        except Exception:
                            pass
                        for _ in range(25):
                            if p.poll() is not None:
                                break
                            time.sleep(0.05)
                        if p.poll() is None:
                            try:
                                p.kill()
                            except Exception:
                                pass
                except Exception:
                    pass
            # 将 ffmpeg 进度输出到 stdout：key=value
            cmd_with_progress = list(cmd)
            insert_at = 3 if len(cmd_with_progress) >= 3 else len(cmd_with_progress)
            # ffmpeg 全局参数：-loglevel/-nostats/-progress
            cmd_with_progress[insert_at:insert_at] = [
                "-loglevel", "error",
                "-nostats",
                "-progress", "pipe:1",
            ]

            last_update = 0.0
            out_time_sec = 0.0
            tail = deque(maxlen=60)

            proc = subprocess.Popen(
                cmd_with_progress,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
            _set_merge_runtime(proc=proc)
            assert proc.stdout is not None

            for raw_line in proc.stdout:
                if _merge_cancel_event.is_set():
                    _stop_proc(proc)
                    try:
                        proc.wait(timeout=0.5)
                    except Exception:
                        pass
                    raise MergeCancelled("用户取消合并")

                line = (raw_line or "").strip()
                if line:
                    tail.append(line)

                # progress 输出：out_time_ms=xxxx / progress=continue|end
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k == "out_time_ms":
                        try:
                            out_time_sec = max(0.0, int(v) / 1_000_000.0)
                        except Exception:
                            pass

                now_mono = time.monotonic()
                if now_mono - last_update >= 0.5:
                    effective = out_time_sec
                    if clip_duration and clip_duration > 0:
                        if effective > clip_duration:
                            effective = clip_duration
                    processed_now = processed_seconds + float(effective)
                    _update_merge_state(
                        stage="slicing",
                        current=current_label,
                        total_clips=int(total_clips_all),
                        done_clips=int(done_clips),
                        total_seconds=float(total_seconds_all),
                        processed_seconds=float(processed_now),
                        percent=_safe_percent(processed_now),
                    )
                    last_update = now_mono

            rc = proc.wait()
            _set_merge_runtime(proc=None)
            if rc != 0:
                raise RuntimeError("ffmpeg 失败：" + "\n".join(list(tail)[-15:]))

        def _run_ffmpeg_concat_with_cancel(cmd: list):
            if _merge_cancel_event.is_set():
                raise MergeCancelled("用户取消合并")

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
            _set_merge_runtime(proc=proc)

            try:
                while True:
                    if _merge_cancel_event.is_set():
                        try:
                            if proc.poll() is None:
                                proc.terminate()
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=0.5)
                        except Exception:
                            pass
                        raise MergeCancelled("用户取消合并")

                    rc = proc.poll()
                    if rc is not None:
                        out, err = proc.communicate(timeout=0.2)
                        if rc != 0:
                            tail = (err or out or "").strip()
                            if len(tail) > 4000:
                                tail = tail[-4000:]
                            raise RuntimeError("ffmpeg 合并失败：" + ("\n" + tail if tail else ""))
                        return
                    time.sleep(0.2)
            finally:
                _set_merge_runtime(proc=None)

        try:
            _update_merge_state(
                status="running",
                stage="slicing",
                total_clips=int(total_clips_all),
                done_clips=0,
                total_seconds=float(total_seconds_all),
                processed_seconds=0.0,
                percent=0.0,
            )

            for vid_idx, vid in enumerate(videos):
                if _merge_cancel_event.is_set():
                    raise MergeCancelled("用户取消合并")
                src = sanitize_name(vid.name)
                if not src.exists():
                    raise FileNotFoundError(f"{vid.name} not found")
                for i, clip in enumerate(vid.clips):
                    if _merge_cancel_event.is_set():
                        raise MergeCancelled("用户取消合并")
                    if clip.end <= clip.start:
                        raise ValueError(f"Clip end <= start in {vid.name}")
                    tmp = OUTPUT_DIR / f"{job.id}_{vid_idx}_{i}.ts"
                    # 先加入清理列表：避免取消/失败发生在 temp_files.append 之前导致残留
                    if tmp not in temp_files:
                        temp_files.append(tmp)
                    duration = clip.end - clip.start
                    # 为了实现精准切割，必须进行重编码 (transcoding)。"-c copy" 只能在关键帧处切割，会导致时间不准确。
                    # 这里使用 libx264 ultrafast 预设来保证速度，CRF 18 保证接近无损的画质。
                    cmd = [
                        "ffmpeg", "-hide_banner", "-y",
                        "-ss", f"{float(clip.start):.3f}",
                        "-i", str(src),
                        "-t", f"{float(duration):.3f}",
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "14",
                        "-c:a", "aac",
                        "-f", "mpegts",
                        str(tmp)
                    ]

                    current_label = f"{vid.name} 片段 {i+1}/{len(vid.clips)}"
                    _update_merge_state(
                        stage="slicing",
                        current=current_label,
                        total_clips=int(total_clips_all),
                        done_clips=int(done_clips),
                        total_seconds=float(total_seconds_all),
                        processed_seconds=float(processed_seconds),
                        percent=_safe_percent(processed_seconds),
                    )

                    _run_ffmpeg_with_progress(cmd, float(duration), current_label)

                    done_clips += 1
                    processed_seconds += float(duration)
                    _update_merge_state(
                        stage="slicing",
                        current=current_label,
                        total_clips=int(total_clips_all),
                        done_clips=int(done_clips),
                        total_seconds=float(total_seconds_all),
                        processed_seconds=float(processed_seconds),
                        percent=_safe_percent(processed_seconds),
                    )
            
            list_file = OUTPUT_DIR / f"{job.id}_list.txt"
            with list_file.open("w", encoding="utf-8") as f:
                for tmp in temp_files:
                    f.write(f"file '{tmp.name}'\n")

            _update_merge_state(
                stage="merging",
                current="合并中",
                percent=max(_safe_percent(processed_seconds), 0.98),
            )

            cmd_concat = ["ffmpeg","-hide_banner","-y","-f","concat","-safe","0","-i",str(list_file),"-c","copy","-bsf:a","aac_adtstoasc",str(out_path)]
            _run_ffmpeg_concat_with_cancel(cmd_concat)
            job.status = "done"

            _update_merge_state(
                running=False,
                status="done",
                stage="done",
                current=None,
                finished_at=int(time.time()),
                out_file=str(out_path.name),
                percent=1.0,
                error=None,
            )
        except Exception as e:
            if isinstance(e, MergeCancelled):
                job.status = "cancelled"
                job.error = str(e)
                _update_merge_state(
                    running=False,
                    status="cancelled",
                    stage="cancelled",
                    current=None,
                    finished_at=int(time.time()),
                    out_file=None,
                    percent=_safe_percent(processed_seconds),
                    error=str(e),
                )
            else:
                job.status = "error"
                job.error = str(e)
                _update_merge_state(
                    running=False,
                    status="error",
                    stage="error",
                    current=None,
                    finished_at=int(time.time()),
                    out_file=str(out_path.name),
                    percent=_safe_percent(processed_seconds),
                    error=str(e),
                )
        finally:
            _set_merge_runtime(proc=None)
            _merge_cancel_event.clear()

            def _unlink_with_retries(p: Path, retries: int = 20, delay: float = 0.1) -> None:
                for _ in range(max(1, retries)):
                    try:
                        if p.exists():
                            p.unlink()
                        return
                    except Exception:
                        time.sleep(delay)

            # 取消时：删除可能已经产生的半成品输出文件
            if getattr(job, "status", None) == "cancelled":
                try:
                    if out_path is not None and out_path.exists():
                        _unlink_with_retries(out_path)
                except Exception:
                    pass

            cleanup_targets = list(temp_files)
            if list_file is not None:
                cleanup_targets.append(list_file)

            # 兜底：按 job_id 模式把可能遗漏的临时文件也删掉（例如取消发生在写入列表之前）
            try:
                for p in OUTPUT_DIR.glob(f"{job.id}_*.ts"):
                    cleanup_targets.append(p)
                for p in OUTPUT_DIR.glob(f"{job.id}_list.txt"):
                    cleanup_targets.append(p)
            except Exception:
                pass

            # 去重
            seen = set()
            uniq_targets: list[Path] = []
            for p in cleanup_targets:
                try:
                    key = str(p)
                except Exception:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                uniq_targets.append(p)

            for tmp in uniq_targets:
                try:
                    _unlink_with_retries(tmp)
                except Exception:
                    pass
    
    bg.add_task(run_merge, job, body.videos, out_path, float(total_seconds), int(total_clips))
    return {"job_id": job_id, "out_file": job.out_path, "merge_token": merge_token}


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


@app.post("/api/delete_output")
async def delete_output(body: DeleteOutputRequest = Body(...)):
    file_name = (getattr(body, "file", None) or "").strip()
    if not file_name:
        raise HTTPException(400, "缺少文件名")

    path = sanitize_output_name(file_name)
    if not path.exists():
        return {"ok": True, "deleted": False, "reason": "not_found"}
    if not path.is_file():
        raise HTTPException(400, "目标不是文件")

    try:
        path.unlink()
    except Exception as e:
        raise HTTPException(500, f"删除失败: {e}")

    return {"ok": True, "deleted": True}


@app.get("/api/upload_status")
async def upload_status(upload_token: Optional[str] = None):
    # 未持有 token 的用户不返回任何状态（避免他人看到投稿进度/日志）
    if not _upload_token_ok(upload_token):
        return {"running": False}
    state = _get_upload_state()
    # 不回传 token
    state = dict(state)
    state.pop("upload_token", None)
    return state


class CancelUploadRequest(BaseModel):
    upload_token: Optional[str] = None


@app.post("/api/cancel_upload")
async def cancel_upload(body: CancelUploadRequest = Body(...)):
    token = (getattr(body, "upload_token", None) or "").strip()
    if not _upload_token_ok(token):
        raise HTTPException(403, "无权限")

    st = _get_upload_state()
    if st.get("running") is not True:
        return {"ok": True, "stopped": False, "reason": "not_running"}

    _update_upload_state(status="cancelling", cancel_requested=True, cancel_reason="user", updated_at=int(time.time()))
    stopped = _terminate_current_upload_proc()
    return {"ok": True, "stopped": bool(stopped)}

# ------------------ B站投稿（测试模式） ------------------
@app.post("/api/upload_bili")
async def upload_bili(data: dict = Body(...)):
    file_name = data.get("file")
    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    tags = data.get("tags", "").strip()

    if not file_name or not title:
        return {"success": False, "message": "文件名或标题不能为空"}

    # 只允许 OUTPUT_DIR 下文件，避免路径穿越
    try:
        video_path = sanitize_output_name(file_name)
    except Exception:
        return {"success": False, "message": "非法文件名"}
    if not video_path.exists():
        return {"success": False, "message": "切片视频不存在"}

    # 同一时间只允许一个投稿任务
    cur = _get_upload_state()
    if cur.get("running") is True:
        return {"success": False, "message": "已有投稿正在进行中，请先停止或等待完成"}

    # 构建将要执行的命令
    cmd = [
        "/rec/app/apps/biliup",
        "-u", "/rec/app/cookies/bilibili/cookies-烦心事远离.json",
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

    # 脚本内开关控制测试模式
    if UPLOAD_TEST_MODE:
        return {
            "success": True,
            "message": "测试模式，不会实际上传",
            "cmd_preview": " ".join(cmd)
        }

    # 真实上传：启动后台进程，前端轮询状态
    upload_token = uuid4().hex
    meta = {
        "file": file_name,
        "title": title,
        "description": description,
        "tags": tags,
        "cmd": " ".join(cmd),
    }
    _start_biliup_upload_job(cmd, upload_token, meta)
    return {"success": True, "upload_token": upload_token}

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

/* ------------------ B站式标签输入 ------------------ */
.bili-tags-editor {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    min-height: 42px;
    padding: 8px 10px;
    border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.16);
    background: rgba(255,255,255,0.06);
    box-sizing: border-box;
    cursor: text;
}

.bili-tags-editor:focus-within {
    border-color: rgba(79,158,255,0.65);
    box-shadow: 0 0 0 3px rgba(79,158,255,0.18);
}

.bili-tags-editor.disabled {
    opacity: 0.7;
    cursor: not-allowed;
}

.bili-tag-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 10px;
    border-radius: 999px;
    line-height: 1;
    user-select: none;
    background: rgba(79,158,255,0.18);
    border: 1px solid rgba(79,158,255,0.25);
    color: rgba(255,255,255,0.95);
    font-size: 13px;
}

.bili-tag-remove {
    width: 18px;
    height: 18px;
    border-radius: 999px;
    border: 0;
    background: rgba(255,255,255,0.18);
    color: rgba(255,255,255,0.92);
    cursor: pointer;
    padding: 0;
    line-height: 18px;
}

.bili-tag-remove:hover {
    background: rgba(255,255,255,0.26);
}

.bili-tags-input {
    flex: 1;
    min-width: 160px;
    border: 0;
    outline: none;
    background: transparent;
    color: rgba(255,255,255,0.95);
    font-size: 14px;
    padding: 6px 4px;
}

.bili-tags-hint {
    font-size: 12px;
    opacity: 0.85;
    margin-top: 6px;
}

/* ------------------ Toast 通知 ------------------ */
.toast {
    position: relative;
    pointer-events: auto;
    width: fit-content;
    max-width: min(380px, calc(100vw - 32px));
    box-sizing: border-box;
    border-radius: 10px;
    color: #fff;
    background: rgba(18, 22, 40, 0.88);
    border: 1px solid rgba(255,255,255,0.14);
    box-shadow: 0 10px 30px rgba(0,0,0,0.35);
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
    padding: 10px 12px;
    overflow: hidden;

    transform: translateY(0);
    opacity: 1;
    max-height: 400px;
    will-change: transform, opacity, max-height, padding;
    transition:
        max-height 220ms cubic-bezier(0.2, 0.9, 0.3, 1),
        padding-top 220ms cubic-bezier(0.2, 0.9, 0.3, 1),
        padding-bottom 220ms cubic-bezier(0.2, 0.9, 0.3, 1),
        opacity 180ms ease,
        transform 220ms cubic-bezier(0.2, 0.9, 0.3, 1);
}

.toast.toast-enter {
    max-height: 0;
    padding-top: 0;
    padding-bottom: 0;
    opacity: 0;
    transform: translateY(6px);
}

.toast.toast-hiding {
    max-height: 0;
    padding-top: 0;
    padding-bottom: 0;
    opacity: 0;
    transform: translateY(6px);
}

.toast-body {
    flex: 1 1 auto;
    min-width: 0;
}

.toast-title {
    font-size: 12px;
    font-weight: 700;
    opacity: 0.9;
    margin-bottom: 2px;
}

.toast-msg {
    font-size: 13px;
    line-height: 1.35;
    word-break: break-word;
    white-space: pre-wrap;
}

/* ------------------ Modal 弹窗 ------------------ */
.modal-overlay {
    position: fixed;
    inset: 0;
    z-index: 10050;
    display: none;
    align-items: center;
    justify-content: center;
    padding: 20px;
    background: rgba(0, 0, 0, 0.55);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
}

/* 确认弹窗需要压过投稿弹窗（uploadModalOverlay） */
#modalOverlay {
    z-index: 10080;
}
.modal-overlay.show {
    display: flex;
}

.modal {
    width: min(520px, calc(100vw - 40px));
    border-radius: 14px;
    background: rgba(18, 22, 40, 0.92);
    border: 1px solid rgba(255,255,255,0.14);
    box-shadow: 0 20px 60px rgba(0,0,0,0.45);
    color: #fff;
    overflow: hidden;
    transform: translateY(10px);
    opacity: 0;
    transition: transform 180ms ease, opacity 180ms ease;
}
.modal-overlay.show .modal {
    transform: translateY(0);
    opacity: 1;
}

.modal-header {
    padding: 14px 16px 8px 16px;
    font-weight: 800;
    font-size: 14px;
}

.modal-body {
    padding: 0 16px 14px 16px;
    font-size: 13px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
    opacity: 0.95;
}

.modal-actions {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    padding: 12px 16px 16px 16px;
}

.modal-btn {
    border: 1px solid rgba(255,255,255,0.14);
    background: rgba(255,255,255,0.08);
    color: #fff;
    border-radius: 10px;
    padding: 8px 12px;
    cursor: pointer;
}
.modal-btn:hover {
    background: rgba(255,255,255,0.12);
}

.modal-btn-primary {
    background: var(--accent-color);
    border-color: transparent;
}
.modal-btn-primary:hover {
    filter: brightness(1.05);
}

/* ------------------ Upload Modal（投稿弹窗） ------------------ */
.upload-modal-overlay {
    z-index: 10060;
}

.upload-modal {
    width: min(720px, calc(100vw - 40px));
}

/* 限制投稿弹窗最大高度并允许内部滚动，避免上下被遮挡 */
.upload-modal {
    max-height: calc(100vh - 40px);
    display: flex;
    flex-direction: column;
}

.upload-modal .modal-body {
    overflow: auto;
    /* 留出 header/actions 区域的高度（估算） */
    max-height: calc(100vh - 160px);
}

.upload-modal-body {
    white-space: normal;
}

.upload-modal-body input {
    width: 100%;
    margin-bottom: 8px;
}

/* 标签编辑器内的 input：不要继承“整行输入框”样式，避免看起来多一个输入框 */
.upload-modal-body #uploadModalTagsEditor {
    margin-bottom: 8px;
}

.upload-modal-body #uploadModalTags.bili-tags-input {
    width: auto !important;
    margin-bottom: 0 !important;
    background: transparent !important;
    border: 0 !important;
    box-shadow: none !important;
    border-radius: 0 !important;
}

.upload-modal-result pre {
    white-space: pre-wrap;
    word-break: break-word;
}

.upload-progress {
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 12px;
    background: rgba(0,0,0,0.18);
    padding: 10px 12px;
}

.upload-progress-top {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    align-items: baseline;
    margin-bottom: 8px;
}

.upload-progress-title {
    font-weight: 800;
}

.upload-progress-percent {
    font-variant-numeric: tabular-nums;
    opacity: 0.95;
}

.upload-progress-bar {
    height: 10px;
    border-radius: 999px;
    background: rgba(255,255,255,0.12);
    overflow: hidden;
}

.upload-progress-bar-fill {
    height: 100%;
    width: 0;
    background: var(--accent-color);
    transition: width 0.25s ease;
}

.upload-progress-meta {
    margin-top: 8px;
    font-size: 12px;
    opacity: 0.9;
    min-height: 16px;
}

.upload-progress-rawline {
    margin-top: 6px;
    font-family: Consolas, monospace;
    font-size: 12px;
    line-height: 1.35;
    white-space: pre-wrap;
    word-break: break-word;
    opacity: 0.95;
    min-height: 16px;
}

.upload-progress-logs {
    margin-top: 10px;
    max-height: min(38vh, 320px);
    overflow: auto;
    padding: 10px;
    border-radius: 10px;
    background: rgba(0,0,0,0.22);
    border: 1px solid rgba(255,255,255,0.10);
    font-family: Consolas, monospace;
    font-size: 12px;
    line-height: 1.35;
    white-space: pre-wrap;
}

/* ------------------ History Modal（历史输出弹窗） ------------------ */
.history-modal {
    width: min(920px, calc(100vw - 40px));
}

.history-modal-body {
    white-space: normal;
    max-height: min(70vh, 560px);
    overflow: auto;
}

/* ------------------ Merge Preview Modal（合并预览弹窗） ------------------ */
.merge-preview-overlay {
    z-index: 10055;
    overflow: auto;
    align-items: flex-start;
}

.merge-preview-modal {
    width: min(980px, calc(100vw - 40px));
    max-height: calc(100vh - 40px);
    display: flex;
    flex-direction: column;
}

.merge-preview-body {
    white-space: normal;
    overflow: auto;
}

.merge-preview-grid {
    display: grid;
    grid-template-columns: 1fr 320px;
    gap: 12px;
    align-items: start;
    min-height: 0;
}

.merge-preview-left {
    min-width: 0;
    min-height: 0;
}

.merge-preview-now {
    font-size: 12px;
    opacity: 0.9;
    white-space: normal;
    overflow-wrap: anywhere;
    word-break: break-word;
    margin-bottom: 8px;
}

.merge-preview-video {
    width: 100%;
    max-height: min(56vh, 520px);
    background: #000;
    border-radius: 12px;
}

.merge-preview-controls {
    margin-top: 10px;
}

.merge-preview-right {
    min-width: 0;
    min-height: 0;
}

.merge-preview-queue {
    border: 1px solid rgba(255,255,255,0.10);
    background: rgba(0,0,0,0.18);
    border-radius: 12px;
    padding: 10px;
    max-height: min(56vh, 520px);
    overflow: auto;
}

.merge-preview-queue-title {
    font-size: 12px;
    font-weight: 800;
    opacity: 0.9;
    margin-bottom: 10px;
    display: flex;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
}

.merge-preview-queue-item {
    display: block;
    width: 100%;
    text-align: left;
    padding: 8px 10px;
    border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.08);
    background: rgba(255,255,255,0.06);
    cursor: pointer;
    margin-bottom: 8px;
}

.merge-preview-queue-item:hover {
    background: rgba(79, 158, 255, 0.16);
    border-color: rgba(79, 158, 255, 0.25);
}

.merge-preview-queue-item.active {
    background: rgba(79, 158, 255, 0.25);
    border-color: rgba(79, 158, 255, 0.45);
}

.merge-preview-queue-item .row1 {
    display: grid;
    grid-template-columns: 1fr;
    gap: 10px;
    align-items: start;
}

.merge-preview-queue-item .name {
    font-size: 12px;
    font-weight: 800;
    white-space: normal;
    overflow-wrap: anywhere;
    word-break: break-word;
}

.merge-preview-queue-item .row2 {
    margin-top: 4px;
    font-family: Consolas, monospace;
    font-size: 12px;
    opacity: 0.8;
    white-space: pre-wrap;
    line-height: 1.35;
}

@media (max-width: 820px) {
    .merge-preview-grid {
        grid-template-columns: 1fr;
    }
    .merge-preview-queue {
        max-height: min(28vh, 260px);
    }
    .merge-preview-video {
        max-height: min(48vh, 420px);
    }
}

/* ------------------ Merge Submit Panel（提交合并区域） ------------------ */
.merge-submit-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    align-items: end;
}

.merge-submit-field label {
    display: block;
    margin-bottom: 6px;
}

.merge-submit-field input {
    width: 100%;
}

.merge-submit-actions {
    grid-column: 1 / -1;
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
}

/* 进度区域与提交按钮保持适当间距（空内容时不占位） */
#mergeStatus {
    margin-top: 12px;
}
#mergeStatus:empty {
    margin-top: 0;
}

@media (max-width: 720px) {
    .merge-submit-grid {
        grid-template-columns: 1fr;
    }
}

/* ------------------ Select + Preview Panel（选择 + 预览合并） ------------------ */
.select-preview-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 16px;
    align-items: start;
}

.select-preview-left {
    min-width: 0;
}

.select-preview-right {
    min-width: 0;
}

.select-preview-panel .file-tree {
    max-height: min(46vh, 520px);
}

/* 避免 panel 内再嵌套一个“面板样式”的 videoContainer 导致双重边框 */
.select-preview-panel #videoContainer {
    background: transparent;
    border: none;
    box-shadow: none;
    padding: 0;
    margin: 0;
}

.select-preview-panel #playerWrapper {
    width: 100% !important;
}

.select-preview-panel #player {
    width: 100% !important;
    height: auto;
    max-height: min(52vh, 560px);
}
</style>
</head>
<body>

<!-- 加载层 -->
<div id="loadingOverlay" class="loading-overlay">加载中...</div>

<!-- 网站说明（全屏弹窗，启动后 2 秒显示；加载内容可在后台继续） -->
<div id="siteIntroOverlay" class="site-intro-overlay" role="dialog" aria-modal="true" aria-hidden="true">
    <div class="site-intro-modal" role="document">
        <div class="site-intro-header">
            <div class="site-intro-title">使用说明</div>
            <button id="siteIntroCloseX" class="modal-btn" type="button" aria-label="关闭">×</button>
        </div>
        <div class="site-intro-body">
            <div style="opacity:0.9; font-size:13px; line-height:1.6;">
                <div style="font-weight:800; margin-bottom:6px;">快速上手</div>
                <ol style="margin:0 0 10px 18px; padding:0;">
                    <li>左侧选择视频文件，点击“预览”。</li>
                    <li>播放到目标位置后，点“设定起点/设定终点”。</li>
                    <li>点“确认添加片段”，重复添加多个片段。</li>
                    <li>点击“开始切片合并”。合并成功后会自动清空当前片段列表。</li>
                </ol>
                <div style="font-weight:800; margin:10px 0 6px;">常用快捷键</div>
                <ul style="margin:0 0 10px 18px; padding:0;">
                    <li>空格：播放/暂停</li>
                    <li>← / →：后退 / 前进 1 秒</li>
                    <li>[ ：回到片段起点（不播放）</li>
                </ul>
                <div style="font-size:12px; opacity:0.8;">提示：此弹窗出现时，页面内容仍在后台加载。</div>
            </div>

            <label class="site-intro-nomore">
                <input id="siteIntroNoMore" type="checkbox"> 下次不再提示
            </label>
        </div>
        <div class="site-intro-actions">
            <button id="siteIntroOk" class="modal-btn modal-btn-primary" type="button">我知道了</button>
        </div>
    </div>
</div>

<!-- Toast 容器 -->
<div id="toastHost" class="toast-host" aria-live="polite" aria-atomic="true"></div>

<!-- Modal 弹窗 -->
<div id="modalOverlay" class="modal-overlay" role="dialog" aria-modal="true" aria-hidden="true">
    <div class="modal">
        <div id="modalTitle" class="modal-header">提示</div>
        <div id="modalMessage" class="modal-body"></div>
        <div class="modal-actions">
            <button id="modalCancel" class="modal-btn" type="button">取消</button>
            <button id="modalOk" class="modal-btn modal-btn-primary" type="button">确定</button>
        </div>
    </div>
</div>

<!-- 投稿 Modal 弹窗（不再在页面中展开表单） -->
<div id="uploadModalOverlay" class="modal-overlay upload-modal-overlay" role="dialog" aria-modal="true" aria-hidden="true">
    <div class="modal upload-modal">
        <div class="modal-header" style="display:flex; align-items:center; justify-content:space-between; gap:10px;">
            <span>投稿到B站</span>
            <button id="uploadModalCloseX" class="modal-btn" type="button" aria-label="关闭">×</button>
        </div>
        <div class="modal-body upload-modal-body">
            <div style="font-size:12px; opacity:0.85; margin-bottom:10px;">文件：<span id="uploadModalFile" style="font-family:monospace;"></span></div>

            <label for="uploadModalTitle">视频标题：</label>
            <input id="uploadModalTitle" type="text" placeholder="输入视频标题">

            <label for="uploadModalDesc">视频简介：</label>
            <input id="uploadModalDesc" type="text" placeholder="输入视频简介">

            <label for="uploadModalTags">视频标签：</label>
            <div id="uploadModalTagsEditor" class="bili-tags-editor" tabindex="0" aria-label="视频标签输入">
                <div id="uploadModalTagsChips" style="display:contents;"></div>
                <input id="uploadModalTags" class="bili-tags-input" type="text" placeholder="回车添加标签，支持空格/逗号" autocomplete="off" autocapitalize="off" spellcheck="false">
            </div>
            <div id="uploadModalTagsHint" class="bili-tags-hint">最多10个标签；回车/空格/逗号添加，退格删除最后一个。</div>

            <div id="uploadModalHint" style="font-size:12px; opacity:0.85; margin-top:6px; display:none;"></div>
            <div id="uploadModalResult" class="upload-modal-result" style="margin-top:10px;"></div>
        </div>
        <div class="modal-actions">
            <button id="uploadModalCancel" class="modal-btn" type="button">关闭</button>
            <button id="uploadModalStop" class="modal-btn" type="button" style="display:none; background:rgba(255, 100, 100, 0.18); border-color: rgba(255,120,120,0.22);">停止投稿</button>
            <button id="uploadModalSubmit" class="modal-btn modal-btn-primary" type="button">投稿到B站</button>
        </div>
    </div>
</div>

<!-- 历史输出 Modal 弹窗 -->
<div id="historyModalOverlay" class="modal-overlay" role="dialog" aria-modal="true" aria-hidden="true">
    <div class="modal history-modal">
        <div class="modal-header" style="display:flex; align-items:center; justify-content:space-between; gap:10px;">
            <span id="historyModalTitle">历史输出</span>
            <button id="historyModalCloseX" class="modal-btn" type="button" aria-label="关闭">×</button>
        </div>
        <div id="historyModalBody" class="modal-body history-modal-body"></div>
        <div class="modal-actions">
            <button id="historyModalClose" class="modal-btn" type="button">关闭</button>
        </div>
    </div>
</div>

<!-- 合并效果预览 Modal 弹窗 -->
<div id="mergePreviewOverlay" class="modal-overlay merge-preview-overlay" role="dialog" aria-modal="true" aria-hidden="true">
    <div class="modal merge-preview-modal">
        <div class="modal-header" style="display:flex; align-items:center; justify-content:space-between; gap:10px;">
            <span id="mergePreviewTitle">合并效果预览</span>
            <button id="mergePreviewCloseX" class="modal-btn" type="button" aria-label="关闭">×</button>
        </div>
        <div class="modal-body merge-preview-body">
            <div class="merge-preview-grid">
                <div class="merge-preview-left">
                    <div id="mergePreviewNow" class="merge-preview-now"></div>
                    <video id="mergePreviewPlayer" class="merge-preview-video" preload="metadata" playsinline></video>

                    <!-- 合并预览自定义控制条（不使用原生 controls） -->
                    <div class="video-controls merge-preview-controls">
                        <div class="progress-row">
                            <input type="range" id="mergePreviewProgress" value="0" min="0" step="0.01" class="custom-range">
                        </div>
                        <div class="buttons-row">
                            <div class="ctrl-group left">
                                <button id="mergePreviewPlayPause" class="icon-btn" type="button" title="播放/暂停">播放</button>
                                <span id="mergePreviewTime" class="time-text">00:00:00 / 00:00:00</span>
                            </div>
                            <div class="ctrl-group right" style="justify-content:flex-end; gap:10px;">
                            </div>
                        </div>
                    </div>
                </div>

                <div class="merge-preview-right">
                    <div class="merge-preview-queue">
                        <div class="merge-preview-queue-title">
                            <span>片段队列</span>
                            <span id="mergePreviewQueueMeta" style="opacity:0.85;"></span>
                        </div>
                        <div id="mergePreviewQueueList"></div>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>

<!-- 主内容 -->
<div class="scroll-wrapper" id="mainContent" style="display:none;">
<div class="container">
<h1>括弧笑直播切片工具</h1>

<div class="panel select-preview-panel">
    <div class="select-preview-grid">
        <div class="select-preview-left">
            <div style="font-weight:700; margin-bottom:6px;">选择视频文件：</div>
            <div id="fileTree" class="file-tree"></div>
        </div>

        <div class="select-preview-right">
            <div id="videoContainer" class="video-placeholder-container video-container-compact">
    <!-- 文件名显示区域 (常驻) -->
    <div style="text-align: center; width: 100%; margin-bottom: 10px;">
        <span id="videoPlaceholder" class="video-placeholder-text" style="display: block; font-weight:bold; word-break:break-all;">请选择视频文件</span>
    </div>

    <!-- 预览按钮区域 -->
    <div id="previewActionArea" style="text-align: center; width: 100%;">
        <button id="previewBtn" style="display:none;">预览</button>
    </div>
    
    <div id="playerWrapper" style="display:none; flex-direction:column; width:fit-content; max-width:100%;">
        <video id="player" preload="metadata" style="display:block; width:100%; height:auto; max-height:60vh;"></video>
        
    <div id="videoControlsContainer" class="video-controls">
            <!-- 进度条行 -->
            <div class="progress-row">
                 <input type="range" id="progressBar" value="0" min="0" step="1" class="custom-range">
            </div>
            
            <!-- 按钮控制行 -->
            <div class="buttons-row main-ctrls">
                <!-- 左侧：播放与时间 -->
                <div class="ctrl-group left">
                    <button id="playPauseBtn" class="icon-btn" title="播放/暂停">播放</button>
                    <span id="timeDisplay" class="time-text">00:00:00 / 00:00:00</span>
                </div>
                
                <!-- 中间：跳跃 -->
                <div class="ctrl-group center">
                     <button id="rewindBtn" class="icon-btn small" title="后退5秒">-5s</button>
                     <button id="rewind1Btn" class="icon-btn small" title="后退1秒">-1s</button>
                     <button id="forward1Btn" class="icon-btn small" title="前进1秒">+1s</button>
                     <button id="forwardBtn" class="icon-btn small" title="前进5秒">+5s</button>
                </div>

                <!-- 右侧：倍速 -->
                <div class="ctrl-group right">
                     <select id="speedSelect" class="speed-select" title="播放速度">
                        <option value="0.5">0.5x</option>
                        <option value="1.0" selected>1.0x</option>
                        <option value="1.25">1.25x</option>
                        <option value="1.5">1.5x</option>
                        <option value="2.0">2.0x</option>
                        <option value="3.0">3.0x</option>
                     </select>
                     <button id="fullscreenBtn" class="icon-btn" title="全屏" style="padding: 4px 10px;">⛶</button>
                </div>
            </div>

            <!-- 快捷键与状态合并显示 -->
            <div class="shortcuts-grid" style="margin-top: 10px; display: grid; grid-template-columns: 1fr 1fr 0.8fr 0.8fr; gap: 10px; align-items: stretch;">
                 <!-- Start Group -->
                 <button id="quickSetStartBtn" class="icon-btn" style="display:flex; flex-direction:column; justify-content:center; align-items:center; gap:2px; height:auto; padding:6px;" title="设为起点">
                     <span style="font-size:12px; font-weight:bold;">Start [Q]</span>
                     <span id="ctrlStartDisp" style="font-size:11px; font-family:monospace; color:var(--accent-color); opacity:0.9;">--:--:--</span>
                 </button>
                 
                 <!-- End Group -->
                 <button id="quickSetEndBtn" class="icon-btn" style="display:flex; flex-direction:column; justify-content:center; align-items:center; gap:2px; height:auto; padding:6px;" title="设为终点">
                     <span style="font-size:12px; font-weight:bold;">End [W]</span>
                     <span id="ctrlEndDisp" style="font-size:11px; font-family:monospace; color:var(--accent-color); opacity:0.9;">--:--:--</span>
                 </button>

                 <!-- Play & Add Buttons -->
                 <button id="quickPlayClipBtn" class="icon-btn" style="height:100%;" title="预览选中片段">Play [P]</button>
                 <button id="quickAddClipBtnCtrl" class="icon-btn" style="height:100%; background:var(--accent-color); border-color:var(--accent-color);" title="添加片段">Add [C]</button>
            </div>
        </div>
    </div>

</div>

        </div>
    </div>
</div>




<!-- 新的切片工坊区域 -->
<div class="panel" id="clipWorkshopPanel">
    <div style="margin-bottom:12px;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
             <h3>切片工坊</h3>
        </div>
        <div id="currentVideoInfoBlock" class="current-video-info" style="display:none; margin-top:8px;">
            <div style="font-size:12px; opacity:0.8;">正在编辑视频</div>
            <div id="currentVideoLabel" style="font-size:16px; font-weight:bold; color:var(--accent-color); word-break:break-all;"></div>
        </div>
    </div>

    <!-- 时间轴/片段控制区 -->
    <div class="clip-studio" style="display:flex; flex-direction:column; gap:20px;">
        <!-- 上：快速控制与添加 -->
        <div style="width:100%; background: rgba(0,0,0,0.2); padding: 15px; border-radius: 8px; box-sizing:border-box;">
            <div style="display: flex; gap: 10px; margin-bottom: 12px;">
                <button id="setStartBtn" class="action-btn" style="flex:1;">设定起点 [Q]</button>
                 <button id="setEndBtn" class="action-btn" style="flex:1;">设定终点 [W]</button>
            </div>

            <div style="display: flex; gap: 10px; align-items: center; margin-bottom: 15px;">
                <input id="newClipStart" type="text" placeholder="起点 (00:00:00)" style="flex:1; min-width:0; text-align:center;">
                <span style="color:var(--muted-color);">→</span>
                <input id="newClipEnd" type="text" placeholder="终点 (00:00:00)" style="flex:1; min-width:0; text-align:center;">
            </div>

            <button id="confirmAddClipBtn" style="width:100%; padding:10px; background:var(--accent-color); border-color:transparent;">
                确认添加片段
            </button>
        </div>

        <!-- 下：已添加片段列表 -->
        <div style="width:100%; box-sizing:border-box;">
            <div style="display:flex; justify-content:space-between; margin-bottom:8px; gap:8px; align-items:center; flex-wrap:wrap;">
                      <div style="font-weight:700;">待合并片段列表：</div>
                 <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
                    <button id="previewMergeBtn" style="padding:2px 8px; font-size:12px; background:rgba(79, 158, 255, 0.25);">预览合并</button>
                    <button id="clearAllClipsFn" style="padding:2px 8px; font-size:12px; background:rgba(255,50,50,0.3);">清空</button>
                 </div>
            </div>
            <div id="newClipListContainer" class="clip-list-scroll">
                <div style="text-align:center; padding:20px; color:var(--muted-color);">暂无片段</div>
            </div>
        </div>
    </div>
</div>

<div class="panel">
    <div style="font-weight:800; margin-bottom:10px;">提交合并</div>
    <div class="merge-submit-grid">
        <div class="merge-submit-field">
            <label for="usernameInput">用户名：</label>
            <input id="usernameInput" name="bili_display_name" type="text" placeholder="您的B站名称" autocomplete="off" data-bwignore="true" data-1p-ignore="true" data-lpignore="true">
        </div>
        <div class="merge-submit-field">
            <label for="clipTitleInput">切片名称（可选）：</label>
            <input id="clipTitleInput" type="text" placeholder="例如：2026-01-31 直播切片">
        </div>
        <div class="merge-submit-actions">
            <button id="mergeAllBtn">开始切片合并</button>
            <button id="openHistoryBtn" type="button" style="display:none;">历史输出</button>
        </div>
    </div>
    <div id="mergeStatus"></div>
    <div id="mergeResult"></div>
</div>

</div>
</div>

<script>
let currentVideoName = '';
let videoTasks = []; // 存储所有视频的任务结构

// 合并顺序覆盖（用于“预览队列拖拽排序”后，提交时按该顺序合并）
let __mergeOrderOverride = null; // [{name,start,end}]
function __invalidateMergeOrderOverride() {
    __mergeOrderOverride = null;
}

function __clipKey(name, start, end) {
    return `${String(name)}|${Number(start)}|${Number(end)}`;
}

function __flattenVideoTasksToClips() {
    const flat = [];
    for (const t of (videoTasks || [])) {
        const name = String(t?.name || '').trim();
        if (!name) continue;
        const clips = Array.isArray(t?.clips) ? t.clips : [];
        for (const c of clips) {
            const start = Number(c?.start);
            const end = Number(c?.end);
            if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
            if (start < 0 || end <= start) continue;
            flat.push({ name, start, end });
        }
    }
    return flat;
}

function __isMergeOrderOverrideValid() {
    if (!Array.isArray(__mergeOrderOverride)) return false;
    const current = __flattenVideoTasksToClips();
    if (__mergeOrderOverride.length !== current.length) return false;

    const counts = new Map();
    for (const c of current) {
        const k = __clipKey(c.name, c.start, c.end);
        counts.set(k, (counts.get(k) || 0) + 1);
    }
    for (const c of __mergeOrderOverride) {
        const name = String(c?.name || '').trim();
        const start = Number(c?.start);
        const end = Number(c?.end);
        if (!name || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) return false;
        const k = __clipKey(name, start, end);
        const left = (counts.get(k) || 0) - 1;
        if (left < 0) return false;
        if (left === 0) counts.delete(k);
        else counts.set(k, left);
    }
    return true;
}

function __buildVideoTasksFromFlatClips(flat) {
    const list = Array.isArray(flat) ? flat : [];
    const tasks = [];
    let lastTask = null;
    for (const c of list) {
        const name = String(c?.name || '').trim();
        const start = Number(c?.start);
        const end = Number(c?.end);
        if (!name || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
        if (!lastTask || lastTask.name !== name) {
            lastTask = { name, clips: [] };
            tasks.push(lastTask);
        }
        lastTask.clips.push({ start, end });
    }
    return tasks;
}

function __applyMergeOrderOverrideToVideoTasks(opts = {}) {
    const shouldRender = opts.render !== false;
    const shouldSave = opts.save === true;

    if (!__isMergeOrderOverrideValid()) return false;
    videoTasks = __buildVideoTasksFromFlatClips(__mergeOrderOverride);
    if (shouldRender) renderNewClipList();
    if (shouldSave) __saveClipToolState();
    return true;
}

function __getTotalClipCountFromVideoTasks() {
    let total = 0;
    for (const t of (videoTasks || [])) {
        const clips = Array.isArray(t?.clips) ? t.clips : [];
        total += clips.length;
    }
    return total;
}
const player = document.getElementById('player');
const playerWrapper = document.getElementById('playerWrapper');
const mergeStatus = document.getElementById('mergeStatus');
const mergeResult = document.getElementById('mergeResult');
const videoContainer = document.getElementById('videoContainer');
const fileTreeDiv = document.getElementById('fileTree');
const previewBtn = document.getElementById('previewBtn');
const videoPlaceholder = document.getElementById('videoPlaceholder');
const videoControlsContainer = document.getElementById('videoControlsContainer');
const progressBar = document.getElementById('progressBar');
const playPauseBtn = document.getElementById('playPauseBtn');
const timeDisplay = document.getElementById('timeDisplay');
const rewindBtn = document.getElementById('rewindBtn');
const rewind1Btn = document.getElementById('rewind1Btn');
const forward1Btn = document.getElementById('forward1Btn');
const forwardBtn = document.getElementById('forwardBtn');
const quickSetStartBtn = document.getElementById('quickSetStartBtn');
const quickSetEndBtn = document.getElementById('quickSetEndBtn');
const quickAddClipBtnCtrl = document.getElementById('quickAddClipBtnCtrl');
const quickPlayClipBtn = document.getElementById('quickPlayClipBtn');
const speedSelect = document.getElementById('speedSelect');
const fullscreenBtn = document.getElementById('fullscreenBtn');
const ctrlStartDisp = document.getElementById('ctrlStartDisp');
const ctrlEndDisp = document.getElementById('ctrlEndDisp');
const usernameInput = document.getElementById('usernameInput');
const clipTitleInput = document.getElementById('clipTitleInput');
const openHistoryBtn = document.getElementById('openHistoryBtn');
const previewMergeBtn = document.getElementById('previewMergeBtn');

// 防止鼠标点击按钮后保留焦点，导致后续空格键触发该按钮：
// 记录最后一次指针类型，若为鼠标，则在 click 后对被点击的 button 执行 blur()
let __lastPointerWasMouse = false;
document.addEventListener('pointerdown', (e) => {
    try { __lastPointerWasMouse = (e && e.pointerType === 'mouse'); } catch (err) { __lastPointerWasMouse = false; }
}, true);
document.addEventListener('click', (e) => {
    try {
        const btn = e.target && e.target.closest ? e.target.closest('button') : null;
        if (btn && __lastPointerWasMouse) {
            try { btn.blur(); } catch (err) {}
        }
    } catch (err) {}
}, true);

// ------------------ 全局合并状态/进度展示 ------------------
let __mergeStatusPollTimer = null; // setTimeout 句柄
let __mergeStatusPollDesired = false; // 是否需要轮询（避免页面打开就一直请求）
let __mergeStatusPollInFlight = false; // 避免并发请求堆积
let __mergeStatusLastState = null;

function __getMergeToken() {
    const key = 'bililive_merge_token';
    try {
        return String(localStorage.getItem(key) || '').trim();
    } catch (e) {
        return '';
    }
}

function __setMergeToken(token) {
    const key = 'bililive_merge_token';
    const t = String(token || '').trim();
    try {
        if (t) localStorage.setItem(key, t);
        else localStorage.removeItem(key);
    } catch (e) {}
}

function __pushLocalMergeHistory(item) {
    const key = 'bililive_merge_history';
    try {
        const raw = localStorage.getItem(key);
        const arr = raw ? JSON.parse(raw) : [];
        const next = Array.isArray(arr) ? arr : [];
        next.unshift({
            ts: Date.now(),
            ...item,
        });
        // 去重（按 job_id/out_file）并限制长度
        const seen = new Set();
        const compact = [];
        for (const x of next) {
            const k = String(x?.job_id || x?.out_file || x?.ts || '');
            if (!k || seen.has(k)) continue;
            seen.add(k);
            compact.push(x);
            if (compact.length >= 30) break;
        }
        localStorage.setItem(key, JSON.stringify(compact));
    } catch (e) {}
}

async function __fetchMergeStatus() {
    try {
        const token = __getMergeToken();
        const url = token ? `/api/merge_status?merge_token=${encodeURIComponent(token)}` : '/api/merge_status';
        const res = await fetch(url, { cache: 'no-store' });
        return await res.json();
    } catch (e) {
        return { running: false };
    }
}

function __stopMergeStatusPolling() {
    __mergeStatusPollDesired = false;
    if (__mergeStatusPollTimer) {
        clearTimeout(__mergeStatusPollTimer);
    }
    __mergeStatusPollTimer = null;
}

function __computeNextMergePollMs(state, { forceFast = false } = {}) {
    const s = state && typeof state === 'object' ? state : { running: false };
    const running = s.running === true;

    // 未授权：后端会返回 running=false，这里也直接停止轮询
    if (!running && !String(__getMergeToken() || '').trim()) return 0;

    // 页面在后台时大幅降频，避免“挂着标签页”刷接口
    if (document.hidden && !forceFast) return 15000;

    if (running) {
        return forceFast ? 800 : 1200;
    }

    // 不在运行：
    // 1) 如果有 done/error 信息需要展示，短暂再确认一次；
    // 2) 否则直接停止轮询（这是“打开页面就一直查询”的根因）。
    const status = String(s.status || '').toLowerCase();
    // 已完成/出错/取消是终态：展示一次即可停止，避免“终态还在刷接口”。
    if (status === 'done' || status === 'error' || status === 'cancelled') return 0;
    return 0;
}

async function __mergeStatusTick({ forceFast = false } = {}) {
    if (!__mergeStatusPollDesired) {
        __mergeStatusPollTimer = null;
        return;
    }

    // 防止网络慢时 tick 重入导致请求堆叠
    if (__mergeStatusPollInFlight) {
        __mergeStatusPollTimer = setTimeout(() => __mergeStatusTick({ forceFast }), 500);
        return;
    }

    __mergeStatusPollInFlight = true;
    const s = await __fetchMergeStatus();
    __mergeStatusPollInFlight = false;

    __mergeStatusLastState = s;
    __renderMergeStatus(s);

    // 若页面刷新/中途关闭导致 pollJob 没跑完，这里用 merge_status 的终态回填“历史输出”。
    try {
        const status = String(s?.status || '').toLowerCase();
        const outFile = String(s?.out_file || '').trim();
        if (!__isOutputHistoryAutofillSuppressed() && status === 'done' && outFile) {
            __addOutputToHistory(outFile);
            __renderOutputHistory();
        }
    } catch (e) {
        // ignore
    }

    const nextMs = __computeNextMergePollMs(s, { forceFast });
    if (nextMs <= 0) {
        __stopMergeStatusPolling();
        return;
    }
    __mergeStatusPollTimer = setTimeout(() => __mergeStatusTick({ forceFast }), nextMs);
}

function __formatUnixTs(ts) {
    const n = Number(ts);
    if (!Number.isFinite(n) || n <= 0) return '';
    try {
        const d = new Date(n * 1000);
        const pad = (x) => String(x).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    } catch {
        return '';
    }
}

function __renderMergeStatus(state) {
    if (!mergeStatus) return;

    const s = state && typeof state === 'object' ? state : { running: false };
    const running = s.running === true;

    // 未授权/无状态：不展示任何合并信息
    if (!running && !String(s.status || '').trim()) {
        mergeStatus.textContent = '';
        return;
    }
    const username = String(s.username || '').trim() || '未知用户';
    const stageRaw = String(s.stage || s.status || '').toLowerCase();
    const stageText = stageRaw === 'slicing' ? '切片中'
        : stageRaw === 'merging' ? '合并中'
        : stageRaw === 'queued' ? '排队中'
        : stageRaw === 'cancelling' ? '取消中'
        : stageRaw === 'cancelled' ? '已取消'
        : (s.status === 'done' ? '完成' : s.status === 'error' ? '出错' : s.status === 'cancelled' ? '已取消' : '');

    const percent = Math.max(0, Math.min(1, Number(s.percent ?? 0)));
    const pctText = `${(percent * 100).toFixed(1)}%`;
    const doneClips = Number(s.done_clips ?? 0);
    const totalClips = Number(s.total_clips ?? 0);
    const current = String(s.current || '').trim();
    const startedAtText = __formatUnixTs(s.started_at);
    const etaHuman = String(s.eta_human || '').trim();

    if (!running) {
        if (s.status === 'done') {
            mergeStatus.textContent = '';
            return;
        }
        if (s.status === 'error' || s.status === 'cancelled') {
            mergeStatus.textContent = '';
            return;
        }

        mergeStatus.textContent = '';
        return;
    }

    const pctNow = (percent * 100);
    mergeStatus.innerHTML = `
        <div class="merge-status-card">
            <div class="merge-status-top">
                <div class="merge-status-title">
                    <span class="merge-status-stage">${stageText || '处理中'}</span>
                    <span class="merge-status-percent">${pctText}</span>
                </div>
                <div class="merge-status-actions">
                    <div class="merge-status-user">用户：${username}</div>
                    <button id="cancelMergeBtn" type="button" class="merge-status-cancel">取消合并</button>
                </div>
            </div>
            <div class="merge-status-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pctNow.toFixed(1)}">
                <div class="merge-status-bar-fill" style="width:${pctNow.toFixed(2)}%;"></div>
            </div>
            <div class="merge-status-meta">
                <div class="merge-status-meta-left">${(Number.isFinite(doneClips) && Number.isFinite(totalClips) && totalClips > 0) ? `片段：${doneClips}/${totalClips}` : ''}${current ? ` · 当前：${current}` : ''}${etaHuman ? ` · 剩余：${etaHuman}` : ''}</div>
                <div class="merge-status-meta-right">${startedAtText ? `开始于：${startedAtText}` : ''}</div>
            </div>
        </div>
    `;

    // 事件绑定：每次渲染会重建 DOM，这里直接绑定即可
    const cancelBtn = mergeStatus.querySelector('#cancelMergeBtn');
    if (cancelBtn) {
        cancelBtn.addEventListener('click', async () => {
            const ok = await showConfirmModal('确定要取消当前合并吗？已生成的临时片段会被清理。', {
                title: '取消合并',
                okText: '取消合并',
                cancelText: '继续等待',
            });
            if (!ok) return;

            try {
                cancelBtn.disabled = true;
                cancelBtn.textContent = '取消中...';
                const res = await fetch('/api/cancel_merge', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ job_id: String(s.job_id || '') || null, merge_token: __getMergeToken() || null }),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    showToast(data.detail || data.message || '取消失败');
                } else {
                    showToast('已发送取消请求');
                }
            } catch (e) {
                showToast('取消请求发送失败');
            } finally {
                __startMergeStatusPolling({ forceFast: true });
            }
        });
    }
}

function __startMergeStatusPolling(opts = {}) {
    const forceFast = opts && opts.forceFast === true;
    __mergeStatusPollDesired = true;
    if (__mergeStatusPollTimer) return;
    // 先立即拉一次；后续根据状态自适应调度/停止
    __mergeStatusTick({ forceFast });
}

// 从后台切回前台时补一次刷新
document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
        // 切回前台时补一次刷新；若发现正在合并，会自动进入轮询
        __startMergeStatusPolling();
    }
});

function __setVideoContainerExpanded(expanded) {
    if (!videoContainer) return;
    videoContainer.classList.toggle('video-container-expanded', !!expanded);
    videoContainer.classList.toggle('video-container-compact', !expanded);
}

// ------------------ 预览播放进度保存/恢复 ------------------
// 说明：仅针对“预览”播放行为做续播；数据保存在浏览器 localStorage。
// 只保存“一条记录”（最后一次预览的视频 + 其播放时间）。
const VIDEO_PROGRESS_SINGLE_KEY = 'videoProgress:single';
const VIDEO_PROGRESS_SAVE_INTERVAL_MS = 2000; // 节流保存频率
const VIDEO_PROGRESS_MIN_SAVE_SECONDS = 2; // 太靠前不保存
const VIDEO_PROGRESS_CLEAR_NEAR_END_SECONDS = 3; // 接近结束自动清除进度

let __restoreProgressForVideo = null;
let __lastProgressSaveAt = 0;

function __loadProgressSingle() {
    try {
        const raw = localStorage.getItem(VIDEO_PROGRESS_SINGLE_KEY);
        if (!raw) return null;
        const obj = JSON.parse(raw);
        const name = String(obj?.name || '');
        const t = Number(obj?.t);
        if (!name) return null;
        if (!Number.isFinite(t) || t < 0) return null;
        return { name, t, at: Number(obj?.at) || 0 };
    } catch (e) {
        return null;
    }
}

function __clearProgressSingle() {
    try {
        localStorage.removeItem(VIDEO_PROGRESS_SINGLE_KEY);
    } catch (e) {
        // ignore
    }
}

function __saveProgress(videoName, currentTime, duration) {
    if (!videoName) return;
    const t = Number(currentTime);
    const d = Number(duration);
    if (!Number.isFinite(t) || t < VIDEO_PROGRESS_MIN_SAVE_SECONDS) return;

    // 播放到末尾附近：认为已看完，清除进度，避免下次从结尾继续
    if (Number.isFinite(d) && d > 0 && t >= Math.max(0, d - VIDEO_PROGRESS_CLEAR_NEAR_END_SECONDS)) {
        const saved = __loadProgressSingle();
        if (saved && saved.name === videoName) {
            __clearProgressSingle();
        }
        return;
    }

    try {
        localStorage.setItem(VIDEO_PROGRESS_SINGLE_KEY, JSON.stringify({
            name: String(videoName),
            t: Math.floor(t),
            at: Date.now(),
        }));
    } catch (e) {
        // ignore
    }
}

function __maybeRestoreProgress() {
    if (!__restoreProgressForVideo) return;
    if (!currentVideoName || __restoreProgressForVideo !== currentVideoName) return;

    const saved = __loadProgressSingle();
    __restoreProgressForVideo = null;
    if (!saved) return;
    if (saved.name !== currentVideoName) return;

    const d = Number(player.duration);
    const target = Number(saved.t);
    if (!Number.isFinite(target) || target < VIDEO_PROGRESS_MIN_SAVE_SECONDS) return;
    if (Number.isFinite(d) && d > 0 && target >= d - VIDEO_PROGRESS_CLEAR_NEAR_END_SECONDS) return;

    // 仅在 metadata ready 后设置 currentTime 才可靠
    try {
        player.currentTime = target;
        showToast(`已从上次进度继续：${formatTime(target)}`);
    } catch (e) {
        // ignore
    }
}

// ------------------ 用户名 & 片段列表持久化 ------------------
// 说明：把用户名和 videoTasks(片段列表)持久化到 localStorage，刷新页面不丢。
const CLIP_TOOL_STATE_KEY = 'clipTool:state:v1';
const BILI_UPLOAD_TAGS_KEY = 'biliUpload:tags:v1';
const OUTPUT_HISTORY_KEY = 'clipTool:outputs:v1';
const OUTPUT_HISTORY_AUTOFILL_SUPPRESS_KEY = 'clipTool:outputs:autofill_suppress:v1';

function __isOutputHistoryAutofillSuppressed() {
    try {
        return String(localStorage.getItem(OUTPUT_HISTORY_AUTOFILL_SUPPRESS_KEY) || '').trim() === '1';
    } catch (e) {
        return false;
    }
}

function __setOutputHistoryAutofillSuppressed(flag) {
    try {
        if (flag) localStorage.setItem(OUTPUT_HISTORY_AUTOFILL_SUPPRESS_KEY, '1');
        else localStorage.removeItem(OUTPUT_HISTORY_AUTOFILL_SUPPRESS_KEY);
    } catch (e) {
        // ignore
    }
}

function __loadOutputHistory() {
    try {
        const raw = localStorage.getItem(OUTPUT_HISTORY_KEY);
        if (!raw) return [];
        const arr = JSON.parse(raw);
        if (!Array.isArray(arr)) return [];
        // shape: [{file, at}]
        const out = [];
        for (const it of arr) {
            const file = String(it?.file || '').trim();
            const at = Number(it?.at) || 0;
            if (!file) continue;
            out.push({ file, at });
        }
        // 新 -> 旧
        out.sort((a, b) => (b.at || 0) - (a.at || 0));
        return out;
    } catch (e) {
        return [];
    }
}

function __saveOutputHistory(list) {
    try {
        localStorage.setItem(OUTPUT_HISTORY_KEY, JSON.stringify(list || []));
    } catch (e) {
        // ignore
    }

    // 历史为空：视为用户主动清空，禁止通过 merge_status 自动回填旧记录。
    // 历史非空：允许自动回填（例如页面刷新时补写“刚完成但 pollJob 未跑完”的记录）。
    try {
        const n = Array.isArray(list) ? list.length : 0;
        __setOutputHistoryAutofillSuppressed(n === 0);
    } catch (e) {
        // ignore
    }
}

function __addOutputToHistory(file) {
    const f = String(file || '').trim();
    if (!f) return;
    const list = __loadOutputHistory();
    // 去重：同名只保留一条（更新时间）
    const now = Date.now();
    const filtered = list.filter(x => x.file !== f);
    filtered.unshift({ file: f, at: now });
    // 限制数量，避免无限增长
    const MAX_ITEMS = 20;
    __saveOutputHistory(filtered.slice(0, MAX_ITEMS));
}

// ------------------ 历史输出弹窗 ------------------
let __historyModalInited = false;
let __historyModalEls = null;

function __initHistoryModalOnce() {
    if (__historyModalInited) return;

    const overlay = document.getElementById('historyModalOverlay');
    const closeX = document.getElementById('historyModalCloseX');
    const closeBtn = document.getElementById('historyModalClose');
    const body = document.getElementById('historyModalBody');
    const title = document.getElementById('historyModalTitle');

    if (!overlay || !closeX || !closeBtn || !body || !title) return;

    __historyModalEls = { overlay, closeX, closeBtn, body, title };
    __historyModalInited = true;

    const closeModal = () => {
        overlay.classList.remove('show');
        overlay.setAttribute('aria-hidden', 'true');
    };

    closeX.addEventListener('click', closeModal);
    closeBtn.addEventListener('click', closeModal);
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closeModal();
    });
    document.addEventListener('keydown', (e) => {
        if (!overlay.classList.contains('show')) return;
        if (e.key === 'Escape') closeModal();
    });
}

function __openHistoryModal() {
    __initHistoryModalOnce();
    if (!__historyModalEls) {
        showToast('历史输出弹窗初始化失败');
        return;
    }
    __historyModalEls.overlay.classList.add('show');
    __historyModalEls.overlay.setAttribute('aria-hidden', 'false');
    __renderOutputHistory();
}

if (openHistoryBtn) {
    openHistoryBtn.addEventListener('click', () => __openHistoryModal());
}

// ------------------ 投稿弹窗（替代页面内展开表单） ------------------
let __uploadModalInited = false;
let __uploadModalUploading = false;
let __uploadModalFileName = '';
let __uploadModalEls = null;
let __uploadModalTags = [];

let __uploadPollTimer = null;
let __uploadPollInFlight = false;

function __getUploadToken() {
    const key = 'bililive_upload_token';
    try {
        return String(localStorage.getItem(key) || '').trim();
    } catch (e) {
        return '';
    }
}

function __setUploadToken(token) {
    const key = 'bililive_upload_token';
    const t = String(token || '').trim();
    try {
        if (t) localStorage.setItem(key, t);
        else localStorage.removeItem(key);
    } catch (e) {}
}

function __stopUploadPolling() {
    if (__uploadPollTimer) clearTimeout(__uploadPollTimer);
    __uploadPollTimer = null;
}

async function __fetchUploadStatus() {
    const token = __getUploadToken();
    const url = token ? `/api/upload_status?upload_token=${encodeURIComponent(token)}` : '/api/upload_status';
    try {
        const res = await fetch(url, { cache: 'no-store' });
        return await res.json();
    } catch (e) {
        return { running: false };
    }
}

function __renderUploadProgress(state, resultEl) {
    const s = state && typeof state === 'object' ? state : { running: false };
    const running = s.running === true;
    const status = String(s.status || '').toLowerCase();
    const pct = Math.max(0, Math.min(1, Number(s.percent ?? 0)));
    const pctText = `${(pct * 100).toFixed(1)}%`;
    const speed = String(s.speed || '').trim();
    const eta = String(s.eta || '').trim();
    const progressLine = String(s.progress_line || '').trim();
    const logs = Array.isArray(s.logs) ? s.logs : [];

    const metaParts = [];
    if (speed) metaParts.push(`速度：${speed}`);
    if (eta) metaParts.push(`剩余：${eta}`);
    const metaText = metaParts.join(' · ');
    const safeLogs = logs.slice(-200).reverse().map(x => String(x)).join('\\n');
    const rawLineHtml = progressLine
        ? `<div class="upload-progress-rawline">${__escapeHtml(progressLine)}</div>`
        : '<div class="upload-progress-rawline" style="opacity:0.55;">（等待输出…）</div>';

    if (!running && !status) {
        resultEl.innerHTML = '';
        return;
    }

    if (status === 'done') {
        resultEl.innerHTML = `
            <div class="upload-progress">
                <div class="upload-progress-top">
                    <div class="upload-progress-title" style="color:rgba(140,255,140,0.95);">投稿完成</div>
                    <div class="upload-progress-percent">100.0%</div>
                </div>
                <div class="upload-progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="100">
                    <div class="upload-progress-bar-fill" style="width:100%;"></div>
                </div>
                <div class="upload-progress-meta">${__escapeHtml(metaText)}</div>
                ${rawLineHtml}
                ${safeLogs ? `<pre class="upload-progress-logs">${__escapeHtml(safeLogs)}</pre>` : ''}
            </div>
        `;
        return;
    }
    if (status === 'error') {
        const err = String(s.error || '').trim();
        const errHtml = err ? `投稿失败：\n${__escapeHtml(err)}` : '投稿失败';
        resultEl.innerHTML = `
            <pre style="color:red;">${errHtml}</pre>
            <div class="upload-progress">
                <div class="upload-progress-top">
                    <div class="upload-progress-title">错误详情</div>
                    <div class="upload-progress-percent">${pctText}</div>
                </div>
                <div class="upload-progress-meta">${__escapeHtml(metaText)}</div>
                ${rawLineHtml}
                ${safeLogs ? `<pre class="upload-progress-logs">${__escapeHtml(safeLogs)}</pre>` : ''}
            </div>
        `;
        return;
    }
    if (status === 'cancelled') {
        resultEl.innerHTML = `
            <div class="upload-progress">
                <div class="upload-progress-top">
                    <div class="upload-progress-title" style="color:#ffb0b0;">已停止投稿</div>
                    <div class="upload-progress-percent">${pctText}</div>
                </div>
                <div class="upload-progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${(pct * 100).toFixed(1)}">
                    <div class="upload-progress-bar-fill" style="width:${(pct * 100).toFixed(2)}%;"></div>
                </div>
                <div class="upload-progress-meta">${__escapeHtml(metaText)}</div>
                ${rawLineHtml}
                ${safeLogs ? `<pre class="upload-progress-logs">${__escapeHtml(safeLogs)}</pre>` : ''}
            </div>
        `;
        return;
    }

    resultEl.innerHTML = `
        <div class="upload-progress">
            <div class="upload-progress-top">
                <div class="upload-progress-title">投稿中</div>
                <div class="upload-progress-percent">${pctText}</div>
            </div>
            <div class="upload-progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${(pct * 100).toFixed(1)}">
                <div class="upload-progress-bar-fill" style="width:${(pct * 100).toFixed(2)}%;"></div>
            </div>
            <div class="upload-progress-meta">${__escapeHtml(metaText)}</div>
            ${rawLineHtml}
            <pre class="upload-progress-logs">${__escapeHtml(safeLogs)}</pre>
        </div>
    `;
}

async function __uploadPollTick(resultEl, submitBtn, stopBtn, hintEl, setDisabled) {
    if (__uploadPollInFlight) {
        __uploadPollTimer = setTimeout(() => __uploadPollTick(resultEl, submitBtn, stopBtn, hintEl, setDisabled), 800);
        return;
    }

    __uploadPollInFlight = true;
    const s = await __fetchUploadStatus();
    __uploadPollInFlight = false;

    __renderUploadProgress(s, resultEl);
    const status = String(s?.status || '').toLowerCase();
    const running = s?.running === true;

    if (running) {
        hintEl.style.display = 'block';
        hintEl.textContent = '投稿进行中（实时进度/日志），如卡住可停止投稿。';
        submitBtn.textContent = '投稿中...';
        submitBtn.disabled = true;
        stopBtn.style.display = '';
        stopBtn.disabled = false;
        setDisabled(true);
        __uploadModalUploading = true;
        __uploadPollTimer = setTimeout(() => __uploadPollTick(resultEl, submitBtn, stopBtn, hintEl, setDisabled), 1000);
        return;
    }

    __stopUploadPolling();
    __uploadModalUploading = false;

    if (status === 'done') {
        hintEl.style.display = 'none';
        stopBtn.style.display = 'none';
        submitBtn.textContent = '投稿成功！';
        submitBtn.disabled = true;
        __setUploadToken('');
        return;
    }
    if (status === 'cancelled') {
        hintEl.style.display = 'none';
        stopBtn.style.display = 'none';
        submitBtn.textContent = '重新投稿';
        submitBtn.disabled = false;
        setDisabled(false);
        __setUploadToken('');
        const reason = String(s?.cancel_reason || '').toLowerCase();
        if (reason === 'user') {
            showToast('取消成功');
        }
        return;
    }

    // error 或其他终态
    hintEl.style.display = 'none';
    stopBtn.style.display = 'none';
    submitBtn.textContent = '重新投稿';
    submitBtn.disabled = false;
    setDisabled(false);
    __setUploadToken('');
}

const BILI_TAG_MAX_COUNT = 10;
const BILI_TAG_MAX_LEN = 20;

function __escapeHtml(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function __splitTags(raw) {
    const s0 = String(raw ?? '').trim();
    if (!s0) return [];
    const s = s0
        .replace(/\\r/g, '\\n')
        .replace(/[，、；;]/g, ',')
        .replace(/\\n/g, ' ');
    return s
        .split(/[\\s,]+/)
        .map(x => String(x || '').trim())
        .filter(Boolean);
}

function __dedupeTags(tags) {
    const out = [];
    const seen = new Set();
    for (const t of (tags || [])) {
        const tag = String(t || '').trim();
        if (!tag) continue;
        const key = tag.toLowerCase();
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(tag);
    }
    return out;
}

function __resetUploadModalTagsFromRaw(raw) {
    __uploadModalTags = __dedupeTags(__splitTags(raw)).slice(0, BILI_TAG_MAX_COUNT);
}

function __renderUploadModalTagsUi(tagsChips, tagsInput, tagsHint) {
    const arr = Array.isArray(__uploadModalTags) ? __uploadModalTags : [];
    const chipsHtml = arr.map((t, i) => {
        const safe = __escapeHtml(t);
        return `<span class="bili-tag-chip" data-i="${i}">${safe}<button type="button" class="bili-tag-remove" data-i="${i}" aria-label="删除">×</button></span>`;
    }).join('');
    tagsChips.innerHTML = chipsHtml;

    if (arr.length >= BILI_TAG_MAX_COUNT) {
        tagsInput.placeholder = `最多 ${BILI_TAG_MAX_COUNT} 个标签`;
    } else {
        tagsInput.placeholder = '回车添加标签，支持空格/逗号';
    }
    tagsHint.textContent = `已添加 ${arr.length}/${BILI_TAG_MAX_COUNT} 个；回车/空格/逗号添加，退格删除最后一个。`;
}

function __initUploadModalOnce() {
    if (__uploadModalInited) return;

    const overlay = document.getElementById('uploadModalOverlay');
    const closeX = document.getElementById('uploadModalCloseX');
    const cancelBtn = document.getElementById('uploadModalCancel');
    const stopBtn = document.getElementById('uploadModalStop');
    const submitBtn = document.getElementById('uploadModalSubmit');
    const fileEl = document.getElementById('uploadModalFile');
    const titleInput = document.getElementById('uploadModalTitle');
    const descInput = document.getElementById('uploadModalDesc');
    const tagsEditor = document.getElementById('uploadModalTagsEditor');
    const tagsChips = document.getElementById('uploadModalTagsChips');
    const tagsInput = document.getElementById('uploadModalTags');
    const tagsHint = document.getElementById('uploadModalTagsHint');
    const hintEl = document.getElementById('uploadModalHint');
    const resultEl = document.getElementById('uploadModalResult');

    if (!overlay || !closeX || !cancelBtn || !stopBtn || !submitBtn || !fileEl || !titleInput || !descInput || !tagsEditor || !tagsChips || !tagsInput || !tagsHint || !hintEl || !resultEl) {
        return;
    }

    __uploadModalEls = { overlay, closeX, cancelBtn, stopBtn, submitBtn, fileEl, titleInput, descInput, tagsEditor, tagsChips, tagsInput, tagsHint, hintEl, resultEl };
    __uploadModalInited = true;

    const renderTags = () => __renderUploadModalTagsUi(tagsChips, tagsInput, tagsHint);

    const persistTags = () => {
        __saveBiliUploadTags((__uploadModalTags || []).join(' '));
    };

    const setTagsFromRaw = (raw) => {
        __resetUploadModalTagsFromRaw(raw);
        renderTags();
        persistTags();
    };

    const addTagsFromRaw = (raw) => {
        const incoming = __splitTags(raw);
        if (!incoming.length) return;

        const current = Array.isArray(__uploadModalTags) ? __uploadModalTags.slice() : [];
        const merged = current.concat(incoming);
        const deduped = __dedupeTags(merged);

        // 单标签长度限制
        for (const t of deduped) {
            if (String(t).length > BILI_TAG_MAX_LEN) {
                showToast(`标签过长（最多${BILI_TAG_MAX_LEN}字）：${t}`);
                return;
            }
        }

        if (deduped.length > BILI_TAG_MAX_COUNT) {
            showToast(`标签最多 ${BILI_TAG_MAX_COUNT} 个`);
            __uploadModalTags = deduped.slice(0, BILI_TAG_MAX_COUNT);
        } else {
            __uploadModalTags = deduped;
        }

        renderTags();
        persistTags();
    };

    const setDisabled = (disabled) => {
        titleInput.disabled = !!disabled;
        descInput.disabled = !!disabled;
        tagsInput.disabled = !!disabled;
        tagsEditor.classList.toggle('disabled', !!disabled);
        submitBtn.disabled = !!disabled;
    };

    const closeModal = async (force = false) => {
        if (!force && __uploadModalUploading) {
            const ok = await showConfirmModal('正在投稿中，确定要关闭投稿窗口吗？', {
                title: '确认关闭',
                okText: '关闭',
                cancelText: '继续投稿',
            });
            if (!ok) return;
        }

        __uploadModalUploading = false;
        __uploadModalFileName = '';
        setDisabled(false);
        __stopUploadPolling();
        hintEl.style.display = 'none';
        overlay.classList.remove('show');
        overlay.setAttribute('aria-hidden', 'true');
    };

    // 标签编辑器交互（B站式 chip 输入）
    tagsEditor.addEventListener('click', () => {
        if (tagsInput.disabled) return;
        tagsInput.focus();
    });

    tagsChips.addEventListener('click', (e) => {
        const btn = e.target?.closest?.('.bili-tag-remove');
        if (!btn) return;
        if (tagsInput.disabled) return;
        const i = Number(btn.getAttribute('data-i'));
        if (!Number.isFinite(i)) return;
        __uploadModalTags = (__uploadModalTags || []).filter((_, idx) => idx !== i);
        renderTags();
        persistTags();
        tagsInput.focus();
    });

    tagsInput.addEventListener('keydown', (e) => {
        if (e.isComposing) return;

        // 回车 / 逗号 / 空格：提交当前输入为标签
        if (e.key === 'Enter' || e.key === ',' || e.key === '，' || e.key === ' ' || e.key === ';' || e.key === '；') {
            const raw = String(tagsInput.value || '').trim();
            if (raw) {
                e.preventDefault();
                addTagsFromRaw(raw);
                tagsInput.value = '';
                return;
            }
        }

        // 退格：空输入时删除最后一个标签
        if (e.key === 'Backspace') {
            const raw = String(tagsInput.value || '');
            if (!raw && (__uploadModalTags || []).length) {
                __uploadModalTags = (__uploadModalTags || []).slice(0, -1);
                renderTags();
                persistTags();
            }
        }
    });

    tagsInput.addEventListener('paste', (e) => {
        if (tagsInput.disabled) return;
        const text = e.clipboardData?.getData?.('text');
        if (!text) return;
        e.preventDefault();
        addTagsFromRaw(text);
        tagsInput.value = '';
    });

    tagsInput.addEventListener('blur', () => {
        const raw = String(tagsInput.value || '').trim();
        if (raw) {
            addTagsFromRaw(raw);
            tagsInput.value = '';
        }
    });

    // 初始化一次渲染
    setTagsFromRaw(__loadBiliUploadTags());

    submitBtn.addEventListener('click', async () => {
        const fileName = String(__uploadModalFileName || '').trim();
        const title = String(titleInput.value || '').trim();
        const desc = String(descInput.value || '').trim();

        // 若输入框里还有未提交的内容，先吞进去
        const pending = String(tagsInput.value || '').trim();
        if (pending) {
            addTagsFromRaw(pending);
            tagsInput.value = '';
        }

        const tags = (__uploadModalTags || []).join(',');
        __saveBiliUploadTags((__uploadModalTags || []).join(' '));

        if (!fileName || !title) {
            showToast('标题不能为空');
            titleInput.focus();
            return;
        }

        // 禁用表单，显示投稿提示 **仅在验证通过后**
        __uploadModalUploading = true;
        setDisabled(true);
        submitBtn.textContent = '投稿中...';
        hintEl.style.display = 'block';
        hintEl.textContent = '投稿过程中可能会因为网络原因出现异常提示，不用担心，投稿正在进行中，可以联系xct258获取帮助...';
        resultEl.innerHTML = '';
        stopBtn.style.display = '';
        stopBtn.disabled = false;

        const userName = (document.getElementById('usernameInput')?.value || '').trim();
        const fullDesc = `投稿用户：${userName}\n\n${desc}\n\n使用投稿工具切片投稿\n\n项目地址：\nhttps://github.com/xct258/web-video-clip`;

        try {
            const res = await fetch('/api/upload_bili', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify({file: fileName, title, description: fullDesc, tags})
            });
            const data = await res.json();

            if (!data.success) {
                __uploadModalUploading = false;
                submitBtn.textContent = '重新投稿';
                setDisabled(false);
                stopBtn.style.display = 'none';
                resultEl.innerHTML = `<pre style="color:red;">投稿失败：\n${data.error || data.message}</pre>`;
                return;
            }

            // 测试模式：直接展示命令预览
            if (data.cmd_preview && !data.upload_token) {
                __uploadModalUploading = false;
                submitBtn.textContent = '投稿成功！';
                submitBtn.disabled = true;
                hintEl.style.display = 'none';
                stopBtn.style.display = 'none';
                resultEl.innerHTML = `<pre style="color:green;">测试模式（未实际上传）：\n${data.cmd_preview}</pre>`;
                return;
            }

            // 真实上传：保存 token 并轮询进度
            __setUploadToken(data.upload_token || '');
            __stopUploadPolling();
            __uploadPollTimer = setTimeout(() => __uploadPollTick(resultEl, submitBtn, stopBtn, hintEl, setDisabled), 200);
        } catch (e) {
            console.error(e);
            __uploadModalUploading = false;
            submitBtn.textContent = '重新投稿';
            setDisabled(false);
            stopBtn.style.display = 'none';
            resultEl.innerHTML = `<pre style="color:red;">投稿异常：\n${e}</pre>`;
        }
    });

    stopBtn.addEventListener('click', async () => {
        if (!__uploadModalUploading) return;
        const ok = await showConfirmModal('确定要停止当前投稿吗？', {
            title: '停止投稿',
            okText: '停止投稿',
            cancelText: '继续投稿',
        });
        if (!ok) return;

        try {
            stopBtn.disabled = true;
            const token = __getUploadToken();
            const res = await fetch('/api/cancel_upload', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ upload_token: token || null }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                showToast(data.detail || data.message || '停止失败');
                stopBtn.disabled = false;
                return;
            }
            showToast('已发送停止请求');
            __stopUploadPolling();
            __uploadPollTimer = setTimeout(() => __uploadPollTick(resultEl, submitBtn, stopBtn, hintEl, setDisabled), 300);
        } catch (e) {
            showToast('停止失败');
            stopBtn.disabled = false;
        }
    });

    cancelBtn.addEventListener('click', () => closeModal(false));
    closeX.addEventListener('click', () => closeModal(false));
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closeModal(false);
    });
    document.addEventListener('keydown', (e) => {
        if (!overlay.classList.contains('show')) return;
        if (e.key === 'Escape') closeModal(false);
    });
}

function __openUploadModal(fileName) {
    __initUploadModalOnce();
    if (!__uploadModalEls) {
        showToast('投稿弹窗初始化失败');
        return;
    }

    const { overlay, submitBtn, stopBtn, fileEl, titleInput, descInput, tagsEditor, tagsChips, tagsInput, tagsHint, hintEl, resultEl } = __uploadModalEls;
    const f = String(fileName || '').trim();
    if (!f) {
        showToast('缺少文件名');
        return;
    }

    __uploadModalFileName = f;
    __uploadModalUploading = false;

    fileEl.textContent = f;
    titleInput.value = '';
    descInput.value = '';

    const savedTags = __loadBiliUploadTags();
    __resetUploadModalTagsFromRaw(savedTags || '');
    tagsInput.value = '';
    __renderUploadModalTagsUi(tagsChips, tagsInput, tagsHint);

    hintEl.style.display = 'none';
    resultEl.innerHTML = '';
    submitBtn.textContent = '投稿到B站';
    submitBtn.disabled = false;
    stopBtn.style.display = 'none';
    stopBtn.disabled = false;

    overlay.classList.add('show');
    overlay.setAttribute('aria-hidden', 'false');
    setTimeout(() => titleInput.focus(), 0);

    // 若存在未完成的投稿（例如页面刷新后），打开弹窗时自动恢复进度展示
    try {
        const token = __getUploadToken();
        if (token) {
            __stopUploadPolling();
            __uploadPollTimer = setTimeout(() => __uploadPollTick(resultEl, submitBtn, stopBtn, hintEl, (d) => {
                titleInput.disabled = !!d;
                descInput.disabled = !!d;
                tagsInput.disabled = !!d;
                tagsEditor.classList.toggle('disabled', !!d);
                submitBtn.disabled = !!d;
            }), 200);
        }
    } catch (e) {
        // ignore
    }
}

function __removeOutputFromHistory(file) {
    const f = String(file || '').trim();
    if (!f) return;
    const list = __loadOutputHistory().filter(x => x.file !== f);
    __saveOutputHistory(list);
}

function __mountUploadForm(containerEl, fileName) {
    __openUploadModal(fileName);
}

function __renderOutputHistory() {
    const host = document.getElementById('historyModalBody');
    if (!host) return;

    const list = __loadOutputHistory();
    if (openHistoryBtn) {
        openHistoryBtn.style.display = list.length ? '' : 'none';
        openHistoryBtn.textContent = list.length ? `历史输出（${list.length}）` : '历史输出';
    }

    if (__historyModalEls && __historyModalEls.title) {
        __historyModalEls.title.textContent = list.length ? `历史输出（${list.length}）` : '历史输出';
    }

    if (!list.length) {
        host.innerHTML = '<div style="text-align:center; padding:18px; opacity:0.75;">暂无历史输出</div>';
        return;
    }

    const itemsHtml = list.map((it) => {
        const file = it.file;
        const safeFile = String(file).replace(/"/g, '&quot;');
        return `
            <div class="glass" style="padding:10px; margin-top:10px;">
                <div style="font-weight:bold; white-space:normal; overflow-wrap:anywhere; word-break:break-word; margin-bottom:8px;">${safeFile}</div>
                <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap; justify-content:flex-end;">
                    <a href="/clips/${encodeURIComponent(file)}" download>
                        <button type="button">下载</button>
                    </a>
                    <button type="button" class="openUploadBtn" data-file="${safeFile}">投稿</button>
                    <button type="button" class="removeOutputBtn" data-file="${safeFile}" style="background:rgba(255, 100, 100, 0.2);">移除</button>
                </div>
            </div>
        `;
    }).join('');

    host.innerHTML = itemsHtml;

    host.querySelectorAll('.removeOutputBtn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const file = String(btn.getAttribute('data-file') || '').trim();
            if (file) {
                try {
                    const res = await fetch('/api/delete_output', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ file }),
                    });
                    await res.json().catch(() => ({}));
                } catch (e) {
                    // ignore
                }
            }

            __removeOutputFromHistory(file);
            __renderOutputHistory();

            // 如果删空了：自动关闭弹窗并隐藏按钮
            if (__loadOutputHistory().length === 0) {
                const overlay = document.getElementById('historyModalOverlay');
                overlay?.classList.remove('show');
                overlay?.setAttribute('aria-hidden', 'true');
            }
        });
    });

    host.querySelectorAll('.openUploadBtn').forEach(btn => {
        btn.addEventListener('click', () => {
            const file = btn.getAttribute('data-file');
            __openUploadModal(file);
        });
    });
}

function __sanitizeVideoTasks(v) {
    if (!Array.isArray(v)) return [];
    const out = [];
    for (const item of v) {
        if (!item || typeof item !== 'object') continue;
        const name = String(item.name || '').trim();
        if (!name) continue;
        const clipsIn = Array.isArray(item.clips) ? item.clips : [];
        const clips = [];
        for (const c of clipsIn) {
            const start = Number(c?.start);
            const end = Number(c?.end);
            if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
            if (start < 0 || end <= start) continue;
            clips.push({ start, end });
        }
        if (clips.length === 0) continue;
        out.push({ name, clips });
    }
    return out;
}

function __loadClipToolState() {
    try {
        const raw = localStorage.getItem(CLIP_TOOL_STATE_KEY);
        if (!raw) return;
        const obj = JSON.parse(raw);

        // 恢复用户名
        const savedUsername = String(obj?.username ?? '').trim();
        if (usernameInput && savedUsername) {
            usernameInput.value = savedUsername;
        }

        // 恢复片段列表
        const savedTasks = __sanitizeVideoTasks(obj?.videoTasks);
        if (savedTasks.length > 0) {
            videoTasks = savedTasks;
            renderNewClipList();
        }
    } catch (e) {
        // ignore
    }
}

function __saveClipToolState() {
    try {
        const state = {
            username: String(usernameInput?.value ?? '').trim(),
            videoTasks: __sanitizeVideoTasks(videoTasks),
            at: Date.now(),
        };
        localStorage.setItem(CLIP_TOOL_STATE_KEY, JSON.stringify(state));
    } catch (e) {
        // ignore
    }
}

if (usernameInput) {
    usernameInput.addEventListener('input', () => __saveClipToolState());
}

function __loadBiliUploadTags() {
    try {
        return String(localStorage.getItem(BILI_UPLOAD_TAGS_KEY) || '').trim();
    } catch (e) {
        return '';
    }
}

function __saveBiliUploadTags(rawTags) {
    try {
        const v = String(rawTags ?? '').trim();
        localStorage.setItem(BILI_UPLOAD_TAGS_KEY, v);
    } catch (e) {
        // ignore
    }
}

// New Elements
const clipWorkshopPanel = document.getElementById('clipWorkshopPanel');
const currentVideoLabel = document.getElementById('currentVideoLabel');
const setStartBtn = document.getElementById('setStartBtn');
const setEndBtn = document.getElementById('setEndBtn');
const newClipStartIn = document.getElementById('newClipStart');
const newClipEndIn = document.getElementById('newClipEnd');
const confirmAddClipBtn = document.getElementById('confirmAddClipBtn');
const newClipListContainer = document.getElementById('newClipListContainer');
const clearAllClipsFn = document.getElementById('clearAllClipsFn');

let tempStart = null;
let tempEnd = null;
const dynamicImageUrl = 'http://192.168.50.4:8181/';

// ------------------ Toast 工具 ------------------
function ensureToastHost() {
    let host = document.getElementById('toastHost');
    if (!host) {
        host = document.createElement('div');
        host.id = 'toastHost';
        host.className = 'toast-host';
        host.setAttribute('aria-live', 'polite');
        host.setAttribute('aria-atomic', 'true');
        document.body.appendChild(host);
    }

    // 强制关键布局样式（但只初始化一次），避免 showToast 高频时反复写 style 导致卡顿
    if (host.dataset.toastInited !== '1') {
        host.dataset.toastInited = '1';
        host.style.position = 'fixed';
        host.style.top = 'auto';
        host.style.bottom = 'calc(108px + env(safe-area-inset-bottom))';
        host.style.left = '0';
        host.style.right = '0';
        host.style.zIndex = '9999';
        host.style.display = 'flex';
        host.style.flexDirection = 'column';
        host.style.alignItems = 'center';
        host.style.gap = '10px';
        host.style.width = '100%';
        host.style.padding = '0 16px';
        host.style.boxSizing = 'border-box';
        host.style.pointerEvents = 'none';
    }
    return host;
}

const __toastConfig = {
    maxToasts: 4,
};

function __toastNormalizeText(v) {
    return (v ?? '').toString().trim();
}

function __toastHideElement(toastEl) {
    if (!toastEl || !toastEl.isConnected) return;
    if (toastEl.classList.contains('toast-hiding')) return;
    toastEl.classList.add('toast-hiding');
    const remove = () => {
        toastEl.removeEventListener('transitionend', onEnd);
        if (toastEl.isConnected) toastEl.remove();
    };
    const onEnd = (e) => {
        if (e.propertyName === 'max-height' || e.propertyName === 'opacity') {
            remove();
        }
    };
    toastEl.addEventListener('transitionend', onEnd);
    setTimeout(remove, 420);
}

function showToast(message, type = 'info', timeout = 2600, title = '') {
    const host = ensureToastHost();

    const msgText = __toastNormalizeText(message);
    const titleText = __toastNormalizeText(title);
    if (!msgText && !titleText) return;

    // 每次调用都创建新 Toast（不合并、不限流）
    const toast = document.createElement('div');
    toast.className = `toast toast-${type} toast-enter`;
    toast.setAttribute('role', 'status');
    toast.style.position = 'relative';
    toast.style.boxSizing = 'border-box';
    toast.style.pointerEvents = 'auto';
    window.__toastZ = (window.__toastZ || 10000) + 1;
    toast.style.zIndex = String(window.__toastZ);

    const body = document.createElement('div');
    body.className = 'toast-body';

    if (titleText) {
        const titleEl = document.createElement('div');
        titleEl.className = 'toast-title';
        titleEl.textContent = titleText;
        body.appendChild(titleEl);
    }

    const msgEl = document.createElement('div');
    msgEl.className = 'toast-msg';
    msgEl.textContent = msgText;
    body.appendChild(msgEl);
    toast.appendChild(body);

    host.appendChild(toast);
    // 说明：仅用 requestAnimationFrame 有时会在首次绘制前移除 class，导致过渡不触发（尤其是 drag/drop 事件后）
    // 这里先强制一次 reflow，再用 setTimeout 推迟到事件循环后，确保动画稳定触发。
    try { toast.getBoundingClientRect(); } catch (e) {}
    setTimeout(() => toast.classList.remove('toast-enter'), 0);

    // 限制堆叠数量：超出时优雅隐藏最旧的
    while (host.children.length > __toastConfig.maxToasts) {
        const oldest = host.firstElementChild;
        if (!oldest) break;
        // 超过 4 条：最旧的立即消失
        oldest.remove();
    }

    if (timeout && timeout > 0) {
        setTimeout(() => __toastHideElement(toast), timeout);
    }
    toast.addEventListener('click', () => __toastHideElement(toast));
}

// ------------------ Modal 工具 ------------------
function showConfirmModal(message, opts = {}) {
    const overlay = document.getElementById('modalOverlay');
    const titleEl = document.getElementById('modalTitle');
    const msgEl = document.getElementById('modalMessage');
    const okBtn = document.getElementById('modalOk');
    const cancelBtn = document.getElementById('modalCancel');
    if (!overlay || !titleEl || !msgEl || !okBtn || !cancelBtn) {
        // 兜底：如果 DOM 不存在，就退回原生 confirm
        return Promise.resolve(confirm(message));
    }

    const title = (opts.title ?? '提示').toString();
    const okText = (opts.okText ?? '确定').toString();
    const cancelText = (opts.cancelText ?? '取消').toString();

    titleEl.textContent = title;
    msgEl.textContent = (message ?? '').toString();
    okBtn.textContent = okText;
    cancelBtn.textContent = cancelText;

    overlay.classList.add('show');
    overlay.setAttribute('aria-hidden', 'false');

    return new Promise((resolve) => {
        const cleanup = () => {
            overlay.classList.remove('show');
            overlay.setAttribute('aria-hidden', 'true');
            okBtn.removeEventListener('click', onOk);
            cancelBtn.removeEventListener('click', onCancel);
            overlay.removeEventListener('click', onOverlay);
            document.removeEventListener('keydown', onKeyDown);
        };

        const onOk = () => { cleanup(); resolve(true); };
        const onCancel = () => { cleanup(); resolve(false); };
        const onOverlay = (e) => {
            // 点击遮罩关闭（等价取消），点击弹窗本体不关闭
            if (e.target === overlay) { cleanup(); resolve(false); }
        };
        const onKeyDown = (e) => {
            if (e.key === 'Escape') { cleanup(); resolve(false); }
            if (e.key === 'Enter') { cleanup(); resolve(true); }
        };

        okBtn.addEventListener('click', onOk);
        cancelBtn.addEventListener('click', onCancel);
        overlay.addEventListener('click', onOverlay);
        document.addEventListener('keydown', onKeyDown);

        // 默认把焦点放到“确定”按钮
        setTimeout(() => okBtn.focus(), 0);
    });
}

// ------------------ 时间转换 ------------------
function formatTime(seconds) {
    let s = Number(seconds);
    if (!Number.isFinite(s) || s < 0) s = 0;
    s = Math.floor(s);
    const hrs = Math.floor(s / 3600);
    const mins = Math.floor((s % 3600) / 60);
    const secs = s % 60;
    return `${String(hrs).padStart(2,'0')}:${String(mins).padStart(2,'0')}:${String(secs).padStart(2,'0')}`;
}

function roundToMs(seconds) {
    let s = Number(seconds);
    if (!Number.isFinite(s)) return 0;
    if (s < 0) s = 0;
    return Math.round(s * 1000) / 1000;
}

function formatTimeMs(seconds) {
    let s = Number(seconds);
    if (!Number.isFinite(s) || s < 0) s = 0;

    const rounded = roundToMs(s);
    let whole = Math.floor(rounded);
    let ms = Math.round((rounded - whole) * 1000);
    if (ms >= 1000) {
        whole += 1;
        ms = 0;
    }

    const hrs = Math.floor(whole / 3600);
    const mins = Math.floor((whole % 3600) / 60);
    const secs = whole % 60;
    return `${String(hrs).padStart(2,'0')}:${String(mins).padStart(2,'0')}:${String(secs).padStart(2,'0')}.${String(ms).padStart(3,'0')}`;
}

function parseTime(str){
    const raw = String(str ?? '').trim();
    if (!raw) return NaN;

    // 兼容：00:01:02.345 / 01:02.345 / 62.345；也接受逗号小数
    const normalized = raw.replace(',', '.');
    const parts = normalized.split(':');
    if (parts.length < 1 || parts.length > 3) return NaN;

    const last = parts[parts.length - 1].trim();
    const secs = Number(last);
    if (!Number.isFinite(secs)) return NaN;

    let total = secs;
    if (parts.length >= 2) {
        const mm = Number(parts[parts.length - 2].trim());
        if (!Number.isFinite(mm)) return NaN;
        total += mm * 60;
    }
    if (parts.length === 3) {
        const hh = Number(parts[0].trim());
        if (!Number.isFinite(hh)) return NaN;
        total += hh * 3600;
    }

    return roundToMs(total);
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

    const introOverlay = document.getElementById('siteIntroOverlay');
    const introOk = document.getElementById('siteIntroOk');
    const introCloseX = document.getElementById('siteIntroCloseX');
    const introNoMore = document.getElementById('siteIntroNoMore');

    const INTRO_KEY = 'bililive_site_intro_dismissed_v1';
    const shouldShowIntro = () => {
        try { return localStorage.getItem(INTRO_KEY) !== '1'; } catch (e) { return true; }
    };
    const closeIntro = () => {
        try {
            if (introNoMore && introNoMore.checked) {
                try { localStorage.setItem(INTRO_KEY, '1'); } catch (e) {}
            }
        } catch (e) {}
        if (introOverlay) {
            introOverlay.classList.remove('show');
            introOverlay.setAttribute('aria-hidden', 'true');
        }
    };
    const showIntro = () => {
        if (!introOverlay) return;
        introOverlay.classList.add('show');
        introOverlay.setAttribute('aria-hidden', 'false');
        try {
            const btn = introOk || introCloseX;
            if (btn && btn.focus) btn.focus();
        } catch (e) {}
    };

    // 内容默认先隐藏：2 秒后与说明弹窗一起出现
    try {
        if (mainContent) {
            mainContent.style.display = 'none';
        }
    } catch (e) {}

    // 绑定一次性事件（重复调用 initPage 也不会造成太多重复；这里做最小防护）
    try {
        if (introOk && !introOk.__bound) {
            introOk.__bound = true;
            introOk.addEventListener('click', closeIntro);
        }
        if (introCloseX && !introCloseX.__bound) {
            introCloseX.__bound = true;
            introCloseX.addEventListener('click', closeIntro);
        }
        if (introOverlay && !introOverlay.__bound) {
            introOverlay.__bound = true;
            introOverlay.addEventListener('click', (e) => {
                if (e.target === introOverlay) closeIntro();
            });
            document.addEventListener('keydown', (e) => {
                if (introOverlay.classList.contains('show') && e.key === 'Escape') {
                    closeIntro();
                }
            });
        }
    } catch (e) {}

    try {
        const themeColor = await fetchThemeColor(dynamicImageUrl);
        setAccentColor(themeColor);
        await preloadImage(dynamicImageUrl);

        // 背景图淡入
        document.documentElement.style.setProperty('--bg-image', `url(${dynamicImageUrl})`);
        document.body.classList.add('bg-ready');
    } catch(e) {
        console.warn("背景图片加载失败，使用默认背景", e);
        // 背景失败也进入“就绪”态（仍保持纯色背景）
        document.body.classList.add('bg-ready');
    } finally {
        // 淡出 loadingOverlay，让用户先看到背景
        try {
            if (loadingOverlay) {
                loadingOverlay.classList.add('hidden');
                setTimeout(() => {
                    try { loadingOverlay.style.display = 'none'; } catch (e) {}
                }, 480);
            }
        } catch (e) {}

        // 1 秒后：同时显示页面内容 + “网站说明”全屏弹窗（如未关闭“不再提示”）
        setTimeout(() => {
            try {
                if (mainContent) {
                    mainContent.style.display = 'block';
                }
            } catch (e) {}

            try {
                if (shouldShowIntro()) showIntro();
            } catch (e) {}
        }, 1000);

        __loadClipToolState();
        try {
            const total = __getTotalClipCountFromVideoTasks();
            const show = Number(total) > 0;
            if (previewMergeBtn) previewMergeBtn.style.display = show ? '' : 'none';
            if (clearAllClipsFn) clearAllClipsFn.style.display = show ? '' : 'none';
        } catch (e) {}
        __renderOutputHistory();
        loadFileTree();
        __startMergeStatusPolling();
    }
}

previewBtn.style.display = 'none'; // 初始化隐藏

// ------------------ 文件树 ------------------
async function loadFileTree() {
    const res = await fetch('/api/tree?path=');
    const tree = await res.json();
    fileTreeDiv.innerHTML = '';

    function formatSize(bytes) {
        if (bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }

    function createTree(nodes) {
        const ul = document.createElement('ul');
        nodes.forEach(node => {
            const li = document.createElement('li');
            const span = document.createElement('span');
            
            // 显示逻辑：优先使用 basename
            const displayName = node.basename || node.name;
            let label = `<div class="file-name">${displayName}</div>`;
            
            if(node.type === 'file') {
                 let metaStr = '';
                 if (node.duration && node.duration > 0) {
                     metaStr = '⏱ ' + formatTime(node.duration);
                 }
                 
                 // 使用 class="file-duration" 便于更新
                 if (metaStr) {
                    label += `<div class="file-duration" style="font-size:0.8em; color:var(--muted-color); margin-top:2px;">${metaStr}</div>`;
                 }
            }
            span.innerHTML = label;

            if(node.type === 'dir') {
                span.className = 'dir';
                li.appendChild(span);
                
                // 包装层，用于 grid 动画
                const wrapper = document.createElement('div');
                wrapper.className = 'tree-node-wrapper';

                // 懒加载：children 只有在展开时才请求并渲染
                li.appendChild(wrapper);

                li.classList.add('collapsed');
                span.addEventListener('click', async e => { 
                    e.stopPropagation();
                    // 手风琴效果：展开时折叠同级其他目录
                    if (li.classList.contains('collapsed')) {
                        Array.from(ul.children).forEach(sibling => {
                            if (sibling !== li) sibling.classList.add('collapsed');
                        });

                        // 展开时（首次）加载子目录树
                        if (!node.childrenLoaded) {
                            const loadingEl = document.createElement('span');
                            loadingEl.textContent = ' ⏳';
                            loadingEl.style.fontSize = '0.6em';
                            loadingEl.className = 'loading-indicator';

                            const oldDisplay = span.style.display;
                            const oldAlign = span.style.alignItems;
                            span.style.display = 'flex';
                            span.style.alignItems = 'center';
                            span.appendChild(loadingEl);

                            try {
                                const res2 = await fetch(`/api/tree?path=${encodeURIComponent(node.path || '')}`);
                                const children = await res2.json();
                                node.children = Array.isArray(children) ? children : [];
                                node.childrenLoaded = true;

                                wrapper.innerHTML = '';
                                if (node.children.length) {
                                    wrapper.appendChild(createTree(node.children));
                                }
                            } catch(err) {
                                console.error('加载目录失败', err);
                            } finally {
                                if (loadingEl) loadingEl.remove();
                                span.style.display = oldDisplay;
                                span.style.alignItems = oldAlign;
                            }
                        }
                        
                        // 展开时加载该目录下所有视频的时长
                        // node.path 存储了相对路径
                        if (node.path && !node.loadedDurations) {
                             // 添加加载中提示
                             let loadingEl = document.createElement('span');
                             loadingEl.textContent = ' ⏳';
                             loadingEl.style.fontSize = '0.6em';
                             loadingEl.className = 'loading-indicator';
                             
                             const oldDisplay = span.style.display;
                             const oldAlign = span.style.alignItems;
                             // 使用 flex 而不是 inline-flex 以保持整行宽度（解决悬停背景不一致问题）
                             span.style.display = 'flex';
                             span.style.alignItems = 'center';

                             span.appendChild(loadingEl);

                             try {
                                 const res = await fetch(`/api/dir_durations?path=${encodeURIComponent(node.path)}`);
                                 const durMap = await res.json();
                                 node.loadedDurations = true;
                                 
                                 // 更新 UI
                                 // wrapper -> ul -> li -> span -> .file-duration
                                 // 直接查找 wrapper 下的所有 span.file 并在 node.children 中查找对应 basename
                                 // wrapper.children[0] 是 ul
                                 if(wrapper.children.length > 0) {
                                     const subUl = wrapper.children[0];
                                     Array.from(subUl.children).forEach((subLi, idx) => {
                                         // 找到对应的数据节点
                                         const subNode = (node.children || [])[idx];
                                         if(subNode && subNode.type === 'file' && durMap[subNode.basename]) {
                                             subNode.duration = durMap[subNode.basename];
                                             const subSpan = subLi.querySelector('span.file');
                                             if(subSpan) {
                                                 let dDiv = subSpan.querySelector('.file-duration');
                                                 if(!dDiv) {
                                                     dDiv = document.createElement('div');
                                                     dDiv.className = 'file-duration';
                                                     dDiv.style.fontSize = '0.8em';
                                                     dDiv.style.color = 'var(--muted-color)';
                                                     dDiv.style.marginTop = '2px';
                                                     subSpan.appendChild(dDiv);
                                                 }
                                                 dDiv.textContent = '⏱ ' + formatTime(subNode.duration);
                                             }
                                         }
                                     });
                                 }
                             } catch(err) {
                                 console.error("加载时长失败", err);
                             } finally {
                                 if(loadingEl) loadingEl.remove();
                                 span.style.display = oldDisplay;
                                 span.style.alignItems = oldAlign;
                             }
                        }
                    }
                    li.classList.toggle('collapsed'); 
                });
            } else {
                span.className = 'file';
                span.addEventListener('click', e => {
                    e.stopPropagation();
                    // 处理高亮
                    document.querySelectorAll('.file-tree span.selected').forEach(el => el.classList.remove('selected'));
                    document.querySelectorAll('.file-tree span.parent-selected').forEach(el => el.classList.remove('parent-selected'));
                    span.classList.add('selected');

                    // 向上高亮父级文件夹
                    let curr = li;
                    while(true) {
                       const ul = curr.parentElement;
                       if(!ul) break;
                       const wrapper = ul.parentElement; // tree-node-wrapper
                       if(!wrapper || !wrapper.classList.contains('tree-node-wrapper')) break;
                       const dirLi = wrapper.parentElement;
                       if(!dirLi) break;
                       
                       const dirSpan = dirLi.querySelector('span.dir');
                       if(dirSpan) dirSpan.classList.add('parent-selected');
                       
                       curr = dirLi;
                    }

                    // 恢复点击文件时设置播放器源，但移除加载时长逻辑（移至文件夹点击）
                    const videoSrc = `/api/video/${encodeURIComponent(node.name)}`;
                    // 如果尚未加载该视频，则设置 src 以加载元数据
                    if (player.getAttribute('src') !== videoSrc) {
                        player.src = videoSrc;
                    }

                    // 隐藏播放器界面，显示预览按钮
                    playerWrapper.style.display = 'none';
                    __setVideoContainerExpanded(false);
                    
                    document.getElementById('previewActionArea').style.display = 'block';
                    document.getElementById('videoPlaceholder').textContent = node.basename || node.name;
                    document.getElementById('videoPlaceholder').style.color = 'var(--accent-color)';
                    previewBtn.style.display = 'inline-block';

                    // 更新当前任务和片段
                    // videoTasks = videoTasks.filter(v => v.clips.length > 0); 
                    // ...
                    currentVideoName = node.name;
                    
                    // UI 重置
                    const videoLabel = document.getElementById('currentVideoLabel');
                    videoLabel.textContent = node.basename || node.name;
                    // 显示信息块
                    document.getElementById('currentVideoInfoBlock').style.display = 'block';

                    tempStart = null;
                    tempEnd = null;
                    updateClipInputs();
                    renderNewClipList();
                });


                li.appendChild(span);
            }
            ul.appendChild(li);
        });
        return ul;
    }
    fileTreeDiv.appendChild(createTree(tree));
}

// ------------------ 渲染片段列表 (重构) ------------------
function renderNewClipList(){
    newClipListContainer.innerHTML = '';

    // 片段列表为空时隐藏“预览合并/清空”按钮
    const __updateClipListActionButtons = () => {
        try {
            const total = __getTotalClipCountFromVideoTasks();
            const show = Number(total) > 0;
            if (previewMergeBtn) previewMergeBtn.style.display = show ? '' : 'none';
            if (clearAllClipsFn) clearAllClipsFn.style.display = show ? '' : 'none';
        } catch (e) {}
    };

    // 若存在合法的顺序覆盖，则先把 videoTasks 同步为该顺序，保证主列表/预览/提交一致
    __applyMergeOrderOverrideToVideoTasks({ render: false, save: false });
    
    // 过滤掉空任务，避免显示空行
    // 但不要永久删除，因为可能用户正在操作
    const validTasks = videoTasks.filter(v => v.clips.length > 0);
    
    if (validTasks.length === 0) {
        newClipListContainer.innerHTML = '<div style="text-align:center; padding:20px; color:var(--muted-color);">暂无片段</div>';
        __updateClipListActionButtons();
        return;
    }

    __updateClipListActionButtons();

    let globalClipIndex = 0; // 全局计数器

    let __mainListDragFromIndex = null;

    const reorderByGlobalIndex = (fromIdx, toIdx) => {
        const from = Number(fromIdx);
        const to = Number(toIdx);
        if (!Number.isFinite(from) || !Number.isFinite(to)) return;
        if (from === to) return;

        const flat = __flattenVideoTasksToClips();
        if (from < 0 || from >= flat.length) return;
        if (to < 0 || to >= flat.length) return;

        const moving = flat.splice(from, 1)[0];
        flat.splice(to, 0, moving);
        __mergeOrderOverride = flat;
        __applyMergeOrderOverrideToVideoTasks({ render: false, save: false });
        renderNewClipList();
        __saveClipToolState();
        showToast('片段顺序已更新');
    };

    // 如果相邻的两个任务是同一个视频，其实在显示上可以合并，
    // 但由于我们的逻辑是 A->B->A，所以不应该合并。这里直接按 videoTasks 顺序渲染即可。

    videoTasks.forEach((video, taskIdx) => {
        if(video.clips.length === 0) return;
        
        const vidGroup = document.createElement('div');
        vidGroup.style.marginBottom = '10px';
        vidGroup.innerHTML = `<div style="font-weight:bold; font-size:13px; color:var(--accent-color); margin-bottom:4px; white-space:normal; overflow-wrap:anywhere; word-break:break-word;">${video.name}</div>`;
        
        video.clips.forEach((c, clipIdx) => {
            globalClipIndex++; // 递增全局序号
            const globalIdx = globalClipIndex - 1;

            const item = document.createElement('div');
            item.className = 'glass';
            item.style.padding = '8px';
            item.style.marginBottom = '6px';
            item.style.display = 'flex';
            item.style.justifyContent = 'space-between';
            item.style.alignItems = 'center';
            item.draggable = true;
            item.dataset.gidx = String(globalIdx);

            // 拖拽排序（全局顺序）
            item.addEventListener('dragstart', (e) => {
                __mainListDragFromIndex = globalIdx;
                try {
                    e.dataTransfer.effectAllowed = 'move';
                    e.dataTransfer.setData('text/plain', String(globalIdx));
                } catch (err) {}
            });
            item.addEventListener('dragover', (e) => {
                e.preventDefault();
                try { e.dataTransfer.dropEffect = 'move'; } catch (err) {}
            });
            item.addEventListener('drop', (e) => {
                e.preventDefault();
                const from = __mainListDragFromIndex;
                __mainListDragFromIndex = null;
                if (from === null || from === undefined) return;
                reorderByGlobalIndex(from, globalIdx);
            });
            item.addEventListener('dragend', () => {
                __mainListDragFromIndex = null;
            });

            const info = document.createElement('div');
            // 使用全局序号
            info.innerHTML = `
                <span style="display:inline-block; width:20px; font-weight:bold; color:var(--accent-color);">${globalClipIndex}.</span>
                <span style="font-family:monospace;">${formatTime(c.start)}</span> 
                <span style="color:var(--muted-color);">➔</span> 
                <span style="font-family:monospace;">${formatTime(c.end)}</span>
            `;
            
            const delBtn = document.createElement('button');
            delBtn.textContent = '×';
            delBtn.style.padding = '2px 8px';
            delBtn.style.marginLeft = '10px';
            delBtn.title = '删除此片段';
            delBtn.style.background = 'rgba(255, 100, 100, 0.2)';
            delBtn.addEventListener('click', () => {
                video.clips.splice(clipIdx, 1);
                // 如果该任务已经没有片段了，是否要删除该任务条目？
                // 为了保持 A->B->A 结构，如果 B 的片段删光了，A和A可能会连在一起。
                // 暂时不自动删除空任务，或者在渲染前 filter。
                // 这里简单起见，仅删除片段。如果任务变空，下次渲染会自动忽略（filter逻辑）。
                // 为了避免空对象堆积，可以做一个清理
                if (video.clips.length === 0) {
                     videoTasks.splice(taskIdx, 1);
                }
                renderNewClipList();
                 __invalidateMergeOrderOverride();
                 __saveClipToolState();
            });

            item.appendChild(info);
            item.appendChild(delBtn);
            vidGroup.appendChild(item);
        });
        newClipListContainer.appendChild(vidGroup);
    });
}

// ------------------ 合并效果预览（按顺序串播） ------------------
let __mergePreviewInited = false;
let __mergePreviewEls = null;
let __mergePreviewQueue = [];
let __mergePreviewIndex = 0;
let __mergePreviewTimeUpdateHandler = null;
let __mergePreviewAdvancing = false;
let __mergePreviewActiveStart = 0;
let __mergePreviewActiveEnd = 0;
let __mergePreviewDragging = false;
let __mergePreviewTotalDuration = 0;
let __mergePreviewTotalProgressBase = 0; // 当前片段开始前的累计时长
let __mergePreviewQueueItemEls = [];
let __mergePreviewDragFromIndex = null;
let __mergePreviewCurrentKey = '';

function __mergePreviewClipKey(c) {
    if (!c) return '';
    return `${String(c.name)}|${Number(c.start)}|${Number(c.end)}`;
}

function __mergePreviewRecomputeSeqAndOffsets() {
    let acc = 0;
    for (let i = 0; i < __mergePreviewQueue.length; i++) {
        const c = __mergePreviewQueue[i];
        if (!c) continue;
        c.seq = i + 1;
        c.offset = acc;
        acc += Math.max(0, Number(c.duration) || 0);
    }
    __mergePreviewTotalDuration = acc;
}

function __mergePreviewApplyOverrideFromQueue() {
    __mergeOrderOverride = (__mergePreviewQueue || []).map(c => ({
        name: String(c?.name || ''),
        start: Number(c?.start),
        end: Number(c?.end)
    })).filter(c => c.name && Number.isFinite(c.start) && Number.isFinite(c.end) && c.end > c.start);
}

function __mergePreviewReorder(fromIndex, toIndex) {
    const from = Number(fromIndex);
    const to = Number(toIndex);
    if (!Number.isFinite(from) || !Number.isFinite(to)) return;
    if (from === to) return;
    if (from < 0 || from >= __mergePreviewQueue.length) return;
    if (to < 0 || to >= __mergePreviewQueue.length) return;

    const moving = __mergePreviewQueue.splice(from, 1)[0];
    __mergePreviewQueue.splice(to, 0, moving);
    __mergePreviewRecomputeSeqAndOffsets();

    // 保持当前播放片段不变（按 key 定位）
    if (__mergePreviewCurrentKey) {
        const newIndex = __mergePreviewQueue.findIndex(c => __mergePreviewClipKey(c) === __mergePreviewCurrentKey);
        if (newIndex >= 0) __mergePreviewIndex = newIndex;
    }

    // 更新总进度相关基准（不重载视频，避免播放中断）
    const active = __mergePreviewQueue[__mergePreviewIndex];
    if (active) {
        __mergePreviewTotalProgressBase = Number(active.offset || 0);
    }

    // 更新 UI（侧栏/进度条/时间）
    __mergePreviewRenderQueueSidebar();
    __mergePreviewUpdateQueueActive();

    if (__mergePreviewEls) {
        const { progress, timeEl } = __mergePreviewEls;
        const totalDur = Math.max(0, __mergePreviewTotalDuration);
        progress.max = String(totalDur);
        const curTotal = __mergePreviewGetTotalPosForCurrent();
        if (!__mergePreviewDragging) {
            progress.value = String(curTotal);
            const percent = totalDur > 0 ? (curTotal / totalDur) * 100 : 0;
            progress.style.background = `linear-gradient(to right, var(--accent-color) 0%, var(--accent-color) ${percent}%, rgba(255,255,255,0.2) ${percent}%, rgba(255,255,255,0.2) 100%)`;
        }
        timeEl.textContent = `${formatTime(curTotal)} / ${formatTime(totalDur)}`;
    }

    __mergePreviewApplyOverrideFromQueue();

    // 同步到主列表（让“待合并片段列表”也立即体现新的顺序）
    __applyMergeOrderOverrideToVideoTasks({ render: true, save: true });
}

function __mergePreviewRenderQueueSidebar() {
    if (!__mergePreviewEls) return;
    const { queueListEl, queueMetaEl, v } = __mergePreviewEls;
    if (!queueListEl) return;

    queueListEl.innerHTML = '';
    __mergePreviewQueueItemEls = [];

    const total = __mergePreviewQueue.length;
    for (let i = 0; i < total; i++) {
        const c = __mergePreviewQueue[i];
        if (!c) continue;

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'merge-preview-queue-item';
        btn.dataset.index = String(i);
        btn.draggable = true;

        const row1 = document.createElement('div');
        row1.className = 'row1';

        const name = document.createElement('div');
        name.className = 'name';
        name.textContent = `${i + 1}. ${c.name}`;

        row1.appendChild(name);

        const row2 = document.createElement('div');
        row2.className = 'row2';
        row2.textContent = `片段：${formatTime(c.start)} - ${formatTime(c.end)}\n时长：${formatTime(c.duration)}`;

        btn.appendChild(row1);
        btn.appendChild(row2);

        btn.addEventListener('click', () => {
            const shouldPlay = v ? !v.paused : true;
            __mergePreviewGoto(i, shouldPlay);
        });

        // 拖拽排序（HTML5 drag&drop）
        btn.addEventListener('dragstart', (e) => {
            __mergePreviewDragFromIndex = i;
            try {
                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('text/plain', String(i));
            } catch (err) {}
        });
        btn.addEventListener('dragover', (e) => {
            e.preventDefault();
            try { e.dataTransfer.dropEffect = 'move'; } catch (err) {}
        });
        btn.addEventListener('drop', (e) => {
            e.preventDefault();
            const from = __mergePreviewDragFromIndex;
            __mergePreviewDragFromIndex = null;
            if (from === null || from === undefined) return;
            __mergePreviewReorder(from, i);
        });
        btn.addEventListener('dragend', () => {
            __mergePreviewDragFromIndex = null;
        });

        queueListEl.appendChild(btn);
        __mergePreviewQueueItemEls.push(btn);
    }

    if (queueMetaEl) {
        queueMetaEl.textContent = `${total} 段 · 总时长 ${formatTime(Math.max(0, __mergePreviewTotalDuration))}`;
    }

    __mergePreviewUpdateQueueActive();
}

function __mergePreviewUpdateQueueActive() {
    if (!__mergePreviewQueueItemEls || __mergePreviewQueueItemEls.length === 0) return;
    for (let i = 0; i < __mergePreviewQueueItemEls.length; i++) {
        const el = __mergePreviewQueueItemEls[i];
        if (!el) continue;
        if (i === __mergePreviewIndex) el.classList.add('active');
        else el.classList.remove('active');
    }

    // 尽量保证当前项可见
    const activeEl = __mergePreviewQueueItemEls[__mergePreviewIndex];
    if (activeEl && typeof activeEl.scrollIntoView === 'function') {
        try { activeEl.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (e) {}
    }
}

function __initMergePreviewOnce() {
    if (__mergePreviewInited) return;

    const overlay = document.getElementById('mergePreviewOverlay');
    const closeX = document.getElementById('mergePreviewCloseX');
    const nowEl = document.getElementById('mergePreviewNow');
    const v = document.getElementById('mergePreviewPlayer');
    const playPauseBtn = document.getElementById('mergePreviewPlayPause');
    const progress = document.getElementById('mergePreviewProgress');
    const timeEl = document.getElementById('mergePreviewTime');
    const queueMetaEl = document.getElementById('mergePreviewQueueMeta');
    const queueListEl = document.getElementById('mergePreviewQueueList');

    if (!overlay || !closeX || !nowEl || !v || !playPauseBtn || !progress || !timeEl) return;

    __mergePreviewEls = { overlay, closeX, nowEl, v, playPauseBtn, progress, timeEl, queueMetaEl, queueListEl };
    __mergePreviewInited = true;

    const closeModal = () => {
        try { v.pause(); } catch (e) {}
        if (__mergePreviewTimeUpdateHandler) {
            v.removeEventListener('timeupdate', __mergePreviewTimeUpdateHandler);
            __mergePreviewTimeUpdateHandler = null;
        }
        __mergePreviewAdvancing = false;
        __mergePreviewDragging = false;
        v.removeAttribute('src');
        v.removeAttribute('data-video-src');
        try { v.srcObject = null; } catch (e) {}
        try { v.load(); } catch (e) {}

        // 重置 UI
        playPauseBtn.textContent = '播放';
        progress.value = '0';
        progress.max = '0';
        timeEl.textContent = '00:00:00 / 00:00:00';

        // 清理队列侧栏
        __mergePreviewQueueItemEls = [];
        if (queueListEl) queueListEl.innerHTML = '';
        if (queueMetaEl) queueMetaEl.textContent = '';

        overlay.classList.remove('show');
        overlay.setAttribute('aria-hidden', 'true');
    };

    closeX.addEventListener('click', closeModal);
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closeModal();
    });
    document.addEventListener('keydown', (e) => {
        if (!overlay.classList.contains('show')) return;
        if (e.key === 'Escape') closeModal();
    });

    // 自定义播放/暂停
    playPauseBtn.addEventListener('click', () => {
        if (v.paused) v.play().catch(() => {});
        else v.pause();
    });

    // 进度条拖拽
    progress.addEventListener('mousedown', () => __mergePreviewDragging = true);
    progress.addEventListener('mouseup', () => __mergePreviewDragging = false);
    progress.addEventListener('touchstart', () => __mergePreviewDragging = true, { passive: true });
    progress.addEventListener('touchend', () => __mergePreviewDragging = false, { passive: true });

    progress.addEventListener('input', () => {
        const val = Number(progress.value);
        const totalDur = Math.max(0, __mergePreviewTotalDuration);
        const clampedTotal = Math.max(0, Math.min(val, totalDur));

        // 将“总时间轴”映射到具体片段
        const mapped = __mergePreviewFindIndexByTotalPos(clampedTotal);
        const idx = mapped.index;
        const inClip = mapped.inClip;
        if (Number.isFinite(idx) && idx !== __mergePreviewIndex) {
            // 跳到对应片段（不自动播放由当前播放状态决定）
            const shouldPlay = !v.paused;
            __mergePreviewGoto(idx, shouldPlay);
        }
        // 定位到片段内时间
        try { v.currentTime = __mergePreviewActiveStart + Math.max(0, inClip); } catch (e) {}

        // 仅更新显示（避免等待 timeupdate）
        timeEl.textContent = `${formatTime(clampedTotal)} / ${formatTime(totalDur)}`;
        const percent = totalDur > 0 ? (clampedTotal / totalDur) * 100 : 0;
        progress.style.background = `linear-gradient(to right, var(--accent-color) 0%, var(--accent-color) ${percent}%, rgba(255,255,255,0.2) ${percent}%, rgba(255,255,255,0.2) 100%)`;
    });

    // 同步按钮状态
    v.addEventListener('play', () => { playPauseBtn.textContent = '暂停'; });
    v.addEventListener('pause', () => { playPauseBtn.textContent = '播放'; });

    // 点击视频也切换播放/暂停（保持轻量交互）
    v.addEventListener('click', () => {
        if (v.paused) v.play().catch(() => {});
        else v.pause();
    });
}

function __buildMergePreviewQueue() {
    const queue = [];
    let seq = 0;
    let acc = 0;
    for (const task of (videoTasks || [])) {
        const name = String(task?.name || '').trim();
        if (!name) continue;
        const clips = Array.isArray(task?.clips) ? task.clips : [];
        for (const c of clips) {
            const start = Number(c?.start);
            const end = Number(c?.end);
            if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
            if (start < 0 || end <= start) continue;
            seq += 1;
            const duration = end - start;
            const offset = acc;
            acc += duration;
            queue.push({ seq, name, start, end, duration, offset });
        }
    }
    __mergePreviewTotalDuration = acc;
    return queue;
}

function __buildMergePreviewQueueFromOverride(overrideList) {
    const queue = [];
    let seq = 0;
    let acc = 0;
    const list = Array.isArray(overrideList) ? overrideList : [];
    for (const c of list) {
        const name = String(c?.name || '').trim();
        const start = Number(c?.start);
        const end = Number(c?.end);
        if (!name) continue;
        if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
        if (start < 0 || end <= start) continue;
        seq += 1;
        const duration = end - start;
        const offset = acc;
        acc += duration;
        queue.push({ seq, name, start, end, duration, offset });
    }
    __mergePreviewTotalDuration = acc;
    return queue;
}

function __mergePreviewGetTotalPosForCurrent() {
    // 当前片段内相对时间 + 该片段之前的累计偏移
    const dur = Math.max(0, __mergePreviewActiveEnd - __mergePreviewActiveStart);
    const v = __mergePreviewEls?.v;
    const curRel = v ? Math.max(0, Math.min(dur, Number(v.currentTime) - __mergePreviewActiveStart)) : 0;
    return Math.max(0, Math.min(__mergePreviewTotalDuration, __mergePreviewTotalProgressBase + curRel));
}

function __mergePreviewFindIndexByTotalPos(totalPos) {
    const pos = Math.max(0, Math.min(totalPos, __mergePreviewTotalDuration));
    // 简单线性查找：队列最多几十条，足够快
    for (let i = 0; i < __mergePreviewQueue.length; i++) {
        const c = __mergePreviewQueue[i];
        if (!c) continue;
        const start = c.offset;
        const end = c.offset + c.duration;
        if (pos >= start && pos < end) {
            return { index: i, inClip: pos - start };
        }
    }
    // pos==总时长：落到最后一段末尾
    if (__mergePreviewQueue.length) {
        const lastIndex = __mergePreviewQueue.length - 1;
        const last = __mergePreviewQueue[lastIndex];
        return { index: lastIndex, inClip: Math.max(0, last.duration) };
    }
    return { index: 0, inClip: 0 };
}

function __mergePreviewUpdateInfo() {
    if (!__mergePreviewEls) return;
    // 方案C：片段信息由右侧“片段队列”承担，这里无需额外显示
}

function __mergePreviewGoto(index, autoPlay) {
    __initMergePreviewOnce();
    if (!__mergePreviewEls) return;
    const { v, nowEl, progress, timeEl } = __mergePreviewEls;

    const total = __mergePreviewQueue.length;
    if (!total) return;
    __mergePreviewIndex = Math.max(0, Math.min(index, total - 1));
    const clip = __mergePreviewQueue[__mergePreviewIndex];
    if (!clip) return;

    __mergePreviewCurrentKey = __mergePreviewClipKey(clip);

    __mergePreviewUpdateInfo();
    nowEl.textContent = `${clip.seq}. ${clip.name}  [${formatTime(clip.start)} ➔ ${formatTime(clip.end)}]`;
    __mergePreviewUpdateQueueActive();

    __mergePreviewActiveStart = clip.start;
    __mergePreviewActiveEnd = clip.end;
    __mergePreviewTotalProgressBase = Number(clip.offset || 0);
    __mergePreviewDragging = false;

    // 初始化进度条（总时长）
    const totalDur = Math.max(0, __mergePreviewTotalDuration);
    progress.max = String(totalDur);
    progress.value = String(__mergePreviewTotalProgressBase);
    timeEl.textContent = `${formatTime(__mergePreviewTotalProgressBase)} / ${formatTime(totalDur)}`;
    const initPercent = totalDur > 0 ? (__mergePreviewTotalProgressBase / totalDur) * 100 : 0;
    progress.style.background = `linear-gradient(to right, var(--accent-color) 0%, var(--accent-color) ${initPercent}%, rgba(255,255,255,0.2) ${initPercent}%, rgba(255,255,255,0.2) 100%)`;

    // 清理旧的 timeupdate
    if (__mergePreviewTimeUpdateHandler) {
        v.removeEventListener('timeupdate', __mergePreviewTimeUpdateHandler);
        __mergePreviewTimeUpdateHandler = null;
    }
    __mergePreviewAdvancing = false;

    const src = `/api/video/${encodeURIComponent(clip.name)}`;
    const hasSrc = !!(v.getAttribute('src') || v.currentSrc);
    const needReload = !hasSrc || (v.getAttribute('data-video-src') !== src);
    v.setAttribute('data-video-src', src);
    if (needReload) {
        v.src = src;
    }

    const seekAndPlay = () => {
        try { v.currentTime = Math.max(0, clip.start); } catch (e) {}
        if (autoPlay) {
            v.play().catch(() => {});
        }
    };

    // readyState>=1 表示 metadata 已可用（duration/seek）
    if (!needReload && v.readyState >= 1) {
        seekAndPlay();
    } else {
        v.addEventListener('loadedmetadata', seekAndPlay, { once: true });
        try { v.load(); } catch (e) {}
    }

    __mergePreviewTimeUpdateHandler = () => {
        if (__mergePreviewAdvancing) return;

        // 更新自定义进度/时间（总时间轴）
        const totalDur = Math.max(0, __mergePreviewTotalDuration);
        const dur = Math.max(0, __mergePreviewActiveEnd - __mergePreviewActiveStart);
        const curRel = Math.max(0, Math.min(dur, Number(v.currentTime) - __mergePreviewActiveStart));
        const curTotal = Math.max(0, Math.min(totalDur, __mergePreviewTotalProgressBase + curRel));
        if (!__mergePreviewDragging) {
            progress.value = String(curTotal);
            const percent = totalDur > 0 ? (curTotal / totalDur) * 100 : 0;
            progress.style.background = `linear-gradient(to right, var(--accent-color) 0%, var(--accent-color) ${percent}%, rgba(255,255,255,0.2) ${percent}%, rgba(255,255,255,0.2) 100%)`;
        }
        timeEl.textContent = `${formatTime(curTotal)} / ${formatTime(totalDur)}`;

        // 留一点余量，避免浮点误差卡住
        if (v.currentTime >= (clip.end - 0.05)) {
            __mergePreviewAdvancing = true;
            try { v.pause(); } catch (e) {}
            setTimeout(() => {
                __mergePreviewAdvancing = false;
                if (__mergePreviewIndex < __mergePreviewQueue.length - 1) {
                    __mergePreviewGoto(__mergePreviewIndex + 1, true);
                } else {
                    showToast('合并预览播放完成');
                }
            }, 80);
        }
    };
    v.addEventListener('timeupdate', __mergePreviewTimeUpdateHandler);
}

function __openMergePreviewModal() {
    __initMergePreviewOnce();
    if (!__mergePreviewEls) {
        showToast('合并预览弹窗初始化失败');
        return;
    }

    // 避免两个播放器同时发声
    try { player.pause(); } catch (e) {}

    const totalClipsNow = __getTotalClipCountFromVideoTasks();
    if (Array.isArray(__mergeOrderOverride) && __mergeOrderOverride.length === totalClipsNow && totalClipsNow > 0) {
        __mergePreviewQueue = __buildMergePreviewQueueFromOverride(__mergeOrderOverride);
    } else {
        __mergePreviewQueue = __buildMergePreviewQueue();
    }
    if (__mergePreviewQueue.length === 0) {
        showToast('暂无片段，无法预览');
        return;
    }

    // 打开预览时同步一次覆盖顺序（确保后续合并提交一致）
    __mergePreviewApplyOverrideFromQueue();

    // 渲染队列侧栏（若 DOM 存在）
    __mergePreviewRenderQueueSidebar();

    __mergePreviewIndex = 0;
    __mergePreviewEls.overlay.classList.add('show');
    __mergePreviewEls.overlay.setAttribute('aria-hidden', 'false');
    __mergePreviewGoto(0, false);
}

if (previewMergeBtn) {
    previewMergeBtn.addEventListener('click', () => __openMergePreviewModal());
}

// ------------------ 新版交互逻辑 ------------------

// 更新输入框显示
function updateClipInputs() {
    newClipStartIn.value = tempStart !== null ? formatTime(tempStart) : '';
    newClipEndIn.value = tempEnd !== null ? formatTime(tempEnd) : '';
    
    if (tempStart !== null) newClipStartIn.style.borderColor = 'var(--accent-color)';
    else newClipStartIn.style.borderColor = 'var(--border-color)';
    
    if (tempEnd !== null) newClipEndIn.style.borderColor = 'var(--accent-color)';
    else newClipEndIn.style.borderColor = 'var(--border-color)';

    // 同步更新控制栏常驻显示
    if(ctrlStartDisp) ctrlStartDisp.textContent = tempStart !== null ? formatTime(tempStart) : '--:--:--';
    if(ctrlEndDisp) ctrlEndDisp.textContent = tempEnd !== null ? formatTime(tempEnd) : '--:--:--';
}

// 允许手动输入时间
function handleManualTimeInput(e, isStart) {
    const val = e.target.value.trim();
    if (!val) {
        if (isStart) tempStart = null;
        else tempEnd = null;
        e.target.style.borderColor = 'var(--border-color)';
        return;
    }

    const sec = parseTime(val);
    if (Number.isFinite(sec)) {
        const rounded = roundToMs(sec);
        if (isStart) tempStart = rounded;
        else tempEnd = rounded;
        e.target.style.borderColor = 'var(--accent-color)';
    } else {
        // 解析失败
        e.target.style.borderColor = 'red';
    }
}

newClipStartIn.addEventListener('input', (e) => handleManualTimeInput(e, true));
newClipEndIn.addEventListener('input', (e) => handleManualTimeInput(e, false));

setStartBtn.addEventListener('click', () => {
    // 未选择视频 -> 提示选择；已选择但未预览 -> 提示先预览后设点
    if (!currentVideoName) {
        showToast('请先选择视频');
        return;
    }

    // 要求用户必须先打开预览（主播放器可见并已加载 src，或合并预览弹窗已打开并加载视频）
    const hasActivePreview = (() => {
        try {
            if (player) {
                const psrc = player.getAttribute('src') || player.currentSrc || '';
                const vis = (playerWrapper && window.getComputedStyle(playerWrapper).display !== 'none');
                if (psrc && vis) return true;
            }
        } catch (e) {}
        try {
            if (typeof __mergePreviewEls !== 'undefined' && __mergePreviewEls && __mergePreviewEls.overlay && __mergePreviewEls.overlay.classList.contains('show')) {
                const mv = __mergePreviewEls.v;
                const msrc = mv && (mv.getAttribute('data-video-src') || mv.currentSrc);
                if (msrc) return true;
            }
        } catch (e) {}
        return false;
    })();

    if (!hasActivePreview) {
        showToast('已选择视频，请先点击预览以加载视频，再设定起点');
        return;
    }

    tempStart = roundToMs(player.currentTime);
    updateClipInputs();
    showToast(`起点已设定: ${formatTime(tempStart)}`);
});

setEndBtn.addEventListener('click', () => {
    if (!currentVideoName) {
        showToast('请先选择视频');
        return;
    }

    const hasActivePreview = (() => {
        try {
            if (player) {
                const psrc = player.getAttribute('src') || player.currentSrc || '';
                const vis = (playerWrapper && window.getComputedStyle(playerWrapper).display !== 'none');
                if (psrc && vis) return true;
            }
        } catch (e) {}
        try {
            if (typeof __mergePreviewEls !== 'undefined' && __mergePreviewEls && __mergePreviewEls.overlay && __mergePreviewEls.overlay.classList.contains('show')) {
                const mv = __mergePreviewEls.v;
                const msrc = mv && (mv.getAttribute('data-video-src') || mv.currentSrc);
                if (msrc) return true;
            }
        } catch (e) {}
        return false;
    })();

    if (!hasActivePreview) {
        showToast('已选择视频，请先点击预览以加载视频，再设定终点');
        return;
    }

    tempEnd = roundToMs(player.currentTime);
    updateClipInputs();
    showToast(`终点已设定: ${formatTime(tempEnd)}`);
});

// 绑定快捷按钮到同样的功能
quickSetStartBtn.addEventListener('click', () => setStartBtn.click());
quickSetEndBtn.addEventListener('click', () => setEndBtn.click());
quickAddClipBtnCtrl.addEventListener('click', () => confirmAddClipBtn.click());

let rangeStopHandler = null;
let clipPauseHandler = null;

function resetQuickPlayBtnState() {
    quickPlayClipBtn.innerHTML = 'Play [P]';
    quickPlayClipBtn.title = '预览选中片段';
}

quickPlayClipBtn.addEventListener('click', () => {
    triggerBtnFeedback(quickPlayClipBtn);
    if (!currentVideoName) {
        showToast('请先选择视频');
        return;
    }
    if (tempStart === null || tempEnd === null) {
        showToast('请先设定起点和终点');
        return;
    }
    if (tempEnd <= tempStart) {
        showToast('终点必须大于起点');
        return;
    }

    // 清除旧的监听器
    if (rangeStopHandler) {
        player.removeEventListener('timeupdate', rangeStopHandler);
        rangeStopHandler = null;
    }
    if (clipPauseHandler) {
        player.removeEventListener('pause', clipPauseHandler);
        clipPauseHandler = null;
    }

    player.currentTime = tempStart;
    player.play();

    // 更新按钮状态
    quickPlayClipBtn.innerHTML = 'Playing...';
    quickPlayClipBtn.title = '正在播放片段...';

    rangeStopHandler = () => {
        if (player.currentTime >= tempEnd) {
            player.pause(); // 会触发 pause 事件
            // 清理timeupdate
            player.removeEventListener('timeupdate', rangeStopHandler);
            rangeStopHandler = null;
        }
    };
    player.addEventListener('timeupdate', rangeStopHandler);

    // 监听暂停事件（无论是自动结束还是用户手动暂停），重置按钮
    clipPauseHandler = () => {
        resetQuickPlayBtnState();
        player.removeEventListener('pause', clipPauseHandler);
        clipPauseHandler = null;
        
        // 如果是手动暂停，也清理 timeupdate
        if (rangeStopHandler) {
            player.removeEventListener('timeupdate', rangeStopHandler);
            rangeStopHandler = null;
        }
    };
    player.addEventListener('pause', clipPauseHandler);
});

confirmAddClipBtn.addEventListener('click', () => {
    if (!currentVideoName) {
        showToast('请先选择视频');
        return;
    }
    if (tempStart === null || tempEnd === null) {
        showToast('请先设定起点和终点');
        return;
    }
    if (tempEnd <= tempStart) {
        showToast('终点时间必须大于起点时间');
        return;
    }

    // 添加到当前视频的任务中
    // 逻辑调整：优先检查最后一个任务是否也是当前视频。如果是，则追加；如果不是，创建新任务。
    let task = null;
    if (videoTasks.length > 0) {
        const last = videoTasks[videoTasks.length - 1];
        if (last.name === currentVideoName) {
            task = last;
        }
    }
    
    if (!task) {
        task = {name: currentVideoName, clips: []};
        videoTasks.push(task);
    }
    
    if(task) {
        const newStart = roundToMs(tempStart);
        const newEnd = roundToMs(tempEnd);

        // 跨任务组检查重叠
        // 遍历所有任务，找出所有属于当前视频的任务组
        // 然后检查所有这些任务组中的片段是否与新片段重叠
        let hasOverlap = false;

        for(const vTask of videoTasks) {
            if(vTask.name === currentVideoName) {
                for(const clip of vTask.clips) {
                    if (newStart < clip.end && newEnd > clip.start) {
                        showToast(`片段重叠：当前选择与已存在片段 [${formatTime(clip.start)} - ${formatTime(clip.end)}] 冲突`);
                        hasOverlap = true;
                        break;
                    }
                }
            }
            if(hasOverlap) break;
        }

        if(hasOverlap) return;

        task.clips.push({ start: newStart, end: newEnd });
        const newClip = task.clips[task.clips.length - 1];
        // 重置临时变量
        tempStart = null;
        tempEnd = null;
        updateClipInputs();
        renderNewClipList();
        __invalidateMergeOrderOverride();
        __saveClipToolState();
        showToast('片段已添加');
    }
});

clearAllClipsFn.addEventListener('click', async () => {
    const ok = await showConfirmModal('确定清空所有待合并的片段吗？', {
        title: '清空确认',
        okText: '清空',
        cancelText: '取消'
    });
    if(ok) {
        // 直接清空任务列表：避免残留空任务导致仍可提交
        videoTasks = [];
        __invalidateMergeOrderOverride();
        renderNewClipList();
        __saveClipToolState();
        showToast('已清空');
    }
});


// ------------------ 提交任务 ------------------
document.getElementById('mergeAllBtn').addEventListener('click', async ()=> {
    const totalClipsNow = __getTotalClipCountFromVideoTasks();

    let videosToSend = null;

    // 若存在“预览拖拽排序”的覆盖顺序，则按覆盖顺序提交（预览顺序=合并顺序）
    if (Array.isArray(__mergeOrderOverride) && __mergeOrderOverride.length === totalClipsNow && totalClipsNow > 0) {
        videosToSend = __mergeOrderOverride.map(c => ({
            name: String(c?.name || '').trim(),
            clips: [{ start: Number(c?.start), end: Number(c?.end) }]
        })).filter(v => v.name && Array.isArray(v.clips) && v.clips.length === 1 && Number.isFinite(v.clips[0].start) && Number.isFinite(v.clips[0].end) && v.clips[0].end > v.clips[0].start);
    } else {
        // 过滤掉 clips 为空的任务组（清空/删除后可能残留空壳）
        videosToSend = (videoTasks || []).filter(v => Array.isArray(v?.clips) && v.clips.length > 0);
    }

    if(!videosToSend || videosToSend.length===0){
        showToast('请至少添加一个视频片段');
        return;
    }

    // 提交前确认：提示成功后将自动清空片段列表
    try {
        const ok = await showConfirmModal(
            '开始切片合并后，若切片合并成功将自动清空当前片段列表（避免重复提交）。是否继续？',
            { title: '开始切片合并确认', okText: '开始合并', cancelText: '取消' }
        );
        if (!ok) return;
    } catch (e) {
        // 若弹窗不可用则降级：不阻断提交流程
    }

    // 总时长限制：只允许合并 20 分钟以内
    let totalSeconds = 0;
    for (const v of videosToSend) {
        const clips = Array.isArray(v?.clips) ? v.clips : [];
        for (const c of clips) {
            const s = Number(c?.start);
            const e = Number(c?.end);
            if (!Number.isFinite(s) || !Number.isFinite(e) || e <= s) continue;
            totalSeconds += (e - s);
        }
    }
    if (totalSeconds > 20 * 60) {
        showToast(`最终合并总时长不能超过20分钟（当前 ${formatTime(totalSeconds)}）`);
        return;
    }

    const username = document.getElementById('usernameInput').value.trim();
    if(!username) {
        showToast('请输入用户名');
        return;
    }

    const clipTitle = (document.getElementById('clipTitleInput')?.value || '').trim();

    mergeStatus.textContent='提交任务...';

    // POST 时把 username 也传给后端
    const res = await fetch('/api/slice_merge_all', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({videos:videosToSend, username: username, out_basename: clipTitle || null})
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        mergeStatus.textContent = '提交失败';
        showToast(data.detail || data.message || '提交失败');
        __startMergeStatusPolling({ forceFast: true });
        return;
    }

    // 保存本次任务 token（只有持有者才能看到进度/取消任务）
    __setMergeToken(data.merge_token || '');

    // 新任务开始：允许历史自动回填（仅用于补写“刚完成但未及时写入历史”的记录）
    __setOutputHistoryAutofillSuppressed(false);
    __pushLocalMergeHistory({
        job_id: data.job_id,
        username,
        out_file: data.out_file,
    });
    __startMergeStatusPolling({ forceFast: true });
    pollJob(data.job_id);
});

async function pollJob(jobId){
    const res = await fetch(`/api/job/${jobId}`);
    const job = await res.json();
    if(job.status === 'done'){
        const outPath = String(job.out_path || '').trim();
        const safeOutPath = __escapeHtml(outPath || '');
        const downloadHref = outPath ? `/clips/${encodeURIComponent(outPath)}` : '';
        mergeResult.innerHTML = `
            <div class="merge-result-card">
                <div class="merge-result-header">
                    <div class="merge-result-title">切片合并完成</div>
                    <div class="merge-result-sub">切片成功后已自动清空当前片段列表</div>
                </div>

                <div class="merge-result-file">
                    <div class="merge-result-label">输出文件</div>
                    <div class="merge-result-path" title="${safeOutPath}">${safeOutPath || '-'}</div>
                </div>

                <div class="merge-result-actions">
                    ${downloadHref ? `<a class="merge-result-btn" href="${downloadHref}" download role="button">下载切片</a>` : ''}
                    ${downloadHref ? `<button id="copyClipLinkBtn" type="button">复制下载链接</button>` : ''}
                    <button id="submitVideoBtn" type="button">烦心事远离账号投稿</button>
                </div>

                <div id="videoFormContainer" class="merge-result-form"></div>
            </div>
        `;

        __addOutputToHistory(job.out_path);
        __renderOutputHistory();

        // 合并成功：自动清空当前片段列表（避免重复提交同一批片段）
        try {
            videoTasks = [];
            __invalidateMergeOrderOverride();
            tempStart = null;
            tempEnd = null;
            updateClipInputs();
            renderNewClipList();
            __saveClipToolState();
            showToast('切片合并完成：已自动清空当前片段列表');
        } catch (e) {
            // ignore
        }

        const submitBtn = document.getElementById('submitVideoBtn');
        submitBtn.addEventListener('click', () => {
            __openUploadModal(job.out_path);
        });

        const copyBtn = document.getElementById('copyClipLinkBtn');
        if (copyBtn) {
            copyBtn.addEventListener('click', async () => {
                try {
                    const url = downloadHref ? (new URL(downloadHref, window.location.href)).toString() : '';
                    if (!url) {
                        showToast('无可复制的链接');
                        return;
                    }
                    if (navigator.clipboard && navigator.clipboard.writeText) {
                        await navigator.clipboard.writeText(url);
                        showToast('已复制下载链接');
                        return;
                    }
                    // 兜底：旧浏览器
                    const ta = document.createElement('textarea');
                    ta.value = url;
                    ta.style.position = 'fixed';
                    ta.style.left = '-9999px';
                    document.body.appendChild(ta);
                    ta.select();
                    document.execCommand('copy');
                    document.body.removeChild(ta);
                    showToast('已复制下载链接');
                } catch (e) {
                    showToast('复制失败');
                }
            });
        }
    } else if(job.status === 'error'){
        mergeResult.innerHTML = `
            <div class="merge-result-card">
                <div class="merge-result-header">
                    <div class="merge-result-title">切片合并失败</div>
                    <div class="merge-result-sub">请检查错误信息后重试</div>
                </div>
                <pre class="merge-result-error">${__escapeHtml(job.error || 'Unknown error')}</pre>
            </div>
        `;
    } else if(job.status === 'cancelled'){
        // 用户主动取消：不在页面上留下“已取消/用户取消合并”等内容
        mergeResult.textContent = '';
        showToast('取消成功');
    } else {
        setTimeout(() => pollJob(jobId), 800);
    }
}

// ------------------ 初始化页面 ------------------
initPage();

// 点击“预览”按钮
previewBtn.addEventListener('click', () => {
    if(currentVideoName){
        player.src = `/api/video/${encodeURIComponent(currentVideoName)}`;
        __restoreProgressForVideo = currentVideoName;
        playerWrapper.style.display = 'flex'; // 显示包裹层
        __setVideoContainerExpanded(true);
        document.getElementById('previewActionArea').style.display = 'none';
        
        // player.play(); // 取消自动播放

        // 核心修改：点击预览后，显示切片工坊面板
        // clipWorkshopPanel.style.display = 'block'; // 已经始终显示，无需再次设置
        updateClipInputs();
        renderNewClipList();

    } else {
        showToast('请先选择视频');
    }
});

// 快退5秒
rewindBtn.addEventListener('click', () => {
    player.currentTime = Math.max(player.currentTime - 5, 0);
});

// 快退1秒
rewind1Btn.addEventListener('click', () => {
    player.currentTime = Math.max(player.currentTime - 1, 0);
});

// 快进1秒
forward1Btn.addEventListener('click', () => {
    player.currentTime = Math.min(player.currentTime + 1, player.duration);
});

// 快进5秒
forwardBtn.addEventListener('click', () => {
    player.currentTime = Math.min(player.currentTime + 5, player.duration);
});

// 倍速控制
if(speedSelect) {
    speedSelect.addEventListener('change', () => {
        player.playbackRate = parseFloat(speedSelect.value);
    });
}

// 全屏控制
if(fullscreenBtn) {
    fullscreenBtn.addEventListener('click', () => {
        fullscreenBtn.blur(); // 移除焦点，防止后续按空格误触发按钮点击或残留焦点样式
        if (!document.fullscreenElement && !document.webkitFullscreenElement) {
            // 进入全屏
            if (playerWrapper.requestFullscreen) {
                playerWrapper.requestFullscreen();
            } else if (playerWrapper.webkitRequestFullscreen) {
                playerWrapper.webkitRequestFullscreen();
            }
        } else {
            // 退出全屏
            if (document.exitFullscreen) {
                document.exitFullscreen();
            } else if (document.webkitExitFullscreen) {
                document.webkitExitFullscreen();
            }
        }
    });
}

// ------------------ 自定义播放控制 ------------------
function togglePlay() {
    if (player.paused) {
        player.play();
    } else {
        player.pause();
    }
}

playPauseBtn.addEventListener('click', togglePlay);

// ------------------ 移动端全屏时自动锁定横屏 ------------------
function tryLockLandscape() {
    try {
        if (screen && screen.orientation && screen.orientation.lock) {
            // 返回 Promise，可能会被拒绝，捕获后不影响主流程
            screen.orientation.lock('landscape').catch(e => console.debug('orientation.lock failed', e));
        } else if (screen && screen.lockOrientation) {
            // 旧 API
            try { screen.lockOrientation('landscape'); } catch(e){ console.debug('lockOrientation failed', e); }
        }
    } catch (e) {
        console.debug('tryLockLandscape error', e);
    }
}

function tryUnlockOrientation() {
    try {
        if (screen && screen.orientation && screen.orientation.unlock) {
            try { screen.orientation.unlock(); } catch(e){ console.debug('orientation.unlock failed', e); }
        } else if (screen && screen.unlockOrientation) {
            try { screen.unlockOrientation(); } catch(e){ console.debug('unlockOrientation failed', e); }
        }
    } catch (e) {
        console.debug('tryUnlockOrientation error', e);
    }
}

// ------------------ 全屏时 Toast 可见性修复 ------------------
// Fullscreen API：进入全屏后，通常只有“全屏元素及其子元素”会被绘制。
// toastHost 默认在 body 下，会导致全屏时看不到消息推送。
let __toastHostHomeParent = null;
let __toastHostHomeNextSibling = null;
let __toastHostHomeBottom = null;
let __toastHostFullscreenResizeHandler = null;

function __computeFullscreenToastBottomPx() {
    // 控制栏在全屏时 bottom: 30px；Toast 需要抬高到控制栏上方
    const baseBottom = 30;
    const gap = 14;
    let controlsH = 0;
    try {
        const rect = videoControlsContainer?.getBoundingClientRect();
        controlsH = Math.max(0, Math.ceil(Number(rect?.height || 0)));
    } catch (e) {
        controlsH = 0;
    }
    // 最小给一个合理值，避免 rect 取不到导致 Toast 贴底
    return Math.max(120, baseBottom + controlsH + gap);
}

function __ensureToastHostInFullscreen(fullscreenEl) {
    const host = ensureToastHost();
    if (!host || !fullscreenEl) return;

    if (!__toastHostHomeParent) {
        __toastHostHomeParent = host.parentElement;
        __toastHostHomeNextSibling = host.nextSibling;
        __toastHostHomeBottom = host.style.bottom || '';
    }

    if (host.parentElement !== fullscreenEl) {
        fullscreenEl.appendChild(host);
    }

    // 控制栏在全屏时使用了很大的 z-index，Toast 需要更高/同级才能显示在上层
    host.style.zIndex = '2147483646';

    // 抬高 Toast，避免遮挡底部控制栏
    const bottomPx = __computeFullscreenToastBottomPx();
    host.style.bottom = `calc(${bottomPx}px + env(safe-area-inset-bottom))`;

    // 全屏时窗口尺寸变化（含缩放/旋转）需要更新 bottom
    if (!__toastHostFullscreenResizeHandler) {
        __toastHostFullscreenResizeHandler = () => {
            const el = document.fullscreenElement || document.webkitFullscreenElement;
            if (el !== playerWrapper) return;
            const px = __computeFullscreenToastBottomPx();
            host.style.bottom = `calc(${px}px + env(safe-area-inset-bottom))`;
        };
        window.addEventListener('resize', __toastHostFullscreenResizeHandler, { passive: true });
    }
}

function __restoreToastHostAfterFullscreen() {
    const host = document.getElementById('toastHost');
    if (!host || !__toastHostHomeParent) return;

    if (host.parentElement !== __toastHostHomeParent) {
        __toastHostHomeParent.insertBefore(host, __toastHostHomeNextSibling);
    }
    host.style.zIndex = '9999';

    // 恢复默认 bottom
    if (__toastHostHomeBottom !== null) {
        host.style.bottom = __toastHostHomeBottom;
    }

    if (__toastHostFullscreenResizeHandler) {
        window.removeEventListener('resize', __toastHostFullscreenResizeHandler);
        __toastHostFullscreenResizeHandler = null;
    }
}

function onFullScreenChange() {
    const el = document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement || document.msFullscreenElement;
    if (el) {
        // 已进入全屏，尝试锁定横屏（仅在支持的环境有效）
        tryLockLandscape();

        // 进入全屏：把 toastHost 移到全屏元素内部，确保消息推送可见
        if (el === playerWrapper) {
            __ensureToastHostInFullscreen(el);
        }
    } else {
        // 退出全屏，尝试解锁
        tryUnlockOrientation();

        // 退出全屏：恢复 toastHost 原位置
        __restoreToastHostAfterFullscreen();
    }
}

document.addEventListener('fullscreenchange', onFullScreenChange);
document.addEventListener('webkitfullscreenchange', onFullScreenChange);
document.addEventListener('mozfullscreenchange', onFullScreenChange);
document.addEventListener('MSFullscreenChange', onFullScreenChange);

// 单击播放/暂停（增加延迟以区分双击全屏）
let clickTimer = null;

function __isPlayerWrapperFullscreen() {
    const el = document.fullscreenElement || document.webkitFullscreenElement;
    return !!el && (el === playerWrapper);
}

function __getControlsOpacity() {
    if (!videoControlsContainer) return 0;
    const style = window.getComputedStyle(videoControlsContainer);
    const opacity = parseFloat(style.opacity || '0');
    return Number.isFinite(opacity) ? opacity : 0;
}

// 记录“控制栏刚刚处于完全显示/被交互过”的时间点：用于在 click 发生时判断
let __controlsLastFullVisibleAt = 0;
let __lastPointerDownAt = 0;
let __lastPointerDownControlsOpacity = 0;

function __markControlsFullVisible() {
    __controlsLastFullVisibleAt = Date.now();
}

if (videoControlsContainer) {
    // hover / touch / 点击等都认为控制栏处于“完全显示”上下文
    videoControlsContainer.addEventListener('mouseenter', __markControlsFullVisible);
    videoControlsContainer.addEventListener('mousemove', __markControlsFullVisible);
    videoControlsContainer.addEventListener('pointerdown', __markControlsFullVisible, true);
    videoControlsContainer.addEventListener('touchstart', __markControlsFullVisible, { passive: true });
}

if (playerWrapper) {
    // 在 pointerdown 捕获阶段记录当时的控制栏 opacity（比 click 时更接近“点击前状态”）
    playerWrapper.addEventListener('pointerdown', () => {
        if (!__isPlayerWrapperFullscreen()) return;
        __lastPointerDownAt = Date.now();
        __lastPointerDownControlsOpacity = __getControlsOpacity();
    }, true);

    // 关键逻辑：全屏时如果控制栏“完全显示”，点击控制栏外只半隐藏，不触发播放/暂停
    playerWrapper.addEventListener('click', (e) => {
        if (!__isPlayerWrapperFullscreen()) return;

        // 点击在控制栏内部：不拦截，保持按钮/进度条等原功能
        if (e.target && e.target.closest && e.target.closest('.video-controls')) return;

        const now = Date.now();
        const pointerDownFresh = __lastPointerDownAt && (now - __lastPointerDownAt) <= 500;
        const wasFullyByPointerDown = pointerDownFresh && (__lastPointerDownControlsOpacity >= 0.95);
        // 关键：仅以“点击当下”是否完全显示为准，避免鼠标移出后仍需点击两次
        const controlsFullyShown = (__getControlsOpacity() >= 0.95) || wasFullyByPointerDown;

        if (!controlsFullyShown) return;

        // 只“半隐藏”：强制回到 0.4（解决某些移动端 hover 粘住/状态不及时的问题）
        playerWrapper.classList.add('controls-force-dim');

        // 阻止继续冒泡到 video 的 click（避免播放/暂停被触发）
        e.preventDefault();
        e.stopPropagation();
    }, true);

    // 用户移动/触摸后，解除强制半透明，让 hover 规则接管
    const __clearForceDim = () => {
        if (!__isPlayerWrapperFullscreen()) return;
        playerWrapper.classList.remove('controls-force-dim');
    };
    playerWrapper.addEventListener('mousemove', __clearForceDim, { passive: true });
    playerWrapper.addEventListener('touchstart', __clearForceDim, { passive: true });
    if (videoControlsContainer) {
        videoControlsContainer.addEventListener('mouseenter', __clearForceDim);
    }
}

player.addEventListener('click', () => {
    if(clickTimer) {
        clearTimeout(clickTimer);
        clickTimer = null;
    } else {
        clickTimer = setTimeout(() => {
            togglePlay();
            clickTimer = null;
        }, 220); // 延迟220ms，留出双击时间
    }
});

player.addEventListener('play', () => {
    playPauseBtn.textContent = '暂停';
});

player.addEventListener('pause', () => {
    playPauseBtn.textContent = '播放';
    __saveProgress(currentVideoName, player.currentTime, player.duration);
});

player.addEventListener('ended', () => {
    const saved = __loadProgressSingle();
    if (saved && saved.name === currentVideoName) {
        __clearProgressSingle();
    }
});

// ------------------ 进度条背景更新 ------------------
function updateProgress(val, max) {
    const percent = (val / max) * 100 || 0;
    progressBar.style.background = `linear-gradient(to right, var(--accent-color) 0%, var(--accent-color) ${percent}%, rgba(255,255,255,0.2) ${percent}%, rgba(255,255,255,0.2) 100%)`;
}

player.addEventListener('timeupdate', () => {
    const cur = formatTime(player.currentTime);
    const dur = formatTime(player.duration || 0);
    timeDisplay.textContent = `${cur} / ${dur}`;
    if (!isDragging) {
        progressBar.value = player.currentTime;
        updateProgress(player.currentTime, player.duration);
    }

    // 节流保存播放进度
    const now = Date.now();
    if (now - __lastProgressSaveAt >= VIDEO_PROGRESS_SAVE_INTERVAL_MS) {
        __lastProgressSaveAt = now;
        __saveProgress(currentVideoName, player.currentTime, player.duration);
    }
});

player.addEventListener('loadedmetadata', () => {
    const cur = formatTime(player.currentTime);
    const dur = formatTime(player.duration || 0);
    timeDisplay.textContent = `${cur} / ${dur}`;
    progressBar.max = player.duration;
    updateProgress(player.currentTime, player.duration);

    __maybeRestoreProgress();
});

let isDragging = false;
progressBar.addEventListener('mousedown', () => isDragging = true);
progressBar.addEventListener('mouseup', () => isDragging = false);
progressBar.addEventListener('touchstart', () => isDragging = true);
progressBar.addEventListener('touchend', () => isDragging = false);

progressBar.addEventListener('input', () => {
    player.currentTime = progressBar.value;
    const cur = formatTime(player.currentTime);
    const dur = formatTime(player.duration || 0);
    timeDisplay.textContent = `${cur} / ${dur}`;
    updateProgress(progressBar.value, player.duration);
});

progressBar.addEventListener('change', () => {
    __saveProgress(currentVideoName, player.currentTime, player.duration);
});

// 页面隐藏/关闭时也保存一次（移动端/切后台更可靠）
window.addEventListener('pagehide', () => {
    __saveProgress(currentVideoName, player.currentTime, player.duration);
    __saveClipToolState();
});

document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
        __saveProgress(currentVideoName, player.currentTime, player.duration);
        __saveClipToolState();
    }
});

// ------------------ 播放器双击全屏 ------------------
player.addEventListener('dblclick', () => {
    if (!document.fullscreenElement && !document.webkitFullscreenElement) {
        // 进入全屏
        if (playerWrapper.requestFullscreen) {
            playerWrapper.requestFullscreen();
        } else if (playerWrapper.webkitRequestFullscreen) {
            playerWrapper.webkitRequestFullscreen();
        }
    } else {
        // 退出全屏
        if (document.exitFullscreen) {
            document.exitFullscreen();
        } else if (document.webkitExitFullscreen) {
            document.webkitExitFullscreen();
        }
    }
});

// ------------------ 快捷键支持 ------------------
function triggerBtnFeedback(btn) {
    if(!btn) return;
    btn.classList.add('click-anim');
    setTimeout(() => btn.classList.remove('click-anim'), 200);
}

document.addEventListener('keydown', (e) => {
    // 忽略输入/可编辑区域和表单控件 keydown，防止空格键意外触发全局快捷键或双触发按钮
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT' || e.target.tagName === 'BUTTON' || e.target.isContentEditable) return;
    if (e.ctrlKey || e.altKey || e.metaKey) return;

    const key = e.key.toLowerCase();
    
    // [Q] 设定起点
    if (key === 'q') {
        setStartBtn.click();
        triggerBtnFeedback(setStartBtn);
        triggerBtnFeedback(quickSetStartBtn);
    } 
    // [W] 设定终点
    else if (key === 'w') {
        setEndBtn.click();
        triggerBtnFeedback(setEndBtn);
        triggerBtnFeedback(quickSetEndBtn);
    } 
    // [C] 添加片段 (Control Add Shortcut)
    else if (key === 'c' || key === 'enter') { 
        // 只有当有起止点时才有效，click handler 会处理校验
        // 如果是 Enter 键，且焦点不在按钮上(已由input排除)，则作为添加键
        e.preventDefault();
        confirmAddClipBtn.click();
        triggerBtnFeedback(confirmAddClipBtn);
        triggerBtnFeedback(quickAddClipBtnCtrl);
    }
    // [P] 预览选中片段
    else if (key === 'p') {
        quickPlayClipBtn.click();
        triggerBtnFeedback(quickPlayClipBtn);
    }
    // [ [ ] 回到片段起点（不播放）
    else if (key === '[') {
        e.preventDefault();
        if (!currentVideoName) {
            try { showToast('请先选择视频'); } catch (err) {}
            return;
        }
        if (tempStart === null) {
            try { showToast('未设定片段起点'); } catch (err) {}
            return;
        }
        try {
            player.pause();
            player.currentTime = Number(tempStart);
            try { showToast('已回到片段起点 — ' + formatTime(tempStart)); } catch (err) {}
        } catch (err) {
            // ignore
        }
    }
    // [ArrowLeft] 后退1秒
    else if (e.key === 'ArrowLeft') {
        e.preventDefault();
        player.pause();
        rewind1Btn.click();
        triggerBtnFeedback(rewind1Btn);
        try { showToast('后退 1 秒'); } catch (err) {}
    }
    // [ArrowRight] 前进1秒
    else if (e.key === 'ArrowRight') {
        e.preventDefault();
        player.pause();
        forward1Btn.click();
        triggerBtnFeedback(forward1Btn);
        try { showToast('前进 1 秒'); } catch (err) {}
    }
    // [Space] 播放/暂停
    else if (key === ' ') {
        e.preventDefault();
        togglePlay();
        triggerBtnFeedback(playPauseBtn);
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
    --bg-image: url('http://192.168.50.4:8181/');
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

html {
    height: 100%;
    overflow: hidden;
}

body {
    background: var(--bg-color);
    position: relative;
    height: 100vh;
    height: 100dvh; /* 动态视口高度，解决移动端地址栏遮挡 */
    overflow: hidden;
    line-height: 1.6;
}

/* 背景图单独一层，便于淡入 */
body::before {
    content: "";
    position: fixed;
    inset: 0;
    background-image: var(--bg-image);
    background-repeat: no-repeat;
    background-position: center;
    background-size: cover;
    opacity: 0;
    transition: opacity 900ms ease;
    z-index: -2;
}

/* 轻微暗角遮罩，增强毛玻璃对比度 */
body::after {
    content: "";
    position: fixed;
    inset: 0;
    background: radial-gradient(ellipse at top, rgba(0,0,0,0.15) 0%, rgba(0,0,0,0.45) 100%);
    opacity: 1;
    z-index: -1;
}

body.bg-ready::before {
    opacity: 1;
}

.scroll-wrapper {
    width: 100%;
    height: 100%;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: rgba(255, 255, 255, 0.5) transparent;
}

.container {
    max-width: 980px;
    margin: 0 auto;
    padding: 20px 16px;
}

/* ------------------ 滚动条样式 ------------------ */
::-webkit-scrollbar {
    width: 10px;
    height: 10px;
    background-color: transparent;
}

::-webkit-scrollbar-thumb {
    background-color: rgba(255, 255, 255, 0.5);
    border-radius: 5px;
}

::-webkit-scrollbar-thumb:hover {
    background-color: rgba(255, 255, 255, 0.8);
}

::-webkit-scrollbar-corner {
    background-color: transparent;
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
.panel, .video-placeholder-container, pre {
    /* 应用统一毛玻璃样式 */
    background: var(--panel-bg);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-sm);
    /* 移除 backdrop-filter 因为它与视频渲染冲突 */
    /* backdrop-filter: blur(8px); */
    /* -webkit-backdrop-filter: blur(8px); */
}

/* ------------------ 输入区域 ------------------ */
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

input[type="range"] {
    outline: none;
    border: none;
}
input[type="range"]:focus {
    outline: none;
    box-shadow: none;
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
}

button:hover {
    background: var(--btn-hover);
    transform: translateY(-1px);
    box-shadow: 0 4px 10px rgba(0,0,0,0.3);
}

.click-anim, button:active {
    background: rgba(79, 158, 255, 0.3);
    transform: translateY(0);
    box-shadow: 0 2px 6px rgba(0,0,0,0.2);
}

/* ------------------ 视频播放器 ------------------ */
.video-placeholder-container {
    width: 100%;
    /* 自适应高度 */
    display: flex;
    flex-direction: column; 
    align-items: center;
    justify-content: center;
    font-size: 18px;
    font-weight: bold;
    position: relative;
    /* 移除 overflow: hidden，防止内容被切断 */
    padding: 8px 0;
    box-sizing: border-box;
}

.video-placeholder-container.video-container-compact {
    /* 没有预览视频时：区域尽量紧凑，只够显示文字/按钮 */
    padding: 8px 0;
}

.video-placeholder-container.video-container-expanded {
    /* 预览后：区域随播放器自然撑开 */
    padding: 10px 0;
}

.video-placeholder-text {
    text-align: center;
    padding: 10px; /* 减小内部 padding */
    color: var(--muted-color);
}

#player {
    max-width: 100%;
    max-height: 60vh;
    box-shadow: var(--shadow-md);
    width: auto;
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
    margin: 0;
    overflow: hidden; /* 配合 grid wrapper */
    min-height: 0;
}

/* 包装层负责动画 */
.tree-node-wrapper {
    display: grid;
    grid-template-rows: 1fr;
    opacity: 1;
    transition: grid-template-rows 0.3s ease-out, opacity 0.3s ease-out;
}

/* 折叠状态 */
.file-tree li.collapsed > .tree-node-wrapper {
    grid-template-rows: 0fr;
    opacity: 0;
    margin: 0; /* 确保无外边距干扰 */
}

.file-tree li {
    cursor: pointer;
    margin: 2px 0; /* 减小间距 */
    padding: 0; /* 移除内边距，交给 span */
    border-radius: var(--radius-sm);
}

/* 移除 LI 的 Hover，改为 SPAN 处理，防止层级叠加 */
.file-tree li:hover {
    background: transparent;
}

.file-tree li span {
    display: block;
    padding: 5px 8px; /* 内边距移到这里 */
    border-radius: var(--radius-sm);
    transition: background 0.2s;
    margin-right: 30px; /* 新增：右侧留白 */

}

/* 普通项 Hover */
.file-tree li span:hover {
    background: rgba(79,158,255,0.1);
}

.file-tree li span.dir::before { content: "📁 "; float: left; margin-right: 5px;}
.file-tree li span.file::before { content: "🎬 "; float: left; margin-right: 5px; }

/* 文件选中高亮 */
.file-tree span.file.selected {
    background: rgba(79, 158, 255, 0.25) !important; /* 增加不透明度 */
    border-radius: 4px;
    position: relative;
    display: block; 
}

/* 选中项的 Hover - 保持高亮并微调，避免冲突 */
.file-tree span.file.selected:hover {
    background: rgba(79, 158, 255, 0.35) !important;
}

/* 选中项的文字颜色 */
.file-tree span.file.selected .file-name {
    color: var(--accent-color);
    font-weight: bold;
    text-shadow: 0 0 8px rgba(79,158,255,0.4);
}

/* 父级文件夹高亮 */
.file-tree span.dir.parent-selected {
    color: var(--accent-color);
    font-weight: bold;
    text-shadow: 0 0 5px rgba(79,158,255,0.3);
    background: rgba(79, 158, 255, 0.1); /* 增加背景色 */
    border-radius: 4px;
}

/* "正在编辑" 徽章 */
.file-tree span.file.selected::after {
    content: "正在编辑";
    position: absolute;
    right: 8px;
    bottom: 5px; /* 让它在底部，与文件大小行对齐 */
    font-size: 11px;
    color: #fff;
    background: var(--accent-color);
    padding: 2px 6px;
    border-radius: 4px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.3);
    pointer-events: none;
    line-height: 1.2;
}

/* ------------------ 新版切片列表 ------------------ */
.clip-list-scroll {
    max-height: 400px;
    overflow-y: auto;
    
    /* 自定义滚动条样式 */
    scrollbar-width: thin;
    scrollbar-color: var(--accent-color) rgba(0,0,0,0.2);
}

.clip-list-scroll::-webkit-scrollbar {
    width: 6px;
}
.clip-list-scroll::-webkit-scrollbar-track {
    background: rgba(0,0,0,0.1);
}
.clip-list-scroll::-webkit-scrollbar-thumb {
    background-color: var(--accent-color);
    border-radius: 3px;
}

.action-btn {
    padding: 12px;
    background: rgba(255,255,255,0.1);
    border: 1px solid rgba(255,255,255,0.1);
    transition: all 0.2s;
}
.action-btn:hover {
    background: rgba(255,255,255,0.2);
}
.action-btn:active {
    transform: scale(0.98);
}

/* ------------------ 合并进度条（mergeStatus） ------------------ */
.merge-status-card {
    padding: 12px 14px;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 12px;
    background: rgba(0,0,0,0.18);
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.merge-status-top {
    display: flex;
    justify-content: space-between;
    gap: 14px;
    align-items: flex-start;
    flex-wrap: wrap;
}

.merge-status-title {
    font-weight: 800;
    display: flex;
    gap: 10px;
    align-items: baseline;
    flex-wrap: wrap;
}

.merge-status-percent {
    font-variant-numeric: tabular-nums;
    opacity: 0.95;
}

.merge-status-actions {
    display: flex;
    column-gap: 12px;
    row-gap: 8px;
    align-items: center;
    justify-content: flex-end;
    flex-wrap: wrap;
}

.merge-status-user {
    opacity: 0.85;
    font-size: 12px;
}

.merge-status-cancel {
    padding: 6px 12px;
    font-size: 12px;
    background: rgba(255, 100, 100, 0.22);
    border: 1px solid rgba(255, 120, 120, 0.25);
}

.merge-status-cancel:hover {
    background: rgba(255, 100, 100, 0.30);
}

.merge-status-bar {
    height: 10px;
    border-radius: 999px;
    background: rgba(255,255,255,0.12);
    overflow: hidden;
}

/* ------------------ 合并结果（mergeResult） ------------------ */
.merge-result-card {
    margin-top: 10px;
    padding: 14px;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 12px;
    background: rgba(0,0,0,0.18);
    box-shadow: var(--shadow-sm);
}

.merge-result-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 10px;
    flex-wrap: wrap;
}

.merge-result-title {
    font-weight: 800;
}

.merge-result-sub {
    font-size: 12px;
    opacity: 0.85;
}

.merge-result-file {
    margin-top: 10px;
    padding: 10px 12px;
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 12px;
}

.merge-result-label {
    font-size: 12px;
    opacity: 0.85;
    margin-bottom: 6px;
}

.merge-result-path {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    font-variant-numeric: tabular-nums;
    word-break: break-all;
    line-height: 1.45;
}

.merge-result-actions {
    margin-top: 12px;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 10px;
}

.merge-result-actions button,
.merge-result-actions .merge-result-btn {
    width: 100%;
}

.merge-result-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    text-decoration: none;
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
}

.merge-result-btn:hover {
    background: var(--btn-hover);
    transform: translateY(-1px);
    box-shadow: 0 4px 10px rgba(0,0,0,0.3);
}

.merge-result-btn:active {
    background: rgba(79, 158, 255, 0.3);
    transform: translateY(0);
    box-shadow: 0 2px 6px rgba(0,0,0,0.2);
}

.merge-result-form {
    margin-top: 12px;
}

.merge-result-error {
    margin-top: 10px;
    padding: 10px 12px;
    border-radius: 12px;
    background: rgba(255, 100, 100, 0.10);
    border: 1px solid rgba(255, 120, 120, 0.18);
    white-space: pre-wrap;
    word-break: break-word;
}

.merge-status-bar-fill {
    height: 100%;
    width: 0;
    background: var(--accent-color);
    transition: width 0.25s ease;
}

.merge-status-meta {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    opacity: 0.9;
    font-size: 12px;
    flex-wrap: wrap;
}

.merge-status-meta-left {
    min-width: 0;
    overflow-wrap: anywhere;
    word-break: break-word;
}

.merge-status-meta-right {
    white-space: nowrap;
    opacity: 0.85;
}

@media (max-width: 520px) {
    .merge-status-actions {
        width: 100%;
        justify-content: space-between;
    }
    .merge-status-bar {
        height: 9px;
    }
}

/* ------------------ 输出结果 ------------------ */
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
}

@media (max-width: 480px) {
    h1 { font-size: 22px; }
    #player { max-height: 35vh; }
    input, button, select { font-size: 14px; padding: 8px; }
}

/* 移动端移除文件夹悬停高亮 */
@media (hover: none) {
    .file-tree span.dir:hover {
        background-color: transparent !important;
    }
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
    transition: opacity 420ms ease;
}

.loading-overlay.hidden {
    opacity: 0;
    pointer-events: none;
}

/* ------------------ 网站说明全屏弹窗（启动 2 秒后显示） ------------------ */
.site-intro-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.55);
    display: none;
    align-items: center;
    justify-content: center;
    padding: 18px;
    z-index: 10000;
}

.site-intro-overlay.show {
    display: flex;
}

.site-intro-modal {
    width: min(860px, calc(100vw - 24px));
    max-height: min(82vh, 720px);
    background: rgba(18, 22, 40, 0.86);
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 14px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.55);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.site-intro-header {
    padding: 12px 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    border-bottom: 1px solid rgba(255,255,255,0.10);
}

.site-intro-title {
    font-weight: 900;
    letter-spacing: 0.5px;
}

.site-intro-body {
    padding: 12px 14px;
    overflow: auto;
}

.site-intro-actions {
    padding: 12px 14px;
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    border-top: 1px solid rgba(255,255,255,0.10);
}

.site-intro-nomore {
    margin-top: 10px;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    opacity: 0.9;
}

.site-intro-nomore input {
    transform: translateY(0.5px);
}

@media (prefers-reduced-motion: reduce) {
    body::before {
        transition: none !important;
    }
    .loading-overlay {
        transition: none !important;
    }
}

/* ------------------ 视频控制栏优化 ------------------ */
.video-controls {
    margin-top: 10px;
    background: rgba(0, 0, 0, 0.3);
    padding: 10px 15px;
    border-radius: var(--radius-sm);
    width: 100%;
    box-shadow: inset 0 1px 1px rgba(255,255,255,0.05); 
}

.progress-row {
    margin-bottom: 8px;
    display: flex;
    align-items: center;
}

.custom-range {
    width: 100%;
    cursor: pointer;
    height: 8px; /* 增加高度 */
    background: rgba(255,255,255,0.2);
    border-radius: 4px;
    -webkit-appearance: none;
    transition: all 0.2s;
}
.custom-range:hover {
    height: 10px; /* 悬停时变高 */
    background: rgba(255,255,255,0.3);
}

.custom-range::-webkit-slider-thumb {
    -webkit-appearance: none;
    height: 16px; /* 增加滑块尺寸 */
    width: 16px;
    border-radius: 50%;
    background: #fff; /* 改为白色增强对比 */
    cursor: pointer;
    margin-top: -4px;
    box-shadow: 0 0 6px rgba(0,0,0,0.6);
    transition: transform 0.1s, background 0.2s;
    border: 2px solid var(--accent-color); /* 增加强调色边框 */
}
.custom-range::-webkit-slider-thumb:hover {
    transform: scale(1.3);
    background: var(--accent-color);
}

.custom-range::-webkit-slider-runnable-track {
    height: 6px;
    border-radius: 3px;
    /* webkit track background is handled by input background */
}

.buttons-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap; /* 自动换行 */
    gap: 10px;
}

.ctrl-group {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap; /* 内容过多时组内也可换行 */
}

.ctrl-group.center {
    justify-content: center;
    flex: 1; /* allow it to take available space */
    min-width: 120px; /* 最小宽度，防止过度挤压 */
}

.icon-btn {
    padding: 6px 14px;
    min-width: 40px;
    text-align: center;
    background: rgba(255,255,255,0.1);
    border-radius: var(--radius-sm);
    border: 1px solid rgba(255,255,255,0.05);
}

.icon-btn:hover {
    background: var(--btn-hover);
}

.icon-btn.small {
    padding: 4px 10px;
    font-size: 12px;
    background: rgba(255,255,255,0.08);
}
.icon-btn.small:hover {
    background: rgba(255,255,255,0.15);
}

/* 鼠标点击按钮时不要显示聚焦虚线（但使用 :focus-visible 保留键盘可见焦点以兼顾无障碍） */
.icon-btn:focus {
    outline: none;
    box-shadow: none;
}
.icon-btn:focus-visible {
    outline: 2px solid rgba(79,158,255,0.9);
    box-shadow: 0 0 0 4px rgba(79,158,255,0.15);
}

.time-text {
    font-family: 'Consolas', monospace;
    font-size: 13px;
    color: var(--muted-color);
    letter-spacing: 0.5px;
}

.speed-select {
    padding: 4px 8px;
    font-size: 12px;
    background: rgba(0,0,0,0.4);
    border: 1px solid rgba(255,255,255,0.15);
    color: var(--fg-color);
    border-radius: 4px;
    cursor: pointer;
    outline: none;
}
.speed-select:hover {
    border-color: var(--accent-color);
}
.speed-select option {
    background: #222;
    color: #fff;
}

/* ------------------ 全屏模式样式 (沉浸式 & 悬浮控制) ------------------ */
#playerWrapper:fullscreen {
    background: #000;
    display: flex !important;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    width: 100vw !important;
    height: 100vh !important;
    max-width: none !important;
    padding: 0;
}

#playerWrapper:fullscreen video {
    width: 100% !important;
    height: 100% !important;
    max-width: none !important;
    max-height: none !important;
    object-fit: contain;
    margin: 0;
}

#playerWrapper:fullscreen .video-controls {
    position: absolute;
    bottom: 30px;
    left: 50%;
    transform: translateX(-50%);
    width: 90%;
    max-width: 800px;
    background: rgba(20, 20, 30, 0.85);
    backdrop-filter: blur(10px);
    z-index: 2147483647;
    border: 1px solid rgba(255,255,255,0.1);
    opacity: 0;
    transition: opacity 0.3s;
}

/* 全屏时，鼠标在屏幕上但未在控制栏上，控制栏半透明 */
#playerWrapper:fullscreen:hover .video-controls {
    opacity: 0.4;
}

/* 全屏时，鼠标悬停在控制栏上，控制栏完全不透明 */
#playerWrapper:fullscreen .video-controls:hover {
    opacity: 1;
}

/* JS 强制“半隐藏”控制栏（避免 click 后仍然保持 1 的极端情况） */
#playerWrapper:fullscreen.controls-force-dim .video-controls {
    opacity: 0.4 !important;
}

/* 兼容 Webkit 全屏 */
#playerWrapper::-webkit-full-screen {
    background: #000;
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
}
#playerWrapper::-webkit-full-screen video {
    width: 100%;
    height: 100%; 
    object-fit: contain;
}

/* 移动端全屏适配 */
@media (max-width: 600px) {
    #playerWrapper:fullscreen .video-controls {
        width: 96%;
        bottom: 10px;
        padding: 8px;
    }
}


""", encoding="utf-8")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
