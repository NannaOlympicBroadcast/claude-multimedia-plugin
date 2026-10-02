#!/usr/bin/env python3
"""Local mode: run GPU workloads on the user's own computer.

Engines
  whisper        faster-whisper (NVIDIA CUDA) or mlx-whisper (Apple Silicon)
  upscale        Real-ESRGAN ncnn-vulkan (any Vulkan GPU: NVIDIA / AMD / Intel / Apple) for images
  upscale-video  Real-ESRGAN per frame, processed in chunks (bounded disk use, resumable)
  interpolate    RIFE ncnn-vulkan frame interpolation (2x / target fps)
  separate       Demucs music source separation (CUDA / MPS)

This script only makes sense where it runs ON the GPU machine: Claude Code on the user's
computer (also `claude remote-control`), or through the local MCP server (local_gpu_mcp.py)
that Cowork starts natively on the host. No usable GPU -> exit code 4; CPU is used only
when --allow-cpu is passed explicitly (i.e. the user agreed).

All model weights / binaries are downloaded with aria2c.

Examples
  local_gpu.py detect
  local_gpu.py install realesrgan | rife | whisper [--model large-v3-turbo] | demucs
  local_gpu.py whisper talk.mp4 --model large-v3-turbo --language zh -o out/talk
  local_gpu.py upscale photo.jpg --model realesrgan-x4plus -o photo_4x.png
  local_gpu.py upscale-video clip.mp4 --model realesr-animevideov3 --scale 2 -o clip_2x.mp4
  local_gpu.py interpolate clip.mp4 --factor 2 -o clip_60fps.mp4
  local_gpu.py separate song.mp3 --two-stems vocals -o stems/
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import platform
import re
import shutil
import site
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mmcommon import (  # noqa: E402
    EXIT_CONFIG, EXIT_NO_GPU, MMError, aria2_download, cfg, ffprobe_json, fmt_ts, require_bin, run,
    write_transcript,
)

DATA = Path(cfg("MM_DATA_DIR") or (Path.home() / ".cache" / "claude-multimedia")).expanduser()
TOOLS = DATA / "tools"
MODELS = DATA / "models"
REGISTRY = TOOLS / "registry.json"
SYSTEM = platform.system()            # Linux / Darwin / Windows
APPLE_SILICON = SYSTEM == "Darwin" and platform.machine() in ("arm64", "aarch64")
EXE = ".exe" if SYSTEM == "Windows" else ""
SOFTWARE_GPU = ("llvmpipe", "lavapipe", "swiftshader", "software", "microsoft basic render")

NCNN_TOOLS = {
    "realesrgan": {"repo": "xinntao/Real-ESRGAN", "asset": r"realesrgan-ncnn-vulkan-.*-{os}\.zip$",
                   "bin": "realesrgan-ncnn-vulkan"},
    "rife": {"repo": "nihui/rife-ncnn-vulkan", "asset": r"rife-ncnn-vulkan-.*-{os}\.zip$",
             "bin": "rife-ncnn-vulkan"},
}
OS_TAG = {"Linux": "ubuntu", "Darwin": "macos", "Windows": "windows"}.get(SYSTEM, "ubuntu")
REALESRGAN_NATIVE = {"realesrgan-x4plus": 4, "realesrgan-x4plus-anime": 4, "realesr-animevideov3": None}
MLX_REPOS = {"large-v3-turbo": "mlx-community/whisper-large-v3-turbo", "turbo": "mlx-community/whisper-large-v3-turbo",
             "large-v3": "mlx-community/whisper-large-v3-mlx"}


class NoGPU(MMError):
    def __init__(self, msg: str):
        super().__init__(
            msg + "\n本地模式不会自动退回 CPU。请确认显卡驱动/CUDA/Vulkan 已安装（运行 `local_gpu.py detect` 查看），"
                  "或在用户明确同意后加 --allow-cpu 用 CPU 运行（会慢很多）。", EXIT_NO_GPU)


def log(msg: str) -> None:
    print(f"[local-gpu] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- CUDA libs from pip wheels
def _nvidia_lib_dirs() -> list[str]:
    dirs = []
    for sp in {*site.getsitepackages(), site.getusersitepackages()}:
        base = Path(sp) / "nvidia"
        for sub in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
            for leaf in ("lib", "bin"):
                d = base / sub / leaf
                if d.is_dir():
                    dirs.append(str(d))
    return dirs


def ensure_cuda_libs() -> None:
    """faster-whisper/CTranslate2 needs cuBLAS + cuDNN 9; make pip-installed copies visible."""
    dirs = _nvidia_lib_dirs()
    if not dirs:
        return
    if SYSTEM == "Windows":
        for d in dirs:
            os.add_dll_directory(d)
        os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
    elif SYSTEM == "Linux" and not os.environ.get("MM_CUDA_REEXEC"):
        cur = os.environ.get("LD_LIBRARY_PATH", "")
        if not all(d in cur.split(os.pathsep) for d in dirs):
            env = dict(os.environ, MM_CUDA_REEXEC="1", LD_LIBRARY_PATH=os.pathsep.join(dirs + ([cur] if cur else [])))
            os.execve(sys.executable, [sys.executable, *sys.argv], env)  # loader reads LD_LIBRARY_PATH at start


# --------------------------------------------------------------------------- detection
def _q(cmd: list[str], timeout: int = 20) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def nvidia_gpus() -> list[dict]:
    if not shutil.which("nvidia-smi"):
        return []
    out = _q(["nvidia-smi", "--query-gpu=index,name,memory.total,driver_version", "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 4 and parts[0].isdigit():
            gpus.append({"index": int(parts[0]), "name": parts[1], "memory_mb": int(float(parts[2])), "driver": parts[3]})
    return gpus


def ct2_cuda_count() -> int | None:
    if not importlib.util.find_spec("ctranslate2"):
        return None
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:  # noqa: BLE001
        return 0


def torch_info() -> dict | None:
    if not importlib.util.find_spec("torch"):
        return None
    try:
        import torch
        return {"version": torch.__version__, "cuda": torch.cuda.is_available(),
                "cuda_devices": [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())],
                "mps": bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def hw_encoders() -> list[str]:
    if not shutil.which("ffmpeg"):
        return []
    out = _q(["ffmpeg", "-hide_banner", "-encoders"])
    names = ("hevc_nvenc", "h264_nvenc", "av1_nvenc", "hevc_videotoolbox", "h264_videotoolbox",
             "hevc_amf", "h264_amf", "hevc_qsv", "h264_qsv")
    return [n for n in names if re.search(rf"\s{n}\s", out)]


def load_registry() -> dict:
    return json.loads(REGISTRY.read_text()) if REGISTRY.is_file() else {}


def tool_bin(name: str) -> Path:
    override = cfg(f"{name.upper()}_BIN")
    if override:
        p = Path(override).expanduser()
        if not p.is_file():
            raise MMError(f"{name.upper()}_BIN 指向的文件不存在: {p}", EXIT_CONFIG)
        return p
    entry = load_registry().get(name)
    if not entry or not Path(entry["bin"]).is_file():
        raise MMError(f"{name} 未安装。请先运行: local_gpu.py install {name}", EXIT_CONFIG)
    return Path(entry["bin"])


def _tiny_png(path: Path, color: str = "red") -> None:
    run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=32x32", "-frames:v", "1", str(path)],
        capture=True)


def ncnn_devices(name: str) -> list[dict]:
    """Run the ncnn tool once on a tiny input and parse the Vulkan devices it reports."""
    binp = tool_bin(name)
    cache = TOOLS / f"{name}.devices.json"
    stamp = f"{binp}:{binp.stat().st_mtime}"
    if cache.is_file():
        c = json.loads(cache.read_text())
        if c.get("stamp") == stamp:
            return c["devices"]
    with tempfile.TemporaryDirectory() as td:
        a, b, o = Path(td) / "a.png", Path(td) / "b.png", Path(td) / "o.png"
        _tiny_png(a)
        if name == "realesrgan":
            args = [str(binp), "-i", str(a), "-o", str(o), "-n", "realesr-animevideov3", "-s", "2"]
        else:
            _tiny_png(b, "blue")
            args = [str(binp), "-0", str(a), "-1", str(b), "-o", str(o)]
        out = _q(args, timeout=180)
    devices = []
    for m in re.finditer(r"^\[(\d+)\s+(.+?)\]\s+queueC", out, re.M):
        nm = m.group(2).strip()
        devices.append({"id": int(m.group(1)), "name": nm, "software": any(s in nm.lower() for s in SOFTWARE_GPU)})
    if not devices and ("vkCreateInstance failed" in out or "invalid gpu device" in out or "no vulkan" in out.lower()):
        devices = []
    cache.write_text(json.dumps({"stamp": stamp, "devices": devices, "probe_log": out[-1500:]}))
    return devices


def pick_ncnn_gpu(name: str, gpu_id: int | None, allow_cpu: bool) -> int:
    devs = ncnn_devices(name)
    if gpu_id is not None:
        d = next((d for d in devs if d["id"] == gpu_id), None)
        if gpu_id >= 0 and not d:
            raise MMError(f"{name}: 找不到 GPU id {gpu_id}，可用设备: {devs}")
        if d and d["software"] and not allow_cpu:
            raise NoGPU(f"{name}: GPU {gpu_id} ({d['name']}) 是软件渲染设备。")
        return gpu_id
    hw = [d for d in devs if not d["software"]]
    if hw:
        return hw[0]["id"]
    if allow_cpu:
        log("没有硬件 Vulkan GPU，按用户要求使用 CPU/软件渲染")
        sw = [d for d in devs if d["software"]]
        return sw[0]["id"] if sw else -1
    raise NoGPU(f"{name}: 未检测到可用的 Vulkan GPU（探测结果: {devs or '无设备'}）。")


def detect() -> dict:
    reg = load_registry()
    info = {
        "platform": f"{SYSTEM} {platform.release()} {platform.machine()}",
        "python": sys.version.split()[0],
        "apple_silicon": APPLE_SILICON,
        "nvidia_gpus": nvidia_gpus(),
        "ctranslate2_cuda_devices": ct2_cuda_count(),
        "torch": torch_info(),
        "mlx_whisper_installed": bool(importlib.util.find_spec("mlx_whisper")),
        "faster_whisper_installed": bool(importlib.util.find_spec("faster_whisper")),
        "demucs_installed": bool(importlib.util.find_spec("demucs")),
        "ffmpeg_hw_encoders": hw_encoders(),
        "aria2c": bool(shutil.which("aria2c")),
        "tools": {k: v.get("version") for k, v in reg.items()},
        "vulkan_devices": {},
    }
    for name in NCNN_TOOLS:
        if name in reg:
            try:
                info["vulkan_devices"][name] = ncnn_devices(name)
            except MMError as e:
                info["vulkan_devices"][name] = f"error: {e}"
    ct2 = info["ctranslate2_cuda_devices"] or 0
    t = info["torch"] or {}
    vk = next((d for v in info["vulkan_devices"].values() if isinstance(v, list) for d in v if not d["software"]), None)
    info["recommended"] = {
        "whisper": "faster-whisper/cuda" if ct2 > 0 else ("mlx-whisper" if APPLE_SILICON else
                   ("需要: local_gpu.py install whisper" if info["nvidia_gpus"] and not info["faster_whisper_installed"] else None)),
        "ncnn(upscale/interpolate)": vk["name"] if vk else (None if reg else "需要: local_gpu.py install realesrgan"),
        "demucs": "cuda" if t.get("cuda") else ("mps" if t.get("mps") else None),
        "video_encoder_candidates": [*hw_encoders(), "libx264"],
    }
    return info


# --------------------------------------------------------------------------- installers
def github_latest_asset(repo: str, pattern: str) -> tuple[str, str]:
    import requests
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=60,
                     headers={"Accept": "application/vnd.github+json"})
    if r.status_code != 200:
        raise MMError(f"无法查询 {repo} 最新版本 (HTTP {r.status_code})。可用 --url 直接指定下载地址。{r.text[:300]}")
    rel = r.json()
    rx = re.compile(pattern.format(os=OS_TAG))
    for a in rel.get("assets", []):
        if rx.search(a["name"]):
            return a["browser_download_url"], rel.get("tag_name", "")
    raise MMError(f"{repo} {rel.get('tag_name')} 中没有匹配 {OS_TAG} 的安装包")


def install_ncnn(name: str, url: str | None) -> dict:
    spec = NCNN_TOOLS[name]
    version = ""
    if not url:
        url, version = github_latest_asset(spec["repo"], spec["asset"])
    TOOLS.mkdir(parents=True, exist_ok=True)
    zpath = TOOLS / Path(url.split("?")[0]).name
    log(f"aria2c 下载 {url}")
    aria2_download(url, zpath)
    dest = TOOLS / zpath.stem
    if dest.exists():
        shutil.rmtree(dest)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(dest)
    zpath.unlink()
    binp = next((p for p in dest.rglob(spec["bin"] + EXE) if p.is_file()), None)
    if not binp:
        raise MMError(f"压缩包中找不到 {spec['bin']}{EXE}")
    binp.chmod(binp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if SYSTEM == "Darwin":
        _q(["xattr", "-dr", "com.apple.quarantine", str(dest)])
    reg = load_registry()
    reg[name] = {"bin": str(binp), "dir": str(binp.parent), "version": version or zpath.stem, "url": url}
    REGISTRY.write_text(json.dumps(reg, indent=2))
    (TOOLS / f"{name}.devices.json").unlink(missing_ok=True)
    devs = ncnn_devices(name)
    return {"tool": name, "bin": str(binp), "version": reg[name]["version"], "vulkan_devices": devs}


def pip_install(*pkgs: str) -> None:
    base = [sys.executable, "-m", "pip", "install", "-U", *pkgs]
    for extra in ([], ["--user"], ["--break-system-packages"]):
        p = subprocess.run(base + extra, capture_output=True, text=True)
        if p.returncode == 0:
            return
        last = p.stderr[-1500:]
    raise MMError(f"pip install {' '.join(pkgs)} 失败:\n{last}")


def hf_download(repo: str) -> Path:
    """Download every file of a Hugging Face repo with aria2c (honours HF_ENDPOINT / HF_TOKEN)."""
    import requests
    ep = (cfg("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")
    headers = {"Authorization": f"Bearer {cfg('HF_TOKEN')}"} if cfg("HF_TOKEN") else {}
    r = requests.get(f"{ep}/api/models/{repo}", headers=headers, timeout=60)
    if r.status_code != 200:
        raise MMError(f"查询模型 {repo} 失败 HTTP {r.status_code}: {r.text[:300]}（国内可设置 HF_ENDPOINT=https://hf-mirror.com）")
    files = [s["rfilename"] for s in r.json().get("siblings", []) if s["rfilename"] not in (".gitattributes", "README.md")]
    dest = MODELS / repo.replace("/", "__")
    for f in files:
        target = dest / f
        if target.is_file() and target.stat().st_size > 0 and not (dest / f"{f}.aria2").exists():
            continue
        log(f"aria2c 下载 {repo}/{f}")
        aria2_download(f"{ep}/{repo}/resolve/main/{f}", target,
                       headers=[f"{k}: {v}" for k, v in headers.items()])
    (dest / ".complete").write_text(repo)
    return dest


def faster_whisper_repo(model: str) -> str:
    if "/" in model:
        return model
    from faster_whisper.utils import _MODELS  # maintained alias table of the installed version
    if model not in _MODELS:
        raise MMError(f"未知 whisper 模型 {model}，可选: {', '.join(_MODELS)}")
    return _MODELS[model]


def install_whisper(model: str, backend: str) -> dict:
    backend = resolve_whisper_backend(backend, need_installed=False)
    if backend == "mlx":
        pip_install("mlx-whisper")
        repo = model if "/" in model else MLX_REPOS.get(model)
        if not repo:
            raise MMError(f"mlx 后端没有内置 {model} 的映射，请直接传 HF 仓库名（如 mlx-community/whisper-large-v3-turbo）")
    else:
        pkgs = ["faster-whisper"]
        if SYSTEM in ("Linux", "Windows") and nvidia_gpus():
            pkgs += ["nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"]   # CTranslate2 4.x: CUDA 12 + cuDNN 9
        pip_install(*pkgs)
        importlib.invalidate_caches()
        repo = faster_whisper_repo(model)
    path = hf_download(repo)
    return {"backend": backend, "model": model, "repo": repo, "path": str(path)}


def install_demucs(model: str) -> dict:
    pip_install("demucs")
    ti = torch_info() or {}
    ckpts = prefetch_demucs(model)
    warn = None
    if not (ti.get("cuda") or ti.get("mps")):
        warn = ("当前 torch 不支持 GPU（CUDA/MPS 不可用）。NVIDIA 用户请按 https://pytorch.org/get-started/locally/ "
                "选择与驱动匹配的 CUDA 版 torch/torchaudio 重新安装。")
    return {"demucs_model": model, "checkpoints": ckpts, "torch": ti, "warning": warn}


def prefetch_demucs(model: str) -> list[str]:
    """Download demucs checkpoints with aria2c into torch hub's cache so demucs skips its own download."""
    import demucs
    remote = Path(demucs.__file__).parent / "remote"
    yml = remote / f"{model}.yaml"
    if not yml.is_file():
        raise MMError(f"未知 demucs 模型 {model}，可选: {sorted(p.stem for p in remote.glob('*.yaml'))}")
    sigs = re.findall(r"[0-9a-f]{8}", yml.read_text().split("models:", 1)[1].split("\n")[0])
    root, urls = "", {}
    for line in (remote / "files.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("root:"):
            root = line.split(":", 1)[1].strip()
            continue
        urls[line.split("-")[0]] = "https://dl.fbaipublicfiles.com/demucs/" + root + line
    hub = Path(cfg("TORCH_HOME") or (Path.home() / ".cache" / "torch")) / "hub" / "checkpoints"
    out = []
    for s in sigs:
        if s not in urls:
            raise MMError(f"demucs 签名 {s} 不在 files.txt 中")
        dest = hub / Path(urls[s]).name
        if not dest.is_file():
            log(f"aria2c 下载 demucs 权重 {dest.name}")
            aria2_download(urls[s], dest)
        out.append(str(dest))
    return out


# --------------------------------------------------------------------------- whisper
def resolve_whisper_backend(backend: str, need_installed: bool = True) -> str:
    if backend in ("faster-whisper", "mlx"):
        return backend
    if APPLE_SILICON:
        return "mlx"
    return "faster-whisper"


def run_whisper(a) -> dict:
    backend = resolve_whisper_backend(a.backend)
    src = Path(a.input).expanduser().resolve()
    if not src.is_file():
        raise MMError(f"文件不存在: {src}")
    prefix = Path(a.output).expanduser() if a.output else src.with_suffix("")
    segs: list[dict] = []
    t0 = time.time()
    if backend == "mlx":
        if not importlib.util.find_spec("mlx_whisper"):
            raise MMError("未安装 mlx-whisper，请运行: local_gpu.py install whisper", EXIT_CONFIG)
        if a.device == "cpu" and not a.allow_cpu:
            raise NoGPU("指定了 cpu。")
        repo = a.model if "/" in a.model else MLX_REPOS.get(a.model)
        if not repo:
            raise MMError(f"mlx 后端不认识模型 {a.model}")
        local = MODELS / repo.replace("/", "__")
        if not (local / ".complete").exists():
            local = hf_download(repo)
        import mlx_whisper
        res = mlx_whisper.transcribe(str(src), path_or_hf_repo=str(local), language=a.language, task=a.task,
                                     word_timestamps=a.word_timestamps, initial_prompt=a.initial_prompt,
                                     verbose=False)
        for s in res.get("segments", []):
            segs.append({"start": float(s["start"]), "end": float(s["end"]), "speaker": None, "text": s["text"].strip()})
        device, lang = "mlx(metal)", res.get("language")
    else:
        if not importlib.util.find_spec("faster_whisper"):
            raise MMError("未安装 faster-whisper，请运行: local_gpu.py install whisper", EXIT_CONFIG)
        cuda = ct2_cuda_count() or 0
        if a.device in ("auto", "cuda"):
            if cuda == 0:
                if a.device == "auto" and a.allow_cpu:
                    device = "cpu"
                else:
                    raise NoGPU("CTranslate2 未检测到 CUDA 设备（需要 NVIDIA 驱动 + CUDA 12 cuBLAS + cuDNN 9）。")
            else:
                device = "cuda"
        else:
            if not a.allow_cpu:
                raise NoGPU("指定了 --device cpu。")
            device = "cpu"
        repo = faster_whisper_repo(a.model) if not Path(a.model).is_dir() else None
        model_dir = Path(a.model) if repo is None else MODELS / repo.replace("/", "__")
        if repo and not (model_dir / ".complete").exists():
            model_dir = hf_download(repo)
        from faster_whisper import BatchedInferencePipeline, WhisperModel
        compute = a.compute_type or ("float16" if device == "cuda" else "int8")
        log(f"faster-whisper {a.model} on {device} ({compute}, gpu {a.gpu})")
        model = WhisperModel(str(model_dir), device=device, device_index=a.gpu, compute_type=compute)
        kw = dict(language=a.language, task=a.task, word_timestamps=a.word_timestamps,
                  initial_prompt=a.initial_prompt, hotwords=a.hotwords)
        if a.batch_size > 1:
            segments, info = BatchedInferencePipeline(model=model).transcribe(str(src), batch_size=a.batch_size, **kw)
        else:
            segments, info = model.transcribe(str(src), vad_filter=not a.no_vad, beam_size=a.beam_size, **kw)
        last = 0.0
        for s in segments:
            segs.append({"start": float(s.start), "end": float(s.end), "speaker": None, "text": s.text.strip()})
            if s.end - last > 60:
                last = s.end
                log(f"进度 {fmt_ts(s.end)[:-4]} / {fmt_ts(info.duration)[:-4]}")
        lang = info.language
    meta = {"engine": f"{backend}:{a.model}", "device": device, "language": lang, "source": str(src)}
    write_transcript(segs, prefix, meta)
    return {"srt": str(prefix.with_suffix(".srt")), "txt": str(prefix.with_suffix(".txt")),
            "json": str(prefix.with_suffix(".json")), "segments": len(segs), "language": lang,
            "device": device, "seconds": round(time.time() - t0, 1)}


# --------------------------------------------------------------------------- ffmpeg helpers
def video_info(path: Path) -> dict:
    info = ffprobe_json(path)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    if not v:
        raise MMError(f"没有视频流: {path}")
    fps = Fraction(v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1")
    if fps <= 0:
        fps = Fraction(v.get("r_frame_rate", "30/1"))
    dur = float(info["format"].get("duration") or v.get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])
    return {"w": int(v["width"]), "h": int(v["height"]), "fps": fps, "duration": dur,
            "frames": int(round(dur * float(fps))), "has_audio": has_audio}


ENCODER_ARGS = {
    "hevc_nvenc": ["-preset", "p5", "-rc", "vbr", "-cq", "19", "-tag:v", "hvc1"],
    "h264_nvenc": ["-preset", "p5", "-rc", "vbr", "-cq", "19"],
    "hevc_videotoolbox": ["-q:v", "65", "-tag:v", "hvc1"],
    "h264_videotoolbox": ["-q:v", "65"],
    "hevc_amf": ["-quality", "quality", "-rc", "cqp", "-qp_i", "20", "-qp_p", "20", "-tag:v", "hvc1"],
    "h264_amf": ["-quality", "quality", "-rc", "cqp", "-qp_i", "20", "-qp_p", "20"],
    "hevc_qsv": ["-global_quality", "20", "-tag:v", "hvc1"],
    "h264_qsv": ["-global_quality", "20"],
    "libx265": ["-crf", "20", "-preset", "medium", "-tag:v", "hvc1"],
    "libx264": ["-crf", "18", "-preset", "medium"],
}


def pick_encoder(choice: str, test: bool = True) -> str:
    if choice != "auto":
        return choice
    for enc in [*hw_encoders(), "libx264"]:
        if not test or enc == "libx264":
            return enc
        p = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=1280x720:d=0.2", "-c:v", enc,
                            *ENCODER_ARGS.get(enc, []), "-f", "null", "-"], capture_output=True, text=True)
        if p.returncode == 0:
            return enc
    return "libx264"


