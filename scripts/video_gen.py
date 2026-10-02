#!/usr/bin/env python3
"""AI video generation: Google Gemini Omni (newest model, auto-discovered) or 即梦 Dreamina CLI.

Providers
  gemini   Gemini Omni via the Interactions API (POST /v1beta/interactions): text-to-video,
           image-to-video, first+last frame, subject references, video edit / extend, and
           multi-turn edits with --previous-id. Needs GEMINI_API_KEY.
  jimeng   即梦 official `dreamina` CLI (Seedance models), logged in with OAuth device flow.
           Consumes the user's 即梦 credits.

Selection: --provider, else VIDEO_PROVIDER (auto|gemini|jimeng; auto = gemini when GEMINI_API_KEY
is set, otherwise jimeng when `dreamina` is installed).

Examples
  video_gen.py gemini "雨夜霓虹街头，一只橘猫走过水洼，电影感推镜" --aspect 16:9 --resolution 720p
  video_gen.py gemini "让画面里的人物转身微笑" --image photo.jpg
  video_gen.py gemini "从白天平滑过渡到夜晚" --image day.jpg --image night.jpg --task image_to_video
  video_gen.py gemini "把小提琴变成透明的" --previous-id v1_xxx          # multi-turn edit
  video_gen.py gemini "继续这个镜头，镜头拉远" --video clip.mp4 --task extend
  video_gen.py jimeng-install                        # official binary via aria2 (no shell rc edits)
  video_gen.py jimeng-login                          # headless OAuth device flow -> prints verification URL + code
  video_gen.py jimeng-login --device-code XXXX       # finish login after the user authorised
  video_gen.py jimeng text2video --prompt "海边日落延时" --ratio 16:9
  video_gen.py jimeng image2video -- <flags from `dreamina image2video -h`>
  video_gen.py jimeng-result --submit-id <id>        # query + download a finished task
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mmcommon import EXIT_CONFIG, MMError, aria2_download, cfg, main_guard, require_cfg  # noqa: E402

EXIT_NEED_LOGIN = 5
GEMINI_BASE = (cfg("GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com").rstrip("/")

# Official install layout (from https://jimeng.jianying.com/cli install.sh)
DREAMINA_BASE = "https://lf3-static.bytednsdoc.com/obj/eden-cn/psj_hupthlyk/ljhwZthlaukjlkulzlp"
DREAMINA_HOME = Path.home() / ".dreamina_cli"


def out_dir(a) -> Path:
    d = Path(a.out_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


# =========================================================================== Gemini Omni
def gemini_headers() -> dict:
    return {"x-goog-api-key": require_cfg("GEMINI_API_KEY", "在 https://aistudio.google.com/apikey 获取。"),
            "Content-Type": "application/json"}


def omni_models() -> list[str]:
    models, token = [], None
    while True:
        params = {"pageSize": 1000, **({"pageToken": token} if token else {})}
        r = requests.get(f"{GEMINI_BASE}/v1beta/models", headers=gemini_headers(), params=params, timeout=60)
        if r.status_code != 200:
            raise MMError(f"Gemini models.list 失败 HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        models += [m["name"].split("/", 1)[-1] for m in data.get("models", [])]
        token = data.get("nextPageToken")
        if not token:
            break
    omni = [m for m in models if m.startswith("gemini-omni")]

    def key(m: str):
        v = re.search(r"gemini-omni-(\d+(?:\.\d+)*)", m)
        ver = tuple(int(x) for x in v.group(1).split(".")) if v else (0,)
        return (ver, 0 if "preview" in m else 1)
    return sorted(omni, key=key, reverse=True)


def pick_omni(a) -> str:
    if a.model or cfg("GEMINI_VIDEO_MODEL"):
        return a.model or cfg("GEMINI_VIDEO_MODEL")
    ranked = omni_models()
    if not ranked:
        raise MMError("models.list 中没有 gemini-omni-* 模型（账号可能无权限或地区不支持），可用 --model 指定。")
    return ranked[0]


def _image_part(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.is_file():
        raise MMError(f"图片不存在: {p}")
    return {"type": "image", "mime_type": mimetypes.guess_type(str(p))[0] or "image/png",
            "data": base64.b64encode(p.read_bytes()).decode()}


def _save_video_item(item: dict, dest: Path) -> Path:
    if item.get("data"):
        dest.write_bytes(base64.b64decode(item["data"]))
        return dest
    uri = item.get("uri")
    if not uri:
        raise MMError(f"返回内容里没有视频数据: {json.dumps(item)[:300]}")
    m = re.search(r"(files/[A-Za-z0-9_-]+)", uri)
    if m:  # Google-hosted file: wait until ACTIVE, then download with aria2c
        name = m.group(1)
        deadline = time.time() + 1800
        while True:
            r = requests.get(f"{GEMINI_BASE}/v1beta/{name}", headers=gemini_headers(), timeout=60)
            if r.status_code != 200:
                raise MMError(f"查询视频文件失败 HTTP {r.status_code}: {r.text[:300]}")
            if r.json().get("state") == "ACTIVE":
                break
            if r.json().get("state") == "FAILED" or time.time() > deadline:
                raise MMError(f"视频文件处理失败/超时: {r.text[:300]}")
            time.sleep(5)
        url = f"{GEMINI_BASE}/v1beta/{name}:download?alt=media"
    else:
        url = uri
    key = gemini_headers()["x-goog-api-key"]
    return aria2_download(url, dest, headers=[f"x-goog-api-key: {key}"])


def run_gemini(a) -> dict:
    from gemini_analyze import upload_file  # Files API resumable upload + poll ACTIVE
    model = pick_omni(a)
    print(f"[video] gemini model={model}", file=sys.stderr)
    parts: list[dict] = []
    if a.video:
        info = upload_file(gemini_headers()["x-goog-api-key"], Path(a.video).expanduser())
        parts.append({"type": "video", "uri": info["uri"], "mime_type": info.get("mimeType", "video/mp4")})
    for img in a.image or []:
        parts.append(_image_part(img))
    body: dict = {"model": model}
    if parts:
        body["input"] = parts + [{"type": "text", "text": a.prompt}]
    else:
        body["input"] = a.prompt
    delivery = a.delivery or ("uri" if a.resolution in ("1080p", "4k") else "base64")
    fmt = {"type": "video", "delivery": delivery}
    if a.aspect:
        fmt["aspect_ratio"] = a.aspect
    if a.resolution:
        fmt["resolution"] = a.resolution
    body["response_format"] = fmt
    if a.task:
        body["generation_config"] = {"video_config": {"task": a.task}}
    if a.previous_id:
        body["previous_interaction_id"] = a.previous_id

    t0 = time.time()
    r = requests.post(f"{GEMINI_BASE}/v1beta/interactions", headers=gemini_headers(), json=body, timeout=1800)
    if r.status_code != 200:
        raise MMError(f"Gemini Omni 请求失败 HTTP {r.status_code}: {r.text[:1500]}")
    data = r.json()
    # Defensive: if the interaction is still running, poll it until it reaches a terminal state.
    while data.get("status") in ("in_progress", "queued", "running") and data.get("id"):
        if time.time() - t0 > 1800:
            raise MMError(f"生成超时，interaction id={data.get('id')}")
        time.sleep(5)
        g = requests.get(f"{GEMINI_BASE}/v1beta/interactions/{data['id']}", headers=gemini_headers(), timeout=60)
        if g.status_code != 200:
            raise MMError(f"查询 interaction 失败 HTTP {g.status_code}: {g.text[:500]}")
        data = g.json()
    if data.get("status") not in (None, "completed"):
        raise MMError(f"生成未完成 status={data.get('status')}: {json.dumps(data, ensure_ascii=False)[:1500]}")

    videos, texts = [], []
    for step in data.get("steps", []):
        for c in step.get("content", []) or []:
            if c.get("type") == "video":
                videos.append(c)
            elif c.get("type") == "text" and c.get("text"):
                texts.append(c["text"])
    if not videos:
        raise MMError(f"响应中没有视频（可能被安全策略拦截）: {' '.join(texts)[:800] or json.dumps(data, ensure_ascii=False)[:800]}")
    stem = a.name or f"omni_{time.strftime('%Y%m%d_%H%M%S')}"
    files = [str(_save_video_item(v, out_dir(a) / f"{stem}_{i}.mp4")) for i, v in enumerate(videos, 1)]
    return {"provider": "gemini", "model": model, "interaction_id": data.get("id"), "files": files,
            "model_text": texts, "seconds": round(time.time() - t0, 1),
            "hint": "用 --previous-id <interaction_id> 继续对话式修改这段视频"}


# =========================================================================== 即梦 Dreamina CLI
def dreamina_bin(required: bool = True) -> str | None:
    recorded = DREAMINA_HOME / "mm_bin_path.txt"  # written by jimeng-install
    for cand in (cfg("DREAMINA_BIN"), recorded.read_text().strip() if recorded.is_file() else None,
                 shutil.which("dreamina"), str(Path.home() / ".local/bin/dreamina"), str(Path.home() / "bin/dreamina.exe")):
        if cand and Path(cand).is_file():
            return cand
    if required:
        raise MMError("未安装即梦 CLI。请运行: video_gen.py jimeng-install", EXIT_CONFIG)
    return None


def dreamina_platform() -> tuple[str, str]:
    sysname, arch = platform.system(), platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(arch)
    if sysname == "Darwin" and arch:
        return f"dreamina_cli_darwin_{arch}", "dreamina"
    if sysname == "Linux" and arch:
        return f"dreamina_cli_linux_{arch}", "dreamina"
    if sysname == "Windows" and arch == "amd64":
        return "dreamina_cli_windows_amd64.exe", "dreamina.exe"
    raise MMError(f"即梦 CLI 不支持当前平台: {sysname} {platform.machine()}")


def jimeng_install(a) -> dict:
    """Mirror the official install.sh with aria2c downloads, but without editing shell rc files."""
    remote_name, local_name = dreamina_platform()
    bindir = Path(a.bin_dir or cfg("DREAMINA_INSTALL_DIR") or (Path.home() / ("bin" if os.name == "nt" else ".local/bin")))
    bindir = bindir.expanduser().resolve()
    target = aria2_download(f"{DREAMINA_BASE}/dreamina_cli_beta/{remote_name}", bindir / local_name)
    target.chmod(0o755)
    if platform.system() == "Darwin":
        subprocess.run(["xattr", "-d", "com.apple.quarantine", str(target)], capture_output=True)
    aria2_download(f"{DREAMINA_BASE}/dreamina_cli_beta/SKILL.md", DREAMINA_HOME / "dreamina" / "SKILL.md")
    aria2_download(f"{DREAMINA_BASE}/version.json", DREAMINA_HOME / "version.json")
    (DREAMINA_HOME / "mm_bin_path.txt").write_text(str(target))
    ver = json.loads((DREAMINA_HOME / "version.json").read_text())
    on_path = shutil.which(local_name) == str(target)
    return {"installed": str(target), "version": ver.get("version"), "release_date": ver.get("release_date"),
            "release_notes": ver.get("release_notes"), "on_PATH": on_path,
            "note": None if on_path else f"{bindir} 不在 PATH 中；脚本会直接用绝对路径调用，也可手动加入 PATH。"}


def _dreamina(args: list[str], timeout: int | None = None) -> tuple[int, str]:
    p = subprocess.run([dreamina_bin(), *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


LOGIN_RX = re.compile(r"not\s+log(ged)?\s*in|login required|未登录|请先登录|unauthori[sz]ed|token (expired|invalid)|relogin", re.I)


def _parse(out: str) -> dict:
    """dreamina prints submit_id / gen_status / fail_reason; accept JSON or key=value text."""
    res: dict = {}
    for m in re.finditer(r"\{.*\}", out, re.S):
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                res.update(obj)
                break
        except json.JSONDecodeError:
            continue
    for k in ("submit_id", "gen_status", "fail_reason"):
        if k not in res:
            m = re.search(rf'"?{k}"?\s*[:=]\s*"?([^\s",}}]+)', out)
            if m:
                res[k] = m.group(1)
    urls = re.findall(r'https?://[^\s"\'<>]+?\.(?:mp4|mov|webm|png|jpe?g|webp)(?:\?[^\s"\'<>]*)?', out)
    if urls:
        res["_media_urls"] = list(dict.fromkeys(urls))
    return res


def _check_login_error(out: str) -> None:
    if LOGIN_RX.search(out):
        print("NEED_LOGIN: 即梦 CLI 未登录或登录已过期。请运行 video_gen.py jimeng-login，"
              "把输出的 verification_uri 和 user_code 告诉用户，在浏览器完成授权后再运行 "
              "video_gen.py jimeng-login --device-code <device_code>。", file=sys.stderr)
        sys.exit(EXIT_NEED_LOGIN)
    if "AigcComplianceConfirmationRequired" in out:
        raise MMError("即梦要求先在网页端完成该模型的一次性授权确认（AigcComplianceConfirmationRequired）。"
                      "请用户登录即梦网页版完成确认后重试。")


def jimeng_download(res: dict, a, submit_id: str) -> list[str]:
    """Prefer aria2c for result URLs; if the CLI output exposes none, use its own --download_dir."""
    d = out_dir(a)
    urls = res.get("_media_urls") or []
    files = []
    for i, u in enumerate(urls, 1):
        ext = Path(u.split("?")[0]).suffix or ".mp4"
        files.append(str(aria2_download(u, d / f"jimeng_{submit_id[:8]}_{i}{ext}")))
    if files:
        return files
    before = set(d.iterdir())
    code, out = _dreamina(["query_result", f"--submit_id={submit_id}", f"--download_dir={d}"], timeout=600)
    if code != 0:
        raise MMError(f"dreamina query_result 下载失败:\n{out[-1500:]}")
    return sorted(str(p) for p in set(d.iterdir()) - before)


def jimeng_finish(res: dict, a, out: str) -> dict:
    sid = res.get("submit_id")
    status = res.get("gen_status")
    result = {"provider": "jimeng", "submit_id": sid, "gen_status": status, "raw_tail": out[-800:]}
    if status == "success" and sid:
        result["files"] = jimeng_download(res, a, sid)
    elif status == "fail":
        raise MMError(f"即梦生成失败: {res.get('fail_reason') or out[-800:]}")
    elif sid:
        result["hint"] = f"任务已提交但尚未完成（gen_status={status}）。稍后运行: video_gen.py jimeng-result --submit-id {sid}"
    return result


def run_jimeng(a) -> dict:
    if a.subcommand not in ("text2video", "image2video", "frames2video", "multiframe2video", "multimodal2video",
                            "text2image", "image2image", "image_upscale"):
        raise MMError(f"不支持的 dreamina 子命令: {a.subcommand}")
    args = [a.subcommand]
    if a.prompt:
        args.append(f"--prompt={a.prompt}")
    if a.ratio:
        args.append(f"--ratio={a.ratio}")
    if a.session is not None:
        args.append(f"--session={a.session}")
    args += a.extra
    args.append(f"--poll={a.poll}")
    print(f"[video] dreamina {' '.join(args)}", file=sys.stderr)
    code, out = _dreamina(args, timeout=a.poll + 600)
    _check_login_error(out)
    res = _parse(out)
    if code != 0 and not res.get("submit_id"):
        raise MMError(f"dreamina 执行失败 (exit {code}):\n{out[-2000:]}\n提示：先运行 dreamina {a.subcommand} -h 核对参数。")
    return jimeng_finish(res, a, out)


def jimeng_result(a) -> dict:
    code, out = _dreamina(["query_result", f"--submit_id={a.submit_id}"], timeout=300)
    _check_login_error(out)
    res = _parse(out)
    res.setdefault("submit_id", a.submit_id)
    if code != 0 and not res.get("gen_status"):
        raise MMError(f"dreamina query_result 失败:\n{out[-1500:]}")
    return jimeng_finish(res, a, out)


def jimeng_login(a) -> dict:
    if a.device_code:
        code, out = _dreamina(["login", "checklogin", f"--device_code={a.device_code}", f"--poll={a.poll}"],
                              timeout=a.poll + 120)
        if code != 0:
            raise MMError(f"登录未完成:\n{out[-1500:]}")
        _, credit = _dreamina(["user_credit"], timeout=120)
        return {"login": "ok", "output": out[-1000:], "user_credit": credit[-800:]}
    code, out = _dreamina(["login", "--headless"], timeout=120)
    if code != 0:
        raise MMError(f"dreamina login --headless 失败:\n{out[-1500:]}")
    info = {k: v for k, v in _parse(out).items() if not k.startswith("_")}
    for k in ("verification_uri", "user_code", "device_code"):
        m = re.search(rf'"?{k}"?\s*[:=]\s*"?([^\s",}}]+)', out)
        if m:
            info[k] = m.group(1)
    info["next"] = ("请用户打开 verification_uri 并输入 user_code 完成授权，然后运行 "
                    "video_gen.py jimeng-login --device-code <device_code>")
    info["raw"] = out[-1500:]
    return info


# =========================================================================== CLI
def choose_provider() -> str:
    p = (cfg("VIDEO_PROVIDER") or "auto").lower()
    if p != "auto":
        return p
    if cfg("GEMINI_API_KEY"):
        return "gemini"
    if dreamina_bin(required=False):
        return "jimeng"
    raise MMError("没有可用的生视频服务：请配置 GEMINI_API_KEY，或运行 video_gen.py jimeng-install 安装即梦 CLI。", EXIT_CONFIG)


@main_guard
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def outputs(p):
        p.add_argument("-d", "--out-dir", default="videos")
        p.add_argument("--name", help="output file stem")

    p = sub.add_parser("auto", help="pick provider from VIDEO_PROVIDER (text-to-video only)")
    p.add_argument("prompt")
    p.add_argument("--aspect", choices=["16:9", "9:16"])
    outputs(p)

    p = sub.add_parser("gemini", help="Gemini Omni")
    p.add_argument("prompt")
    p.add_argument("--model", help="force model id (default: newest gemini-omni-* from models.list)")
    p.add_argument("--image", action="append", help="image input, repeatable (1=animate, 2=first/last or references)")
    p.add_argument("--video", help="video to edit / extend (uploaded via Files API, <=10s)")
    p.add_argument("--previous-id", help="previous interaction id for multi-turn editing")
    p.add_argument("--task", choices=["text_to_video", "image_to_video", "reference_to_video", "edit", "extend"])
    p.add_argument("--aspect", choices=["16:9", "9:16"])
    p.add_argument("--resolution", choices=["360p", "720p", "1080p", "4k"])
    p.add_argument("--delivery", choices=["base64", "uri"])
    p.add_argument("--list-models", action="store_true")
    outputs(p)

    p = sub.add_parser("jimeng-install", help="install / update the official dreamina CLI via aria2c")
    p.add_argument("--bin-dir")

    p = sub.add_parser("jimeng-login", help="headless OAuth device flow")
    p.add_argument("--device-code")
    p.add_argument("--poll", type=int, default=120)

    p = sub.add_parser("jimeng", help="run a dreamina generator command")
    p.add_argument("subcommand", help="text2video / image2video / frames2video / multiframe2video / multimodal2video ...")
    p.add_argument("--prompt")
    p.add_argument("--ratio", help="e.g. 16:9 / 9:16 (omit for seedance2.5 image2video/frames2video)")
    p.add_argument("--session", type=int)
    p.add_argument("--poll", type=int, default=600, help="seconds to wait for a terminal result")
    outputs(p)

    p = sub.add_parser("jimeng-result", help="query (and download) a dreamina task")
    p.add_argument("--submit-id", required=True)
    outputs(p)

    argv = sys.argv[1:]
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    a.extra = extra

    if a.cmd == "gemini" and a.list_models:
        print("\n".join(omni_models()))
        return
    if a.cmd == "auto":
        prov = choose_provider()
        if prov == "gemini":
            a.model = a.image = a.video = a.previous_id = a.task = a.resolution = a.delivery = None
            res = run_gemini(a)
        else:
            a.subcommand, a.ratio, a.session, a.poll = "text2video", a.aspect, None, 600
            res = run_jimeng(a)
    else:
        res = {"gemini": run_gemini, "jimeng-install": jimeng_install, "jimeng-login": jimeng_login,
               "jimeng": run_jimeng, "jimeng-result": jimeng_result}[a.cmd](a)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
