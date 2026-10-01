#!/usr/bin/env python3
"""Voice-over / dubbing with Microsoft Edge TTS (edge-tts).

Modes
  text   : --text "..." | --text-file a.txt         -> one audio file (+ .srt)
  srt    : --srt subs.srt                            -> timeline-aligned dub track; each cue is
           synthesized and time-fitted (atempo) into its slot so it stays in sync
  video  : --srt subs.srt --video in.mp4             -> also mux the dub into the video
           (--mix replace | duck | mix)
  voices : --list-voices [--locale zh-CN]

Examples
  edge_tts_dub.py --text "大家好，欢迎收看" -v zh-CN-XiaoxiaoNeural -o hello.mp3
  edge_tts_dub.py --srt zh.srt -v zh-CN-YunxiNeural -o dub.wav
  edge_tts_dub.py --srt zh.srt --video in.mp4 --mix duck -o dubbed.mp4
  edge_tts_dub.py --list-voices --locale zh-CN
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mmcommon import (  # noqa: E402
    MMError, cfg, ffprobe_json, main_guard, media_duration, parse_ts, require_bin, run,
)

try:
    import edge_tts
except ImportError as e:  # pragma: no cover
    raise SystemExit("[ERROR] 缺少 edge-tts：pip install -U edge-tts") from e

SR = 24000


def proxy() -> str | None:
    return cfg("EDGE_TTS_PROXY") or cfg("HTTPS_PROXY") or cfg("https_proxy")


async def synth(text: str, voice: str, out: Path, rate: str, volume: str, pitch: str, srt: Path | None = None) -> None:
    com = edge_tts.Communicate(text, voice, rate=rate, volume=volume, pitch=pitch, proxy=proxy())
    sub = edge_tts.SubMaker()
    with out.open("wb") as fh:
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                fh.write(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                sub.feed(chunk)
    if out.stat().st_size == 0:
        raise MMError(f"edge-tts 未返回音频（voice={voice}）。请检查网络或 voice 名称。")
    if srt:
        srt.write_text(sub.get_srt(), encoding="utf-8")


def parse_srt(path: Path) -> list[dict]:
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip())
    cues = []
    for b in blocks:
        lines = [l for l in b.split("\n") if l.strip()]
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        st, en = [x.strip().split(" ")[0].replace(",", ".") for x in lines[ti].split("-->")]
        text = " ".join(lines[ti + 1:])
        text = re.sub(r"<[^>]+>|\{[^}]+\}", "", text)          # strip tags
        text = re.sub(r"^\[S?\d+\]\s*", "", text).strip()       # strip speaker labels like [S1]
        if text:
            cues.append({"start": parse_ts(st), "end": parse_ts(en), "text": text})
    return cues


def atempo_chain(factor: float) -> str:
    parts = []
    while factor > 2.0:
        parts.append("atempo=2.0")
        factor /= 2.0
    while factor < 0.5:
        parts.append("atempo=0.5")
        factor /= 0.5
    parts.append(f"atempo={factor:.4f}")
    return ",".join(parts)


async def dub_srt(a: argparse.Namespace, out_wav: Path) -> dict:
    import numpy as np
    import soundfile as sf

    cues = parse_srt(Path(a.srt))
    if not cues:
        raise MMError("SRT 中没有可用字幕")
    sem = asyncio.Semaphore(a.concurrency)
    report = {"cues": len(cues), "sped_up": 0, "overflow": []}
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)

        async def one(i: int, c: dict) -> None:
            async with sem:
                await synth(c["text"], a.voice, tdp / f"{i:05d}.mp3", a.rate, a.volume, a.pitch)

        await asyncio.gather(*(one(i, c) for i, c in enumerate(cues)))

        total = max(c["end"] for c in cues)
        if a.video:
            total = max(total, media_duration(a.video))
        track = np.zeros(int((total + 5) * SR), dtype=np.float32)
        for i, c in enumerate(cues):
            mp3 = tdp / f"{i:05d}.mp3"
            slot = max(0.05, c["end"] - c["start"])
            dur = media_duration(mp3)
            filt = []
            if dur > slot * 1.02:
                factor = min(dur / slot, a.max_speed)
                filt.append(atempo_chain(factor))
                report["sped_up"] += 1
                if dur / factor > slot + 0.05:
                    report["overflow"].append({"index": i + 1, "start": c["start"], "over_sec": round(dur / factor - slot, 2)})
            wav = tdp / f"{i:05d}.wav"
            cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(mp3), "-ac", "1", "-ar", str(SR)]
            if filt:
                cmd += ["-af", ",".join(filt)]
            run(cmd + [str(wav)], capture=True)
            data, _ = sf.read(str(wav), dtype="float32")
            s = int(c["start"] * SR)
            e = min(len(track), s + len(data))
            track[s:e] += data[: e - s]
        peak = float(np.max(np.abs(track))) or 1.0
        if peak > 0.99:
            track /= peak / 0.99
        end = int((total) * SR)
        sf.write(str(out_wav), track[:end], SR)
    return report


def mux(video: Path, dub: Path, out: Path, mode: str, duck_db: float) -> None:
    has_audio = any(s.get("codec_type") == "audio" for s in ffprobe_json(video).get("streams", []))
    if not has_audio:
        mode = "replace"
    if mode == "replace":
        fc = None
        maps = ["-map", "0:v", "-map", "1:a"]
    elif mode == "duck":
        # Lower original audio while the dub is speaking (sidechain compression).
        fc = (f"[0:a]volume={duck_db}dB[bg];[1:a]asplit=2[d1][d2];"
              "[bg][d1]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=300[bgd];"
              "[bgd][d2]amix=inputs=2:normalize=0:duration=first[aout]")
        maps = ["-map", "0:v", "-map", "[aout]"]
    else:  # mix
        fc = f"[0:a]volume={duck_db}dB[bg];[bg][1:a]amix=inputs=2:normalize=0:duration=first[aout]"
        maps = ["-map", "0:v", "-map", "[aout]"]
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-i", str(dub)]
    if fc:
        cmd += ["-filter_complex", fc]
    cmd += maps + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)]
    run(cmd, capture=True)


@main_guard
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--text")
    src.add_argument("--text-file")
    src.add_argument("--srt")
    src.add_argument("--list-voices", action="store_true")
    ap.add_argument("--locale", help="filter voices, e.g. zh-CN / en-US / ja-JP")
    ap.add_argument("-v", "--voice", default=cfg("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural"))
    ap.add_argument("--rate", default="+0%", help='e.g. "+10%%" / "-5%%"')
    ap.add_argument("--volume", default="+0%")
    ap.add_argument("--pitch", default="+0Hz")
    ap.add_argument("--video", help="mux dub into this video (srt mode)")
    ap.add_argument("--mix", choices=["replace", "duck", "mix"], default="duck")
    ap.add_argument("--bg-db", type=float, default=-12.0, help="original audio gain for duck/mix (dB)")
    ap.add_argument("--max-speed", type=float, default=1.6, help="max atempo speed-up to fit a cue")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("-o", "--output", help="output file (.mp3/.wav, or .mp4 with --video)")
    a = ap.parse_args()

    if a.list_voices:
        voices = asyncio.run(edge_tts.list_voices(proxy=proxy()))
        for v in sorted(voices, key=lambda x: x["ShortName"]):
            if a.locale and not v["Locale"].lower().startswith(a.locale.lower()):
                continue
            tags = ",".join(v.get("VoiceTag", {}).get("VoicePersonalities", []) or [])
            print(f"{v['ShortName']}\t{v['Gender']}\t{v['Locale']}\t{tags}")
        return

    require_bin("ffmpeg")
    if a.text or a.text_file:
        text = a.text if a.text else Path(a.text_file).read_text(encoding="utf-8")
        out = Path(a.output or "tts.mp3").expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        mp3 = out if out.suffix.lower() == ".mp3" else out.with_suffix(".tmp.mp3")
        asyncio.run(synth(text, a.voice, mp3, a.rate, a.volume, a.pitch, srt=out.with_suffix(".srt")))
        if mp3 != out:
            run(["ffmpeg", "-v", "error", "-y", "-i", str(mp3), str(out)], capture=True)
            mp3.unlink()
        print(json.dumps({"audio": str(out), "srt": str(out.with_suffix('.srt')),
                          "duration": round(media_duration(out), 2), "voice": a.voice}, ensure_ascii=False, indent=2))
        return

    if a.srt:
        out = Path(a.output or ("dubbed.mp4" if a.video else "dub.wav")).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        dub_wav = out.with_suffix(".dub.wav") if a.video else out.with_suffix(".wav")
        report = asyncio.run(dub_srt(a, dub_wav))
        result = {"dub_track": str(dub_wav), **report}
        if a.video:
            mux(Path(a.video), dub_wav, out, a.mix, a.bg_db)
            result["video"] = str(out)
        elif out.suffix.lower() != ".wav":
            run(["ffmpeg", "-v", "error", "-y", "-i", str(dub_wav), str(out)], capture=True)
            result["audio"] = str(out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    ap.error("need --text, --text-file, --srt or --list-voices")


if __name__ == "__main__":
    main()
