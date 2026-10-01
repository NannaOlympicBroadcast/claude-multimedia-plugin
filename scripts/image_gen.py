#!/usr/bin/env python3
"""Universal image generation / editing: OpenAI GPT Image, Google Nano Banana (Gemini image),
ByteDance Seedream (火山引擎方舟 / BytePlus ModelArk).

Provider + model are chosen automatically from config:
  IMAGE_PROVIDER        auto | openai | gemini | seedream          (default auto)
  IMAGE_PROVIDER_ORDER  priority for auto                           (default openai,gemini,seedream)
  IMAGE_TIER            quality | fast                              (default quality)
  OPENAI_IMAGE_MODEL / GEMINI_IMAGE_MODEL / SEEDREAM_MODEL           (pin a model, optional)
With auto, the first provider whose API key is configured is used; within a provider the
newest model is discovered via the provider's models.list API (OpenAI, Gemini). Seedream has
no key-scoped list endpoint, so a dated candidate list (newest first) is tried in order.

Examples
  image_gen.py "赛博朋克风格的上海外滩夜景，电影感" --aspect 16:9
  image_gen.py "把背景换成海边日落" --ref photo.jpg --provider gemini
  image_gen.py "四格漫画：猫咪学做饭" -n 4 --provider seedream --tier fast
  image_gen.py --list-models
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import mimetypes
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mmcommon import MMError, aria2_download, cfg, main_guard  # noqa: E402

# Seedream model ids, newest/best first. Sources: 火山方舟「图片生成教程」 & BytePlus ModelArk docs.
SEEDREAM = {
    "cn": {
        "base": "https://ark.cn-beijing.volces.com/api/v3",
        "models": {
            "pro": "doubao-seedream-5-0-pro-260628",
            "flash": "doubao-seedream-5-0-flash-260915",
            "lite": "doubao-seedream-5-0-260128",
            "4.5": "doubao-seedream-4-5-251128",
            "4.0": "doubao-seedream-4-0-250828",
        },
    },
    "intl": {
        "base": "https://ark.ap-southeast.bytepluses.com/api/v3",
        "models": {
            "pro": "seedream-5-0-pro-260628",
            "flash": "seedream-5-0-flash-260915",
            "lite": "seedream-5-0-260128",
            "4.5": "seedream-4-5-251128",
            "4.0": "seedream-4-0-250828",
        },
    },
}
SEEDREAM_ORDER = {"quality": ["pro", "lite", "flash", "4.5", "4.0"], "fast": ["flash", "lite", "4.5", "pro", "4.0"]}
SEEDREAM_NO_SEQUENTIAL = ("pro", "flash")  # 5.0 pro/flash reject sequential_image_generation


def _ver(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+(?:\.\d+)*)", text)
    return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)


# --------------------------------------------------------------------------- helpers
def ref_to_bytes(ref: str) -> tuple[bytes, str]:
    if re.match(r"^https?://", ref):
        tmp = Path(".mm_cache") / f"ref_{int(time.time()*1000)}{Path(ref.split('?')[0]).suffix or '.png'}"
        aria2_download(ref, tmp)
        ref = str(tmp)
    p = Path(ref).expanduser()
    if not p.is_file():
        raise MMError(f"参考图不存在: {ref}")
    return p.read_bytes(), mimetypes.guess_type(str(p))[0] or "image/png"


def save_outputs(items: list[tuple[str, bytes | str]], out_dir: Path, stem: str) -> list[str]:
    """items: (ext, bytes) or ('url', url). URLs are fetched with aria2c."""
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for i, (kind, payload) in enumerate(items, 1):
        if kind == "url":
            ext = Path(str(payload).split("?")[0]).suffix or ".png"
            dest = out_dir / f"{stem}_{i}{ext}"
            aria2_download(str(payload), dest)
        else:
            dest = out_dir / f"{stem}_{i}.{kind}"
            dest.write_bytes(payload)  # type: ignore[arg-type]
        saved.append(str(dest))
    return saved


def aspect_dims(aspect: str, long_edge: int, mult: int = 16) -> tuple[int, int]:
    w, h = (float(x) for x in aspect.split(":"))
    if w >= h:
        W, H = long_edge, long_edge * h / w
    else:
        W, H = long_edge * w / h, long_edge
    return int(round(W / mult) * mult), int(round(H / mult) * mult)


# --------------------------------------------------------------------------- OpenAI
class OpenAIProvider:
    name = "openai"
    TIER = {"quality": {"sunburst": 3, "": 2, "flare": 1, "mini": 0},
            "fast": {"flare": 3, "mini": 2, "": 1, "sunburst": 0}}

    def __init__(self) -> None:
        self.key = cfg("OPENAI_API_KEY")
        self.base = (cfg("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")

    def available(self) -> bool:
        return bool(self.key)

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    def ranked(self, tier: str) -> list[str]:
        r = requests.get(f"{self.base}/models", headers=self._h(), timeout=60)
        if r.status_code != 200:
            raise MMError(f"OpenAI models.list 失败 HTTP {r.status_code}: {r.text[:500]}")
        ids = [m["id"] for m in r.json().get("data", []) if re.match(r"^gpt-image-\d", m.get("id", ""))]
        def key(mid: str):
            suffix = re.sub(r"^gpt-image-[\d.]+-?", "", mid)
            t = self.TIER[tier].get(suffix, -1)
            return (_ver(mid), t) if tier == "quality" else (t, _ver(mid))
        return sorted(ids, key=key, reverse=True)

    def pick(self, tier: str) -> str:
        pinned = cfg("OPENAI_IMAGE_MODEL")
        if pinned:
            return pinned
        ranked = self.ranked(tier)
        if not ranked:
            raise MMError("OpenAI 账号下没有可用的 gpt-image-* 模型（可能需要组织验证），或设置 OPENAI_IMAGE_MODEL。")
        return ranked[0]

    def generate(self, a, model: str) -> list[tuple[str, bytes | str]]:
        if a.size:
            size = a.size
        elif a.aspect and _ver(model) >= (2,):
            # gpt-image-2+ accepts free sizes (multiples of 16, edge <= 3840, ratio 1:3..3:1).
            edge = {"1K": 1024, "2K": 2048, "4K": 3840}.get((a.resolution or "").upper(), 1536)
            size = "x".join(map(str, aspect_dims(a.aspect, edge)))
        elif a.aspect:
            r = eval_ratio(a.aspect)
            size = "1024x1024" if abs(r - 1) < 0.05 else ("1536x1024" if r > 1 else "1024x1536")
        else:
            size = "auto"
        fields = {"model": model, "prompt": a.prompt, "n": a.n, "size": size}
        if a.quality:
            fields["quality"] = a.quality
        if a.format:
            fields["output_format"] = a.format
        if a.transparent:
            fields["background"] = "transparent"
        if a.ref:
            files = []
            for ref in a.ref:
                data, mime = ref_to_bytes(ref)
                files.append(("image[]", (Path(ref).name or "ref.png", data, mime)))
            r = requests.post(f"{self.base}/images/edits", headers=self._h(),
                              data={k: str(v) for k, v in fields.items()}, files=files, timeout=600)
        else:
            r = requests.post(f"{self.base}/images/generations", headers=self._h(), json=fields, timeout=600)
        if r.status_code != 200:
            raise MMError(f"OpenAI 图片接口 HTTP {r.status_code}: {r.text[:1000]}")
        ext = a.format or "png"
        out = []
        for d in r.json().get("data", []):
            if d.get("b64_json"):
                out.append((ext, base64.b64decode(d["b64_json"])))
            elif d.get("url"):
                out.append(("url", d["url"]))
        return out


def eval_ratio(aspect: str) -> float:
    w, h = (float(x) for x in aspect.split(":"))
    return w / h


# --------------------------------------------------------------------------- Gemini (Nano Banana)
class GeminiProvider:
    name = "gemini"
    TIER = {"quality": {"pro": 2, "flash": 1, "flash-lite": 0},
            "fast": {"flash": 2, "flash-lite": 1, "pro": 0}}

    def __init__(self) -> None:
        self.key = cfg("GEMINI_API_KEY")
        self.base = (cfg("GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com").rstrip("/")

    def available(self) -> bool:
        return bool(self.key)

    def ranked(self, tier: str) -> list[str]:
        models, token = [], None
        while True:
            params = {"pageSize": 1000, **({"pageToken": token} if token else {})}
            r = requests.get(f"{self.base}/v1beta/models", headers={"x-goog-api-key": self.key}, params=params, timeout=60)
            if r.status_code != 200:
                raise MMError(f"Gemini models.list 失败 HTTP {r.status_code}: {r.text[:500]}")
            data = r.json()
            models += data.get("models", [])
            token = data.get("nextPageToken")
            if not token:
                break
        cands = []
        for m in models:
            name = m.get("name", "").split("/", 1)[-1]
            mm = re.match(r"^gemini-(\d+(?:\.\d+)?)-(pro|flash-lite|flash)-image(?:-(preview.*))?$", name)
            if not mm or "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            ver = tuple(int(x) for x in mm.group(1).split("."))
            t = self.TIER[tier][mm.group(2)]
            stable = 0 if mm.group(3) else 1
            cands.append(((t, ver, stable) if tier == "quality" else (ver, t, stable), name))
        return [n for _, n in sorted(cands, reverse=True)]

    def pick(self, tier: str) -> str:
        pinned = cfg("GEMINI_IMAGE_MODEL")
        if pinned:
            return pinned
        ranked = self.ranked(tier)
        if not ranked:
            raise MMError("Gemini models.list 中没有 *-image 模型，请设置 GEMINI_IMAGE_MODEL。")
        return ranked[0]

    def generate(self, a, model: str) -> list[tuple[str, bytes | str]]:
        parts = []
        for ref in a.ref or []:
            data, mime = ref_to_bytes(ref)
            parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}})
        parts.append({"text": a.prompt})
        img_cfg = {}
        if a.aspect:
            img_cfg["aspectRatio"] = a.aspect
        if a.resolution:
            img_cfg["imageSize"] = a.resolution.upper()
        gen = {"responseModalities": ["TEXT", "IMAGE"]}
        if img_cfg:
            gen["imageConfig"] = img_cfg
        out = []
        for _ in range(a.n):
            r = requests.post(f"{self.base}/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": self.key},
                              json={"contents": [{"role": "user", "parts": parts}], "generationConfig": gen},
                              timeout=600)
            if r.status_code != 200:
                raise MMError(f"Gemini 图片生成 HTTP {r.status_code}: {r.text[:1000]}")
            data = r.json()
            got = False
            for c in data.get("candidates", []):
                for p in c.get("content", {}).get("parts", []):
                    inline = p.get("inlineData") or p.get("inline_data")
                    if inline and not p.get("thought"):
                        ext = (inline.get("mimeType") or inline.get("mime_type") or "image/png").split("/")[-1]
                        out.append((ext.replace("jpeg", "jpg"), base64.b64decode(inline["data"])))
                        got = True
                    elif p.get("text"):
                        print(f"[gemini] {p['text'][:300]}", file=sys.stderr)
            if not got:
                raise MMError(f"Gemini 未返回图片: {json.dumps(data.get('promptFeedback') or data.get('candidates', [{}])[0].get('finishReason'), ensure_ascii=False)}")
        return out


# --------------------------------------------------------------------------- Seedream
class SeedreamProvider:
    name = "seedream"

    def __init__(self) -> None:
        self.region = (cfg("SEEDREAM_REGION") or ("intl" if cfg("BYTEPLUS_API_KEY") and not cfg("ARK_API_KEY") else "cn")).lower()
        if self.region not in SEEDREAM:
            raise MMError("SEEDREAM_REGION 只能是 cn(火山引擎) 或 intl(BytePlus)")
        self.key = cfg("BYTEPLUS_API_KEY") if self.region == "intl" else cfg("ARK_API_KEY")
        self.key = self.key or cfg("ARK_API_KEY") or cfg("BYTEPLUS_API_KEY")
        self.base = (cfg("SEEDREAM_BASE_URL") or SEEDREAM[self.region]["base"]).rstrip("/")

    def available(self) -> bool:
        return bool(self.key)

    def ranked(self, tier: str) -> list[str]:
        models = SEEDREAM[self.region]["models"]
        return [models[k] for k in SEEDREAM_ORDER[tier]]

    def pick(self, tier: str) -> str:
        return cfg("SEEDREAM_MODEL") or self.ranked(tier)[0]

    def _variant(self, model: str) -> str:
        for k, v in SEEDREAM[self.region]["models"].items():
            if v == model:
                return k
        return "pro" if "pro" in model else "flash" if "flash" in model else "lite"

    def _post(self, body: dict) -> requests.Response:
        return requests.post(f"{self.base}/images/generations", json=body, timeout=600,
                             headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})

    def generate(self, a, model: str) -> list[tuple[str, bytes | str]]:
        variant = self._variant(model)
        body: dict = {"model": model, "prompt": a.prompt, "response_format": "url", "watermark": a.watermark}
        if a.size:
            body["size"] = a.size
        elif a.aspect:
            edge = {"1K": 1024, "1.5K": 1536, "2K": 2048, "3K": 3072, "4K": 4096}.get((a.resolution or "2K").upper(), 2048)
            if variant in SEEDREAM_NO_SEQUENTIAL:
                edge = min(edge, 2048)  # 5.0 pro / flash: 1K / 1.5K / 2K
            body["size"] = "x".join(map(str, aspect_dims(a.aspect, edge, 8)))
        else:
            body["size"] = (a.resolution or "2K").upper()
        if a.format and variant in SEEDREAM_NO_SEQUENTIAL:
            body["output_format"] = "jpeg" if a.format in ("jpg", "jpeg") else "png"
        if a.ref:
            imgs = []
            for ref in a.ref:
                if re.match(r"^https?://", ref):
                    imgs.append(ref)
                else:
                    data, mime = ref_to_bytes(ref)
                    imgs.append(f"data:{mime.lower()};base64,{base64.b64encode(data).decode()}")
            body["image"] = imgs[0] if len(imgs) == 1 else imgs
        calls = 1
        if a.n > 1:
            if variant in SEEDREAM_NO_SEQUENTIAL:
                calls = a.n
            else:
                body["sequential_image_generation"] = "auto"
                body["sequential_image_generation_options"] = {"max_images": min(a.n, 15)}
        out = []
        for _ in range(calls):
            r = self._post(body)
            if r.status_code != 200:
                raise SeedreamHTTPError(r.status_code, r.text)
            for d in r.json().get("data", []):
                if d.get("url"):
                    out.append(("url", d["url"]))
                elif d.get("b64_json"):
                    out.append((d.get("output_format") or "png", base64.b64decode(d["b64_json"])))
                elif d.get("error"):
                    print(f"[seedream] 单张失败: {d['error']}", file=sys.stderr)
        return out


class SeedreamHTTPError(MMError):
    def __init__(self, status: int, text: str):
        super().__init__(f"Seedream HTTP {status}: {text[:1000]}")
        self.status = status
        self.text = text

    @property
    def model_unavailable(self) -> bool:
        return self.status == 404 or bool(re.search(r"ModelNotOpen|NotFound|not (found|exist|activated|open)", self.text, re.I))


PROVIDERS = {"openai": OpenAIProvider, "gemini": GeminiProvider, "seedream": SeedreamProvider}


def choose_provider(name: str) -> object:
    if name != "auto":
        p = PROVIDERS[name]()
        if not p.available():
            raise MMError(f"provider={name} 但未配置对应 API Key", 2)
        return p
    order = [x.strip() for x in (cfg("IMAGE_PROVIDER_ORDER") or "openai,gemini,seedream").split(",") if x.strip()]
    for n in order:
        p = PROVIDERS[n]()
        if p.available():
            return p
    raise MMError("没有可用的生图服务：请配置 OPENAI_API_KEY / GEMINI_API_KEY / ARK_API_KEY(火山) / BYTEPLUS_API_KEY 之一。", 2)


@main_guard
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prompt", nargs="?")
    ap.add_argument("--provider", choices=["auto", *PROVIDERS], default=None)
    ap.add_argument("--model", help="force model id")
    ap.add_argument("--tier", choices=["quality", "fast"], default=None)
    ap.add_argument("--ref", action="append", help="reference image (path or URL), repeatable -> edit / fusion")
    ap.add_argument("-n", type=int, default=1, help="number of images")
    ap.add_argument("--aspect", help="e.g. 1:1, 16:9, 9:16, 4:3, 3:2")
    ap.add_argument("--resolution", help="1K / 2K / 4K (Gemini, Seedream)")
    ap.add_argument("--size", help="explicit size, e.g. 1536x1024 (provider-specific)")
    ap.add_argument("--quality", help="OpenAI quality: low/medium/high/xhigh/max/auto")
    ap.add_argument("--format", choices=["png", "jpeg", "jpg", "webp"], help="output format where supported")
    ap.add_argument("--transparent", action="store_true", help="OpenAI transparent background")
    ap.add_argument("--watermark", action="store_true", help="Seedream: keep AI watermark")
    ap.add_argument("-d", "--out-dir", default="images")
    ap.add_argument("--name", help="output file stem")
    ap.add_argument("--list-models", action="store_true", help="show ranked models for configured providers")
    a = ap.parse_args()
    if a.format == "jpg":
        a.format = "jpeg"

    tier = a.tier or cfg("IMAGE_TIER", "quality")
    provider_name = a.provider or cfg("IMAGE_PROVIDER", "auto")

    if a.list_models:
        for n, cls in PROVIDERS.items():
            p = cls()
            if not p.available():
                print(f"## {n}: (未配置 key)")
                continue
            print(f"## {n} [{tier}]")
            try:
                for m in p.ranked(tier):
                    print(f"  {m}")
            except MMError as e:
                print(f"  ERROR {e}")
        return
    if not a.prompt:
        ap.error("prompt is required")

    p = choose_provider(provider_name)
    pinned = a.model or None
    candidates = [pinned] if pinned else [p.pick(tier)]
    if not pinned and isinstance(p, SeedreamProvider) and not cfg("SEEDREAM_MODEL"):
        candidates = p.ranked(tier)

    last_err = None
    for model in candidates:
        print(f"[image] provider={p.name} model={model} tier={tier}", file=sys.stderr)
        try:
            items = p.generate(a, model)
        except SeedreamHTTPError as e:
            if e.model_unavailable and len(candidates) > 1:
                print(f"[image] {model} 未开通/不可用，尝试下一个候选: {e.text[:200]}", file=sys.stderr)
                last_err = e
                continue
            raise
        if not items:
            raise MMError("接口返回成功但没有图片数据")
        stem = a.name or f"{p.name}_{time.strftime('%Y%m%d_%H%M%S')}"
        files = save_outputs(items, Path(a.out_dir).expanduser(), stem)
        print(json.dumps({"provider": p.name, "model": model, "files": files}, ensure_ascii=False, indent=2))
        return
    raise MMError(f"所有 Seedream 候选模型均不可用，请在方舟控制台开通模型或设置 SEEDREAM_MODEL。最后错误: {last_err}")


if __name__ == "__main__":
    main()