def encode_frames(pattern: str, fps: Fraction, out: Path, encoder: str, vf: str | None = None) -> None:
    cmd = ["ffmpeg", "-v", "error", "-y", "-framerate", f"{fps.numerator}/{fps.denominator}", "-i", pattern]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-c:v", encoder, *ENCODER_ARGS.get(encoder, []), "-pix_fmt", "yuv420p", str(out)]
    run(cmd, capture=True)


def mux_audio(video_only: Path, source: Path, out: Path, has_audio: bool) -> None:
    if not has_audio:
        shutil.move(str(video_only), out)
        return
    base = ["ffmpeg", "-v", "error", "-y", "-i", str(video_only), "-i", str(source), "-map", "0:v", "-map", "1:a?",
            "-c:v", "copy", "-shortest"]
    p = subprocess.run(base + ["-c:a", "copy", str(out)], capture_output=True, text=True)
    if p.returncode != 0:  # source audio codec not allowed in this container -> AAC
        run(base + ["-c:a", "aac", "-b:a", "256k", str(out)], capture=True)


def check_disk(path: Path, need_bytes: float, what: str) -> None:
    free = shutil.disk_usage(path).free
    if free < need_bytes * 1.2:
        raise MMError(f"{what} 预计需要 {need_bytes/1e9:.1f} GB 临时空间，{path} 只剩 {free/1e9:.1f} GB。"
                      "请调小 --chunk-sec 或用 --work-dir 指向更大的磁盘。")


