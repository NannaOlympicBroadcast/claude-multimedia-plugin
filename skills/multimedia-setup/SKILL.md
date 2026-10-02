---
name: multimedia-setup
description: 安装/检查 multimedia-studio 插件的运行环境（aria2、ffmpeg、deno、yt-dlp 最新版、edge-tts、librosa 等）并引导配置 API Key（Gemini、OpenAI、腾讯云、火山引擎/BytePlus）。首次使用插件、脚本报“缺少依赖/配置”(退出码 2)、或用户询问如何配置时使用。
---

# 环境安装与配置

## 1. 安装依赖

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/setup_env.sh"          # 安装 + 自检
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py"           # 只自检（依赖版本 + 已配置哪些 key）
```

`setup_env.sh` 会：用系统包管理器（apt / dnf / brew / choco / winget）安装 `aria2`、`ffmpeg`；`pip install -U` 安装 `yt-dlp[default]`、`deno`(YouTube 需要的 JS 运行时)、`edge-tts`、`librosa`、`soundfile`、`numpy`、`requests`、`matplotlib`。
安装失败（无 root、无网络、包管理器不可用）时脚本会打印失败项——把输出原样告诉用户，让用户手动安装，**不要**用替代工具绕过（例如没有 aria2 时不能改用单线程下载）。

## 2. 配置 Key

在 `~/.config/claude-multimedia/config.env`（或项目根目录 `.env`，或环境变量）写入，模板见 `${CLAUDE_PLUGIN_ROOT}/config.example.env`：

| 功能 | 变量 | 获取 |
|---|---|---|
| Gemini 音视频分析 / Nano Banana | `GEMINI_API_KEY` | https://aistudio.google.com/apikey |
| GPT Image | `OPENAI_API_KEY`（可选 `OPENAI_BASE_URL`） | https://platform.openai.com/api-keys |
| Seedream（国内） | `ARK_API_KEY` | 火山引擎方舟控制台 → API Key 管理，并开通 Seedream 模型 |
| Seedream（海外） | `BYTEPLUS_API_KEY`，`SEEDREAM_REGION=intl` | BytePlus ModelArk 控制台 |
| 腾讯云转写 | `TENCENTCLOUD_SECRET_ID` / `TENCENTCLOUD_SECRET_KEY` | https://console.cloud.tencent.com/cam/capi ，开通语音识别 |
| Edge TTS | 无需 key | — |

**安全**：不要让用户把 key 粘贴到对话里；指导用户自己编辑配置文件（`chmod 600`）。真实环境变量优先于配置文件。

## 3. 验证

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gemini_analyze.py" --list-models
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/image_gen.py" --list-models
```
