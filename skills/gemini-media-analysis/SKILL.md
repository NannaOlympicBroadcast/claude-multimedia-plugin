---
name: gemini-media-analysis
description: 用 Google Gemini 最新模型（通过 models.list 自动发现最新版本）理解和分析视频、音频：内容摘要、逐字转写+说话人、镜头/场景拆分、高光片段、屏幕文字 OCR、音频/音乐描述、自定义问答。支持本地文件（Files API 上传）与 YouTube 链接。当用户要求“分析/总结/看懂/描述这个视频或音频”、找高光、按时间轴拆解内容时使用。
---

# Gemini 音视频分析（自动使用最新模型 🔍）

脚本：`${CLAUDE_PLUGIN_ROOT}/scripts/gemini_analyze.py`（下文 `$GA`）。需要 `GEMINI_API_KEY`（https://aistudio.google.com/apikey）。

## 模型选择（🔍 自动找最新）

- 默认每次调用 `GET /v1beta/models` 列出账号可用模型，在 `gemini-<版本>-pro|flash[-preview]` 中按 **版本号最高 → 稳定版优先 → pro 优先** 排序，自动排除 tts / live / image / transcribe / embedding 等专用模型。
- `--prefer pro|flash|lite` 改变偏好；`--model <id>` 或环境变量 `GEMINI_MODEL` 固定模型。
- `python3 $GA --list-models` 查看排序结果。stderr 会打印本次使用的模型，汇报结果时请告诉用户用的是哪个模型。
- 截至 2026-09 官方模型页推荐新项目使用 `gemini-3.8-flash`（来源：ai.google.dev/gemini-api/docs/models）；脚本不硬编码它，而是以 models.list 实际返回为准。

## 输入

- 本地视频/音频：自动经 Files API 可续传上传并轮询到 `ACTIVE`，结束后删除（`--keep-files` 保留）。
- YouTube 公开链接：直接以 `fileUri` 传给 Gemini，无需下载。
- 其它站点链接（B站、抖音…）：先用 video-download 技能下载，再分析本地文件。
- 单次最多 10 个输入，可做多视频对比。

## 预设与用法

| `--preset` | 输出 |
|---|---|
| `summary`（默认） | 一句话概括、带时间戳的分点摘要、实体、结论、高光建议 |
| `transcript` | 带时间戳+说话人的逐字稿 |
| `scenes` | 镜头/场景表（起止时间、画面、人物动作、画面文字、声音） |
| `highlights` | 高光片段 JSON（start/end 秒、标题、理由、文案）——可直接交给 ffmpeg 剪辑 |
| `audio` | 音频类型、说话人、情绪、背景音、音乐风格结构 |
| `ocr` | 屏幕文字提取（带时间戳） |

```bash
python3 $GA video.mp4 --preset summary -o summary.md
python3 $GA "https://www.youtube.com/watch?v=ID" --preset scenes
python3 $GA talk.mp3 --preset transcript --lang 英文
python3 $GA clip.mp4 -p "视频里出现了哪些品牌 logo？给出时间点" --start 1:00 --end 3:00 --fps 2
python3 $GA a.mp4 b.mp4 -p "对比两段视频的剪辑节奏和配色"
python3 $GA video.mp4 --preset highlights --json -o highlights.json
python3 $GA video.mp4 -p "视频中提到的事件是否属实？" --grounding     # 开启 Google 搜索 grounding
```

## 与其它技能联动

- `highlights` 输出的 start/end → `ffmpeg_tools.py cut --range s-e ... --concat` 生成高光合集；或把时间点交给 `ffmpeg_tools.py wall --times` 拼屏幕墙。
- 需要精确字级时间戳的中文字幕时，优先用 tencent-asr-transcribe；Gemini 适合理解与总结。

## 错误处理

- 缺 key → 退出码 2，请用户配置 `GEMINI_API_KEY`。
- HTTP 4xx/5xx 会原样输出 API 返回体；文件过大/处理失败时如实告知，不要伪造分析结果。
