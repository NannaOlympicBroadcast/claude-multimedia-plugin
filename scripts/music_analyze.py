#!/usr/bin/env python3
"""Music analysis with librosa.

Reports: duration, tempo (BPM) + beat grid, key/mode (Krumhansl-Schmuckler on chroma_cqt),
loudness (RMS dBFS, dynamic range), spectral features (centroid / bandwidth / rolloff / flatness),
harmonic-percussive balance, onset density, structural sections (agglomerative segmentation),
energy curve. Video files are accepted (audio is extracted with ffmpeg).

Examples
  music_analyze.py song.mp3
  music_analyze.py song.flac --sections 8 --plot -o report     # report.json + report.md (+ report.png)
  music_analyze.py clip.mp4 --start 30 --duration 60
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mmcommon import MMError, fmt_ts, main_guard, parse_ts, require_bin, run  # noqa: E402

try:
    import librosa
except ImportError as e:  # pragma: no cover
    raise SystemExit("[ERROR] 缺少 librosa：pip install -U librosa soundfile") from e

KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Krumhansl-Kessler key profiles
MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".m4v", ".ts"}


def estimate_key(chroma: np.ndarray) -> dict:
    prof = chroma.mean(axis=1)
    scores = []
    for i in range(12):
        for mode, tmpl in (("major", MAJOR), ("minor", MINOR)):
            r = np.corrcoef(prof, np.roll(tmpl, i))[0, 1]
            scores.append((float(r), KEYS[i], mode))
    scores.sort(reverse=True)
    best, second = scores[0], scores[1]
    return {"key": best[1], "mode": best[2], "confidence": round(best[0], 3),
            "runner_up": f"{second[1]} {second[2]} ({second[0]:.3f})"}


def db(x: np.ndarray) -> np.ndarray:
    return 20 * np.log10(np.maximum(x, 1e-10))


def analyze(path: Path, sr: int, n_sections: int, offset: float, duration: float | None) -> tuple[dict, dict]:
    y, sr = librosa.load(str(path), sr=sr, mono=True, offset=offset, duration=duration)
    if y.size == 0:
        raise MMError("音频为空")
    dur = len(y) / sr
    hop = 512

    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, hop_length=hop)
    tempo = float(np.atleast_1d(tempo)[0])
    beat_times = librosa.frames_to_time(beats, sr=sr, hop_length=hop)
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, hop_length=hop, units="time")
    dtempo = librosa.feature.tempo(onset_envelope=onset_env, sr=sr, hop_length=hop, aggregate=None)

    y_h, y_p = librosa.effects.hpss(y)
    chroma = librosa.feature.chroma_cqt(y=y_h, sr=sr, hop_length=hop)
    key = estimate_key(chroma)

    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    rms_db = db(rms)
    cent = librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop)[0]
    bw = librosa.feature.spectral_bandwidth(y=y, sr=sr, hop_length=hop)[0]
    roll = librosa.feature.spectral_rolloff(y=y, sr=sr, hop_length=hop)[0]
    flat = librosa.feature.spectral_flatness(y=y, hop_length=hop)[0]
    zcr = librosa.feature.zero_crossing_rate(y, hop_length=hop)[0]
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=hop)

    # Structure: beat-synchronous chroma + MFCC, agglomerative clustering into sections.
    feat = np.vstack([librosa.util.normalize(chroma, axis=0), librosa.util.normalize(mfcc, axis=1)])
    if len(beats) > n_sections * 2:
        sync = librosa.util.sync(feat, beats, aggregate=np.median)
        bounds_beats = librosa.segment.agglomerative(sync, n_sections)
        bound_frames = beats[np.clip(bounds_beats - 1, 0, len(beats) - 1)]
        bound_frames[0] = 0
    else:
        bounds = librosa.segment.agglomerative(feat, n_sections)
        bound_frames = bounds
    bound_times = sorted(set([0.0, *librosa.frames_to_time(bound_frames, sr=sr, hop_length=hop).tolist()]))
    merged: list[float] = []
    for t in bound_times:  # drop boundaries closer than 2 s to the previous one / the end
        if t < dur - 2 and (not merged or t - merged[-1] >= 2):
            merged.append(t)
    bound_times = (merged or [0.0]) + [dur]
    sections = []
    for i in range(len(bound_times) - 1):
        s, e = bound_times[i], bound_times[i + 1]
        fs, fe = librosa.time_to_frames([s, e], sr=sr, hop_length=hop)
        seg_rms = rms[fs:max(fe, fs + 1)]
        sections.append({"start": round(s + offset, 2), "end": round(e + offset, 2),
                         "start_ts": fmt_ts(s + offset)[3:-4], "end_ts": fmt_ts(e + offset)[3:-4],
                         "energy_db": round(float(db(np.array([seg_rms.mean()]))[0]), 1)})
    # Label sections by relative energy for a quick read.
    if sections:
        e_vals = np.array([s["energy_db"] for s in sections])
        lo, hi = np.percentile(e_vals, 33), np.percentile(e_vals, 66)
        for s in sections:
            s["energy"] = "high" if s["energy_db"] >= hi else ("low" if s["energy_db"] <= lo else "mid")

    harm_e, perc_e = float(np.sum(y_h ** 2)), float(np.sum(y_p ** 2))
    peak = float(np.max(np.abs(y)))
    win = max(1, int(round(1.0 * sr / hop)))
    energy_curve = [round(float(db(np.array([rms[i:i + win].mean()]))[0]), 1) for i in range(0, len(rms), win)]

    report = {
        "file": str(path),
        "offset": offset,
        "duration_sec": round(dur, 2),
        "sample_rate": sr,
        "tempo_bpm": round(tempo, 1),
        "tempo_range_bpm": [round(float(np.percentile(dtempo, 10)), 1), round(float(np.percentile(dtempo, 90)), 1)],
        "beats": len(beat_times),
        "beat_times_first_16": [round(float(t + offset), 3) for t in beat_times[:16]],
        "key": key,
        "loudness": {
            "peak_dbfs": round(float(db(np.array([peak]))[0]), 2),
            "rms_dbfs_mean": round(float(db(np.array([np.sqrt(np.mean(y ** 2))]))[0]), 2),
            "dynamic_range_db": round(float(np.percentile(rms_db, 95) - np.percentile(rms_db, 10)), 1),
            "crest_factor_db": round(float(db(np.array([peak]))[0] - db(np.array([np.sqrt(np.mean(y ** 2))]))[0]), 1),
        },
        "spectral": {
            "centroid_hz_mean": round(float(cent.mean()), 1),
            "bandwidth_hz_mean": round(float(bw.mean()), 1),
            "rolloff85_hz_mean": round(float(roll.mean()), 1),
            "flatness_mean": round(float(flat.mean()), 4),
            "zcr_mean": round(float(zcr.mean()), 4),
            "brightness": "bright" if cent.mean() > 3000 else ("dark" if cent.mean() < 1500 else "balanced"),
        },
        "harmonic_percussive_ratio": round(harm_e / max(perc_e, 1e-9), 2),
        "onset_rate_per_sec": round(len(onsets) / dur, 2),
        "chroma_profile": {k: round(float(v), 3) for k, v in zip(KEYS, chroma.mean(axis=1))},
        "sections": sections,
        "energy_curve_db_per_sec": energy_curve,
    }
    arrays = {"y": y, "sr": sr, "hop": hop, "rms_db": rms_db, "chroma": chroma, "beat_times": beat_times,
              "bounds": [s["start"] - offset for s in sections]}
    return report, arrays


def to_markdown(r: dict) -> str:
    k, l, s = r["key"], r["loudness"], r["spectral"]
    lines = [
        f"# 音乐分析报告 — {Path(r['file']).name}",
        "",
        f"- 时长: {r['duration_sec']} s",
        f"- 速度: **{r['tempo_bpm']} BPM**（10–90% 区间 {r['tempo_range_bpm'][0]}–{r['tempo_range_bpm'][1]}），检测到 {r['beats']} 拍",
        f"- 调性: **{k['key']} {k['mode']}**（置信 {k['confidence']}，次选 {k['runner_up']}）",
        f"- 响度: 峰值 {l['peak_dbfs']} dBFS，平均 RMS {l['rms_dbfs_mean']} dBFS，动态范围 {l['dynamic_range_db']} dB，峰均比 {l['crest_factor_db']} dB",
        f"- 音色: 频谱质心 {s['centroid_hz_mean']} Hz（{s['brightness']}），带宽 {s['bandwidth_hz_mean']} Hz，滚降 {s['rolloff85_hz_mean']} Hz",
        f"- 谐波/打击能量比: {r['harmonic_percussive_ratio']}；起音密度 {r['onset_rate_per_sec']} 次/秒",
        "",
        "## 段落结构",
        "",
        "| # | 开始 | 结束 | 能量(dB) | 强度 |",
        "|---|---|---|---|---|",
    ]
    for i, sec in enumerate(r["sections"], 1):
        lines.append(f"| {i} | {sec['start_ts']} | {sec['end_ts']} | {sec['energy_db']} | {sec['energy']} |")
    return "\n".join(lines) + "\n"


def plot(arrays: dict, out_png: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import librosa.display
    except ImportError as e:
        raise MMError("--plot 需要 matplotlib：pip install matplotlib") from e
    y, sr, hop = arrays["y"], arrays["sr"], arrays["hop"]
    fig, ax = plt.subplots(3, 1, figsize=(14, 9), sharex=True)
    librosa.display.waveshow(y, sr=sr, ax=ax[0], alpha=0.6)
    ax[0].vlines(arrays["beat_times"], -1, 1, color="r", alpha=0.25, linewidth=0.6)
    ax[0].set_title("Waveform + beats")
    S = librosa.amplitude_to_db(np.abs(librosa.stft(y, hop_length=hop)), ref=np.max)
    librosa.display.specshow(S, sr=sr, hop_length=hop, x_axis="time", y_axis="log", ax=ax[1])
    ax[1].set_title("Spectrogram (log)")
    librosa.display.specshow(arrays["chroma"], sr=sr, hop_length=hop, x_axis="time", y_axis="chroma", ax=ax[2])
    for b in arrays["bounds"]:
        for x in ax:
            x.axvline(b, color="w" if x is not ax[0] else "k", linestyle="--", linewidth=1)
    ax[2].set_title("Chroma + section boundaries")
    fig.tight_layout()
    fig.savefig(out_png, dpi=110)


@main_guard
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("--sr", type=int, default=22050)
    ap.add_argument("--sections", type=int, default=6, help="number of structural sections")
    ap.add_argument("--start", help="analysis start (sec or mm:ss)")
    ap.add_argument("--duration", type=float, help="analysis length in seconds")
    ap.add_argument("--plot", action="store_true", help="save waveform/spectrogram/chroma PNG (needs matplotlib)")
    ap.add_argument("-o", "--output-prefix", help="write <prefix>.json / .md (/.png)")
    a = ap.parse_args()

    src = Path(a.input).expanduser().resolve()
    if not src.is_file():
        raise MMError(f"文件不存在: {src}")
    offset = parse_ts(a.start) if a.start else 0.0
    with tempfile.TemporaryDirectory() as td:
        audio = src
        if src.suffix.lower() in VIDEO_EXT:
            require_bin("ffmpeg")
            audio = Path(td) / "audio.wav"
            run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", str(a.sr), str(audio)], capture=True)
        report, arrays = analyze(audio, a.sr, a.sections, offset, a.duration)
    report["file"] = str(src)

    if a.output_prefix:
        prefix = Path(a.output_prefix).expanduser()
        prefix.parent.mkdir(parents=True, exist_ok=True)
        prefix.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        prefix.with_suffix(".md").write_text(to_markdown(report), encoding="utf-8")
        if a.plot:
            plot(arrays, prefix.with_suffix(".png"))
        print(f"[OK] {prefix}.json / {prefix}.md" + (f" / {prefix}.png" if a.plot else ""), file=sys.stderr)
    elif a.plot:
        plot(arrays, src.with_suffix(".analysis.png"))
    print(to_markdown(report))
    print("```json\n" + json.dumps({k: v for k, v in report.items() if k != "energy_curve_db_per_sec"},
                                   ensure_ascii=False, indent=2) + "\n```")


if __name__ == "__main__":
    main()
