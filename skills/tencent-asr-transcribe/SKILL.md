---
name: tencent-asr-transcribe
description: 用腾讯云语音识别（录音文件识别 CreateRecTask，默认大模型2.0引擎 16k_zh_en_2.0）把音频/视频转写成带时间戳的 SRT 字幕、TXT 和 JSON，支持说话人分离、31 种方言、热词、长音频自动分片。当用户要求转写、出字幕、做会议纪要、语音转文字（尤其中文/方言）时使用。
---

# 腾讯云 ASR 转写

脚本：`${CLAUDE_PLUGIN_ROOT}/scripts/tencent_asr.py`（下文 `$ASR`）

需要 `TENCENTCLOUD_SECRET_ID` / `TENCENTCLOUD_SECRET_KEY`（https://console.cloud.tencent.com/cam/capi ，需开通“语音识别”服务）。脚本自己实现 TC3-HMAC-SHA256 签名，调用 `asr.tencentcloudapi.com`（Version 2019-06-14）的 `CreateRecTask` + `DescribeTaskStatus` 轮询。

## 流程

1. 本地文件 → ffmpeg 转 16 kHz 单声道 32 kbps MP3；
2. 接口规定 base64 数据 ≤ 5 MB，因此按 15 分钟切片（`--chunk-sec` 可调），逐片提交并把时间戳按片偏移合并；
3. 有公网 URL 时用 `--url`（无 5 MB 限制，不分片）；
4. 输出 `<prefix>.srt`、`<prefix>.txt`（带时间戳/说话人）、`<prefix>.json`（分句、起止秒、SpeakerId）。

## 引擎（EngineModelType，来源：腾讯云 API 文档 1093/37823）

| 引擎 | 说明 |
|---|---|
| `16k_zh_en_2.0`（默认） | 大模型2.0，中英 + 粤语/四川/上海/闽南等 31 种方言，支持说话人分离，抗噪 |
| `16k_zh_en_meeting` | 大模型2.0，多人会议场景，**配合 `--speakers` 使用** |
| `16k_zh_en` | 大模型1.0 中英 |
| `16k_multi_lang` | 15 种语言自动识别 |
| `8k_zh_large` | 电话场景大模型 |
| `16k_en` / `16k_ja` / `16k_ko` … | 通用引擎各语种 |

## 用法

```bash
python3 $ASR interview.mp4 -o out/interview                 # 默认大模型2.0
python3 $ASR meeting.m4a --engine 16k_zh_en_meeting --speakers -o out/meeting
python3 $ASR podcast.mp3 --speakers --speaker-number 2
python3 $ASR --url "https://cdn.example.com/a.mp3" -o out/a
python3 $ASR talk.wav --hotword-id <热词表ID> --filter-modal 1   # 热词 + 过滤语气词
```

stdout 输出各文件路径与分句数。转写结果可直接：
- 交给 edge-tts-dubbing 用 `--srt` 做配音；
- 交给 `ffmpeg_tools.py subs` 烧录字幕；
- 让 Claude 基于 `.txt` 写纪要/摘要。

## 错误处理

- 缺凭据 → 退出码 2；API 错误会打印 `Code / Message / RequestId`，原样告诉用户（常见：未开通服务、余额不足、引擎与采样率不匹配）。
- 不要在失败时改用其它 ASR 或编造转写文本。