# --------------------------------------------------------------------------- Real-ESRGAN
def realesrgan_cmd(binp: Path, inp: Path, out: Path, model: str, native: int, gpu: int, a) -> list[str]:
    cmd = [str(binp), "-i", str(inp), "-o", str(out), "-n", model, "-s", str(native), "-g", str(gpu)]
    if a.model_dir:
        cmd += ["-m", str(Path(a.model_dir).expanduser())]
    if a.tile:
        cmd += ["-t", str(a.tile)]
    if a.tta:
        cmd += ["-x"]
    if getattr(a, "threads", None):
        cmd += ["-j", a.threads]
    return cmd


def native_scale(model: str, want: int) -> int:
    fixed = REALESRGAN_NATIVE.get(model, 4)
    if fixed is None:            # realesr-animevideov3 supports 2/3/4 natively
        return want if want in (2, 3, 4) else 4
    return fixed


def run_upscale(a) -> dict:
    binp = tool_bin("realesrgan")
    gpu = pick_ncnn_gpu("realesrgan", a.gpu, a.allow_cpu)
    src = Path(a.input).expanduser().resolve()
    if not src.exists():
        raise MMError(f"输入不存在: {src}")
    native = native_scale(a.model, a.scale)
    fmt = a.format
    if src.is_dir():
        out = Path(a.output or f"{src}_x{a.scale}").expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
    else:
        out = Path(a.output or src.with_name(f"{src.stem}_x{a.scale}.{fmt}")).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        fmt = out.suffix.lstrip(".").lower().replace("jpeg", "jpg") or fmt
    cmd = realesrgan_cmd(binp, src, out, a.model, native, gpu, a) + ["-f", fmt]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(binp.parent))
    if p.returncode != 0:
        raise MMError(f"realesrgan 失败 (exit {p.returncode}):\n{(p.stdout + p.stderr)[-2000:]}")
    files = sorted(str(f) for f in out.iterdir()) if out.is_dir() else [str(out)]
    if native != a.scale:  # e.g. x4plus model but user wants 2x -> lanczos downscale of the 4x result
        factor = a.scale / native
        for f in files:
            tmp = Path(f).with_name(Path(f).stem + ".tmp" + Path(f).suffix)
            run(["ffmpeg", "-v", "error", "-y", "-i", f, "-vf", f"scale=iw*{factor}:ih*{factor}:flags=lanczos", str(tmp)],
                capture=True)
            os.replace(tmp, f)
    return {"outputs": files, "model": a.model, "scale": a.scale, "gpu_id": gpu, "seconds": round(time.time() - t0, 1)}


