---
name: edge-tts-dubbing
description: 用微软 Edge TTS（edge-tts，免费、无需 key）给文本或 SRT 字幕配音：生成旁白音频+字幕，按字幕时间轴逐句对齐生成配音轨（超长句自动 atempo 加速），并可混入视频（替换原声/压低原声 ducking/混音）。当用户要求配音、旁白、朗读、TTS、把字幕变成语音、给视频换配音时使用。
---

# Edge TTS 配音

脚本：`${CLAUDE_PLUGIN_ROOT}/scripts/edge_tts_dub.py`（下文 `$TTS`），依赖 `edge-tts`、ffmpeg、numpy、soundfile。

## 选择音色

```bash
python3 $TTS --list-voices --locale zh-CN     # 中文音色
python3 $TTS --list-voices --locale en-US
```
常用：`zh-CN-XiaoxiaoNeural`(女，默认)、`zh-CN-YunxiNeural`(男)、`zh-CN-YunyangNeural`(新闻男)、`zh-CN-XiaoyiNeural`(女)、`zh-TW-HsiaoChenNeural`、`zh-HK-HiuMaanNeural`、`en-US-AriaNeural`、`ja-JP-NanamiNeural`。以 `--list-voices` 实际返回为准。

## 三种模式

```bash
# 1) 文本 → 音频 + 同步字幕 (.srt)
python3 $TTS --text "大家好，欢迎收看本期节目" -v zh-CN-YunxiNeural --rate "+5%" -o out/narration.mp3
python3 $TTS --text-file script.txt -o out/narration.mp3

# 2) SRT → 按时间轴对齐的配音轨（每句放在原字幕起点，超出时间槽自动加速，最大 --max-speed 1.6）
python3 $TTS --srt subs.srt -v zh-CN-XiaoxiaoNeural -o out/dub.wav

# 3) SRT + 视频 → 直接输出配好音的视频
python3 $TTS --srt subs.srt --video in.mp4 --mix duck  -o out/dubbed.mp4   # 原声压低并侧链闪避
python3 $TTS --srt subs.srt --video in.mp4 --mix replace -o out/dubbed.mp4 # 完全替换原声
python3 $TTS --srt subs.srt --video in.mp4 --mix mix --bg-db -18 -o out/dubbed.mp4
```

- `--rate "+10%"`、`--volume "-5%"`、`--pitch "+2Hz"` 调整语速音量音高。
- 输出 JSON 中 `sped_up` 为被加速的句数，`overflow` 列出即使加速后仍超出时间槽的句子（index/起点/超出秒数）——汇报给用户，建议精简这些句子的文案或放宽 `--max-speed`。
- 字幕中的 `[S1]` 说话人标签、HTML/ASS 标签会被自动去掉再朗读。

## 典型工作流：视频翻译配音

1. video-download 下载 → 2. tencent-asr-transcribe 出原文 SRT → 3. Claude 翻译 SRT（保持序号与时间轴不变）→ 4. 本技能 `--srt 译文.srt --video 原视频 --mix duck` → 5. 可选 `ffmpeg_tools.py subs` 烧录译文字幕。

## 错误处理

edge-tts 需要能访问微软语音服务（`speech.platform.bing.com`）。网络失败时脚本报错退出——如实告诉用户，不要生成静音占位音频。代理可用 `EDGE_TTS_PROXY` 指定（默认继承 `HTTPS_PROXY`）。
