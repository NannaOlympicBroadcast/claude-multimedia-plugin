---
name: antigravity-subagent
description: 把多模态任务交给 Google Antigravity CLI（agy）里的 Gemini agent 当子代理执行——Gemini 原生理解图片/视频/音频/文档，并可用内置 generate_image 工具（Nano Banana 2）生图。适合“看素材+出图”一体化任务：看视频写分镜并配插画、根据草图出 UI 设计稿、按参考图批量出海报、图文混合报告。当用户提到 Antigravity、agy、让 Gemini agent/子代理来做时使用。
---

# Antigravity 子代理（agy）

脚本：`${CLAUDE_PLUGIN_ROOT}/scripts/antigravity_agent.py`（下文 `$AG`）。插件还带了一个 Claude 子代理 `antigravity-runner`，可以把整段任务委派给它，它会按下面的流程执行并汇报。

## 它是什么

- Antigravity CLI（命令 `agy`）是 Google 的终端 agent 工具，headless 模式 `agy -p "..." --output-format json|stream-json` 可脚本化调用（来源：antigravity.google/docs/cli/headless）。
- 推理模型可选 Gemini 3.8 / 3.7 / 3.6 Flash、Gemini 3.1 Pro 等（来源：antigravity.google/docs/models）；生图由内置 `generate_image` 工具完成，底层模型为 Nano Banana 2（同上，及 docs/sdk/tools）。
- 自定义 agent 用带 YAML frontmatter 的 Markdown 定义，放在工作区 `.agents/agents/<name>.md`（来源：antigravity.google/docs/subagents）。本插件自带 `media-studio` agent（`antigravity/agents/media-studio.md`）：只开放 `view_file`、`generate_image`、`write_to_file`、`grep_search`，不执行 shell。

## 流程

1. **安装/更新**：`python3 $AG install`。读取官方发布清单 → **aria2c** 下载 → SHA-512 校验 → 安装到 `~/.local/bin/agy`（Windows：`%LOCALAPPDATA%\agy\bin`）。与官方 `curl -fsSL https://antigravity.google/cli/install.sh | bash` 获取的是同一个包，但不改 shell 配置文件。
2. **登录**（二选一，需要用户自己操作）：
   - Google 账号：请用户在**自己的终端**运行一次 `agy`，按提示完成登录（远程 SSH 会显示授权码流程），凭据存入系统钥匙串；
   - Gemini API Key：配置 `GEMINI_API_KEY` 后运行 `python3 $AG auth-apikey`（向 `~/.gemini/antigravity-cli/settings.json` 写入 `"modelProvider": "gemini"`；`--revert` 撤销）。官方说明：只设环境变量不设 modelProvider 不生效。
3. **执行任务**：
   ```bash
   python3 $AG run "看这段视频，挑 4 个关键画面写分镜说明，并为每个分镜用 generate_image 画一张插画" --file clip.mp4 -d out/storyboard
   python3 $AG run "根据草图生成 3 版移动端首页设计稿（亮色/暗色/高对比）" --file sketch.png --effort high
   python3 $AG run "把第 2 张改成赛博朋克风格" --conversation <上次返回的 conversation_id>
   python3 $AG models          # 查看可用模型 slug，再用 --model 指定
   ```
   - 每次运行会新建工作区：`inputs/`（输入文件的硬链接/副本）、`outputs/`、`.agents/agents/media-studio.md`；结束后把新产生的媒体/文档文件复制到 `-d` 目录。
   - 输出 JSON：`response`（agent 的总结）、`files`、`tools_used`、`subagents`（agy 内部又派生的子代理）、`conversation_id`、`permission_notices`、`usage`。
   - 拿到图片后用 Read 查看再向用户展示。
4. `--agent default` 使用 agy 默认 agent（工具更多，但 shell 命令在 headless 模式默认被软拒绝）。

## 规则

- **权限**：headless 模式下需要确认的工具会被“软拒绝”（运行继续、退出码 0、stderr 提示），结果里的 `permission_notices` 会列出来——如实告诉用户。只有用户明确同意时才加 `--dangerously-skip-permissions`（允许 agent 执行任意命令和写文件）。
- **卡住保护**：官方 issue #548 报告 headless 在权限确认时可能挂起；脚本在 `--timeout`（默认 15m）之外再加宽限后强制结束并报错。
- 退出码：`5` = 未登录（按第 2 步让用户登录，不要替用户登录）；`2` = 未安装；`1` = agy 返回非 SUCCESS，原样转述 `error`。
- 运行位置：agy 在哪台机器上运行就用哪台机器的登录态。Claude Code 本地 / `claude remote-control` 会话中即为用户电脑；Cowork 的 shell 在虚拟机里，需要在虚拟机里用 API Key 模式。
- 不要在 agy 失败时偷偷改用别的生图服务；需要换方案时先问用户（可选 image-generation 技能）。
