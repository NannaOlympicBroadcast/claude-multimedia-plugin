#!/usr/bin/env python3
"""Check binaries, Python packages and configured API keys for the claude-multimedia plugin."""
from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mmcommon import cfg  # noqa: E402


def ver(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def main() -> None:
    ok = True
    print("== 可执行文件 ==")
    for name, cmd in [("aria2c", ["aria2c", "--version"]), ("ffmpeg", ["ffmpeg", "-version"]),
                      ("ffprobe", ["ffprobe", "-version"]), ("deno", ["deno", "--version"])]:
        if shutil.which(name):
            print(f"  [ok]   {name:8s} {ver(cmd)[:70]}")
        else:
            print(f"  [MISS] {name}")
            ok = ok and name == "deno"  # deno only needed for YouTube
    print("== Python 包 ==")
    for mod, label in [("yt_dlp", "yt-dlp"), ("edge_tts", "edge-tts"), ("librosa", "librosa"),
                       ("soundfile", "soundfile"), ("numpy", "numpy"), ("requests", "requests"),
                       ("matplotlib", "matplotlib (可选, --plot)")]:
        try:
            m = importlib.import_module(mod)
            v = getattr(m, "__version__", "") or getattr(getattr(m, "version", None), "__version__", "")
            print(f"  [ok]   {label:24s} {v}")
        except ImportError:
            print(f"  [MISS] {label}")
            ok = ok and mod == "matplotlib"
    print("== API Key（只显示是否配置） ==")
    keys = [
        ("GEMINI_API_KEY", "Gemini 分析 / Nano Banana"),
        ("OPENAI_API_KEY", "GPT Image"),
        ("ARK_API_KEY", "Seedream 火山引擎"),
        ("BYTEPLUS_API_KEY", "Seedream BytePlus"),
        ("TENCENTCLOUD_SECRET_ID", "腾讯云 ASR"),
        ("TENCENTCLOUD_SECRET_KEY", "腾讯云 ASR"),
    ]
    for k, label in keys:
        print(f"  [{'set' if cfg(k) else '---'}]  {k:24s} {label}")
    print("== 生图选择 ==")
    print(f"  IMAGE_PROVIDER={cfg('IMAGE_PROVIDER', 'auto')}  IMAGE_TIER={cfg('IMAGE_TIER', 'quality')}  "
          f"ORDER={cfg('IMAGE_PROVIDER_ORDER', 'openai,gemini,seedream')}")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
