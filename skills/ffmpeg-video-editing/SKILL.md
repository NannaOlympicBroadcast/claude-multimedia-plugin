---
name: ffmpeg-video-editing
description: 用 FFmpeg 剪辑视频：查看信息、按多个时间段裁剪并合并、拼接多个文件、抽帧、输入多个时间点直接抽帧拼成一张屏幕墙（contact sheet / 九宫格）、提取音频、做 GIF、变速、横竖屏转换、烧录字幕、替换/混合音轨。当用户要求剪辑、截取片段、合并视频、截图、拼屏幕墙、做缩略图墙、转竖屏、加字幕时使用。
---

# FFmpeg 视频剪辑

脚本：`${CLAUDE_PLUGIN_ROOT}/scripts/ffmpeg_tools.py`（下文 `$FF`）。时间格式均可用 `83.5`、`1:23`、`00:01:23.500`、`1m23s`。

## ★ 多时间点 → 屏幕墙（一条命令）

```bash
# 指定任意多个时间点（逗号分隔，中文逗号也可），自动排成网格，每帧右下角标注时间码
python3 $FF wall in.mp4 --times "0:05,0:30,1:12.5,2:40,3:05,4:18" -o wall.jpg
python3 $FF wall in.mp4 --times "0:05,0:30,1:12,2:40" --cols 2 --width 640 --title "第3集 关键画面" --font /path/NotoSansCJK.ttc -o wall.jpg
python3 $FF wall in.mp4 --times-file times.txt -o wall.jpg     # 每行一个时间点
python3 $FF wall in.mp4 --every 30 -o wall.jpg                  # 每 30 秒一帧
python3 $FF wall in.mp4 --count 16 -o wall.jpg                  # 均匀 16 帧（4x4）
```
选项：`--cols/--rows`（默认近似正方形）、`--width` 单帧宽(默认 480)、`--gap` 间距、`--bg` 背景色、`--no-label` 不标时间码、`--sort` 按时间排序、`--title` 顶部标题（中文需 CJK 字体 `--font`）。
越界时间点会报错列出，不会静默跳过。生成后用 Read 工具查看图片确认效果。

## 剪辑与拼接

```bash
python3 $FF probe in.mp4                                         # 时长/分辨率/帧率/编码
python3 $FF cut in.mp4 --range 0:10-0:25 -o clip.mp4             # 精确剪切（重编码 x264 crf20）
python3 $FF cut in.mp4 --range 0:10-0:25 --copy -o clip.mp4      # 流复制，极快但切点对齐关键帧
python3 $FF cut in.mp4 --range 0:10-0:25 --range 1:02-1:30 --range 3:00-3:12 --concat -o highlights.mp4
python3 $FF concat a.mp4 b.mp4 c.mp4 -o joined.mp4               # 参数一致时流复制，否则自动重编码到 1080p30
python3 $FF concat a.mp4 b.mov --reencode -o joined.mp4
```

## 其它

```bash
python3 $FF frames in.mp4 --times "0:05,1:00" -d frames          # 单独导出帧图片
python3 $FF audio in.mp4 --format mp3 -o a.mp3                   # 提取音频(mp3/wav/m4a/flac, --sr 16000 --mono)
python3 $FF gif in.mp4 --start 0:10 --end 0:14 --fps 12 --width 480 -o a.gif
python3 $FF speed in.mp4 --factor 1.5 -o fast.mp4                # 音画同步变速
python3 $FF scale in.mp4 --aspect 9:16 --height 1920 -o v.mp4    # 横转竖(居中裁切)，--mode pad 加黑边
python3 $FF subs in.mp4 --subs zh.srt --style "FontName=Noto Sans CJK SC,FontSize=20" -o sub.mp4
python3 $FF mux in.mp4 --audio dub.wav --mode replace -o out.mp4   # --mode mix --bg-db -12 混音
```

需要更复杂的效果（转场、画中画、调色、关键帧动画）时，可直接写 ffmpeg 命令；先 `probe` 看参数再动手，输出前确认不会覆盖用户原文件。

## 联动

- gemini-media-analysis `--preset highlights` 的 start/end → `cut --range ... --concat`；`--preset scenes` 的时间点 → `wall --times`。
- tencent-asr-transcribe 的 SRT → `subs` 烧录；edge-tts-dubbing 的配音 → `mux`。
