# Multimedia Studio（Claude / Cowork 多媒体处理插件）

插件名 `multimedia-studio`（`claude-` 前缀是 Anthropic 保留名，第三方插件不能使用）。

一个 Claude Code / Claude Cowork 插件，把常见的多媒体工作流封装成 12 个技能 + 1 个 Claude 子代理 + 一个本地 GPU MCP 服务 + 一组可独立运行的 Python 脚本：

| 技能 | 功能 | 脚本 |
|---|---|---|
| `video-download` | yt-dlp 下载网络视频/音频/字幕；**每次先升级到最新版**；需要登录时停止并**向用户索取 cookies.txt** | `scripts/ytdlp_download.py` |
| `fast-download` | **aria2c 多线程**下载（插件内所有下载都先走 aria2） | `scripts/fast_download.py` |
| `gemini-media-analysis` | Gemini **自动发现最新模型** 🔍 分析音视频（摘要/逐字稿/分镜/高光/OCR） | `scripts/gemini_analyze.py` |
| `tencent-asr-transcribe` | 腾讯云录音文件识别（大模型 2.0）→ SRT/TXT/JSON，说话人分离、长音频分片 | `scripts/tencent_asr.py` |
| `edge-tts-dubbing` | Edge TTS 配音；SRT 逐句对齐配音轨，可直接混入视频（ducking） | `scripts/edge_tts_dub.py` |
| `image-generation` | 通用生图：按配置自动选 **最新 GPT Image / Nano Banana / Seedream** | `scripts/image_gen.py` |
| `video-generation` | AI 生视频：**Gemini Omni 最新模型**（自动发现）或**即梦 dreamina CLI**（Seedance），文/图生视频、首尾帧、视频续写、多轮修改 | `scripts/video_gen.py` |
| `antigravity-subagent` | 把多模态任务交给 **Google Antigravity CLI（agy）** 的 Gemini agent：原生理解图/视频/音频，内置 `generate_image`（Nano Banana 2）出图；插件自带 `media-studio` agent 与 Claude 子代理 `antigravity-runner` | `scripts/antigravity_agent.py`、`antigravity/agents/media-studio.md`、`agents/antigravity-runner.md` |
| `ffmpeg-video-editing` | FFmpeg 剪辑；**输入多个时间点直接抽帧拼成屏幕墙** | `scripts/ffmpeg_tools.py` |
| `librosa-music-analysis` | BPM/节拍、调性、响度、音色、段落结构、能量曲线 | `scripts/music_analyze.py` |
| `local-gpu-mode` | **本地模式**：用用户自己电脑的显卡跑 Whisper 转写、Real-ESRGAN 图片/视频超分、RIFE 插帧、Demucs 分轨 | `scripts/local_gpu.py`、`scripts/local_gpu_mcp.py`（MCP） |
| `multimedia-setup` | 一键安装依赖 + 自检 + 配置 key 指引 | `scripts/setup_env.sh`、`scripts/doctor.py` |

## 安装

### Claude Code

```bash
# 添加本仓库为插件市场并安装
/plugin marketplace add NannaOlympicBroadcast/claude-multimedia-plugin
/plugin install multimedia-studio@multimedia-studio-marketplace

# 或本地调试
claude --plugin-dir /path/to/claude-multimedia-plugin
```

### Claude Cowork

在 Cowork 的插件管理中添加仓库 `NannaOlympicBroadcast/claude-multimedia-plugin` 作为市场后安装 `multimedia-studio`，或上传本目录的 zip。插件不包含顶层 `bin/` 目录，符合 Cowork 的安装要求。

### 依赖

```bash
bash scripts/setup_env.sh      # 安装 aria2、ffmpeg、yt-dlp[default]、deno、edge-tts、librosa…并自检
python3 scripts/doctor.py      # 仅自检
```

## 本地模式（用用户自己的显卡）

| 运行位置 | 连接方式 |
|---|---|
| Claude Code 跑在用户电脑上（CLI、桌面 App，或 `claude remote-control` 后从手机/网页远程驱动） | 技能直接调用 `scripts/local_gpu.py`，进程就在用户电脑上 |
| Claude Cowork 桌面 App | shell 在隔离 VM 中、拿不到宿主机 GPU；插件通过 `.mcp.json` 注册的 `local-gpu` MCP 服务在宿主机原生运行，Claude 调用其工具（`whisper_transcribe`、`upscale_video` 等，长任务返回 job_id 轮询） |
| 云端容器会话 | 连不到用户电脑，需改到上面两种会话中执行 GPU 任务 |

