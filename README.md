# Claude Multimedia Toolkit（Claude / Cowork 多媒体处理插件）

一个 Claude Code / Claude Cowork 插件，把常见的多媒体工作流封装成 9 个技能 + 一组可独立运行的 Python 脚本：

| 技能 | 功能 | 脚本 |
|---|---|---|
| `video-download` | yt-dlp 下载网络视频/音频/字幕；**每次先升级到最新版**；需要登录时停止并**向用户索取 cookies.txt** | `scripts/ytdlp_download.py` |
| `fast-download` | **aria2c 多线程**下载（插件内所有下载都先走 aria2） | `scripts/fast_download.py` |
| `gemini-media-analysis` | Gemini **自动发现最新模型** 🔍 分析音视频（摘要/逐字稿/分镜/高光/OCR） | `scripts/gemini_analyze.py` |
| `tencent-asr-transcribe` | 腾讯云录音文件识别（大模型 2.0）→ SRT/TXT/JSON，说话人分离、长音频分片 | `scripts/tencent_asr.py` |
| `edge-tts-dubbing` | Edge TTS 配音；SRT 逐句对齐配音轨，可直接混入视频（ducking） | `scripts/edge_tts_dub.py` |
| `image-generation` | 通用生图：按配置自动选 **最新 GPT Image / Nano Banana / Seedream** | `scripts/image_gen.py` |
| `ffmpeg-video-editing` | FFmpeg 剪辑；**输入多个时间点直接抽帧拼成屏幕墙** | `scripts/ffmpeg_tools.py` |
| `librosa-music-analysis` | BPM/节拍、调性、响度、音色、段落结构、能量曲线 | `scripts/music_analyze.py` |
| `multimedia-setup` | 一键安装依赖 + 自检 + 配置 key 指引 | `scripts/setup_env.sh`、`scripts/doctor.py` |

## 安装

### Claude Code

```bash
# 添加本仓库为插件市场并安装
/plugin marketplace add NannaOlympicBroadcast/claude-multimedia-plugin
/plugin install claude-multimedia@claude-multimedia-marketplace

# 或本地调试
claude --plugin-dir /path/to/claude-multimedia-plugin
```

### Claude Cowork

在 Cowork 的插件管理中添加仓库 `NannaOlympicBroadcast/claude-multimedia-plugin` 作为市场后安装 `claude-multimedia`，或上传本目录的 zip。插件不包含顶层 `bin/` 目录，符合 Cowork 的安装要求。

### 依赖

```bash
bash scripts/setup_env.sh      # 安装 aria2、ffmpeg、yt-dlp[default]、deno、edge-tts、librosa…并自检
python3 scripts/doctor.py      # 仅自检
```

## 配置

复制 `config.example.env` 到 `~/.config/claude-multimedia/config.env`（或项目根目录 `.env`），填入需要的 key：

- `GEMINI_API_KEY`：Gemini 分析 + Nano Banana
- `OPENAI_API_KEY`：GPT Image
- `ARK_API_KEY`（火山引擎方舟，国内）/ `BYTEPLUS_API_KEY` + `SEEDREAM_REGION=intl`（海外）：Seedream
- `TENCENTCLOUD_SECRET_ID` / `TENCENTCLOUD_SECRET_KEY`：腾讯云 ASR
- Edge TTS 无需 key

生图自动选择：`IMAGE_PROVIDER=auto|openai|gemini|seedream`、`IMAGE_PROVIDER_ORDER=openai,gemini,seedream`、`IMAGE_TIER=quality|fast`。

## 设计约定

- **不做静默降级**：缺依赖、缺 key、网络失败、yt-dlp 升级失败时，脚本以明确的错误和退出码结束（`2`=缺依赖/配置，`3`=需要 cookies），由 Claude 转告用户处理，而不是换用 mock、占位图或单线程下载。
- **最新模型靠接口发现，而不是写死**：Gemini 与 OpenAI 通过 models.list 实时排序；Seedream 没有可用 API Key 调用的列表接口，内置按发布日期排序的候选，未开通时依次尝试并说明。
- **下载一律走 aria2c**：yt-dlp 使用 `--downloader aria2c`，生图返回的 URL、参考图 URL 也经 aria2c 下载；自动继承 `HTTPS_PROXY` 与 CA 证书设置。

## 截至 2026-10-01 核实的模型/接口信息

| 项目 | 内容 | 来源 |
|---|---|---|
| Gemini 推荐模型 | 新项目推荐 `gemini-3.8-flash` / `gemini-3.5-flash-lite`；2.5 系列仅限老用户 | [Gemini models](https://ai.google.dev/gemini-api/docs/models)、[changelog](https://ai.google.dev/gemini-api/docs/changelog) |
| Gemini 视频输入 | Files API 可续传上传 → 轮询 ACTIVE；YouTube URL 直接作为 file_uri | [Video understanding](https://ai.google.dev/gemini-api/docs/video-understanding) |
| Nano Banana | `gemini-3-pro-image`（Pro）、`gemini-3.1-flash-image`（2）、`gemini-3.1-flash-lite-image`（2 Lite） | [Gemini models](https://ai.google.dev/gemini-api/docs/models) |
| GPT Image | `gpt-image-2.5-sunburst`（最强）、`gpt-image-2.5-flare`（快）；quality 支持 `xhigh`/`max` | [OpenAI models](https://developers.openai.com/api/docs/models)、[Image generation guide](https://developers.openai.com/api/docs/guides/image-generation) |
| Seedream | 5.0 pro `doubao-seedream-5-0-pro-260628`、5.0 flash `doubao-seedream-5-0-flash-260915`、5.0 lite `doubao-seedream-5-0-260128`；`POST /api/v3/images/generations`；5.0 pro/flash 不支持组图参数 | [火山方舟图片生成教程](https://www.volcengine.com/docs/ark/seedream-4-0-5-0)、[图片生成 API](https://docs.volcengine.com/docs/82379/1541523)；BytePlus 模型 ID 来自第三方文档 [LaoZhang API](https://docs.laozhang.ai/en/api-capabilities/seedream-image) |
| 腾讯云 ASR | `CreateRecTask` / `DescribeTaskStatus`，Version 2019-06-14；大模型 2.0 引擎 `16k_zh_en_2.0`、`16k_zh_en_meeting` | [录音文件识别请求](https://cloud.tencent.com/document/product/1093/37823)、[结果查询](https://cloud.tencent.com/document/product/1093/37822) |
| edge-tts | 7.2.8（2026-03-22） | [PyPI edge-tts](https://pypi.org/project/edge-tts/) |
| Cowork 插件结构 | `.claude-plugin/plugin.json` + `skills/<name>/SKILL.md`；skill 内容中 `${CLAUDE_PLUGIN_ROOT}` 会被替换 | [Plugins reference](https://code.claude.com/docs/en/plugins-reference) |

## 目录结构

```
.claude-plugin/plugin.json         插件清单
.claude-plugin/marketplace.json    单插件市场（source: ./）
skills/*/SKILL.md                  9 个技能
scripts/                           可独立运行的 Python 工具
config.example.env                 配置模板
```

## License

MIT
