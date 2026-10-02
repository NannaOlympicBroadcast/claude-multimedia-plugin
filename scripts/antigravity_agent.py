#!/usr/bin/env python3
"""Delegate multimodal tasks to Google Antigravity CLI (`agy`) as a subagent.

Antigravity CLI runs Gemini-based agents (natively multimodal) and ships a built-in
`generate_image` tool (Nano Banana 2). This wrapper runs `agy` headless
(`agy -p ... --output-format stream-json`) inside a per-task workspace that contains copies of
the input files, a bundled custom agent (`media-studio`), and an `outputs/` folder; it then
collects generated files and the final answer.

Commands
  install                 install / update agy: official manifest -> aria2c download -> SHA-512 check
  status                  is agy installed, which auth mode
  models                  `agy models`
  run "<task>" [--file F ...] [--agent media-studio] [--model SLUG] [-d OUT]
  auth-apikey             switch agy to Gemini-API-key auth (writes modelProvider=gemini to its settings)

Examples
  antigravity_agent.py run "看这段视频，挑 3 个关键画面写分镜说明，并为每个分镜生成一张插画" --file clip.mp4
  antigravity_agent.py run "根据这张草图生成 3 版 App 首页 UI 设计图" --file sketch.png --effort high
  antigravity_agent.py run "继续：把第 2 张改成暗色主题" --conversation <conversation_id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mmcommon import EXIT_CONFIG, MMError, aria2_download, cfg, main_guard  # noqa: E402

EXIT_NEED_LOGIN = 5
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_AGENTS = PLUGIN_ROOT / "antigravity" / "agents"
# From https://antigravity.google/cli/install.sh
MANIFEST_BASE = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests"
AGY_SETTINGS = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
WORK_ROOT = Path(cfg("MM_DATA_DIR") or (Path.home() / ".cache" / "claude-multimedia")).expanduser() / "antigravity"
MEDIA_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".webm", ".mp3", ".wav", ".svg", ".md", ".json", ".html"}


# --------------------------------------------------------------------------- install
def platform_key() -> str:
    s, m = platform.system(), platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(m)
    if not arch:
        raise MMError(f"Antigravity CLI 不支持的架构: {m}")
    if s == "Darwin":
        return f"darwin_{arch}"
    if s == "Windows":
        return f"windows_{arch}"
    if s == "Linux":
        musl = any(Path(p).exists() for p in ("/lib/libc.musl-x86_64.so.1", "/lib/libc.musl-aarch64.so.1"))
        return f"linux_{arch}_musl" if musl else f"linux_{arch}"
    raise MMError(f"Antigravity CLI 不支持的系统: {s}")


def default_bin_dir() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "agy" / "bin"
    return Path.home() / ".local" / "bin"


def agy_bin(required: bool = True) -> str | None:
    exe = "agy.exe" if platform.system() == "Windows" else "agy"
    for cand in (cfg("AGY_BIN"), shutil.which("agy"), str(default_bin_dir() / exe)):
        if cand and Path(cand).is_file():
            return cand
    if required:
        raise MMError("未安装 Antigravity CLI。请运行: antigravity_agent.py install", EXIT_CONFIG)
    return None


def cmd_install(a) -> dict:
    key = platform_key()
    r = requests.get(f"{MANIFEST_BASE}/{key}.json", timeout=60)
    if r.status_code != 200:
        raise MMError(f"获取 Antigravity 发布清单失败 HTTP {r.status_code}: {r.text[:300]}")
    man = r.json()
    url, sha512, version = man["url"], man["sha512"], man.get("version")
    staging = WORK_ROOT / "staging"
    payload = aria2_download(url, staging / Path(url.split("?")[0]).name)
    h = hashlib.sha512()
    with payload.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != sha512:
        payload.unlink(missing_ok=True)
        raise MMError("Antigravity 安装包 SHA-512 校验失败，已删除，安装中止。")
    bindir = Path(a.bin_dir).expanduser().resolve() if a.bin_dir else default_bin_dir()
    bindir.mkdir(parents=True, exist_ok=True)
    target = bindir / ("agy.exe" if key.startswith("windows") else "agy")
    if payload.name.endswith(".tar.gz"):
        with tarfile.open(payload) as tf:
            member = tf.getmember("antigravity")
            src = tf.extractfile(member)
            assert src is not None
            target.write_bytes(src.read())
    else:
        shutil.copyfile(payload, target)
    payload.unlink(missing_ok=True)
    target.chmod(0o755)
    if key.startswith("darwin"):
        subprocess.run(["xattr", "-d", "com.apple.quarantine", str(target)], capture_output=True)
    on_path = shutil.which(target.name) == str(target)
    return {"installed": str(target), "version": version, "sha512": "verified", "on_PATH": on_path,
            "next": ("首次使用需登录：在终端运行一次 `agy` 完成 Google 账号登录（会存入系统钥匙串），"
                     "或运行 antigravity_agent.py auth-apikey 改用 GEMINI_API_KEY。"
                     + ("" if on_path else f" {bindir} 不在 PATH 中，脚本会用绝对路径调用。"))}


def read_settings() -> dict:
    if AGY_SETTINGS.is_file():
        try:
            return json.loads(AGY_SETTINGS.read_text())
        except json.JSONDecodeError as e:
            raise MMError(f"{AGY_SETTINGS} 不是合法 JSON: {e}") from e
    return {}


def cmd_auth_apikey(a) -> dict:
    if not cfg("GEMINI_API_KEY"):
        raise MMError("先配置 GEMINI_API_KEY（agy 只从环境变量 GEMINI_API_KEY 读取）", EXIT_CONFIG)
    s = read_settings()
    if a.revert:
        s.pop("modelProvider", None)
    else:
        s["modelProvider"] = "gemini"
    AGY_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    AGY_SETTINGS.write_text(json.dumps(s, indent=2))
    return {"settings": str(AGY_SETTINGS), "modelProvider": s.get("modelProvider", "(account login)")}


def cmd_status(a) -> dict:
    b = agy_bin(required=False)
    s = read_settings()
    return {"agy": b, "settings": str(AGY_SETTINGS) if AGY_SETTINGS.exists() else None,
            "auth": "gemini-api-key" if s.get("modelProvider") == "gemini" else "google-account (keyring)",
            "GEMINI_API_KEY_set": bool(cfg("GEMINI_API_KEY"))}


def agy_env() -> dict:
    env = dict(os.environ)
    if cfg("GEMINI_API_KEY"):  # config file values -> real env, agy only reads the environment
        env["GEMINI_API_KEY"] = cfg("GEMINI_API_KEY")
    return env


def cmd_models(a) -> dict:
    p = subprocess.run([agy_bin(), "models"], capture_output=True, text=True, timeout=120, env=agy_env())
    if p.returncode != 0:
        check_auth(p.stderr + p.stdout)
        raise MMError(f"agy models 失败:\n{(p.stderr or p.stdout)[-1500:]}")
    return {"models": p.stdout.strip()}


# --------------------------------------------------------------------------- run
AUTH_RX = re.compile(r"sign[- ]?in|log ?in|not authenticated|unauthenticated|GEMINI_API_KEY is not set|"
                     r"terms of service|keyring|credentials", re.I)


def check_auth(text: str) -> None:
    if AUTH_RX.search(text or ""):
        print("NEED_LOGIN: Antigravity CLI 未登录或凭据不可用。请用户在自己的终端运行一次 `agy` 完成 Google 账号登录"
              "（远程 SSH 时按提示粘贴授权码），或配置 GEMINI_API_KEY 后运行 antigravity_agent.py auth-apikey。\n"
              + text[-800:], file=sys.stderr)
        sys.exit(EXIT_NEED_LOGIN)


def prepare_workspace(a) -> Path:
    ws = Path(a.workspace).expanduser().resolve() if a.workspace else WORK_ROOT / "runs" / (time.strftime("%Y%m%d-%H%M%S-") + os.urandom(3).hex())
    (ws / "inputs").mkdir(parents=True, exist_ok=True)
    (ws / "outputs").mkdir(exist_ok=True)
    for f in a.file or []:
        src = Path(f).expanduser().resolve()
        if not src.is_file():
            raise MMError(f"输入文件不存在: {src}")
        dst = ws / "inputs" / src.name
        if not dst.exists():
            try:
                os.link(src, dst)          # hard link: no copy for big media
            except OSError:
                shutil.copy2(src, dst)
    agents_dir = ws / ".agents" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for md in BUNDLED_AGENTS.glob("*.md"):
        shutil.copy2(md, agents_dir / md.name)
    return ws


def build_prompt(a, ws: Path) -> str:
    inputs = sorted(p.name for p in (ws / "inputs").iterdir())
    lines = [a.task.strip(), "", "---"]
    if inputs:
        lines.append("输入文件（已放在工作区 inputs/ 目录，请用 view_file 直接查看图片/视频/音频/文档内容）：")
        lines += [f"- inputs/{n}" for n in inputs]
    lines += [
        "要求：",
        "1. 所有生成的图片和文件都保存到工作区 outputs/ 目录（generate_image 生成的图片也要保存或复制到 outputs/）。",
        "2. 最后用简洁的中文总结做了什么，并逐条列出 outputs/ 中每个文件的文件名和用途。",
        "3. 无法完成时如实说明原因，不要编造结果。",
    ]
    return "\n".join(lines)


PATH_RX = re.compile(r"""([A-Za-z]:[\\/][^\s"'<>|]+|/[^\s"'<>|]+)\.(png|jpe?g|webp|gif|mp4|mov|webm|mp3|wav|svg)""", re.I)


def cmd_run(a) -> dict:
    binp = agy_bin()
    ws = prepare_workspace(a)
    before = {p: p.stat().st_mtime for p in ws.rglob("*") if p.is_file()}
    cmd = [binp, "-p", build_prompt(a, ws), "--output-format", "stream-json", "--print-timeout", a.timeout]
    if a.agent and a.agent != "default":
        cmd += ["--agent", a.agent]
    if a.model:
        cmd += ["--model", a.model]
    if a.effort:
        cmd += ["--effort", a.effort]
    if a.conversation:
        cmd += ["--conversation", a.conversation]
    if a.dangerously_skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    print(f"[antigravity] workspace={ws}", file=sys.stderr)
    print(f"[antigravity] {' '.join(c if c != cmd[2] else '<prompt>' for c in cmd)}", file=sys.stderr)

    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=str(ws), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            bufsize=1, env=agy_env())
    err_buf: list[str] = []
    err_thread = threading.Thread(target=lambda: err_buf.extend(proc.stderr), daemon=True)  # type: ignore[arg-type]
    err_thread.start()
    # Hard wall-clock limit: headless agy can stall on permission prompts (antigravity-cli issue #548).
    limit = parse_dur(a.timeout) + int(cfg("AGY_GRACE_SEC", "120"))
    killer = threading.Timer(limit, proc.kill)
    killer.start()
    result, tools, subagents, image_paths, text = None, [], [], [], []
    try:
        assert proc.stdout
        for line in proc.stdout:  # read while running: waiting for exit first can hang (agy docs)
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("event")
            if kind == "step_update":
                su = ev.get("step_update") or {}
                if su.get("text_delta"):
                    text.append(su["text_delta"])
                ti = su.get("tool_info")
                if ti:
                    name = ti.get("name")
                    tools.append(name)
                    blob = json.dumps(ti, ensure_ascii=False)
                    if name and "image" in name.lower():
                        image_paths += [m.group(0) for m in PATH_RX.finditer(blob)]
                    if ti.get("error"):
                        print(f"[antigravity] tool {name} error: {ti['error']}", file=sys.stderr)
                    else:
                        print(f"[antigravity] tool {name}", file=sys.stderr)
                if su.get("subagent_info"):
                    subagents.append(su["subagent_info"])
            elif kind == "result":
                result = ev.get("result") or {}
    finally:
        rc = proc.wait()
        killer.cancel()
        err_thread.join(timeout=10)
    stderr = "".join(err_buf)
    if result is None:
        check_auth(stderr)
        if time.time() - t0 >= limit:
            raise MMError(f"agy 超过 {limit}s 未结束，已终止（可能卡在权限确认）。stderr:\n{stderr[-1500:]}")
        raise MMError(f"agy 没有返回结果 (exit {rc}):\n{stderr[-2000:]}")
    if result.get("status") != "SUCCESS":
        check_auth((result.get("error") or "") + stderr)
        raise MMError(f"agy 状态 {result.get('status')}: {result.get('error') or stderr[-1500:]}")

    # Collect deliverables: new/changed files in the workspace + images referenced by image tools.
    out_dir = Path(a.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    produced = [p for p in ws.rglob("*") if p.is_file() and ".agents" not in p.parts and "inputs" not in p.parts
                and before.get(p) != p.stat().st_mtime]
    for ip in image_paths:
        p = Path(ip)
        if p.is_file() and p not in produced:
            produced.append(p)
    files = []
    for p in produced:
        if p.suffix.lower() not in MEDIA_EXT:
            continue
        dest = out_dir / p.name
        if dest.resolve() != p.resolve():
            shutil.copy2(p, dest)
        files.append(str(dest))
    soft_denied = [l for l in stderr.splitlines() if "permission" in l.lower() or "denied" in l.lower()]
    return {
        "status": result.get("status"),
        "conversation_id": result.get("conversation_id"),
        "response": result.get("response") or "".join(text),
        "files": files,
        "tools_used": sorted(set(t for t in tools if t)),
        "subagents": subagents,
        "permission_notices": soft_denied[-10:],
        "duration_seconds": result.get("duration_seconds"),
        "usage": result.get("usage"),
        "workspace": str(ws),
        "hint": "用 --conversation <conversation_id> 继续同一个对话修改结果",
    }


def parse_dur(s: str) -> int:
    m = re.fullmatch(r"(\d+)([smh]?)", s.strip())
    if not m:
        raise MMError(f"无法解析时长: {s}（例: 300s / 15m / 1h）")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


@main_guard
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install")
    p.add_argument("--bin-dir")
    sub.add_parser("status")
    sub.add_parser("models")
    p = sub.add_parser("auth-apikey")
    p.add_argument("--revert", action="store_true", help="go back to Google-account login")
    p = sub.add_parser("run")
    p.add_argument("task")
    p.add_argument("--file", action="append", help="input file (image/video/audio/doc), repeatable")
    p.add_argument("--agent", default="media-studio", help="Antigravity agent; 'default' = CLI default agent")
    p.add_argument("--model", help="model slug from `agy models`")
    p.add_argument("--effort", choices=["low", "medium", "high"])
    p.add_argument("--conversation", help="continue a previous conversation_id")
    p.add_argument("--workspace", help="reuse a workspace dir (default: new one per run)")
    p.add_argument("--timeout", default="15m", help="agy --print-timeout (e.g. 15m)")
    p.add_argument("-d", "--out-dir", default="antigravity_outputs")
    p.add_argument("--dangerously-skip-permissions", action="store_true",
                   help="auto-approve ALL agy tools (commands, writes) - only with explicit user consent")
    a = ap.parse_args()
    fn = {"install": cmd_install, "status": cmd_status, "models": cmd_models,
          "auth-apikey": cmd_auth_apikey, "run": cmd_run}[a.cmd]
    print(json.dumps(fn(a), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