def run_upscale_video(a) -> dict:
    binp = tool_bin("realesrgan")
    gpu = pick_ncnn_gpu("realesrgan", a.gpu, a.allow_cpu)
    src = Path(a.input).expanduser().resolve()
    vi = video_info(src)
    native = native_scale(a.model, a.scale)
    out = Path(a.output or src.with_name(f"{src.stem}_x{a.scale}.mp4")).expanduser().resolve()
    work = Path(a.work_dir or out.parent / f".{out.stem}.work").expanduser()
    work.mkdir(parents=True, exist_ok=True)
    enc = pick_encoder(a.encoder)
    fps = vi["fps"]
    chunk_frames = max(1, int(a.chunk_sec * float(fps)))
    n_chunks = max(1, math.ceil(vi["frames"] / chunk_frames))
    check_disk(work, vi["w"] * vi["h"] * native * native * 1.6 * chunk_frames, "超分分片")
    vf = None
    if native != a.scale:
        vf = f"scale={vi['w'] * a.scale // 2 * 2}:{vi['h'] * a.scale // 2 * 2}:flags=lanczos"
    log(f"{vi['w']}x{vi['h']} {float(fps):.3f}fps {vi['frames']} 帧 -> x{a.scale}，{n_chunks} 段，GPU {gpu}，编码器 {enc}")
    t0 = time.time()
    parts = []
    for k in range(n_chunks):
        part = work / f"part{k:05d}.mp4"
        parts.append(part)
        if part.is_file() and part.stat().st_size > 0:
            continue  # resumable
        fin, fout = work / "in", work / "out"
        for d in (fin, fout):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir()
        t_start = k * chunk_frames / float(fps)
        run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t_start:.6f}", "-i", str(src), "-frames:v", str(chunk_frames),
             "-fps_mode", "cfr", "-r", f"{fps.numerator}/{fps.denominator}", "-an", str(fin / "%08d.png")], capture=True)
        if not any(fin.iterdir()):
            parts.pop()
            break
        p = subprocess.run(realesrgan_cmd(binp, fin, fout, a.model, native, gpu, a) + ["-f", "png"],
                           capture_output=True, text=True, cwd=str(binp.parent))
        if p.returncode != 0:
            raise MMError(f"realesrgan 失败于第 {k+1} 段:\n{(p.stdout + p.stderr)[-2000:]}")
        tmp = work / f"part{k:05d}.tmp.mp4"
        encode_frames(str(fout / "%08d.png"), fps, tmp, enc, vf)
        os.replace(tmp, part)
        shutil.rmtree(fin)
        shutil.rmtree(fout)
        done = (k + 1) / n_chunks
        el = time.time() - t0
        log(f"段 {k+1}/{n_chunks} 完成，已用 {el/60:.1f} 分钟，预计剩余 {el/done*(1-done)/60:.1f} 分钟")
    lst = work / "parts.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    vid = work / "video_only.mp4"
    run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(vid)], capture=True)
    mux_audio(vid, src, out, vi["has_audio"])
    if not a.keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return {"output": str(out), "scale": a.scale, "model": a.model, "encoder": enc, "gpu_id": gpu,
            "chunks": len(parts), "minutes": round((time.time() - t0) / 60, 1)}


