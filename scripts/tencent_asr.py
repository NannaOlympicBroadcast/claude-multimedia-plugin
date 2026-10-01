#!/usr/bin/env python3
"""Transcribe audio/video with Tencent Cloud ASR (录音文件识别 CreateRecTask + DescribeTaskStatus).

- Local files are converted with ffmpeg to 16 kHz mono MP3 and sent as base64 (SourceType=1).
  The API caps base64 payloads at 5 MB, so long media is split into chunks and the
  timestamps are re-offset when merging.
- A public URL can be passed with --url (SourceType=0, no size limit).
- Outputs: .srt, .txt, .json next to --output-prefix.

Auth: TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY (TC3-HMAC-SHA256 signing).

Examples
  tencent_asr.py interview.mp4 -o out/interview
  tencent_asr.py meeting.m4a --engine 16k_zh_en_meeting --speakers -o out/meeting
  tencent_asr.py --url https://example.com/a.mp3 -o out/a
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mmcommon import MMError, cfg, fmt_ts, main_guard, media_duration, require_bin, require_cfg, run  # noqa: E402

HOST = "asr.tencentcloudapi.com"
SERVICE = "asr"
VERSION = "2019-06-14"
MAX_B64 = 5 * 1024 * 1024
BITRATE_K = 32          # 16k mono speech @32 kbps -> ~240 KB/min
CHUNK_SEC = 900         # 15 min -> ~3.6 MB raw -> ~4.8 MB base64 (just under 5 MB)


# --------------------------------------------------------------------------- TC3 signing
def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def call(action: str, payload: dict, region: str = "") -> dict:
    sid = require_cfg("TENCENTCLOUD_SECRET_ID", "在 https://console.cloud.tencent.com/cam/capi 创建。")
    skey = require_cfg("TENCENTCLOUD_SECRET_KEY")
    token = cfg("TENCENTCLOUD_SESSION_TOKEN")
    ts = int(time.time())
    date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    ct = "application/json; charset=utf-8"
    canonical = "\n".join([
        "POST", "/", "",
        f"content-type:{ct}\nhost:{HOST}\nx-tc-action:{action.lower()}\n",
        "content-type;host;x-tc-action",
        hashlib.sha256(body.encode("utf-8")).hexdigest(),
    ])
    scope = f"{date}/{SERVICE}/tc3_request"
    to_sign = "\n".join(["TC3-HMAC-SHA256", str(ts), scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    k = _sign(_sign(_sign(("TC3" + skey).encode("utf-8"), date), SERVICE), "tc3_request")
    signature = hmac.new(k, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "Authorization": f"TC3-HMAC-SHA256 Credential={sid}/{scope}, SignedHeaders=content-type;host;x-tc-action, Signature={signature}",
        "Content-Type": ct,
        "Host": HOST,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(ts),
        "X-TC-Version": VERSION,
    }
    if region:
        headers["X-TC-Region"] = region
    if token:
        headers["X-TC-Token"] = token
    r = requests.post(f"https://{HOST}", headers=headers, data=body.encode("utf-8"), timeout=120)
    if r.status_code != 200:
        raise MMError(f"腾讯云 {action} HTTP {r.status_code}: {r.text[:800]}")
    resp = r.json().get("Response", {})
    if "Error" in resp:
        e = resp["Error"]
        raise MMError(f"腾讯云 {action} 错误 {e.get('Code')}: {e.get('Message')} (RequestId {resp.get('RequestId')})")
    return resp


# --------------------------------------------------------------------------- task flow
def create_task(a: argparse.Namespace, *, url: str | None = None, data_b64: str | None = None, size: int = 0) -> int:
    payload = {
        "EngineModelType": a.engine,
        "ChannelNum": 1,
        "ResTextFormat": a.res_format,
        "SourceType": 0 if url else 1,
    }
    if url:
        payload["Url"] = url
    else:
        payload["Data"] = data_b64
        payload["DataLen"] = size
    if a.speakers:
        payload["SpeakerDiarization"] = 1
        if a.speaker_number is not None:
            payload["SpeakerNumber"] = a.speaker_number
    if a.hotword_id:
        payload["HotwordId"] = a.hotword_id
    if a.filter_modal is not None:
        payload["FilterModal"] = a.filter_modal
    if a.convert_num is not None:
        payload["ConvertNumMode"] = a.convert_num
    resp = call("CreateRecTask", payload, a.region)
    return int(resp["Data"]["TaskId"])


def wait_task(task_id: int, region: str, timeout: int = 7200) -> dict:
    deadline = time.time() + timeout
    delay = 3
    while True:
        data = call("DescribeTaskStatus", {"TaskId": task_id}, region)["Data"]
        st = data.get("Status")
        if st == 2:
            return data
        if st == 3:
            raise MMError(f"识别任务 {task_id} 失败: {data.get('ErrorMsg')}")
        if time.time() > deadline:
            raise MMError(f"识别任务 {task_id} 超时")
        print(f"[asr] task {task_id} {data.get('StatusStr')} ...", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 1.5, 15)


def to_segments(data: dict, offset: float) -> list[dict]:
    segs = []
    for d in data.get("ResultDetail") or []:
        text = (d.get("FinalSentence") or "").strip()
        if not text:
            continue
        segs.append({
            "start": offset + d.get("StartMs", 0) / 1000.0,
            "end": offset + d.get("EndMs", 0) / 1000.0,
            "speaker": d.get("SpeakerId"),
            "text": text,
        })
    if not segs and data.get("Result"):
        # ResTextFormat=0 returns only "[0:0.020,0:1.420]  text" lines; keep them as one block.
        segs.append({"start": offset, "end": offset, "speaker": None, "text": data["Result"].strip()})
    return segs


def write_outputs(segs: list[dict], prefix: Path, meta: dict) -> None:
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


@main_guard
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="local audio/video file")
    ap.add_argument("--url", help="public audio URL instead of a local file")
    ap.add_argument("-o", "--output-prefix", help="output path prefix (default: next to input)")
    ap.add_argument("--engine", default=cfg("TENCENT_ASR_ENGINE", "16k_zh_en_2.0"),
                    help="EngineModelType, e.g. 16k_zh_en_2.0 (大模型2.0) / 16k_zh_en_meeting / 16k_zh_en / 16k_multi_lang / 16k_en / 16k_ja")
    ap.add_argument("--res-format", type=int, default=2, help="ResTextFormat 0-5 (default 2: 词级时间戳+标点分段)")
    ap.add_argument("--speakers", action="store_true", help="enable SpeakerDiarization")
    ap.add_argument("--speaker-number", type=int, help="expected speaker count (0=auto)")
    ap.add_argument("--hotword-id", help="HotwordId")
    ap.add_argument("--filter-modal", type=int, choices=[0, 1, 2], help="语气词过滤 0/1/2")
    ap.add_argument("--convert-num", type=int, choices=[0, 1, 3], help="ConvertNumMode")
    ap.add_argument("--region", default=cfg("TENCENT_ASR_REGION", ""), help="X-TC-Region (optional)")
    ap.add_argument("--chunk-sec", type=int, default=CHUNK_SEC, help="chunk length for base64 upload")
    a = ap.parse_args()

    if not a.input and not a.url:
        ap.error("need an input file or --url")
    segs: list[dict] = []
    meta = {"engine": a.engine, "speakers": a.speakers, "source": a.url or a.input}

    if a.url:
        prefix = Path(a.output_prefix or "transcript").expanduser()
        tid = create_task(a, url=a.url)
        print(f"[asr] TaskId {tid}", file=sys.stderr)
        segs = to_segments(wait_task(tid, a.region), 0.0)
    else:
        src = Path(a.input).expanduser().resolve()
        if not src.is_file():
            raise MMError(f"文件不存在: {src}")
        prefix = Path(a.output_prefix).expanduser() if a.output_prefix else src.with_suffix("")
        require_bin("ffmpeg")
        total = media_duration(src)
        meta["duration"] = total
        with tempfile.TemporaryDirectory() as td:
            starts = [0.0]
            while starts[-1] + a.chunk_sec < total:
                starts.append(starts[-1] + a.chunk_sec)
            for idx, st in enumerate(starts):
                chunk = Path(td) / f"chunk{idx:03d}.mp3"
                run(["ffmpeg", "-v", "error", "-y", "-ss", f"{st:.3f}", "-t", str(a.chunk_sec), "-i", str(src),
                     "-vn", "-ac", "1", "-ar", "16000", "-b:a", f"{BITRATE_K}k", str(chunk)], capture=True)
                raw = chunk.read_bytes()
                b64 = base64.b64encode(raw).decode()
                if len(b64) > MAX_B64:
                    raise MMError(f"分片 base64 超过 5MB ({len(b64)} B)，请调小 --chunk-sec 或使用 --url")
                tid = create_task(a, data_b64=b64, size=len(raw))
                print(f"[asr] chunk {idx+1}/{len(starts)} @ {fmt_ts(st)} -> TaskId {tid}", file=sys.stderr)
                segs += to_segments(wait_task(tid, a.region), st)

    write_outputs(segs, prefix, meta)
    print(json.dumps({"srt": str(prefix.with_suffix('.srt')), "txt": str(prefix.with_suffix('.txt')),
                      "json": str(prefix.with_suffix('.json')), "segments": len(segs)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
