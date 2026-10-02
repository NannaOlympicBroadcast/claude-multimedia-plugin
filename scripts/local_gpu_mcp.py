#!/usr/bin/env python3
"""MCP server (stdio) exposing the user's local GPU to Claude.

Why an MCP server: in Claude Cowork, shell commands run inside an isolated VM without the
host GPU, while local plugin MCP servers run natively on the user's machine. This server is
started by the plugin (.mcp.json) on the host and runs local_gpu.py there.

Long GPU jobs (video upscaling can take hours) run as detached background processes; tools
return a job_id immediately and `gpu_job_status` reports progress / results. Paths are paths
on the HOST machine (e.g. inside the folder the user connected to Cowork).

Only stdlib is used so the server starts even before any engine is installed.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCAL_GPU = HERE / "local_gpu.py"
DATA = Path(os.environ.get("MM_DATA_DIR") or (Path.home() / ".cache" / "claude-multimedia")).expanduser()
JOBS = DATA / "jobs"
SERVER_INFO = {"name": "multimedia-studio-local-gpu", "version": "0.2.0"}
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

ALLOW_CPU = {"type": "boolean", "description": "Use CPU when no GPU is found. Only set true after the user explicitly agreed (much slower)."}
GPU = {"type": "integer", "description": "GPU index (default: first hardware GPU)"}

TOOLS = [
    {
        "name": "gpu_detect",
        "description": "Report the host's GPUs (NVIDIA/CUDA, Apple Silicon, Vulkan), installed local engines and recommended backends. Call this first in local mode.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "gpu_install",
        "description": "Install a local engine on the host (binaries/models are downloaded with aria2c). Runs as a background job.",
        "inputSchema": {"type": "object", "properties": {
            "component": {"type": "string", "enum": ["realesrgan", "rife", "whisper", "demucs"]},
            "model": {"type": "string", "description": "whisper: large-v3-turbo | large-v3 | distil-large-v3.5 | HF repo; demucs: htdemucs | htdemucs_ft"},
            "url": {"type": "string", "description": "realesrgan/rife: explicit release zip URL"},
        }, "required": ["component"]},
    },
    {
        "name": "whisper_transcribe",
        "description": "Transcribe audio/video on the local GPU with Whisper (faster-whisper on NVIDIA CUDA, mlx-whisper on Apple Silicon). Writes .srt/.txt/.json. Background job.",
        "inputSchema": {"type": "object", "properties": {
            "input": {"type": "string", "description": "absolute path on the host"},
            "output_prefix": {"type": "string"},
            "model": {"type": "string", "default": "large-v3-turbo"},
            "language": {"type": "string", "description": "zh / en / ja ... (omit = auto)"},
            "task": {"type": "string", "enum": ["transcribe", "translate"]},
            "word_timestamps": {"type": "boolean"},
            "initial_prompt": {"type": "string"},
            "gpu": GPU, "allow_cpu": ALLOW_CPU,
        }, "required": ["input"]},
    },
    {
        "name": "upscale_image",
        "description": "Super-resolve an image or a folder of images with Real-ESRGAN (ncnn-vulkan) on the local GPU. Background job.",
        "inputSchema": {"type": "object", "properties": {
            "input": {"type": "string"}, "output": {"type": "string"},
            "model": {"type": "string", "description": "realesrgan-x4plus (photo, default) | realesrgan-x4plus-anime | realesr-animevideov3"},
            "scale": {"type": "integer", "enum": [2, 3, 4]},
            "format": {"type": "string", "enum": ["png", "jpg", "webp"]},
            "tile": {"type": "integer"}, "gpu": GPU, "allow_cpu": ALLOW_CPU,
        }, "required": ["input"]},
    },
    {
        "name": "upscale_video",
        "description": "Super-resolve a video frame-by-frame with Real-ESRGAN on the local GPU (chunked, resumable, audio kept, HW encoder when available). Background job; can take long.",
        "inputSchema": {"type": "object", "properties": {
            "input": {"type": "string"}, "output": {"type": "string"},
            "model": {"type": "string", "description": "realesr-animevideov3 (fast, anime/cartoon) | realesrgan-x4plus (real footage, slow)"},
            "scale": {"type": "integer", "enum": [2, 3, 4]},
            "encoder": {"type": "string", "description": "auto | hevc_nvenc | h264_videotoolbox | libx264 ..."},
            "chunk_sec": {"type": "number"}, "tile": {"type": "integer"}, "gpu": GPU, "allow_cpu": ALLOW_CPU,
        }, "required": ["input"]},
    },
    {
        "name": "interpolate_video",
        "description": "Frame interpolation with RIFE (ncnn-vulkan) on the local GPU, e.g. 30->60 fps. Background job.",
        "inputSchema": {"type": "object", "properties": {
            "input": {"type": "string"}, "output": {"type": "string"},
            "factor": {"type": "number", "description": "default 2"}, "fps": {"type": "string", "description": "target fps, overrides factor"},
            "model": {"type": "string", "description": "default rife-v4.6"}, "uhd": {"type": "boolean"},
            "gpu": GPU, "allow_cpu": ALLOW_CPU,
        }, "required": ["input"]},
    },
    {
        "name": "separate_stems",
        "description": "Separate music into stems (vocals/drums/bass/other) with Demucs on the local GPU (CUDA/MPS). Background job.",
        "inputSchema": {"type": "object", "properties": {
            "input": {"type": "string"}, "output_dir": {"type": "string"},
            "model": {"type": "string", "description": "htdemucs (default) | htdemucs_ft | htdemucs_6s"},
            "two_stems": {"type": "string", "description": "e.g. vocals"}, "mp3": {"type": "boolean"},
            "allow_cpu": ALLOW_CPU,
        }, "required": ["input"]},
    },
    {
        "name": "gpu_job_status",
        "description": "Status / progress log / result of a background GPU job. wait_seconds (max 55) blocks until the job finishes or the time is up.",
        "inputSchema": {"type": "object", "properties": {
            "job_id": {"type": "string"}, "wait_seconds": {"type": "integer"},
        }, "required": ["job_id"]},
    },
    {
        "name": "gpu_job_list",
        "description": "List recent background GPU jobs.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "gpu_job_cancel",
        "description": "Cancel a running background GPU job.",
        "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    },
]


# --------------------------------------------------------------------------- argument mapping (whitelist only)
def _flags(args: dict, mapping: dict[str, str], bools: dict[str, str]) -> list[str]:
    out: list[str] = []
    for key, flag in mapping.items():
        v = args.get(key)
        if v is not None and v != "":
            out += [flag, str(v)]
    for key, flag in bools.items():
        if args.get(key):
            out.append(flag)
    return out


def _host_path(p: str) -> str:
    path = Path(p).expanduser()
    if not path.is_absolute():
        raise ValueError(f"请传宿主机上的绝对路径: {p}")
    if not path.exists():
        raise ValueError(f"宿主机上不存在: {path}")
    return str(path)


def build_cli(tool: str, args: dict) -> list[str]:
    common = {"gpu": "--gpu"}
    cpu = {"allow_cpu": "--allow-cpu"}
    if tool == "gpu_install":
        return ["install", args["component"], *_flags(args, {"model": "--model", "url": "--url"}, {})]
    if tool == "whisper_transcribe":
        return ["whisper", _host_path(args["input"]), *_flags(args, {
            "output_prefix": "-o", "model": "--model", "language": "--language", "task": "--task",
            "initial_prompt": "--initial-prompt", **common}, {"word_timestamps": "--word-timestamps", **cpu})]
    if tool == "upscale_image":
        return ["upscale", _host_path(args["input"]), *_flags(args, {
            "output": "-o", "model": "--model", "scale": "--scale", "format": "--format", "tile": "--tile", **common}, cpu)]
    if tool == "upscale_video":
        return ["upscale-video", _host_path(args["input"]), *_flags(args, {
            "output": "-o", "model": "--model", "scale": "--scale", "encoder": "--encoder", "chunk_sec": "--chunk-sec",
            "tile": "--tile", **common}, cpu)]
    if tool == "interpolate_video":
        return ["interpolate", _host_path(args["input"]), *_flags(args, {
            "output": "-o", "factor": "--factor", "fps": "--fps", "model": "--model", **common}, {"uhd": "--uhd", **cpu})]
    if tool == "separate_stems":
        return ["separate", _host_path(args["input"]), *_flags(args, {
            "output_dir": "-o", "model": "--model", "two_stems": "--two-stems"}, {"mp3": "--mp3", **cpu})]
    raise ValueError(f"unknown tool {tool}")


# --------------------------------------------------------------------------- jobs
def start_job(tool: str, cli: list[str]) -> dict:
    job_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    d = JOBS / job_id
    d.mkdir(parents=True)
    cmd = [sys.executable, str(LOCAL_GPU), "--result-json", str(d / "result.json"), *cli]
    (d / "job.json").write_text(json.dumps({"tool": tool, "cmd": cmd, "started": time.time()}, ensure_ascii=False))
    log = open(d / "log.txt", "wb")
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000200}  # survive server restarts
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
    (d / "pid").write_text(str(proc.pid))
    return {"job_id": job_id, "status": "running", "hint": "用 gpu_job_status 查询进度（可传 wait_seconds 等待）"}


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # reap if it is our zombie child
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


def job_status(job_id: str, wait: int = 0) -> dict:
    d = JOBS / job_id
    if not d.is_dir() or "/" in job_id or "\\" in job_id:
        raise ValueError(f"job 不存在: {job_id}")
    deadline = time.time() + max(0, min(wait or 0, 55))
    while True:
        meta = json.loads((d / "job.json").read_text())
        log_tail = (d / "log.txt").read_text(errors="replace").splitlines()[-15:] if (d / "log.txt").exists() else []
        if (d / "result.json").exists():
            res = json.loads((d / "result.json").read_text())
            return {"job_id": job_id, "tool": meta["tool"], "status": "done" if res.get("ok") else "failed",
                    "result": res, "elapsed_sec": round(time.time() - meta["started"])}
        pid = int((d / "pid").read_text())
        if (d / "cancelled").exists():
            return {"job_id": job_id, "tool": meta["tool"], "status": "cancelled", "log_tail": log_tail}
        if not _alive(pid):
            return {"job_id": job_id, "tool": meta["tool"], "status": "failed",
                    "error": "进程已退出但没有写出结果", "log_tail": log_tail}
        if time.time() >= deadline:
            return {"job_id": job_id, "tool": meta["tool"], "status": "running",
                    "elapsed_sec": round(time.time() - meta["started"]), "log_tail": log_tail}
        time.sleep(2)


def job_list() -> list[dict]:
    if not JOBS.is_dir():
        return []
    out = []
    for d in sorted(JOBS.iterdir(), reverse=True)[:20]:
        try:
            s = job_status(d.name)
            out.append({k: s.get(k) for k in ("job_id", "tool", "status", "elapsed_sec")})
        except (ValueError, OSError, json.JSONDecodeError):
            continue
    return out


def job_cancel(job_id: str) -> dict:
    d = JOBS / job_id
    if not d.is_dir():
        raise ValueError(f"job 不存在: {job_id}")
    pid = int((d / "pid").read_text())
    (d / "cancelled").write_text("1")
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(pid, signal.SIGTERM)
    except OSError:
        pass
    return {"job_id": job_id, "status": "cancelled"}


def call_tool(name: str, args: dict) -> dict:
    if name == "gpu_detect":
        p = subprocess.run([sys.executable, str(LOCAL_GPU), "detect"], capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            raise RuntimeError(p.stderr[-2000:] or p.stdout[-2000:])
        return json.loads(p.stdout)
    if name == "gpu_job_status":
        return job_status(args["job_id"], int(args.get("wait_seconds") or 0))
    if name == "gpu_job_list":
        return {"jobs": job_list()}
    if name == "gpu_job_cancel":
        return job_cancel(args["job_id"])
    return start_job(name, build_cli(name, args))


# --------------------------------------------------------------------------- JSON-RPC over stdio
def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req: dict) -> dict | None:
    method, rid = req.get("method"), req.get("id")
    if rid is None:  # notification (e.g. notifications/initialized)
        return None
    try:
        if method == "initialize":
            asked = (req.get("params") or {}).get("protocolVersion")
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": "Local GPU tools run on the user's own computer. Call gpu_detect first; long tasks return a job_id - poll gpu_job_status. Never pass allow_cpu=true unless the user agreed.",
            }}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": rid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = req.get("params") or {}
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in {t["name"] for t in TOOLS}:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
            try:
                result = call_tool(name, args)
                is_err = isinstance(result, dict) and result.get("status") == "failed"
                text = json.dumps(result, ensure_ascii=False, indent=2)
            except Exception as e:  # noqa: BLE001 - tool errors are reported to the model, not as protocol errors
                is_err, text = True, f"{type(e).__name__}: {e}"
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}], "isError": is_err}}
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    except Exception as e:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        reqs = req if isinstance(req, list) else [req]
        for r in reqs:
            resp = handle(r)
            if resp is not None:
                send(resp)


if __name__ == "__main__":
    main()
