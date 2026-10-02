---
name: video-generation
description: AI 生成视频：用 Google Gemini Omni 最新模型（models.list 自动发现，当前为 gemini-omni-1.1-flash）或即梦官方 dreamina CLI（Seedance 系列）做文生视频、图生视频、首尾帧过渡、多图参考、视频编辑/续写、对话式多轮修改。当用户要求“生成视频/做个短片/让图片动起来/图生视频/首尾帧/续写视频/用即梦或 Seedance 生成”时使用。
---

# AI 生视频（Gemini Omni / 即梦 CLI）

脚本：`${CLAUDE_PLUGIN_ROOT}/scripts/video_gen.py`（下文 `$VG`）。**两种服务都会消耗付费额度/积分——提交真实生成任务前先向用户确认**（提示词、比例、清晰度、张数）。

## 选哪个

| | Gemini Omni | 即梦 dreamina CLI |
|---|---|---|
| 认证 | `GEMINI_API_KEY` | 即梦账号 OAuth 设备码登录 |
| 能力 | 文/图生视频、首尾帧、多图参考、上传视频编辑或续写（≤10 s）、`--previous-id` 多轮修改；单段 3–10 s，多轮续写最长 40 s；分辨率 360p/720p/1080p/4k；比例 16:9 / 9:16 | text2video、image2video、frames2video、multiframe2video（多图故事）、multimodal2video（全能参考，图/视频/音频）；Seedance 2.5 需会员；生成要排队 |
| 适合 | 需要对话式迭代修改、可编程调用 | 国内用户、中文提示词、Seedance 画质、已有即梦会员积分 |

`--provider` 未指定时：`VIDEO_PROVIDER=auto` → 配了 `GEMINI_API_KEY` 用 Gemini，否则用已安装的即梦 CLI。

## Gemini Omni

```bash
python3 $VG gemini "雨夜霓虹街头，一只橘猫走过水洼，低机位推镜" --aspect 16:9 --resolution 720p
python3 $VG gemini "让照片里的人转身微笑" --image photo.jpg
python3 $VG gemini "从白天平滑过渡到夜晚" --image first.jpg --image last.jpg          # 首尾帧
python3 $VG gemini "两只猫在玩毛线球" --image cat1.png --image cat2.png --task reference_to_video
python3 $VG gemini "当人触碰镜子时让镜面泛起涟漪" --video clip.mp4 --task edit
python3 $VG gemini "继续这个场景，镜头慢慢拉远" --video clip.mp4 --task extend
python3 $VG gemini "把小提琴变成透明的" --previous-id v1_xxx                      # 多轮修改上一条结果
python3 $VG gemini x --list-models
```
- 调用 `POST /v1beta/interactions`（来源：ai.google.dev/gemini-api/docs/omni）。模型按版本号自动选最新、稳定版优先；`--model` / `GEMINI_VIDEO_MODEL` 可固定。
- 1080p/4k 默认用 URI 交付：轮询文件到 ACTIVE 后用 **aria2c** 下载；小视频以 base64 内联返回。
- 输出 JSON 含 `interaction_id`，记下来供下一轮 `--previous-id` 修改。
- 上传视频编辑/续写在欧洲经济区、瑞士、英国不可用（官方文档说明）。

## 即梦 dreamina CLI

1. **安装 / 更新**：`python3 $VG jimeng-install`。会从官方 CDN 用 aria2c 下载与平台匹配的二进制、官方 SKILL.md 和 version.json，**不修改 shell 配置文件**。官方原始安装方式是 `curl -fsSL https://jimeng.jianying.com/cli | bash`（来源：dreamina.capcut.com/tools/dreamina-cli）。
2. **登录**（OAuth 设备码）：`python3 $VG jimeng-login` → 把输出的 `verification_uri` 和 `user_code` 原样告诉用户，等用户在浏览器授权后运行 `python3 $VG jimeng-login --device-code <device_code>`，然后**主动告诉用户登录成功**并展示 `user_credit` 余额。
3. **先看帮助再提交**：每个子命令的参数和支持的模型/时长/比例/分辨率以 `dreamina <子命令> -h` 为准（官方 SKILL.md 要求，不要凭记忆硬编码）。在用户电脑上运行 `dreamina text2video -h` 等查看。
4. **生成**：
   ```bash
   python3 $VG jimeng text2video --prompt "海边日落延时摄影，金色光线" --ratio 16:9
   python3 $VG jimeng image2video --prompt "让人物眨眼微笑" -- <从 dreamina image2video -h 查到的图片参数>
   python3 $VG jimeng frames2video --prompt "季节变换" -- <首帧/尾帧参数>
   python3 $VG jimeng multimodal2video --prompt "..." -- <参考素材参数，可选模型>
   ```
   `--prompt`、`--ratio`、`--session`、`--poll` 由脚本传递；其余参数放在 `--` 之后原样交给 dreamina。Seedance 2.5 的 image2video / frames2video **不要传 `--ratio`**（输出跟随首帧比例，CLI 会拒绝）。
5. **结果判定**：只有 `gen_status=success` 才算成功；`querying` 只表示已提交。超过 `--poll` 仍未完成时记下 `submit_id`，稍后 `python3 $VG jimeng-result --submit-id <id>`。成功后结果 URL 经 aria2c 下载到 `-d` 目录（若输出中拿不到 URL，则用 dreamina 自带的 `--download_dir`）。
6. **错误处理**：
   - 退出码 `5` / `NEED_LOGIN`：回到第 2 步登录；
   - `AigcComplianceConfirmationRequired`：请用户先登录即梦网页版完成该模型的一次性授权确认再重试；
   - `gen_status=fail`：把 `fail_reason` 原样告诉用户（如真人人脸受限），不要换别的服务偷偷重试。

## 联动

- 先用 image-generation 生成首帧/参考图 → 本技能图生视频 → ffmpeg-video-editing 拼接多段、加字幕 → edge-tts-dubbing 配旁白 → local-gpu-mode 超分到 4K / 插帧到 60fps。
