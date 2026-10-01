#!/usr/bin/env python3
"""Download online video/audio with yt-dlp (always updated to latest) + aria2c acceleration.

Behaviour
  1. Before every run, yt-dlp is upgraded to the newest release (stable or nightly,
     see YTDLP_CHANNEL). If the upgrade fails we stop with an error instead of using a
     stale yt-dlp (pass --skip-update only if the user explicitly accepts that).
  2. Downloads go through aria2c (multi-connection) via yt-dlp's --downloader.
  3. If the site needs login / anti-bot verification, exit code 3 + "NEED_COOKIES"
     is printed so the agent asks the user for a cookies.txt file.

Examples
  ytdlp_download.py "https://www.youtube.com/watch?v=xxxx" -d downloads
  ytdlp_download.py URL --audio-only --audio-format mp3
  ytdlp_download.py URL --cookies ~/cookies.txt
  ytdlp_download.py URL --cookies-from-browser chrome
  ytdlp_download.py URL --info   # only print metadata JSON
  ytdlp_download.py URL -- --embed-thumbnail --sponsorblock-remove all   # raw yt-dlp args after --
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mmcommon import (  # noqa: E402
    EXIT_NEED_COOKIES, MMError, aria2_env_args, cfg, main_guard, require_bin,
)

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-multimedia" / "ytdlp_update.json"

COOKIE_PATTERNS = [
    r"Sign in to confirm",
    r"not a bot",
    r"--cookies",
    r"cookies-from-browser",
    r"[Ll]ogin required",
    r"log ?in to",
    r"requires? (authentication|login|an account)",
    r"members?[- ]only",
    r"Private video",
    r"confirm your age",
    r"age[- ]restricted",
    r"HTTP Error 403",
    r"status=403",           # aria2c: googlevideo/CDN refused the signed URL
    r"Requested format is not available",  # often only reduced formats without login / PO token
    r"HTTP Error 412",
    r"HTTP Error 401",
    r"This video is only available",
    r"premium",
    r"account",
]


# --------------------------------------------------------------------------- update
def _ytdlp_cmd() -> list[str]:
    """Prefer the Python module (so pip upgrades take effect immediately)."""
    probe = subprocess.run([sys.executable, "-c", "import yt_dlp"], capture_output=True)
    if probe.returncode == 0:
        return [sys.executable, "-m", "yt_dlp"]
    return [require_bin("yt-dlp", "请运行 scripts/setup_env.sh 或 pip install -U 'yt-dlp[default]'")]


def _installed_version(cmd: list[str]) -> str:
    p = subprocess.run([*cmd, "--version"], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def ensure_latest(channel: str, force: bool) -> list[str]:
    interval = int(cfg("YTDLP_UPDATE_INTERVAL", "3600"))
    state = {}
    if CACHE.is_file():
        try:
            state = json.loads(CACHE.read_text())
        except json.JSONDecodeError:
            state = {}
    cmd = _ytdlp_cmd()
    before = _installed_version(cmd)
    fresh = (
        not force
        and state.get("channel") == channel
        and state.get("version") == before
        and time.time() - state.get("checked", 0) < interval
    )
    if fresh:
        print(f"[yt-dlp] {before} ({channel}) — 最近 {interval}s 内已检查过更新", file=sys.stderr)
        return cmd

    print(f"[yt-dlp] 当前版本 {before or '未安装'}，正在更新到最新 {channel} ...", file=sys.stderr)
    errors = []
    if cmd[0] == sys.executable:
        pip = [sys.executable, "-m", "pip", "install", "-U", "-q", "yt-dlp[default]"]
        if channel == "nightly":
            pip.insert(4, "--pre")
        for extra in ([], ["--user"], ["--break-system-packages"]):
            p = subprocess.run(pip + extra, capture_output=True, text=True)
            if p.returncode == 0:
                break
            errors.append(p.stderr[-800:])
        else:
            p = None
        ok = p is not None
    else:
        target = "nightly" if channel == "nightly" else "stable"
        p = subprocess.run([*cmd, "--update-to", target], capture_output=True, text=True)
        ok = p.returncode == 0
        if not ok:
            errors.append((p.stdout + p.stderr)[-800:])

    if not ok:
        raise MMError(
            "yt-dlp 自动更新失败，已停止（要求使用最新版）。错误：\n" + "\n---\n".join(errors) +
            "\n可手动执行: pip install -U 'yt-dlp[default]'  或  yt-dlp -U。"
            "若用户明确同意使用当前版本，可加 --skip-update。"
        )
    after = _installed_version(cmd)
    print(f"[yt-dlp] 版本: {before or '-'} -> {after}", file=sys.stderr)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"channel": channel, "version": after, "checked": time.time()}))
    return cmd


# --------------------------------------------------------------------------- download
def build_args(a: argparse.Namespace) -> list[str]:
    conns = max(1, min(16, int(a.connections or cfg("ARIA2_CONNECTIONS", "16"))))
    args: list[str] = []
    if not a.info:
        require_bin("aria2c", "多线程下载需要 aria2：apt install aria2 / brew install aria2 / choco install aria2")
        aria_opts = f"-x {conns} -s {conns} -k 1M --file-allocation=none --summary-interval=0 --console-log-level=warn"
        aria_opts += "".join(f" {x}" for x in aria2_env_args())
        args += [
            "--downloader", "aria2c",
            "--downloader-args", f"aria2c:{aria_opts}",
            "--concurrent-fragments", str(a.fragments),
        ]
    # YouTube needs a JS runtime (EJS). yt-dlp enables deno by default; fall back to node/bun if present.
    if not shutil.which("deno"):
        for rt in ("node", "bun"):
            if shutil.which(rt):
                args += ["--js-runtimes", rt]
                break
    cookies = a.cookies or cfg("YTDLP_COOKIES_FILE")
    browser = a.cookies_from_browser or cfg("YTDLP_COOKIES_FROM_BROWSER")
    if cookies:
        cp = Path(cookies).expanduser()
        if not cp.is_file():
            raise MMError(f"cookies 文件不存在: {cp}")
        args += ["--cookies", str(cp)]
    elif browser:
        args += ["--cookies-from-browser", browser]

    if a.info:
        return args + ["--dump-single-json", "--skip-download"] + ([] if a.playlist else ["--no-playlist"])

    out_dir = Path(a.dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    args += ["-P", str(out_dir), "-o", a.output_template, "--print", "after_move:filepath", "--no-simulate"]
    args += ["--yes-playlist"] if a.playlist else ["--no-playlist"]
    if a.audio_only:
        args += ["-f", "ba/b", "-x", "--audio-format", a.audio_format]
    else:
        fmt = a.format or (f"bv*[height<={a.max_height}]+ba/b[height<={a.max_height}]" if a.max_height else "bv*+ba/b")
        args += ["-f", fmt, "--merge-output-format", a.merge_format]
    if a.subs:
        args += ["--write-subs", "--write-auto-subs", "--sub-langs", a.subs, "--convert-subs", "srt"]
    if a.sections:
        args += ["--download-sections", a.sections]
    if a.proxy or cfg("YTDLP_PROXY"):
        args += ["--proxy", a.proxy or cfg("YTDLP_PROXY")]
    return args + a.extra


def needs_cookies(stderr: str) -> bool:
    err_lines = [l for l in stderr.splitlines() if "ERROR" in l or "error" in l.lower()]
    blob = "\n".join(err_lines) or stderr
    return any(re.search(p, blob, re.I) for p in COOKIE_PATTERNS)


@main_guard
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("-d", "--dir", default="downloads", help="output directory")
    ap.add_argument("-o", "--output-template", default="%(title).120B [%(id)s].%(ext)s")
    ap.add_argument("--audio-only", action="store_true")
    ap.add_argument("--audio-format", default="mp3")
    ap.add_argument("-f", "--format", help="raw yt-dlp format selector")
    ap.add_argument("--max-height", type=int, help="e.g. 1080")
    ap.add_argument("--merge-format", default="mp4")
    ap.add_argument("--subs", help="subtitle langs, e.g. 'zh.*,en.*'")
    ap.add_argument("--sections", help='yt-dlp --download-sections, e.g. "*00:01:00-00:02:30"')
    ap.add_argument("--playlist", action="store_true", help="download whole playlist")
    ap.add_argument("--cookies", help="Netscape-format cookies.txt")
    ap.add_argument("--cookies-from-browser", help="chrome|firefox|edge|safari|... (local machine only)")
    ap.add_argument("--proxy")
    ap.add_argument("-x", "--connections", type=int, help="aria2c connections per file (<=16)")
    ap.add_argument("--fragments", type=int, default=8, help="concurrent fragments for HLS/DASH")
    ap.add_argument("--info", action="store_true", help="print metadata JSON only")
    ap.add_argument("--channel", choices=["stable", "nightly"], default=None, help="yt-dlp update channel")
    ap.add_argument("--force-update", action="store_true")
    ap.add_argument("--skip-update", action="store_true", help="DON'T update (only if the user explicitly allows)")
    argv = sys.argv[1:]
    extra: list[str] = []
    if "--" in argv:  # everything after a bare -- goes straight to yt-dlp
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    a.extra = extra

    channel = a.channel or cfg("YTDLP_CHANNEL", "stable")
    cmd = _ytdlp_cmd() if a.skip_update else ensure_latest(channel, a.force_update)
    full = [*cmd, *build_args(a), a.url]
    print("[yt-dlp] " + " ".join(full), file=sys.stderr)

    proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    err_lines: list[str] = []
    out_buf: list[str] = []
    assert proc.stderr and proc.stdout
    # stdout (--print results / JSON) is drained in a thread so a large payload can't block;
    # stderr is streamed live (progress / warnings).
    reader = threading.Thread(target=lambda: out_buf.append(proc.stdout.read()), daemon=True)
    reader.start()
    for line in proc.stderr:
        err_lines.append(line)
        sys.stderr.write(line)
    rc = proc.wait()
    reader.join()
    out_lines = "".join(out_buf).splitlines()
    stderr = "".join(err_lines)

    if rc != 0:
        used_cookies = bool(a.cookies or a.cookies_from_browser or cfg("YTDLP_COOKIES_FILE") or cfg("YTDLP_COOKIES_FROM_BROWSER"))
        if needs_cookies(stderr + "\n" + "\n".join(out_lines)):  # aria2c logs land on stdout
            msg = (
                "NEED_COOKIES: 该站点要求登录/人机验证，当前无法下载。\n"
                + ("已提供的 cookies 可能已过期或账号无权限，请重新导出。\n" if used_cookies else "")
                + "请向用户索取 Netscape 格式的 cookies.txt：\n"
                "  1) 在已登录该站点的浏览器中安装扩展 “Get cookies.txt LOCALLY”(Chrome) 或 “cookies.txt”(Firefox)\n"
                "  2) 打开视频页面后导出 cookies.txt 并上传/提供路径\n"
                "  3) 重新运行: ytdlp_download.py URL --cookies /path/to/cookies.txt\n"
                "  (本地电脑也可用 --cookies-from-browser chrome)"
            )
            print(msg, file=sys.stderr)
            sys.exit(EXIT_NEED_COOKIES)
        raise MMError(f"yt-dlp 失败 (exit {rc})。最后的错误输出:\n{stderr[-3000:]}")

    if a.info:
        print("\n".join(out_lines))
        return
    files = [l for l in out_lines if l.strip() and Path(l.strip()).exists()]  # drop aria2c log noise
    print(json.dumps({"files": files}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
