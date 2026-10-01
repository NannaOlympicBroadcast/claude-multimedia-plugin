---
name: image-generation
description: 通用 AI 生图/改图工具：根据配置自动选择 OpenAI 最新 GPT Image 模型、Google Nano Banana（Gemini image 系列）或字节 Seedream（火山引擎方舟 / BytePlus ModelArk），支持文生图、参考图编辑、多图融合、多张/组图、比例与分辨率、透明背景。当用户要求生成图片、海报、封面、插画、头图、改图、换背景、图生图时使用。
---

# 通用生图（GPT Image / Nano Banana / Seedream）

脚本：`${CLAUDE_PLUGIN_ROOT}/scripts/image_gen.py`（下文 `$IMG`）

## 自动选择逻辑

1. **服务商**：`IMAGE_PROVIDER=auto`（默认）时按 `IMAGE_PROVIDER_ORDER`（默认 `openai,gemini,seedream`）取第一个已配置 key 的服务商；也可 `--provider openai|gemini|seedream` 指定。
2. **模型**（`IMAGE_TIER=quality|fast`，或 `--tier`）：
   - OpenAI：调用 `GET /v1/models` 找出全部 `gpt-image-*`，按版本号最高优先；quality 偏好 `sunburst`、fast 偏好 `flare`/`mini`。截至 2026-09 官方最新为 `gpt-image-2.5-sunburst`（最强）/ `gpt-image-2.5-flare`（快）（来源：developers.openai.com/api/docs/models）。
   - Gemini（Nano Banana）：调用 models.list 找 `gemini-*-image`，quality 优先 pro（Nano Banana Pro `gemini-3-pro-image`），fast 优先 flash（Nano Banana 2 `gemini-3.1-flash-image`）（来源：ai.google.dev/gemini-api/docs/models）。
   - Seedream：方舟没有用 API Key 可调用的模型列表接口，脚本内置按发布时间排序的候选（5.0 pro `…-260628` / 5.0 flash `…-260915` / 5.0 lite `…-260128` / 4.5 / 4.0），国内火山引擎带 `doubao-` 前缀（来源：火山方舟图片生成教程）。若某模型未开通（404/ModelNotOpen），自动试下一个并在 stderr 说明。
   - 任何服务商都可用 `--model` 或 `OPENAI_IMAGE_MODEL` / `GEMINI_IMAGE_MODEL` / `SEEDREAM_MODEL` 固定。
3. `python3 $IMG --list-models` 查看各服务商当前排序。

## 用法

```bash
python3 $IMG "雨夜霓虹灯下的上海弄堂，电影感，35mm" --aspect 16:9
python3 $IMG "极简风格的咖啡品牌 logo，透明背景" --provider openai --transparent --format png
python3 $IMG "把照片背景换成冰岛黑沙滩，保留人物" --ref photo.jpg --provider gemini --resolution 2K
python3 $IMG "融合图1的人物与图2的服装" --ref a.jpg --ref b.jpg --provider seedream
python3 $IMG "同一只橘猫的春夏秋冬四张插画" -n 4 --provider seedream --tier fast
python3 $IMG "产品海报，标题写‘秋季新品’" --quality high --aspect 3:4 -d out/posters --name autumn
```

参数：`--aspect`(1:1/16:9/9:16/4:3/3:2…)、`--resolution`(1K/2K/4K，Gemini/Seedream；OpenAI gpt-image-2+ 用于计算长边)、`--size WxH`(直接指定)、`--quality`(OpenAI: low/medium/high/xhigh/max/auto)、`--format`、`--transparent`(OpenAI)、`--watermark`(Seedream 保留水印，默认关闭)、`-n`。

输出 JSON：`{"provider", "model", "files": [...]}`。返回 URL 的服务（Seedream）结果会经 **aria2c** 下载到本地（遵守“下载先走 aria2”规则）。生成后用 Read 工具查看图片再向用户展示/说明。

## 配置

```
IMAGE_PROVIDER=auto            # auto|openai|gemini|seedream
IMAGE_PROVIDER_ORDER=openai,gemini,seedream
IMAGE_TIER=quality             # quality|fast
OPENAI_API_KEY=  OPENAI_BASE_URL=https://api.openai.com/v1
GEMINI_API_KEY=
ARK_API_KEY=                   # 火山引擎方舟（国内，SEEDREAM_REGION=cn）
BYTEPLUS_API_KEY=              # BytePlus ModelArk（海外，SEEDREAM_REGION=intl）
```

## 错误处理

- 未配置任何 key → 退出码 2，告诉用户需要配置哪个 key。
- 内容审核拒绝、额度不足、模型未开通等错误原样转述，不要换用占位图或伪造结果。