# --------------------------------------------------------------------------- RIFE
def run_interpolate(a) -> dict:
    binp = tool_bin("rife")
    gpu = pick_ncnn_gpu("rife", a.gpu, a.allow_cpu)
    src = Path(a.input).expanduser().resolve()
    vi = video_info(src)
    fps = vi["fps"]
    target_fps = Fraction(a.fps).limit_denominator(1001) if a.fps else fps * Fraction(a.factor).limit_denominator(1000)
    out = Path(a.output or src.with_name(f"{src.stem}_{float(target_fps):.0f}fps.mp4")).expanduser().resolve()
    model_dir = Path(a.model).expanduser() if Path(a.model).is_dir() else binp.parent / a.model
    if not model_dir.is_dir():
        avail = sorted(p.name for p in binp.parent.iterdir() if p.is_dir() and p.name.startswith("rife"))
        raise MMError(f"RIFE 模型目录不存在: {model_dir}，可选: {avail}")
    work = Path(a.work_dir or out.parent / f".{out.stem}.work").expanduser()
    shutil.rmtree(work, ignore_errors=True)
    (work / "in").mkdir(parents=True)
    (work / "out").mkdir()
    ratio = float(target_fps / fps)
    check_disk(work, vi["w"] * vi["h"] * 1.6 * vi["frames"] * (1 + ratio), "插帧")
    enc = pick_encoder(a.encoder)
    t0 = time.time()
    run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-fps_mode", "cfr", "-r", f"{fps.numerator}/{fps.denominator}",
         "-an", str(work / "in" / "%08d.png")], capture=True)
    n_in = len(list((work / "in").iterdir()))
    n_out = int(round(n_in * ratio))
    cmd = [str(binp), "-i", str(work / "in"), "-o", str(work / "out"), "-m", str(model_dir), "-n", str(n_out),
           "-g", str(gpu), "-f", "%08d.png"]
    if a.uhd:
        cmd.append("-u")
    log(f"RIFE {n_in} -> {n_out} 帧 ({float(fps):.3f} -> {float(target_fps):.3f} fps)，GPU {gpu}")
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(binp.parent))
    if p.returncode != 0:
        raise MMError(f"rife 失败:\n{(p.stdout + p.stderr)[-2000:]}")
    vid = work / "video_only.mp4"
    encode_frames(str(work / "out" / "%08d.png"), target_fps, vid, enc)
    mux_audio(vid, src, out, vi["has_audio"])
    if not a.keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return {"output": str(out), "fps": float(target_fps), "frames": n_out, "encoder": enc, "gpu_id": gpu,
            "minutes": round((time.time() - t0) / 60, 1)}


