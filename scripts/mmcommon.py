"""Shared helpers for the claude-multimedia plugin scripts.

- Config loading (env vars + optional dotenv files)
- aria2c multi-connection downloads (required before any download)
- Small subprocess / ffprobe / timestamp utilities
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Sequence

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# Exit codes shared by all scripts so the skills can react consistently.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2          # missing key / missing binary -> ask the user to configure
EXIT_NEED_COOKIES = 3    # yt-dlp needs cookies -> ask the user for a cookies file
EXIT_NO_GPU = 4          # local mode found no usable GPU -> ask the user (never silently use CPU)


class MMError(RuntimeError):
    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- config
_CONFIG_LOADED = False


def _config_files() -> list[Path]:
    files = []
    explicit = os.environ.get("MM_CONFIG")
    if explicit:
        files.append(Path(explicit).expanduser())
    files += [
        Path.cwd() / ".env",
        Path.home() / ".config" / "claude-multimedia" / "config.env",
        PLUGIN_ROOT / "config.env",
    ]
    return files


def load_config() -> None:
    """Load KEY=VALUE files. Real environment variables always win."""
    global _CONFIG_LOADED
    if _CONFIG_LOADED:
        return
    _CONFIG_LOADED = True
    for f in _config_files():
        if not f.is_file():
            continue
        for raw in f.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, value = line.partition("=")
            value = value.strip()
            if value[:1] in ('"', "'"):
                value = value[1:].split(value[0], 1)[0]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].strip()  # inline comment
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value


def cfg(name: str, default: str | None = None) -> str | None:
    load_config()
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def require_cfg(name: str, hint: str = "") -> str:
    val = cfg(name)
    if not val:
        raise MMError(
            f"缺少配置 {name}。请在环境变量或 ~/.config/claude-multimedia/config.env 中设置。{hint}",
            EXIT_CONFIG,
        )
    return val


# --------------------------------------------------------------------------- binaries
def require_bin(name: str, install_hint: str = "") -> str:
    path = shutil.which(name)
    if not path:
        raise MMError(
            f"未找到可执行文件 `{name}`。{install_hint or '请先运行 scripts/setup_env.sh 安装依赖。'}",
            EXIT_CONFIG,
        )
    return path


def run(cmd: Sequence[str], check: bool = True, capture: bool = False, **kw) -> subprocess.CompletedProcess:
    if capture:
        kw.setdefault("stdout", subprocess.PIPE)
        kw.setdefault("stderr", subprocess.PIPE)
        kw.setdefault("text", True)
    proc = subprocess.run(list(cmd), **kw)
    if check and proc.returncode != 0:
        err = (proc.stderr or "")[-4000:] if capture else ""
        raise MMError(f"命令失败 (exit {proc.returncode}): {' '.join(map(str, cmd))}\n{err}")
    return proc


# --------------------------------------------------------------------------- aria2
def aria2_args(connections: int | None = None) -> list[str]:
    n = int(connections or cfg("ARIA2_CONNECTIONS", "16"))
    n = max(1, min(n, 16))  # aria2c caps -x at 16
    return [
        f"--max-connection-per-server={n}",
        f"--split={n}",
        "--min-split-size=1M",
        "--continue=true",
        "--file-allocation=none",
        "--max-tries=5",
        "--retry-wait=2",
        "--summary-interval=0",
        "--console-log-level=warn",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
    ]


def aria2_env_args() -> list[str]:
    """Proxy / CA flags so aria2c works behind corporate or sandbox proxies."""
    out = []
    proxy = cfg("ARIA2_ALL_PROXY") or cfg("HTTPS_PROXY") or cfg("https_proxy")
    if proxy:
        out.append(f"--all-proxy={proxy}")
        no_proxy = cfg("NO_PROXY") or cfg("no_proxy")
        if no_proxy:
            out.append(f"--no-proxy={no_proxy}")
    ca = cfg("ARIA2_CA_CERT") or cfg("SSL_CERT_FILE") or cfg("REQUESTS_CA_BUNDLE")
    if ca and Path(ca).is_file():
        out.append(f"--ca-certificate={ca}")
    return out


def aria2_download(
    url: str,
    dest: str | Path,
    headers: Iterable[str] = (),
    connections: int | None = None,
) -> Path:
    """Download one URL with aria2c multi-connection acceleration.

    aria2c is mandatory for downloads in this plugin; if it is missing we stop and
    tell the user instead of silently falling back to a single-connection client.
    """
    aria2c = require_bin("aria2c", "多线程下载需要 aria2：apt install aria2 / brew install aria2 / choco install aria2")
    dest = Path(dest).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [aria2c, *aria2_args(connections), "-d", str(dest.parent), "-o", dest.name]
    for h in headers:
        cmd += ["--header", h]
    cmd += aria2_env_args()
    cmd.append(url)
    proc = run(cmd, check=False, capture=True)
    if proc.returncode != 0 or not dest.exists():
        raise MMError(f"aria2c 下载失败 (exit {proc.returncode}): {url}\n{(proc.stdout or '')[-2000:]}{(proc.stderr or '')[-2000:]}")
    return dest


# --------------------------------------------------------------------------- media utils
def ffprobe_json(path: str | Path) -> dict:
    require_bin("ffprobe")
    proc = run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture=True,
    )
    return json.loads(proc.stdout)


def media_duration(path: str | Path) -> float:
    info = ffprobe_json(path)
    dur = info.get("format", {}).get("duration")
    if dur is None:
        for s in info.get("streams", []):
            if s.get("duration"):
                dur = s["duration"]
                break
    if dur is None:
        raise MMError(f"无法获取时长: {path}")
    return float(dur)


def parse_ts(value: str | float | int) -> float:
    """Parse '83.5', '1:23', '01:02:03.250', '1h2m3s', '90s' into seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?", s)
    if m and any(m.groups()):
        h, mi, se = (float(x) if x else 0.0 for x in m.groups())
        return h * 3600 + mi * 60 + se
    parts = s.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError as e:
        raise MMError(f"无法解析时间点: {value!r}") from e
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def fmt_ts(seconds: float, sep: str = ".") -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def write_transcript(segs: list[dict], prefix: Path, meta: dict) -> None:
    """Write <prefix>.srt / .txt / .json from [{start, end, speaker, text}]."""
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with (prefix.with_suffix(".srt")).open("w", encoding="utf-8") as f:
        for i, s in enumerate(segs, 1):
            spk = f"[S{s['speaker']}] " if s.get("speaker") not in (None, -1) and meta.get("speakers") else ""
            f.write(f"{i}\n{fmt_ts(s['start'], ',')} --> {fmt_ts(s['end'], ',')}\n{spk}{s['text']}\n\n")
    with (prefix.with_suffix(".txt")).open("w", encoding="utf-8") as f:
        for s in segs:
            spk = f"说话人{s['speaker']}: " if s.get("speaker") not in (None, -1) and meta.get("speakers") else ""
            f.write(f"[{fmt_ts(s['start'])[:-4]}] {spk}{s['text']}\n")
    prefix.with_suffix(".json").write_text(json.dumps({"meta": meta, "segments": segs}, ensure_ascii=False, indent=2),
                                           encoding="utf-8")


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text))


def die(err: BaseException) -> None:
    code = getattr(err, "code", EXIT_ERROR)
    print(f"[ERROR] {err}", file=sys.stderr)
    sys.exit(code if isinstance(code, int) else EXIT_ERROR)


def main_guard(fn):
    """Decorator: turn MMError into a clean stderr message + exit code."""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except MMError as e:
            die(e)
        except KeyboardInterrupt:
            sys.exit(130)
    return wrapper
