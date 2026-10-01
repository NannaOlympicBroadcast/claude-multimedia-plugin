#!/usr/bin/env python3
"""Analyze video / audio with the newest Gemini model (auto-discovered via models.list).

Inputs can be local files (uploaded through the Gemini Files API, polled until ACTIVE)
or public YouTube URLs (passed natively as file_uri). For other sites, download first
with ytdlp_download.py.

Examples
  gemini_analyze.py video.mp4 --preset summary
  gemini_analyze.py "https://www.youtube.com/watch?v=xxxx" --preset scenes -o scenes.md
  gemini_analyze.py talk.mp3 --preset transcript --lang en
  gemini_analyze.py clip.mp4 --prompt "这段视频里出现了哪些品牌？" --start 60 --end 120 --fps 2
  gemini_analyze.py --list-models
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mmcommon import MMError, cfg, main_guard, parse_ts, require_cfg  # noqa: E402

BASE = (cfg("GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com").rstrip("/")
YOUTUBE_RE = re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", re.I)

# Model names that are NOT general multimodal understanding models.
EXCLUDE = ("tts", "live", "image", "transcribe", "embedding", "robotics", "native-audio",
           "computer-use", "omni", "deep-research", "antigravity", "veo", "lyria", "imagen",
           "aqa", "learnlm", "gemma", "nano")
MODEL_RE = re.compile(r"^models/gemini-(\d+(?:\.\d+)?)-(pro|flash)(-lite)?(?:-(.*))?$")

PRESETS = {
    "summary": (
        "请完整观看/收听这段媒体，用{lang}输出：\n"
        "1. 一句话概括\n2. 详细内容摘要（分点，带 [mm:ss] 时间戳）\n"
        "3. 关键人物/实体/品牌\n4. 关键结论或观点\n5. 值得剪辑的高光片段（起止时间 + 理由）"
    ),
    "transcript": (
        "请逐字转写这段媒体中的全部语音，用原语言输出，每段前标注 [hh:mm:ss] 时间戳和说话人（说话人1/说话人2...）。"
        "若有非语音的重要声音（音乐、掌声等）用方括号注明。最后用{lang}附上内容摘要。"
    ),
    "scenes": (
        "请把这段视频按镜头/场景拆分，用{lang}输出 Markdown 表格：开始时间 | 结束时间 | 画面描述 | 人物/动作 | "
        "画面中的文字 | 声音/对白要点。时间格式 mm:ss。"
    ),
    "highlights": (
        "请找出这段视频中最适合做短视频的 5-10 个高光片段，用{lang}输出 JSON 数组，"
        "每项包含 start(秒), end(秒), title, reason, suggested_caption。只输出 JSON。"
    ),
    "audio": (
        "请分析这段音频，用{lang}输出：音频类型（语音/音乐/混合）、说话人数与情绪、语速、背景音、"
        "若是音乐则描述风格、乐器、节奏、情绪、结构（带时间戳），以及音质问题。"
    ),
    "ocr": "请提取视频中出现的所有屏幕文字（字幕、标题、PPT、招牌等），带 [mm:ss] 时间戳，按时间排序，用原文输出。",
}


def headers(api_key: str) -> dict:
    return {"x-goog-api-key": api_key}


def list_models(api_key: str) -> list[dict]:
    models, token = [], None
    while True:
        params = {"pageSize": 1000}
        if token:
            params["pageToken"] = token
        r = requests.get(f"{BASE}/v1beta/models", headers=headers(api_key), params=params, timeout=60)
        if r.status_code != 200:
            raise MMError(f"Gemini models.list 失败 HTTP {r.status_code}: {r.text[:800]}")
        data = r.json()
        models += data.get("models", [])
        token = data.get("nextPageToken")
        if not token:
            return models


def rank_models(models: list[dict], prefer: str) -> list[tuple[tuple, str]]:
    ranked = []
    for m in models:
        name = m.get("name", "")
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        low = name.lower()
        if any(x in low for x in EXCLUDE):
            continue
        mm = MODEL_RE.match(name)
        if not mm:
            continue
        ver = tuple(int(x) for x in mm.group(1).split("."))
        tier, lite, suffix = mm.group(2), bool(mm.group(3)), (mm.group(4) or "")
        if lite and prefer != "lite":
            continue
        if suffix and not suffix.startswith("preview"):
            continue  # skip exp / dated oddities, keep "-preview*"
        stable = 0 if suffix else 1
        tier_score = {"pro": 1, "flash": 0}[tier]
        if prefer == "lite":
            key = (int(lite), ver, stable, tier_score)
        elif prefer == "pro":
            key = (tier_score, ver, stable)
        elif prefer == "flash":
            key = (1 - tier_score, ver, stable)
        else:  # latest: newest version first, stable over preview, pro over flash on ties
            key = (ver, stable, tier_score)
        ranked.append((key, name.split("/", 1)[1]))
    ranked.sort(reverse=True)
    return ranked


def pick_model(api_key: str, prefer: str) -> str:
    override = cfg("GEMINI_MODEL")
    if override:
        return override
    ranked = rank_models(list_models(api_key), prefer)
    if not ranked:
        raise MMError("models.list 中没有找到可用于多模态理解的 gemini-*-pro/flash 模型，请设置 GEMINI_MODEL。")
    return ranked[0][1]


def upload_file(api_key: str, path: Path) -> dict:
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    size = path.stat().st_size
    start = requests.post(
        f"{BASE}/upload/v1beta/files",
        headers={
            **headers(api_key),
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
        json={"file": {"display_name": path.name[:120]}},
        timeout=60,
    )
    upload_url = start.headers.get("x-goog-upload-url")
    if start.status_code != 200 or not upload_url:
        raise MMError(f"Files API 初始化上传失败 HTTP {start.status_code}: {start.text[:800]}")
    print(f"[gemini] 上传 {path.name} ({size/1e6:.1f} MB, {mime}) ...", file=sys.stderr)
    with path.open("rb") as fh:
        up = requests.post(
            upload_url,
            headers={"X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize",
                     "Content-Length": str(size)},
            data=fh,
            timeout=3600,
        )
    if up.status_code != 200:
        raise MMError(f"Files API 上传失败 HTTP {up.status_code}: {up.text[:800]}")
    info = up.json()["file"]
    # Poll until ACTIVE (video processing can take a while).
    deadline = time.time() + 1800
    while info.get("state") == "PROCESSING":
        if time.time() > deadline:
            raise MMError(f"文件处理超时: {info.get('name')}")
        time.sleep(5)
        g = requests.get(f"{BASE}/v1beta/{info['name']}", headers=headers(api_key), timeout=60)
        if g.status_code != 200:
            raise MMError(f"查询文件状态失败 HTTP {g.status_code}: {g.text[:500]}")
        info = g.json()
    if info.get("state") != "ACTIVE":
        raise MMError(f"文件处理失败: {json.dumps(info, ensure_ascii=False)[:800]}")
    print(f"[gemini] 文件就绪: {info['name']}", file=sys.stderr)
    return info


def delete_file(api_key: str, name: str) -> None:
    requests.delete(f"{BASE}/v1beta/{name}", headers=headers(api_key), timeout=30)


@main_guard
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="*", help="local media files or YouTube URLs (max 10)")
    ap.add_argument("--preset", choices=sorted(PRESETS), default=None)
    ap.add_argument("-p", "--prompt", help="custom prompt (combined with preset if both given)")
    ap.add_argument("--lang", default="中文", help="output language for presets (default 中文)")
    ap.add_argument("--model", help="force a model id (otherwise newest is auto-selected)")
    ap.add_argument("--prefer", choices=["latest", "pro", "flash", "lite"], default=None,
                    help="model ranking preference (default GEMINI_PREFER or latest)")
    ap.add_argument("--start", help="clip start (sec or hh:mm:ss) — applies to every video input")
    ap.add_argument("--end", help="clip end")
    ap.add_argument("--fps", type=float, help="frame sampling rate (default model default ~1fps)")
    ap.add_argument("--grounding", action="store_true", help="enable Google Search grounding tool")
    ap.add_argument("--json", action="store_true", help="ask for application/json output")
    ap.add_argument("-o", "--output", help="write result to this file")
    ap.add_argument("--keep-files", action="store_true", help="don't delete uploaded files afterwards")
    ap.add_argument("--list-models", action="store_true", help="show ranked candidate models and exit")
    a = ap.parse_args()

    api_key = require_cfg("GEMINI_API_KEY", "在 https://aistudio.google.com/apikey 获取。")
    prefer = a.prefer or cfg("GEMINI_PREFER", "latest")

    if a.list_models:
        for key, name in rank_models(list_models(api_key), prefer):
            print(f"{name}\t{key}")
        return
    if not a.inputs:
        ap.error("need at least one input")
    if len(a.inputs) > 10:
        ap.error("Gemini 单次请求最多 10 个视频")

    model = a.model or pick_model(api_key, prefer)
    print(f"[gemini] 使用模型: {model}", file=sys.stderr)

    vmeta = {}
    if a.start is not None:
        vmeta["startOffset"] = f"{parse_ts(a.start):.3f}s"
    if a.end is not None:
        vmeta["endOffset"] = f"{parse_ts(a.end):.3f}s"
    if a.fps:
        vmeta["fps"] = a.fps

    parts, uploaded = [], []
    try:
        for item in a.inputs:
            if YOUTUBE_RE.match(item):
                part = {"fileData": {"fileUri": item}}
            elif re.match(r"^https?://", item):
                raise MMError(f"非 YouTube 链接请先用 ytdlp_download.py 下载到本地再分析: {item}")
            else:
                p = Path(item).expanduser()
                if not p.is_file():
                    raise MMError(f"文件不存在: {p}")
                info = upload_file(api_key, p)
                uploaded.append(info["name"])
                part = {"fileData": {"mimeType": info.get("mimeType"), "fileUri": info["uri"]}}
            if vmeta:
                part["videoMetadata"] = vmeta
            parts.append(part)

        prompt_bits = []
        if a.preset:
            prompt_bits.append(PRESETS[a.preset].format(lang=a.lang))
        if a.prompt:
            prompt_bits.append(a.prompt)
        if not prompt_bits:
            prompt_bits.append(PRESETS["summary"].format(lang=a.lang))
        parts.append({"text": "\n\n".join(prompt_bits)})

        body: dict = {"contents": [{"role": "user", "parts": parts}]}
        if a.json:
            body["generationConfig"] = {"responseMimeType": "application/json"}
        if a.grounding:
            body["tools"] = [{"googleSearch": {}}]

        r = requests.post(f"{BASE}/v1beta/models/{model}:generateContent",
                          headers=headers(api_key), json=body, timeout=1800)
        if r.status_code != 200:
            raise MMError(f"generateContent 失败 HTTP {r.status_code}: {r.text[:1500]}")
        data = r.json()
        cands = data.get("candidates") or []
        if not cands:
            raise MMError(f"模型未返回内容: {json.dumps(data.get('promptFeedback', data), ensure_ascii=False)[:800]}")
        text = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []) if not p.get("thought"))
        finish = cands[0].get("finishReason")
        if finish and finish not in ("STOP", "MAX_TOKENS"):
            print(f"[gemini] finishReason={finish}", file=sys.stderr)
        usage = data.get("usageMetadata", {})
        print(f"[gemini] tokens: prompt={usage.get('promptTokenCount')} output={usage.get('candidatesTokenCount')}",
              file=sys.stderr)
    finally:
        if not a.keep_files:
            for name in uploaded:
                delete_file(api_key, name)

    header = f"<!-- model: {model} -->\n"
    if a.output:
        Path(a.output).expanduser().write_text(header + text, encoding="utf-8")
        print(f"[OK] 已写入 {a.output}", file=sys.stderr)
    print(text)


if __name__ == "__main__":
    main()