# --------------------------------------------------------------------------- Demucs
def run_separate(a) -> dict:
    if not importlib.util.find_spec("demucs"):
        raise MMError("未安装 demucs，请运行: local_gpu.py install demucs", EXIT_CONFIG)
    ti = torch_info() or {}
    if a.device == "auto":
        device = "cuda" if ti.get("cuda") else ("mps" if ti.get("mps") else None)
        if not device:
            if not a.allow_cpu:
                raise NoGPU(f"torch 不支持 CUDA/MPS（{ti}）。")
            device = "cpu"
    else:
        device = a.device
        if device == "cpu" and not a.allow_cpu:
            raise NoGPU("指定了 --device cpu。")
    src = Path(a.input).expanduser().resolve()
    out = Path(a.output or src.parent / "stems").expanduser().resolve()
    prefetch_demucs(a.model)
    cmd = [sys.executable, "-m", "demucs", "-n", a.model, "-d", device, "-o", str(out)]
    if a.two_stems:
        cmd += ["--two-stems", a.two_stems]
    if a.mp3:
        cmd += ["--mp3"]
    cmd.append(str(src))
    t0 = time.time()
    log(" ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise MMError(f"demucs 失败:\n{(p.stdout + p.stderr)[-2000:]}")
    track_dir = out / a.model / src.stem
    return {"stems": sorted(str(f) for f in track_dir.iterdir()) if track_dir.is_dir() else [],
            "dir": str(track_dir), "device": device, "seconds": round(time.time() - t0, 1)}


# --------------------------------------------------------------------------- CLI
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--result-json", help="also write the result / error as JSON to this path (used by the MCP job runner)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("detect", help="GPU / driver / tool report")

    p = sub.add_parser("install", help="install engines and models (downloads via aria2c)")
    p.add_argument("component", choices=["realesrgan", "rife", "whisper", "demucs"])
    p.add_argument("--url", help="ncnn tools: explicit release zip URL")
    p.add_argument("--model", help="whisper: large-v3-turbo (default) / large-v3 / distil-large-v3.5 / repo id; demucs: htdemucs")
    p.add_argument("--backend", choices=["auto", "faster-whisper", "mlx"], default="auto")

    def common(q, gpu_default=None):
        q.add_argument("--gpu", type=int, default=gpu_default, help="GPU index")
        q.add_argument("--allow-cpu", action="store_true", help="only if the user explicitly agreed to CPU")

    p = sub.add_parser("whisper")
    p.add_argument("input")
    p.add_argument("-o", "--output", help="output prefix (.srt/.txt/.json)")
    p.add_argument("--model", default=cfg("LOCAL_WHISPER_MODEL", "large-v3-turbo"))
    p.add_argument("--backend", choices=["auto", "faster-whisper", "mlx"], default="auto")
    p.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    p.add_argument("--compute-type", help="float16 / int8_float16 / int8 ...")
    p.add_argument("--language", help="zh / en / ja ... (default auto-detect)")
    p.add_argument("--task", choices=["transcribe", "translate"], default="transcribe")
    p.add_argument("--word-timestamps", action="store_true")
    p.add_argument("--initial-prompt", help="context / spelling hints, e.g. 简体中文，带标点。")
    p.add_argument("--hotwords", help="faster-whisper hotwords")
    p.add_argument("--batch-size", type=int, default=8, help=">1 uses BatchedInferencePipeline (faster on GPU)")
    p.add_argument("--beam-size", type=int, default=5)
    p.add_argument("--no-vad", action="store_true")
    common(p, 0)

    def sr_args(q):
        q.add_argument("--model", default="realesrgan-x4plus",
                       help="realesrgan-x4plus (photo) / realesrgan-x4plus-anime / realesr-animevideov3 / custom name with --model-dir")
        q.add_argument("--model-dir", help="directory with custom ncnn .param/.bin models (e.g. Upscayl models)")
        q.add_argument("--scale", type=int, default=4, choices=[2, 3, 4])
        q.add_argument("--tile", type=int, default=0, help="tile size (0=auto); lower it on VRAM errors")
        q.add_argument("--tta", action="store_true")
        q.add_argument("--threads", help="load:proc:save, e.g. 1:2:2")
        common(q)

    p = sub.add_parser("upscale", help="image or folder of images")
    p.add_argument("input")
    p.add_argument("-o", "--output")
    p.add_argument("--format", default="png", choices=["png", "jpg", "webp"])
    sr_args(p)

    p = sub.add_parser("upscale-video")
    p.add_argument("input")
    p.add_argument("-o", "--output")
    p.add_argument("--chunk-sec", type=float, default=20)
    p.add_argument("--encoder", default="auto", help="auto / hevc_nvenc / h264_videotoolbox / libx264 ...")
    p.add_argument("--work-dir")
    p.add_argument("--keep-work", action="store_true")
    sr_args(p)

    p = sub.add_parser("interpolate", help="RIFE frame interpolation")
    p.add_argument("input")
    p.add_argument("-o", "--output")
    p.add_argument("--factor", type=float, default=2.0)
    p.add_argument("--fps", help="target fps (overrides --factor), e.g. 60 or 59.94")
    p.add_argument("--model", default="rife-v4.6", help="model folder shipped with rife-ncnn-vulkan, or a path")
    p.add_argument("--uhd", action="store_true", help="UHD mode for 4K input")
    p.add_argument("--encoder", default="auto")
    p.add_argument("--work-dir")
    p.add_argument("--keep-work", action="store_true")
    common(p)

    p = sub.add_parser("separate", help="Demucs stem separation")
    p.add_argument("input")
    p.add_argument("-o", "--output", help="output dir (default: <input dir>/stems)")
    p.add_argument("--model", default="htdemucs", help="htdemucs / htdemucs_ft / htdemucs_6s / mdx_extra ...")
    p.add_argument("--two-stems", help="e.g. vocals -> vocals + no_vocals")
    p.add_argument("--mp3", action="store_true")
    p.add_argument("--device", choices=["auto", "cuda", "mps", "cpu"], default="auto")
    common(p)

    a = ap.parse_args()
    if a.cmd == "whisper":
        ensure_cuda_libs()

    def emit(obj: dict, code: int = 0) -> None:
        if a.result_json:
            Path(a.result_json).write_text(json.dumps({"ok": code == 0, "exit_code": code, **obj}, ensure_ascii=False, indent=2))
        print(json.dumps(obj, ensure_ascii=False, indent=2))
        sys.exit(code)

    try:
        require_bin("ffmpeg")
        if a.cmd == "detect":
            emit(detect())
        if a.cmd == "install":
            if a.component in NCNN_TOOLS:
                emit(install_ncnn(a.component, a.url))
            if a.component == "whisper":
                emit(install_whisper(a.model or "large-v3-turbo", a.backend))
            emit(install_demucs(a.model or "htdemucs"))
        handlers = {"whisper": run_whisper, "upscale": run_upscale, "upscale-video": run_upscale_video,
                    "interpolate": run_interpolate, "separate": run_separate}
        emit(handlers[a.cmd](a))
    except MMError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        if a.result_json:
            Path(a.result_json).write_text(json.dumps({"ok": False, "exit_code": e.code, "error": str(e)},
                                                      ensure_ascii=False, indent=2))
        sys.exit(e.code)


if __name__ == "__main__":
    main()
