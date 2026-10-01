#!/usr/bin/env python3
"""FFmpeg toolbox for video editing.

Subcommands
  probe        media info (duration, streams, resolution, fps)
  cut          cut one or many segments:  --range 00:10-00:25 --range 1:02:00-1:02:30 [--concat]
  concat       join files (stream copy if compatible, else re-encode)
  frames       extract frames at given timestamps
  wall         extract frames at timestamps and tile them into ONE image (contact sheet / 屏幕墙)
  audio        extract audio track
  gif          make a GIF from a range
  speed        change playback speed
  scale        resize / crop to aspect (e.g. 9:16 for shorts)
  subs         burn subtitles (.srt/.ass) into video
  mux          replace / add an audio track

Timestamps accept 83.5 / 1:23 / 00:01:23.500 / 1m23s.

Examples
  ffmpeg_tools.py wall in.mp4 --times "0:05,0:30,1:12,2:40,3:05,4:18" --cols 3 -o wall.jpg
  ffmpeg_tools.py wall in.mp4 --every 30 -o wall.jpg            # one frame every 30s
  ffmpeg_tools.py wall in.mp4 --count 16 -o wall.jpg            # 16 evenly spaced frames
  ffmpeg_tools.py cut in.mp4 --range 0:10-0:25 --range 1:00-1:20 --concat -o highlights.mp4
  ffmpeg_tools.py scale in.mp4 --aspect 9:16 --height 1920 -o vertical.mp4
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mmcommon import MMError, ffprobe_json, fmt_ts, main_guard, media_duration, parse_ts, require_bin, run  # noqa: E402

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/arial.ttf",
]


def ff(*args: str) -> None:
    run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *args], capture=True)


def esc_filter_path(p: str) -> str:
    return p.replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def find_font(user_font: str | None) -> str | None:
    if user_font:
        if not Path(user_font).is_file():
            raise MMError(f"字体文件不存在: {user_font}")
        return user_font
    for f in FONT_CANDIDATES:
        if Path(f).is_file():
            return f
    return None


def parse_times(a, duration: float) -> list[float]:
    times: list[float] = []
    if a.times:
        for chunk in a.times.replace("，", ",").replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                times.append(parse_ts(chunk))
    if a.times_file:
        for line in Path(a.times_file).read_text(encoding="utf-8").splitlines():
            line = line.strip().split()[0] if line.strip() else ""
            if line and not line.startswith("#"):
                times.append(parse_ts(line))
    if a.every:
        t = a.every / 2
        while t < duration:
            times.append(t)
            t += a.every
    if a.count:
        step = duration / a.count
        times += [step * (i + 0.5) for i in range(a.count)]
    if not times:
        raise MMError("请提供 --times / --times-file / --every / --count 之一")
    bad = [t for t in times if t < 0 or t > duration]
    if bad:
        raise MMError(f"时间点超出视频时长 {duration:.2f}s: {[round(b, 2) for b in bad]}")
    return times


def grab(src: str, t: float, out: Path, width: int, label: str | None, font: str | None, fontsize: int) -> None:
    vf = [f"scale={width}:-2"]
    if label is not None:
        if not font:
            raise MMError("未找到可用字体用于时间戳标注，请用 --font 指定字体文件，或加 --no-label")
        vf.append(
            f"drawtext=fontfile='{esc_filter_path(font)}':text='{label.replace(':', chr(92) + ':')}':"
            f"x=w-tw-12:y=h-th-12:fontsize={fontsize}:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=8"
        )
    # -ss before -i = fast keyframe seek, then accurate decode to the exact timestamp.
    ff("-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", "-vf", ",".join(vf), "-q:v", "2", str(out))
    if not out.exists():
        raise MMError(f"在 {t:.2f}s 处抽帧失败")


# --------------------------------------------------------------------------- subcommands
def cmd_probe(a) -> None:
    info = ffprobe_json(a.input)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    au = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    out = {
        "duration": float(info["format"].get("duration", 0)),
        "size_mb": round(int(info["format"].get("size", 0)) / 1e6, 2),
        "bitrate_kbps": round(int(info["format"].get("bit_rate", 0)) / 1000),
        "video": v and {"codec": v.get("codec_name"), "width": v.get("width"), "height": v.get("height"),
                        "fps": v.get("avg_frame_rate"), "pix_fmt": v.get("pix_fmt")},
        "audio": au and {"codec": au.get("codec_name"), "sample_rate": au.get("sample_rate"),
                         "channels": au.get("channels")},
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_frames(a) -> None:
    dur = media_duration(a.input)
    times = parse_times(a, dur)
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    font = None if a.no_label else find_font(a.font)
    files = []
    for i, t in enumerate(times, 1):
        p = out_dir / f"frame_{i:03d}_{fmt_ts(t).replace(':', '-')}.{a.ext}"
        grab(a.input, t, p, a.width, None if a.no_label else fmt_ts(t)[:-4], font, a.fontsize)
        files.append(str(p))
    print(json.dumps({"frames": files}, ensure_ascii=False, indent=2))


def cmd_wall(a) -> None:
    """Frames at N timestamps -> one tiled image (grid)."""
    dur = media_duration(a.input)
    times = parse_times(a, dur)
    if a.sort:
        times.sort()
    n = len(times)
    cols = a.cols or math.ceil(math.sqrt(n))
    rows = a.rows or math.ceil(n / cols)
    if cols * rows < n:
        raise MMError(f"{cols}x{rows} 网格放不下 {n} 帧")
    font = None if a.no_label else find_font(a.font)
    with tempfile.TemporaryDirectory() as td:
        for i, t in enumerate(times):
            label = None if a.no_label else (fmt_ts(t)[:-4] if dur >= 3600 else fmt_ts(t)[3:-4])
            grab(a.input, t, Path(td) / f"f{i:04d}.png", a.width, label, font, a.fontsize)
        # Pad remaining grid cells with blank frames so `tile` outputs a full grid.
        first = Path(td) / "f0000.png"
        for j in range(n, cols * rows):
            ff("-i", str(first), "-vf", f"drawbox=c={a.bg}:t=fill", str(Path(td) / f"f{j:04d}.png"))
        vf = f"tile={cols}x{rows}:padding={a.gap}:margin={a.gap}:color={a.bg}"
        if a.title:
            if not font:
                raise MMError("标题需要字体，请用 --font 指定")
            vf += (f",pad=iw:ih+{a.fontsize * 2 + a.gap}:0:{a.fontsize * 2 + a.gap}:color={a.bg},"
                   f"drawtext=fontfile='{esc_filter_path(font)}':text='{a.title.replace(':', chr(92) + ':')}':"
                   f"x=(w-tw)/2:y={a.gap}:fontsize={int(a.fontsize * 1.4)}:fontcolor=white")
        out = Path(a.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        ff("-framerate", "1", "-i", str(Path(td) / "f%04d.png"), "-vf", vf, "-frames:v", "1", "-q:v", "2", str(out))
    print(json.dumps({"wall": str(out), "frames": n, "grid": f"{cols}x{rows}",
                      "times": [round(t, 3) for t in times]}, ensure_ascii=False, indent=2))


def _ranges(a) -> list[tuple[float, float]]:
    res = []
    for r in a.range:
        if "-" not in r:
            raise MMError(f"范围格式应为 start-end: {r}")
        s, e = r.split("-", 1)
        s_, e_ = parse_ts(s), parse_ts(e)
        if e_ <= s_:
            raise MMError(f"结束时间必须大于开始时间: {r}")
        res.append((s_, e_))
    return res


def cmd_cut(a) -> None:
    ranges = _ranges(a)
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    enc = (["-c", "copy", "-avoid_negative_ts", "make_zero"] if a.copy else
           ["-c:v", "libx264", "-preset", a.preset, "-crf", str(a.crf), "-c:a", "aac", "-b:a", "192k"])
    pieces = []
    for i, (s, e) in enumerate(ranges, 1):
        p = out if (len(ranges) == 1) else out.with_name(f"{out.stem}_part{i:02d}{out.suffix}")
        ff("-ss", f"{s:.3f}", "-i", a.input, "-t", f"{e - s:.3f}", *enc, "-map", "0:v?", "-map", "0:a?", str(p))
        pieces.append(p)
    result = {"parts": [str(p) for p in pieces]}
    if a.concat and len(pieces) > 1:
        _concat(pieces, out, reencode=False)
        result["concat"] = str(out)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _concat(files: list[Path], out: Path, reencode: bool) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as lst:
        for f in files:
            lst.write(f"file '{Path(f).resolve().as_posix()}'\n")
    try:
        if not reencode:
            try:
                ff("-f", "concat", "-safe", "0", "-i", lst.name, "-c", "copy", str(out))
                return
            except MMError:
                print("[concat] 流复制失败，改为重新编码", file=sys.stderr)
        inputs, fc = [], ""
        for i, f in enumerate(files):
            inputs += ["-i", str(f)]
            fc += (f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
                   f"setsar=1,fps=30[v{i}];[{i}:a]aresample=48000[a{i}];")
        fc += "".join(f"[v{i}][a{i}]" for i in range(len(files))) + f"concat=n={len(files)}:v=1:a=1[v][a]"
        ff(*inputs, "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", "20",
           "-c:a", "aac", str(out))
    finally:
        Path(lst.name).unlink(missing_ok=True)


def cmd_concat(a) -> None:
    out = Path(a.output)
    _concat([Path(f) for f in a.inputs], out, a.reencode)
    print(json.dumps({"output": str(out)}, ensure_ascii=False))


def cmd_audio(a) -> None:
    codec = {"mp3": ["-c:a", "libmp3lame", "-q:a", "2"], "wav": ["-c:a", "pcm_s16le"], "m4a": ["-c:a", "aac", "-b:a", "192k"],
             "flac": ["-c:a", "flac"]}[a.format]
    out = Path(a.output or Path(a.input).with_suffix(f".{a.format}"))
    extra = (["-ar", str(a.sr)] if a.sr else []) + (["-ac", "1"] if a.mono else [])
    ff("-i", a.input, "-vn", *codec, *extra, str(out))
    print(json.dumps({"audio": str(out)}, ensure_ascii=False))


def cmd_gif(a) -> None:
    s, e = parse_ts(a.start), parse_ts(a.end)
    vf = f"fps={a.fps},scale={a.width}:-1:flags=lanczos,split[x][y];[x]palettegen=stats_mode=diff[p];[y][p]paletteuse=dither=bayer"
    ff("-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", a.input, "-filter_complex", vf, "-loop", "0", a.output)
    print(json.dumps({"gif": a.output}, ensure_ascii=False))


def cmd_speed(a) -> None:
    f = a.factor
    atempo = []
    x = f
    while x > 2.0:
        atempo.append("atempo=2.0"); x /= 2.0
    while x < 0.5:
        atempo.append("atempo=0.5"); x /= 0.5
    atempo.append(f"atempo={x:.4f}")
    has_audio = any(s.get("codec_type") == "audio" for s in ffprobe_json(a.input)["streams"])
    args = ["-i", a.input, "-filter:v", f"setpts=PTS/{f}"]
    args += ["-filter:a", ",".join(atempo)] if has_audio else ["-an"]
    ff(*args, a.output)
    print(json.dumps({"output": a.output}, ensure_ascii=False))


def cmd_scale(a) -> None:
    if a.aspect:
        w, h = (int(x) for x in a.aspect.split(":"))
        H = a.height or 1920
        W = int(round(H * w / h / 2) * 2)
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}" if a.mode == "crop" else
              f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black")
    else:
        vf = f"scale={a.width or -2}:{a.height or -2}"
    ff("-i", a.input, "-vf", vf, "-c:v", "libx264", "-crf", str(a.crf), "-c:a", "copy", a.output)
    print(json.dumps({"output": a.output}, ensure_ascii=False))


def cmd_subs(a) -> None:
    style = f":force_style='{a.style}'" if a.style else ""
    ff("-i", a.input, "-vf", f"subtitles='{esc_filter_path(str(Path(a.subs).resolve()))}'{style}",
       "-c:v", "libx264", "-crf", str(a.crf), "-c:a", "copy", a.output)
    print(json.dumps({"output": a.output}, ensure_ascii=False))


def cmd_mux(a) -> None:
    if a.mode == "replace":
        ff("-i", a.input, "-i", a.audio, "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-shortest", a.output)
    else:
        ff("-i", a.input, "-i", a.audio, "-filter_complex",
           f"[0:a]volume={a.bg_db}dB[a0];[a0][1:a]amix=inputs=2:normalize=0:duration=first[a]",
           "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", a.output)
    print(json.dumps({"output": a.output}, ensure_ascii=False))


def add_time_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--times", help='comma separated timestamps, e.g. "0:05,0:30,1:12.5"')
    p.add_argument("--times-file", help="file with one timestamp per line")
    p.add_argument("--every", type=float, help="one frame every N seconds")
    p.add_argument("--count", type=int, help="N evenly spaced frames")
    p.add_argument("--width", type=int, default=480, help="width of each frame (px)")
    p.add_argument("--no-label", action="store_true", help="don't draw timestamp labels")
    p.add_argument("--font", help="font file for labels (CJK font for Chinese titles)")
    p.add_argument("--fontsize", type=int, default=22)


@main_guard
def main() -> None:
    require_bin("ffmpeg")
    require_bin("ffprobe")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe"); p.add_argument("input"); p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("wall", help="timestamps -> tiled contact sheet")
    p.add_argument("input"); add_time_args(p)
    p.add_argument("--cols", type=int); p.add_argument("--rows", type=int)
    p.add_argument("--gap", type=int, default=6); p.add_argument("--bg", default="black")
    p.add_argument("--title"); p.add_argument("--sort", action="store_true", help="sort timestamps")
    p.add_argument("-o", "--output", default="wall.jpg"); p.set_defaults(fn=cmd_wall)

    p = sub.add_parser("frames"); p.add_argument("input"); add_time_args(p)
    p.add_argument("-d", "--out-dir", default="frames"); p.add_argument("--ext", default="jpg"); p.set_defaults(fn=cmd_frames)

    p = sub.add_parser("cut"); p.add_argument("input")
    p.add_argument("--range", action="append", required=True, help="start-end, repeatable")
    p.add_argument("--concat", action="store_true", help="join all parts into -o")
    p.add_argument("--copy", action="store_true", help="stream copy (fast, keyframe-aligned)")
    p.add_argument("--crf", type=int, default=20); p.add_argument("--preset", default="veryfast")
    p.add_argument("-o", "--output", required=True); p.set_defaults(fn=cmd_cut)

    p = sub.add_parser("concat"); p.add_argument("inputs", nargs="+")
    p.add_argument("--reencode", action="store_true"); p.add_argument("-o", "--output", required=True); p.set_defaults(fn=cmd_concat)

    p = sub.add_parser("audio"); p.add_argument("input")
    p.add_argument("--format", choices=["mp3", "wav", "m4a", "flac"], default="mp3")
    p.add_argument("--sr", type=int); p.add_argument("--mono", action="store_true")
    p.add_argument("-o", "--output"); p.set_defaults(fn=cmd_audio)

    p = sub.add_parser("gif"); p.add_argument("input"); p.add_argument("--start", required=True); p.add_argument("--end", required=True)
    p.add_argument("--fps", type=int, default=12); p.add_argument("--width", type=int, default=480)
    p.add_argument("-o", "--output", default="out.gif"); p.set_defaults(fn=cmd_gif)

    p = sub.add_parser("speed"); p.add_argument("input"); p.add_argument("--factor", type=float, required=True)
    p.add_argument("-o", "--output", required=True); p.set_defaults(fn=cmd_speed)

    p = sub.add_parser("scale"); p.add_argument("input")
    p.add_argument("--aspect", help="target aspect, e.g. 9:16"); p.add_argument("--mode", choices=["crop", "pad"], default="crop")
    p.add_argument("--width", type=int); p.add_argument("--height", type=int); p.add_argument("--crf", type=int, default=20)
    p.add_argument("-o", "--output", required=True); p.set_defaults(fn=cmd_scale)

    p = sub.add_parser("subs"); p.add_argument("input"); p.add_argument("--subs", required=True)
    p.add_argument("--style", help="ASS force_style, e.g. 'FontName=Noto Sans CJK SC,FontSize=22'")
    p.add_argument("--crf", type=int, default=20); p.add_argument("-o", "--output", required=True); p.set_defaults(fn=cmd_subs)

    p = sub.add_parser("mux"); p.add_argument("input"); p.add_argument("--audio", required=True)
    p.add_argument("--mode", choices=["replace", "mix"], default="replace"); p.add_argument("--bg-db", type=float, default=-12)
    p.add_argument("-o", "--output", required=True); p.set_defaults(fn=cmd_mux)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
