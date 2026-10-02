---
name: local-gpu-mode
description: 本地模式——连接用户自己的电脑，用本机显卡（NVIDIA CUDA / Apple Silicon / AMD·Intel Vulkan）运行 Whisper 语音转写、Real-ESRGAN 图片/视频超分辨率、RIFE 插帧补帧、Demucs 人声/伴奏分离。当用户提到“用我的显卡/本地 GPU/本地跑/离线”、超分、放大画质、4K 修复、补帧到 60fps、Whisper 本地转写、分离人声伴奏时使用。
---

# 本地模式（用户电脑上的 GPU）

引擎脚本：`${CLAUDE_PLUGIN_ROOT}/scripts/local_gpu.py`（下文 `$LG`）
MCP 服务：插件自带的 `local-gpu` 服务器（`scripts/local_gpu_mcp.py`），工具名 `gpu_detect`、`gpu_install`、`whisper_transcribe`、`upscale_image`、`upscale_video`、`interpolate_video`、`separate_stems`、`gpu_job_status`、`gpu_job_list`、`gpu_job_cancel`。

## 第一步：判断“怎么连到用户的电脑”

| 当前运行环境 | 怎么用 GPU |
|---|---|
| Claude Code 在用户电脑上运行（终端 CLI、桌面 App 的 Code、或 `claude remote-control` 被手机/网页远程驱动） | 直接用 Bash 运行 `python3 $LG ...`，脚本就在用户电脑上执行 |
| Claude Cowork（桌面 App） | Cowork 的 shell 在隔离虚拟机里，**看不到宿主机显卡**；改用插件的 `local-gpu` MCP 工具——本地插件 MCP 服务在用户电脑上原生运行（来源：Aurascape《What Data Can Claude Cowork Access》2026）。路径必须是**宿主机上的绝对路径**（用户连接的文件夹里的路径） |
| 云端会话（claude.ai/code 网页、云端容器） | 云端容器连不到用户电脑。告诉用户：在自己电脑上打开 Claude Desktop，或在要处理的文件夹里运行 `claude remote-control`，在那个会话里继续本地任务；当前会话可以先完成不依赖 GPU 的部分（下载、剪辑方案、文案） |

判断方法：先调用 `gpu_detect` MCP 工具（若可用），或运行 `python3 $LG detect`。看到 `nvidia_gpus` 为空、Vulkan 设备只有 `llvmpipe/SwiftShader`（`software: true`）、且不是 Apple Silicon，说明当前位置没有可用 GPU——**不要**在这里硬跑。

## 第二步：安装需要的引擎（下载全部经 aria2c）

```bash
python3 $LG install realesrgan      # Real-ESRGAN ncnn-vulkan：自动查询 GitHub 最新 release 下载对应系统的包
python3 $LG install rife            # RIFE ncnn-vulkan 插帧
python3 $LG install whisper --model large-v3-turbo   # NVIDIA: faster-whisper + cuBLAS/cuDNN 9 (pip)；Apple Silicon: mlx-whisper
python3 $LG install demucs          # Demucs + 预取 htdemucs 权重；torch 需为 CUDA/MPS 版
```
MCP 等价：`gpu_install {"component": "whisper", "model": "large-v3-turbo"}`（后台任务，用 `gpu_job_status` 看结果）。
国内下载 Hugging Face 模型慢时设置 `HF_ENDPOINT=https://hf-mirror.com`。GitHub API 不通时可用 `--url` 指定 release zip。

## 第三步：执行任务

```bash
# Whisper 转写（自动：NVIDIA→faster-whisper CUDA float16 + 批处理；Apple Silicon→mlx-whisper）
python3 $LG whisper talk.mp4 --model large-v3-turbo --language zh --initial-prompt "以下是简体中文普通话。" -o out/talk
python3 $LG whisper en.mp4 --task translate                    # 直接译成英文
python3 $LG whisper a.mp3 --model large-v3 --word-timestamps   # 更准、更慢

# 图片超分（单张或整个文件夹）
python3 $LG upscale photo.jpg --model realesrgan-x4plus -o photo_x4.png
python3 $LG upscale anime/ --model realesrgan-x4plus-anime --scale 2 -o anime_x2/

# 视频超分（分段处理 → 磁盘占用有上限、可断点续跑；保留音轨；自动选硬件编码器 NVENC/VideoToolbox/AMF/QSV）
python3 $LG upscale-video ep01.mp4 --model realesr-animevideov3 --scale 2 -o ep01_x2.mp4
python3 $LG upscale-video old.mp4 --model realesrgan-x4plus --scale 2 --tile 256 -o old_x2.mp4

# 插帧
python3 $LG interpolate clip.mp4 --factor 2 -o clip_2x.mp4
python3 $LG interpolate clip.mp4 --fps 60 --uhd -o clip_60.mp4       # 4K 素材加 --uhd

# 人声/伴奏分离
python3 $LG separate song.mp3 --two-stems vocals -o stems/
python3 $LG separate song.mp3 --model htdemucs_6s -o stems/          # 6 轨（含吉他、钢琴）
```

模型选择建议：真人实拍用 `realesrgan-x4plus`（慢，质量高）；动画/卡通/游戏画面用 `realesr-animevideov3`（快，原生支持 2/3/4 倍）；显存不足（out of memory / vkAllocate 失败）时加 `--tile 256` 或更小。

## 规则

1. **没有 GPU 不偷偷用 CPU**：找不到可用 GPU 时脚本以退出码 `4` 结束。把报错原样告诉用户，建议检查驱动（NVIDIA 驱动 + CUDA 12；AMD/Intel 需 Vulkan 驱动），或问用户是否接受 CPU 运行（慢 10–100 倍）。只有用户明确同意后才加 `--allow-cpu` / `allow_cpu: true`。
2. 长任务（视频超分、插帧、长音频）在 MCP 下是后台任务：拿到 `job_id` 后用 `gpu_job_status {"job_id": ..., "wait_seconds": 55}` 轮询，进度信息在 `log_tail`。用户要求停止时调用 `gpu_job_cancel`。
3. 视频超分前先 `ffmpeg_tools.py probe` 看分辨率和时长，向用户说明预计耗时（脚本每段完成会打印已用/剩余时间）；4K 输出很大，确认磁盘空间。
4. 输出默认写在输入文件旁边（`*_x2.mp4`、`*_x4.png`、`stems/`），不要覆盖原文件。
5. 与其它技能联动：Whisper 生成的 `.srt` 可交给 edge-tts-dubbing 配音、`ffmpeg_tools.py subs` 烧录；Demucs 分出的伴奏可交给 librosa-music-analysis；云端模式下需要转写时用 tencent-asr-transcribe。

## 退出码

`0` 成功；`1` 运行错误（看 stderr）；`2` 引擎未安装/配置缺失（先 install）；`4` 没有可用 GPU（按规则 1 处理）。
