---
name: video-download
description: 用 yt-dlp 下载网络视频/音频/字幕（YouTube、B站、抖音、X、Vimeo 等上千站点）。每次下载前自动把 yt-dlp 更新到最新版，下载经 aria2c 多线程加速；遇到登录/人机验证/403 时停止并向用户索取 cookies.txt。当用户给出视频链接并要求下载、保存、提取音频或字幕、或后续要做分析/转写/剪辑时使用。
---

# 网络视频下载（yt-dlp + aria2）

脚本：`${CLAUDE_PLUGIN_ROOT}/scripts/ytdlp_download.py`（下文记作 `$YTDL`）

## 必须遵守的规则

1. **始终用最新 yt-dlp**：脚本每次运行先执行升级（pip `-U yt-dlp[default]` 或 `yt-dlp --update-to`），1 小时内不重复检查。升级失败脚本会直接报错退出——把错误原样告诉用户，**不要**自行加 `--skip-update`，除非用户明确同意用旧版本。
2. **先 aria2 再下载**：脚本已强制 `--downloader aria2c`（每文件最多 16 连接，HLS/DASH 分片并发）。若提示缺少 aria2c，先运行 multimedia-setup 技能安装，不要改用单线程下载。
3. **需要 cookie 时停下来问用户**：退出码 `3` 且输出 `NEED_COOKIES` 表示站点需要登录 / 人机验证 / 403。此时：
   - 立刻停止，**不要**尝试换 IP、伪造请求头、换下载站或其它绕过手段；
   - 把脚本给出的导出指引转述给用户：在已登录该站点的浏览器里用 “Get cookies.txt LOCALLY”(Chrome) 或 “cookies.txt”(Firefox) 扩展导出 Netscape 格式 cookies.txt，并上传或提供路径；
   - 拿到文件后重跑：`python3 $YTDL URL --cookies /path/to/cookies.txt`；
   - 如果已带 cookies 仍报 NEED_COOKIES，说明 cookie 过期或账号无权限，请用户重新导出。
   - 在用户本机（非云端沙箱）也可用 `--cookies-from-browser chrome|firefox|edge|safari`。
4. YouTube 需要 JS 运行时（deno 优先）。脚本会自动探测 deno/node/bun；都没有时运行 setup 安装 deno。

## 常用命令

```bash
# 最佳画质视频 (mp4)
python3 $YTDL "URL" -d downloads

# 限制分辨率 / 只要音频 / 带字幕
python3 $YTDL "URL" --max-height 1080
python3 $YTDL "URL" --audio-only --audio-format mp3
python3 $YTDL "URL" --subs "zh.*,en.*"

# 只下载一段（节省时间）
python3 $YTDL "URL" --sections "*00:01:00-00:02:30"

# 只看元数据（标题、时长、格式列表），不下载
python3 $YTDL "URL" --info

# 整个播放列表 / 每个文件的连接数 / 透传 yt-dlp 原生参数
python3 $YTDL "URL" --playlist
python3 $YTDL "URL" -x 8
python3 $YTDL "URL" -- --embed-thumbnail --sponsorblock-remove all

# 使用 nightly 通道（站点刚改版、stable 失效时）
python3 $YTDL "URL" --channel nightly --force-update
```

成功时 stdout 输出 `{"files": [...]}`，即最终文件路径，可直接交给 gemini-media-analysis、tencent-asr-transcribe、ffmpeg-video-editing 等技能继续处理。

## 退出码

| 码 | 含义 | 处理 |
|---|---|---|
| 0 | 成功 | 使用 `files` 中的路径 |
| 1 | 其它错误 | 把 stderr 尾部给用户；可尝试 `--channel nightly --force-update` |
| 2 | 缺依赖/配置 | 运行 multimedia-setup |
| 3 | NEED_COOKIES | 按规则 3 向用户索取 cookies.txt |

## 配置（可选，写在 `~/.config/claude-multimedia/config.env` 或项目 `.env`）

`YTDLP_CHANNEL=stable|nightly`、`YTDLP_COOKIES_FILE=`、`YTDLP_COOKIES_FROM_BROWSER=`、`YTDLP_PROXY=`、`ARIA2_CONNECTIONS=16`、`YTDLP_UPDATE_INTERVAL=3600`