| 引擎 | GPU 后端 | 说明 |
|---|---|---|
| Whisper | faster-whisper（CTranslate2, NVIDIA CUDA 12 + cuDNN 9）/ mlx-whisper（Apple Silicon） | 默认 `large-v3-turbo`，模型从 Hugging Face 经 aria2c 下载，支持 `HF_ENDPOINT` 镜像 |
| 超分辨率 | Real-ESRGAN ncnn-vulkan（NVIDIA / AMD / Intel / Apple，经 Vulkan/MoltenVK） | 图片、文件夹、视频（分段处理、可续跑、保留音轨、自动选硬件编码器） |
| 插帧 | RIFE ncnn-vulkan（默认 rife-v4.6） | 2× 或指定目标帧率 |
| 分轨 | Demucs（PyTorch CUDA / MPS） | 权重预先用 aria2c 下载到 torch hub 缓存 |

没有可用 GPU 时一律以退出码 4 停止，只有用户明确同意才用 `--allow-cpu`。MCP 服务的 Python 命令可在插件配置 `python_command` 中修改（Windows 填 `python` 或 `py`）。

```bash
python3 scripts/local_gpu.py detect
python3 scripts/local_gpu.py install realesrgan && python3 scripts/local_gpu.py upscale-video in.mp4 --model realesr-animevideov3 --scale 2
python3 scripts/local_gpu.py install whisper && python3 scripts/local_gpu.py whisper talk.mp4 --language zh
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
| faster-whisper | 1.2.1（2025-10-31）；GPU 需 cuBLAS for CUDA 12 + cuDNN 9；支持 large-v3 / turbo / distil-large-v3 | [PyPI faster-whisper](https://pypi.org/project/faster-whisper/) |
| Real-ESRGAN ncnn-vulkan | 最新 release v0.2.5.0（包名 `realesrgan-ncnn-vulkan-20220424-*.zip`） | [Real-ESRGAN releases](https://github.com/xinntao/Real-ESRGAN/releases) |
| RIFE ncnn-vulkan | release `20221029` | [rife-ncnn-vulkan releases](https://github.com/nihui/rife-ncnn-vulkan/releases) |
| Demucs | 4.1.0；权重托管在 `dl.fbaipublicfiles.com/demucs/` | [PyPI demucs](https://pypi.org/project/demucs/) |
| Gemini Omni | `gemini-omni-1.1-flash` 于 2026-08-27 GA（视频续写、首尾帧、分辨率参数）；2026-06-30 发布 `gemini-omni-flash-preview`；接口 `POST /v1beta/interactions`，单段 3–10 秒，多轮续写最长 40 秒 | [Gemini changelog](https://ai.google.dev/gemini-api/docs/changelog)、[Omni guide](https://ai.google.dev/gemini-api/docs/omni) |
| 即梦 CLI | 官方 `dreamina` CLI，安装 `curl -fsSL https://jimeng.jianying.com/cli \| bash`，OAuth 设备码登录，`query_result --submit_id --download_dir`；当前版本 1.4.18（2026-09-10，“视频生成支持比例控制”） | [Dreamina CLI 官方页](https://dreamina.capcut.com/tools/dreamina-cli)、官方 version.json |
| Antigravity CLI | `agy` headless：`-p`、`--output-format json/stream-json`、`--agent`、`--model`、`--conversation`、`--print-timeout`（默认 5m）、`--dangerously-skip-permissions`；无法获批的工具会被软拒绝 | [Headless mode](https://antigravity.google/docs/cli/headless) |
| Antigravity 安装/认证 | `curl -fsSL https://antigravity.google/cli/install.sh \| bash`（发布清单 + SHA-512 校验，当前 1.2.14）；API Key 模式需在 settings.json 设 `modelProvider: gemini` | [Installation & auth](https://antigravity.google/docs/cli/install)、官方 install.sh |
| Antigravity 生图/子代理 | `generate_image` 内置工具使用 Nano Banana 2；自定义 agent 放 `.agents/agents/<name>.md` | [Models](https://antigravity.google/docs/models)、[SDK tools](https://antigravity.google/docs/sdk/tools)、[Subagents](https://antigravity.google/docs/subagents) |
| Cowork 执行模型 | shell 命令在隔离 VM 中执行；本地插件 MCP 服务在设备上原生运行 | [Aurascape: Claude Cowork data access](https://aurascape.ai/answers/claude-cowork-data-access) |
| Cowork 插件结构 | `.claude-plugin/plugin.json` + `skills/<name>/SKILL.md`；skill 内容中 `${CLAUDE_PLUGIN_ROOT}` 会被替换 | [Plugins reference](https://code.claude.com/docs/en/plugins-reference) |

## 目录结构

```
.claude-plugin/plugin.json         插件清单
.claude-plugin/marketplace.json    单插件市场（source: ./）
skills/*/SKILL.md                  12 个技能
agents/antigravity-runner.md       Claude 子代理：委派任务给 Antigravity
antigravity/agents/media-studio.md Antigravity 侧的多模态媒体 agent
.mcp.json                          本地 GPU MCP 服务（local-gpu）
scripts/                           可独立运行的 Python 工具
config.example.env                 配置模板
```

## License

MIT
