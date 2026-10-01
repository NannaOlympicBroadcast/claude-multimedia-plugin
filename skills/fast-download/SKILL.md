---
name: fast-download
description: 用 aria2c 多线程/多连接加速下载任意直链文件（视频、音频、图片、模型、压缩包、m3u8 以外的 HTTP/FTP 资源），支持断点续传、批量下载、自定义请求头和 cookies。本插件中所有“下载类”操作都必须先走 aria2；当用户要求下载某个 URL、批量下载文件，或其它技能需要拉取远程素材时使用。
---

# aria2 多线程下载

脚本：`${CLAUDE_PLUGIN_ROOT}/scripts/fast_download.py`

**规则：本插件所有下载动作（直链文件、生图结果 URL、参考图 URL、yt-dlp 视频）一律经 aria2c 多连接下载。**
不要用 `curl`/`wget`/`requests` 单线程下载代替；若 aria2c 不存在，先执行 multimedia-setup 安装，安装失败则把错误告诉用户。网页视频（需要解析的站点链接）请用 video-download 技能，它内部同样使用 aria2。

```bash
DL="${CLAUDE_PLUGIN_ROOT}/scripts/fast_download.py"

python3 $DL "https://example.com/file.mp4" -o downloads/file.mp4      # 单文件，默认 16 连接
python3 $DL URL1 URL2 URL3 -d downloads -j 4                             # 多文件并行 4 个
python3 $DL -i urls.txt -d downloads                                     # 列表文件（每行一个 URL）
python3 $DL URL -o a.zip --header "Referer: https://site/" --header "User-Agent: Mozilla/5.0"
python3 $DL URL -o a.mp4 --cookies cookies.txt                           # 需要登录的直链
python3 $DL URL -o a.bin -x 8                                            # 自定义每文件连接数(1-16)
```

- 默认参数：`-x16 -s16 -k1M --continue=true --file-allocation=none --max-tries=5`，中断后重跑同一命令即可续传。
- 自动继承 `HTTPS_PROXY` / `SSL_CERT_FILE`（也可用 `ARIA2_ALL_PROXY`、`ARIA2_CA_CERT` 单独指定）。
- 返回 403/401：告诉用户该链接需要鉴权，请其提供 cookies 或 Referer，**不要**尝试绕过。
- 在 Python 代码里复用：`from mmcommon import aria2_download; aria2_download(url, dest)`。
