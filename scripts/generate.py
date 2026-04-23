#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
API 生图脚本（V2：双引擎 + 环境变量 + --style 快捷调用）

- 从 .env 读取 API 密钥和配置（engine.json 仅保留非敏感参数）
- 双引擎 fallback：yunwu（Gemini）→ grsai（nano-banana）
- --style 参数：从 prompts.yaml 读取风格模板，自动填充变量和比例
- 保持 stdlib-only（零外部依赖）
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import mimetypes
import os
import pathlib
import re
import ssl
import sys
import time
import urllib.error
import urllib.request


def _eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def _read_text(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _read_json(p: pathlib.Path) -> dict:
    return json.loads(_read_text(p))


def _mask(s: str) -> str:
    if not s:
        return ""
    s = str(s)
    if len(s) <= 8:
        return "***"
    return s[:4] + "..." + s[-4:]


# ---------------------------------------------------------------------------
# .env 加载（简易解析，无依赖）
# ---------------------------------------------------------------------------

def _load_dotenv(dotenv_path: str | pathlib.Path):
    """
    简易 .env 加载器：逐行解析 KEY=VALUE，设置到 os.environ。
    跳过空行和 # 开头的注释行。
    """
    p = pathlib.Path(dotenv_path)
    if not p.exists():
        return
    for line in _read_text(p).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        # 去引号
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        if k and k not in os.environ:  # 不覆盖已有的环境变量
            os.environ[k] = v


def _find_dotenv() -> pathlib.Path | None:
    """按优先级搜索 .env 文件，返回找到的路径或 None。"""
    script_dir = pathlib.Path(__file__).resolve().parent
    skill_root = script_dir.parent

    # 候选路径（按优先级）
    candidates: list[pathlib.Path] = []

    # 1. skill_root（与 scripts/、config/ 同级，推荐位置）
    candidates.append(skill_root / ".env")

    # 2. 向上逐级搜索（最多 6 级，遇 .git 停止）
    d = skill_root
    for _ in range(6):
        d = d.parent
        if d == d.parent:
            break
        candidates.append(d / ".env")
        if (d / ".git").exists():
            break

    # 3. 当前工作目录
    candidates.append(pathlib.Path.cwd() / ".env")

    for c in candidates:
        if c.exists():
            return c
    return None


def _preferred_dotenv_path() -> pathlib.Path:
    """返回 .env 文件的最佳存放位置（skill_root 同级）。"""
    script_dir = pathlib.Path(__file__).resolve().parent
    return script_dir.parent / ".env"


def _init_dotenv():
    """从多个候选位置加载 .env。"""
    found = _find_dotenv()
    if found:
        _load_dotenv(found)
    else:
        preferred = _preferred_dotenv_path()
        _eprint(f"提示：未找到 .env 文件。推荐位置：{preferred}")
        _eprint(f"  运行 python scripts/generate.py --setup 进行首次配置")


# ---------------------------------------------------------------------------
# 简易 YAML 解析器（仅支持 prompts.yaml 的结构）
# ---------------------------------------------------------------------------

def _load_prompts_yaml(p: pathlib.Path) -> dict:
    """
    简易 YAML → dict 解析器。
    支持 prompts.yaml 的结构：一级 key → 二级 key → 属性行 + | 块标量。
    """
    text = _read_text(p)
    result: dict = {}
    current_section: str | None = None
    current_item: str | None = None
    current_key: str | None = None
    multiline_buffer: list[str] = []
    in_multiline = False

    def _flush_multiline():
        nonlocal current_key, multiline_buffer, in_multiline
        if in_multiline and current_section and current_item and current_key:
            val = "\n".join(multiline_buffer).strip() + "\n"
            if current_section not in result:
                result[current_section] = {}
            if current_item not in result[current_section]:
                result[current_section][current_item] = {}
            result[current_section][current_item][current_key] = val
        in_multiline = False
        multiline_buffer = []
        current_key = None

    def _indent_level(line: str) -> int:
        """返回缩进空格数"""
        count = 0
        for ch in line:
            if ch == ' ':
                count += 1
            elif ch == '\t':
                count += 4
            else:
                break
        return count

    lines = text.splitlines()
    for line in lines:
        stripped = line.strip()

        # 空行或注释
        if not stripped or stripped.startswith("#"):
            if in_multiline:
                multiline_buffer.append("")
            continue

        indent = _indent_level(line)

        # 检查是否退出多行模式：非空行且缩进回到属性级别或更少
        if in_multiline:
            # 多行内容：缩进 > 属性行（属性行 indent=4，多行内容 indent=6+）
            if indent > 4:
                multiline_buffer.append(stripped)
                continue
            else:
                _flush_multiline()

        # 一级 key（indent 0，如 "cover:"）
        if indent == 0 and stripped.endswith(":") and not ":" in stripped[:-1]:
            _flush_multiline()
            current_section = stripped[:-1].strip()
            current_item = None
            if current_section not in result:
                result[current_section] = {}
            continue

        # 二级 key（indent 2，如 "  visual_note:"）
        if indent == 2 and stripped.endswith(":") and not ":" in stripped[:-1]:
            _flush_multiline()
            current_item = stripped[:-1].strip()
            if current_section and current_item not in result.get(current_section, {}):
                result[current_section][current_item] = {}
            continue

        # 属性行（indent 4+）：key: value 或 key: | (多行块)
        if indent >= 4 and current_section and current_item:
            colon_pos = stripped.find(":")
            if colon_pos > 0:
                prop_key = stripped[:colon_pos].strip()
                prop_val = stripped[colon_pos + 1:].strip()

                if prop_val == "|":
                    # 多行块开始
                    current_key = prop_key
                    in_multiline = True
                    multiline_buffer = []
                    continue

                if prop_val.startswith("[") and prop_val.endswith("]"):
                    items = re.findall(r'"([^"]*)"', prop_val)
                    if not items:
                        items = re.findall(r"'([^']*)'", prop_val)
                    result[current_section][current_item][prop_key] = items
                elif prop_val.startswith('"') and prop_val.endswith('"'):
                    result[current_section][current_item][prop_key] = prop_val[1:-1]
                elif prop_val.startswith("'") and prop_val.endswith("'"):
                    result[current_section][current_item][prop_key] = prop_val[1:-1]
                elif prop_val:
                    result[current_section][current_item][prop_key] = prop_val

    _flush_multiline()
    return result


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _strip_known_version_suffix(url: str) -> tuple[str, str | None]:
    u = (url or "").strip().rstrip("/")
    for v in ("v1beta", "v1alpha", "v1"):
        suf = "/" + v
        if u.endswith(suf):
            return u[: -len(suf)], v
    return u, None


def _candidate_generate_content_urls(base_url: str, model: str, api_version: str | None) -> list[str]:
    root, inferred = _strip_known_version_suffix(base_url)
    versions: list[str] = []
    if api_version and api_version != "auto":
        versions = [api_version]
    elif inferred:
        versions = [inferred]
    else:
        versions = ["v1beta", "v1"]
    return [root.rstrip("/") + f"/{v}/models/{model}:generateContent" for v in versions]


def _request_json(url: str, headers: dict, payload: dict, timeout_s: int) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url=url, data=body, method="POST", headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
            txt = raw.decode("utf-8", errors="replace")
            try:
                j = json.loads(txt)
            except json.JSONDecodeError:
                j = None
            return {"ok": 200 <= resp.status < 300, "status": resp.status, "url": url,
                    "headers": dict(resp.headers.items()), "raw_text": txt, "json": j}
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        txt = raw.decode("utf-8", errors="replace")
        try:
            j = json.loads(txt) if txt else None
        except json.JSONDecodeError:
            j = None
        return {"ok": False, "status": getattr(e, "code", None), "url": url,
                "headers": dict(getattr(e, "headers", {}).items()), "raw_text": txt, "json": j}
    except Exception as e:
        return {"ok": False, "status": None, "url": url, "headers": {},
                "raw_text": str(e), "json": None}


def _sleep_s(seconds: float):
    if seconds <= 0:
        return
    time.sleep(seconds)


def _guess_mime(path: pathlib.Path) -> str:
    ext = path.suffix.lower()
    if ext == ".png":
        return "image/png"
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".webp":
        return "image/webp"
    guess, _ = mimetypes.guess_type(str(path))
    return guess or "application/octet-stream"


def _ext_from_mime(mime: str) -> str:
    m = (mime or "").lower()
    if "png" in m:
        return ".png"
    if "jpeg" in m or "jpg" in m:
        return ".jpg"
    if "webp" in m:
        return ".webp"
    return ".bin"


def _parse_frontmatter_and_body(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        k, v = s.split(":", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        meta[k] = v
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


def _normalize_image_size(v: str | None) -> str | None:
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in ("1K", "2K", "4K"):
        return s
    return s if s else None


def _normalize_output_format(v) -> str:
    if v is None:
        return "auto"
    s = str(v).strip().lower()
    if not s or s == "auto":
        return "auto"
    if s in ("png", "webp"):
        return s
    if s in ("jpg", "jpeg"):
        return "jpg"
    return "auto"


def _normalize_jpg_quality(v) -> int | None:
    if v is None:
        return None
    try:
        q = int(str(v).strip())
    except Exception:
        return None
    return max(1, min(95, q))


def _ext_from_output_format(fmt: str) -> str | None:
    f = (fmt or "").strip().lower()
    if f == "png":
        return ".png"
    if f == "jpg":
        return ".jpg"
    if f == "webp":
        return ".webp"
    return None


def _extract_inline_images(resp_json: dict) -> list[dict]:
    if not isinstance(resp_json, dict):
        return []
    cands = resp_json.get("candidates")
    if not isinstance(cands, list) or not cands:
        return []
    c0 = cands[0] if isinstance(cands[0], dict) else {}
    content = c0.get("content") if isinstance(c0.get("content"), dict) else {}
    parts = content.get("parts")
    if not isinstance(parts, list):
        return []
    out = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        inline = part.get("inlineData") or part.get("inline_data")
        if not isinstance(inline, dict):
            continue
        b64 = inline.get("data")
        if not isinstance(b64, str) or not b64:
            continue
        mime = inline.get("mimeType") or inline.get("mime_type") or ""
        out.append({"b64": b64, "mime": mime, "thought": bool(part.get("thought"))})
    return out


def _redact_response_json(resp_json: dict) -> dict:
    if not isinstance(resp_json, dict):
        return resp_json
    j = json.loads(json.dumps(resp_json, ensure_ascii=False))
    for cand in j.get("candidates", []):
        if not isinstance(cand, dict):
            continue
        content = cand.get("content")
        if not isinstance(content, dict):
            continue
        for part in content.get("parts", []):
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and "data" in inline:
                inline["data"] = "<base64 已省略>"
    return j


def _build_payload(prompt: str, aspect_ratio: str, image_size: str | None, ref_images: list[pathlib.Path]) -> dict:
    parts: list[dict] = [{"text": prompt}]
    for p in ref_images:
        b = p.read_bytes()
        parts.append({"inlineData": {"mimeType": _guess_mime(p), "data": base64.b64encode(b).decode("ascii")}})
    gen_cfg: dict = {"responseModalities": ["TEXT", "IMAGE"], "imageConfig": {"aspectRatio": aspect_ratio}}
    if image_size:
        gen_cfg["imageConfig"]["imageSize"] = image_size
    return {"contents": [{"role": "user", "parts": parts}], "generationConfig": gen_cfg}


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def _load_config(config_path: pathlib.Path) -> dict:
    if config_path.exists():
        cfg = _read_json(config_path)
        if isinstance(cfg, dict):
            return cfg
        raise SystemExit(f"配置文件不是 JSON 对象：{config_path}")
    example = config_path.parent / "config.example.json"
    if example.exists():
        _eprint(f"提示：未找到 config.json，正在使用示例配置：{example}")
        cfg = _read_json(example)
        if isinstance(cfg, dict):
            return cfg
        raise SystemExit(f"示例配置不是 JSON 对象：{example}")
    raise SystemExit(f"未找到配置文件：{config_path}")


def _get_cfg(cfg: dict) -> tuple[str, dict]:
    output_dir = cfg.get("output_dir")
    settings = cfg.get("settings") if isinstance(cfg.get("settings"), dict) else {}
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise SystemExit("配置缺少 output_dir")
    return output_dir, settings


def _ensure_parent(p: pathlib.Path):
    p.parent.mkdir(parents=True, exist_ok=True)


def _write_bytes(p: pathlib.Path, data: bytes):
    _ensure_parent(p)
    p.write_bytes(data)


def _try_convert_image_bytes(img_bytes: bytes, out_path: pathlib.Path, jpg_quality: int | None = None) -> bool:
    suf = out_path.suffix.lower().lstrip(".")
    if not suf:
        return False
    if suf == "jpeg":
        suf = "jpg"
    if suf not in ("png", "jpg", "webp"):
        return False
    try:
        from PIL import Image
        import io
    except Exception:
        return False
    try:
        im = Image.open(io.BytesIO(img_bytes))
        _ensure_parent(out_path)
        if suf == "jpg":
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg.convert("RGB")
            else:
                im = im.convert("RGB")
            save_kwargs: dict = {}
            if jpg_quality is not None:
                save_kwargs["quality"] = int(jpg_quality)
                save_kwargs["optimize"] = True
            im.save(str(out_path), format="JPEG", **save_kwargs)
            return True
        im.save(str(out_path), format=suf.upper())
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 引擎一：yunwu（Gemini generateContent）
# ---------------------------------------------------------------------------

def _generate_via_gemini(prompt, out_path, aspect_ratio, image_size, ref_images,
                         base_url, model, api_key, timeout_s, max_retries,
                         retry_backoff_s, auth_mode, api_version,
                         output_format, jpg_quality, save_response_json,
                         save_thought_images, prompt_file):
    """yunwu/Gemini 生图路径，返回 (success, saved_paths, debug_info)"""
    payload = _build_payload(prompt=prompt, aspect_ratio=aspect_ratio,
                             image_size=image_size, ref_images=ref_images)
    urls = _candidate_generate_content_urls(base_url=base_url, model=model, api_version=api_version)
    if not urls:
        return False, [], "无法从 base_url 生成请求地址"

    auth_attempts = ["google", "bearer"] if auth_mode == "auto" else [auth_mode]
    if auth_mode not in ("auto", "google", "bearer"):
        auth_attempts = ["google", "bearer"]

    final = None
    used = {"url": None, "auth": None}

    for url in urls:
        for auth in auth_attempts:
            headers = {"Accept": "application/json"}
            if auth == "google":
                headers["x-goog-api-key"] = api_key
            else:
                headers["Authorization"] = f"Bearer {api_key}"

            attempt = 0
            while True:
                r = _request_json(url=url, headers=headers, payload=payload, timeout_s=timeout_s)
                final = r
                status = r.get("status")
                if status == 404:
                    break
                if status in (401, 403):
                    break
                if r.get("ok"):
                    used = {"url": url, "auth": auth}
                    break
                retriable = (status is None) or (status == 429) or (isinstance(status, int) and 500 <= status <= 599)
                if retriable and attempt < max_retries:
                    wait_s = retry_backoff_s * (2 ** attempt) if retry_backoff_s > 0 else (1.0 * (2 ** attempt))
                    attempt += 1
                    _eprint(f"请求失败（status={status}），重试 {attempt}/{max_retries}，等待 {wait_s:.1f}s ...")
                    _sleep_s(wait_s)
                    continue
                used = {"url": url, "auth": auth}
                break
            if final and final.get("ok"):
                break
        if final and final.get("ok"):
            break

    if final is None:
        return False, [], "请求未执行（异常）"

    if not final.get("ok"):
        status = final.get("status")
        return False, [], f"yunwu 请求失败 status={status}: {(final.get('raw_text') or '')[:500]}"

    # 提取图片
    imgs = _extract_inline_images(final.get("json"))
    if not imgs:
        return False, [], "未在回包中找到 inlineData 图片"

    if not save_thought_images:
        finals = [x for x in imgs if not x.get("thought")]
        imgs_to_save = finals if finals else imgs
    else:
        imgs_to_save = imgs

    # 确定输出路径
    desired_ext = _ext_from_output_format(output_format)
    if out_path:
        base_out = pathlib.Path(out_path).expanduser()
        base_has_suffix = bool(base_out.suffix)
    else:
        return False, [], "需要指定 out_path"

    out_paths = []
    for idx, item in enumerate(imgs_to_save, start=1):
        mime_ext = _ext_from_mime(str(item.get("mime") or ""))
        ext = desired_ext or mime_ext
        if base_has_suffix:
            if idx == 1:
                out_paths.append(base_out)
            else:
                out_paths.append(base_out.with_name(f"{base_out.stem}-{idx:03d}{base_out.suffix}"))
        else:
            parent, stem = base_out.parent, base_out.name
            out_paths.append(parent / (stem + (f"-{idx:03d}" if idx > 1 else "") + ext))

    saved_paths = []
    for item, target in zip(imgs_to_save, out_paths):
        img_bytes = base64.b64decode("".join(str(item.get("b64") or "").split()))
        target_ext = (target.suffix or "").lower()
        wants_jpg_quality = (jpg_quality is not None) and (output_format == "jpg" or target_ext in (".jpg", ".jpeg"))

        if output_format == "auto" and not wants_jpg_quality:
            _write_bytes(target, img_bytes)
            saved_paths.append(target)
            continue

        if _try_convert_image_bytes(img_bytes, target, jpg_quality if wants_jpg_quality else None):
            saved_paths.append(target)
            continue

        mime_ext = _ext_from_mime(str(item.get("mime") or ""))
        final_path = target
        if target_ext and mime_ext != ".bin" and target_ext != mime_ext.lower():
            final_path = target.with_suffix(mime_ext)
        _write_bytes(final_path, img_bytes)
        saved_paths.append(final_path)

    return True, saved_paths, {"engine": "yunwu", "status": final.get("status"), "used": used}


# ---------------------------------------------------------------------------
# 本地参考图上传（阿里云 OSS）
# ---------------------------------------------------------------------------

def _upload_to_oss(file_path):
    """上传本地图片到阿里云 OSS，返回公开访问 URL。失败返回 None。"""
    try:
        import oss2
    except ImportError:
        _eprint("oss2 未安装，无法上传本地参考图到 OSS（pip install oss2）")
        return None
    ak = os.getenv("OSS_ACCESS_KEY_ID")
    sk = os.getenv("OSS_ACCESS_KEY_SECRET")
    endpoint = os.getenv("OSS_ENDPOINT")
    bucket_name = os.getenv("OSS_BUCKET")
    if not all([ak, sk, endpoint, bucket_name]):
        _eprint("OSS 环境变量未全部配置（需 OSS_ACCESS_KEY_ID/SECRET/ENDPOINT/BUCKET），跳过本地上传")
        return None
    date_str = time.strftime('%Y%m%d')
    fname = pathlib.Path(file_path).name
    key = f"visual-forge/ref/{date_str}/{fname}"
    try:
        auth = oss2.Auth(ak, sk)
        bkt = oss2.Bucket(auth, endpoint, bucket_name)
        bkt.put_object_from_file(key, str(file_path))
        url = f"https://{bucket_name}.{endpoint}/{key}"
        _eprint(f"OSS 上传成功：{url}")
        return url
    except Exception as e:
        _eprint(f"OSS 上传失败：{e}")
        return None


# ---------------------------------------------------------------------------
# 引擎 1.5：yunwu OpenAI Images 格式（gpt-image-2 等 dall-e-3 兼容模型）
# ---------------------------------------------------------------------------

def _generate_via_openai_images(prompt, out_path, aspect_ratio,
                                 base_url, model, api_key, timeout_s):
    """yunwu / OpenAI Images 兼容路径（dall-e-3 格式）。返回 (success, saved_paths, debug_info)

    响应格式：{data: [{b64_json: "..."}], ...}
    先尝试 urllib（清代理），失败降级 curl 子进程。
    """
    import subprocess
    import tempfile

    url = base_url.rstrip("/") + "/images/generations"

    # gpt-image-2 支持的尺寸
    size_map = {
        "4:3": "1536x1024",
        "3:4": "1024x1536",
        "16:9": "1792x1024",
        "1:1": "1024x1024",
    }
    size = size_map.get(aspect_ratio, "auto")

    body = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": "medium",
        "n": 1,
    }

    effective_timeout = max(timeout_s, 180)
    _eprint(f"yunwu OpenAI Images 请求：url={url} model={model} size={size} timeout={effective_timeout}s")

    resp_text = None

    # === 尝试 1：Python urllib（清除代理环境变量） ===
    try:
        saved_proxies = {}
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
            if k in os.environ:
                saved_proxies[k] = os.environ.pop(k)

        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=effective_timeout, context=context) as resp:
            resp_text = resp.read().decode("utf-8", "ignore")

        os.environ.update(saved_proxies)

    except Exception as e:
        # 恢复代理
        for k, v in saved_proxies.items():
            os.environ[k] = v
        _eprint(f"yunwu urllib 失败，降级 curl：{e}")

        # === 尝试 2：curl 子进程 ===
        try:
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False)
                body_file = f.name

            curl_env = os.environ.copy()
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
                curl_env.pop(k, None)

            cmd = [
                "curl", "-s", "-X", "POST", url,
                "-H", "Content-Type: application/json",
                "-H", "Accept: application/json",
                "-H", f"Authorization: Bearer {api_key}",
                "-d", f"@{body_file}",
                "--max-time", str(effective_timeout),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=effective_timeout + 10, env=curl_env)
            os.unlink(body_file)

            if result.returncode != 0:
                return False, [], f"yunwu curl 失败：exit={result.returncode} {(result.stderr or '')[:300]}"

            resp_text = result.stdout

        except FileNotFoundError:
            return False, [], "curl 未找到，请安装 curl"
        except subprocess.TimeoutExpired:
            return False, [], f"yunwu curl 超时（{effective_timeout}s）"
        except Exception as e2:
            return False, [], f"yunwu curl 异常：{e2}"

    if not resp_text:
        return False, [], "yunwu 未收到任何响应"

    # 解析 JSON 响应：{data: [{b64_json: "..."}]}
    try:
        resp_json = json.loads(resp_text)
    except json.JSONDecodeError:
        return False, [], f"yunwu 响应非 JSON：{resp_text[:300]}"

    data_list = resp_json.get("data")
    if not isinstance(data_list, list) or not data_list:
        return False, [], f"yunwu 响应无 data 字段：{str(resp_json)[:300]}"

    b64 = data_list[0].get("b64_json")
    if not b64:
        return False, [], "yunwu 响应 data[0] 中无 b64_json"

    img_data = base64.b64decode(b64)
    target = pathlib.Path(out_path).expanduser()
    _ensure_parent(target)
    _write_bytes(target, img_data)

    return True, [target], {"engine": "yunwu-openai-images", "model": model, "size": size}


# ---------------------------------------------------------------------------
# 引擎二：grsai 统一引擎（nano-banana + gpt-image）
# ---------------------------------------------------------------------------

def _generate_via_grsai(prompt, out_path, aspect_ratio, image_size,
                        model=None, ref_urls=None):
    """grsai 统一生图路径，按模型自动路由端点。返回 (success, saved_paths, debug_info)"""
    api_key = os.getenv("BANANA_API_KEY")
    if not api_key:
        return False, [], "BANANA_API_KEY 未配置"

    model = model or "nano-banana-2"
    is_gpt_image = model.startswith("gpt-image")

    # 端点路由
    if is_gpt_image:
        api_url = os.getenv("GRSAI_DRAW_API_URL")
        if not api_url:
            return False, [], "GRSAI_DRAW_API_URL 未配置"
    else:
        api_url = os.getenv("BANANA_API_URL")
        if not api_url:
            return False, [], "BANANA_API_URL 未配置"

    # 认证头（统一 Bearer）
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    # 请求体构建
    body = {
        "model": model,
        "prompt": prompt,
        "shutProgress": False,
    }
    if is_gpt_image:
        # gpt-image 用 size 字段
        size_map = {"4:3": "3:2", "3:4": "2:3", "16:9": "16:9", "1:1": "1:1"}
        body["size"] = size_map.get(aspect_ratio, "auto")
    else:
        # nano-banana 用 aspectRatio + imageSize
        body["aspectRatio"] = aspect_ratio
        body["imageSize"] = image_size or "2K"
    if ref_urls:
        body["urls"] = ref_urls

    _eprint(f"grsai 请求：endpoint={'gpt-image' if is_gpt_image else 'nano-banana'} model={model}")

    try:
        req = urllib.request.Request(
            api_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        context = ssl._create_unverified_context()
        timeout_s = int(os.getenv("LLM_TIMEOUT", "120"))

        with urllib.request.urlopen(req, timeout=timeout_s, context=context) as resp:
            response_text = resp.read().decode("utf-8", "ignore")

            # 解析流式 JSON（多个 JSON 对象拼接）
            json_objs = []
            depth = 0
            start = 0
            for i, char in enumerate(response_text):
                if char == "{":
                    if depth == 0:
                        start = i
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            json_objs.append(json.loads(response_text[start:i + 1]))
                        except Exception:
                            pass

            if not json_objs:
                return False, [], f"grsai 响应解析失败，无有效 JSON。Raw: {response_text[:300]}"

            final_result = json_objs[-1]

            # 兼容两种响应格式：直接返回 / webhook data 包装
            results = final_result.get("results", [])
            status = final_result.get("status")
            progress = final_result.get("progress", 0)
            if not results:
                data = final_result.get("data", {})
                if isinstance(data, dict):
                    results = data.get("results", [])
                    progress = data.get("progress", 0)

            # 判断成功
            succeeded = (status == "succeeded"
                         or (isinstance(progress, (int, str)) and int(str(progress)) >= 100)
                         or bool(results))

            if not succeeded and not results:
                error_msg = final_result.get("error") or final_result.get("message") or final_result.get("msg") or "未知错误"
                return False, [], f"grsai API 错误: {error_msg} (status={status}, progress={progress})"

            img_url = results[0].get("url") or results[0].get("uri")
            if not img_url:
                return False, [], "grsai 返回的图片 URL 为空"

            # 下载图片
            img_context = ssl._create_unverified_context()
            with urllib.request.urlopen(img_url, timeout=60, context=img_context) as dl_resp:
                img_data = dl_resp.read()

            target = pathlib.Path(out_path).expanduser()
            _ensure_parent(target)

            target_ext = target.suffix.lower()
            jpg_quality = int(os.getenv("VF_JPG_QUALITY", "85"))

            if target_ext in (".jpg", ".jpeg") and jpg_quality:
                if not _try_convert_image_bytes(img_data, target, jpg_quality):
                    _write_bytes(target, img_data)
            else:
                _write_bytes(target, img_data)

            engine_label = "grsai-gpt-image" if is_gpt_image else "grsai"
            return True, [target], {"engine": engine_label, "status": status or "succeeded", "model": model}

    except Exception as e:
        return False, [], f"grsai 请求异常: {e}"


# ---------------------------------------------------------------------------
# 首次配置向导
# ---------------------------------------------------------------------------

def _print_setup_guide():
    """非交互模式：输出配置指南，供 AI 代理解析并指导用户。"""
    preferred = _preferred_dotenv_path()
    found = _find_dotenv()
    print("=" * 56)
    print("  Visual Forge 配置指南")
    print("=" * 56)
    print()
    if found:
        print(f"  .env 已找到：{found}")
    else:
        print(f"  .env 未找到。推荐位置：{preferred}")
    print()
    print("  支持两种引擎（只需配置一种）：")
    print()
    print("  方案 A：yunwu（推荐）")
    print("    注册：https://yunwu.ai/register?aff=ml8W")
    print("    需要：LLM_API_KEY=sk-xxx")
    print()
    print("  方案 B：grsai（备用）")
    print("    注册：海外 https://grsai.com/zh / 国内 https://grsai.ai/zh")
    print("    需要：BANANA_API_KEY=sk-xxx")
    print()
    print("  快速配置（三选一）：")
    print(f"    1. 终端运行：python scripts/generate.py --setup")
    print(f"    2. 手动创建：cp .env.example {preferred}")
    print("    3. 环境变量：export LLM_API_KEY=sk-xxx")
    print("=" * 56)


def _run_setup():
    """交互式首次配置向导。"""
    if not sys.stdin.isatty():
        _print_setup_guide()
        return

    preferred = _preferred_dotenv_path()

    print("=" * 56)
    print("  Visual Forge 首次配置向导")
    print("=" * 56)
    print()
    print(f"  .env 将创建在：{preferred}")
    print()
    print("  支持两种引擎（只需配置一种）：")
    print()
    print("  方案 A：yunwu（推荐）")
    print("    注册地址：https://yunwu.ai/register?aff=ml8W")
    print("    需要填写：LLM_API_KEY")
    print()
    print("  方案 B：grsai（备用）")
    print("    注册地址：海外 https://grsai.com/zh")
    print("             国内 https://grsai.ai/zh")
    print("    需要填写：BANANA_API_KEY")
    print()

    if preferred.exists():
        print(f"  注意：{preferred} 已存在，将追加缺失的密钥。")
        existing = _read_text(preferred)
    else:
        existing = ""

    lines: list[str] = []

    # yunwu key
    yunwu_configured = False
    if "LLM_API_KEY=" in existing:
        val_line = existing.split("LLM_API_KEY=")[1].split("\n")[0].strip()
        if val_line and "your-api-key" not in val_line and val_line != "sk-":
            yunwu_configured = True
            print("  [跳过] LLM_API_KEY 已配置")
    if not yunwu_configured:
        key = input("  请输入 LLM_API_KEY（直接回车跳过）：").strip()
        if key:
            lines.append(f"LLM_API_KEY={key}")
            lines.append("LLM_BASE_URL=https://yunwu.ai/v1")
            print("  -> LLM_API_KEY 已记录")

    # grsai key
    grsai_configured = False
    if "BANANA_API_KEY=" in existing:
        val_line = existing.split("BANANA_API_KEY=")[1].split("\n")[0].strip()
        if val_line and "your-api-key" not in val_line and val_line != "sk-":
            grsai_configured = True
            print("  [跳过] BANANA_API_KEY 已配置")
    if not grsai_configured:
        key = input("  请输入 BANANA_API_KEY（直接回车跳过）：").strip()
        if key:
            lines.append(f"BANANA_API_KEY={key}")
            lines.append("BANANA_API_URL=http://grsai.dakka.com.cn/v1/draw/nano-banana")
            lines.append("GRSAI_DRAW_API_URL=https://grsai.dakka.com.cn/v1/draw/completions")
            print("  -> BANANA_API_KEY 已记录")

    if not lines:
        print()
        print("  未输入任何密钥，配置已取消。")
        print("  你可以稍后手动编辑 .env 或重新运行 --setup")
        return

    # 写入
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        with open(preferred, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n# === Visual Forge（由 --setup 自动添加）===\n")
            for line in lines:
                f.write(line + "\n")
            f.write("\n# 输出参数\n")
            f.write("VF_PROVIDER=auto\n")
            f.write("VF_IMAGE_SIZE=2K\n")
            f.write("VF_OUTPUT_FMT=jpg\n")
            f.write("VF_JPG_QUALITY=85\n")
            f.write("LLM_TIMEOUT=120\n")

        print()
        print(f"  配置已写入：{preferred}")
        print("  现在可以运行生图命令了！")
    except OSError as e:
        print(f"\n  写入失败：{e}")
        print(f"  请手动创建 {preferred}")
    print("=" * 56)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    # --setup 模式：在 argparse 之前截获
    if "--setup" in sys.argv:
        _run_setup()
        return

    # 加载 .env
    _init_dotenv()

    ap = argparse.ArgumentParser(description="API 生图：双引擎 fallback，支持 --style 快捷调用。")
    ap.add_argument("--config", default=None, help="配置文件路径")
    ap.add_argument("--prompt-file", default=None, help="提示词文件路径（带 YAML 头部）")
    ap.add_argument("--prompt", default=None, help="提示词文本")
    ap.add_argument("--reference", nargs="*", default=[], help="参考图路径（本地文件，用于 yunwu/Gemini）")
    ap.add_argument("--reference-url", nargs="*", default=[], help="参考图 URL（用于 gpt-image 图生图）")
    ap.add_argument("--out", default=None, help="输出图片路径")
    ap.add_argument("--aspect-ratio", default=None, help="图片比例（优先从 prompt 文件或 --style 读取）")
    ap.add_argument("--image-size", default=None, help="分辨率（优先从 prompt 文件读取）")
    ap.add_argument("--style", default=None,
                    help="风格名（从 prompts.yaml 读取，如 visual_note, cyberpunk, kawaii）")
    ap.add_argument("--provider", default=None,
                    help="生图引擎：auto / yunwu / grsai（覆盖 VF_PROVIDER 环境变量）")
    ap.add_argument("--model", default=None,
                    help="模型名（覆盖环境变量中的默认模型，如 gemini-3.1-flash-image-preview）")
    ap.add_argument("--setup", action="store_true",
                    help="首次配置向导：引导创建 .env 文件并填入 API Key")

    args = ap.parse_args()

    # 加载引擎配置
    skill_dir = pathlib.Path(__file__).resolve().parent.parent
    config_path = pathlib.Path(args.config).expanduser().resolve() if args.config else (skill_dir / "config" / "engine.json")
    cfg = _load_config(config_path)
    output_dir_str, settings = _get_cfg(cfg)

    # 从 engine.json providers 读取配置，环境变量仅提供 URL/Key
    providers_cfg = cfg.get("providers", {})
    yunwu_cfg = providers_cfg.get("yunwu", {})
    grsai_cfg = providers_cfg.get("grsai", {})

    # yunwu 配置
    base_url = os.getenv(yunwu_cfg.get("base_url_env", "LLM_BASE_URL"), "https://yunwu.ai/v1")
    api_key = os.getenv(yunwu_cfg.get("api_key_env", "LLM_API_KEY"), "")
    yunwu_default_model = yunwu_cfg.get("default_model", "gemini-3.1-flash-image-preview")

    # grsai 配置
    grsai_default_model = grsai_cfg.get("default_model", "nano-banana-2")

    # 模型选择优先级：--model CLI > engine.json default_model > 旧环境变量兜底
    model = (args.model
             or yunwu_default_model
             or os.getenv("YUNWU_AI_Banana_Pro_Model", ""))

    timeout_s = int(os.getenv("LLM_TIMEOUT", str(settings.get("timeout_s", 120))))
    max_retries = int(settings.get("max_retries", 2))
    retry_backoff_s = float(settings.get("retry_backoff_s", 1.0))
    auth_mode = str(settings.get("auth_mode", "auto")).strip() or "auto"
    api_version = str(settings.get("api_version", "auto")).strip() or "auto"
    save_response_json = bool(settings.get("save_response_json", False))
    save_thought_images = bool(settings.get("save_thought_images", False))

    output_format = _normalize_output_format(os.getenv("VF_OUTPUT_FMT", str(settings.get("output_format", "auto"))))
    jpg_quality = _normalize_jpg_quality(os.getenv("VF_JPG_QUALITY", str(settings.get("jpg_quality", 85))))

    if not api_key:
        raise SystemExit(
            f"缺少 API Key\n"
            f"\n"
            f".env 文件：{'已找到(' + str(_find_dotenv()) + ')' if _find_dotenv() else '未找到'}\n"
            f".env 推荐位置：{_preferred_dotenv_path()}\n"
            f"\n"
            f"快速修复（三选一）：\n"
            f"  1. 运行配置向导：python scripts/generate.py --setup\n"
            f"  2. 手动创建：cp .env.example .env  然后填入 API Key\n"
            f"  3. 设环境变量：export LLM_API_KEY=sk-xxx\n"
            f"\n"
            f"API Key 获取地址：\n"
            f"  yunwu：https://yunwu.ai/register?aff=ml8W\n"
            f"  grsai：https://grsai.com/zh 或 https://grsai.ai/zh"
        )

    # --style 参数处理
    style_ratio = None
    if args.style:
        prompts_path = skill_dir / "config" / "prompts.yaml"
        if not prompts_path.exists():
            raise SystemExit(f"--style 需要 prompts.yaml，但未找到：{prompts_path}")

        prompts_data = _load_prompts_yaml(prompts_path)

        # 在所有场景中搜索风格
        style_found = None
        for scene_name, scene_data in prompts_data.items():
            if not isinstance(scene_data, dict):
                continue
            if args.style in scene_data:
                style_found = scene_data[args.style]
                _eprint(f"风格匹配：{scene_name}/{args.style}")
                break

        if not style_found:
            available = []
            for scene_name, scene_data in prompts_data.items():
                if isinstance(scene_data, dict):
                    available.extend(f"{scene_name}/{k}" for k in scene_data.keys())
            raise SystemExit(f"未找到风格 '{args.style}'，可用风格：{', '.join(available[:20])}...")

        # 获取风格比例
        style_ratio = style_found.get("ratio") or (style_found.get("ratelist", ["4:3"])[0] if style_found.get("ratelist") else None)

        if style_ratio:
            _eprint(f"风格比例：{style_ratio}")

        # 区分完整模板（prompt/template）和前缀修饰词（modifier）
        full_template = style_found.get("prompt") or style_found.get("template") or ""
        modifier = style_found.get("modifier") or ""
        user_desc = (args.prompt or "").strip()

        if full_template:
            # 完整模板：替换变量 {METAPHOR}/{TOPIC}/{DESCRIPTION} 等
            for var in ("{METAPHOR}", "{TOPIC}", "{DESCRIPTION}", "{title}", "{subtitle}", "{stats}"):
                if var in full_template:
                    full_template = full_template.replace(var, user_desc)
                    break
            args.prompt = full_template
        elif modifier:
            # modifier 模式：modifier + 用户描述（保持原语言）
            args.prompt = f"{modifier} {user_desc}, no text no watermark" if user_desc else modifier
        else:
            args.prompt = user_desc

    # 解析 prompt
    prompt_text = ""
    meta: dict = {}
    prompt_file = pathlib.Path(args.prompt_file).expanduser().resolve() if args.prompt_file else None

    if prompt_file:
        raw = _read_text(prompt_file)
        meta, prompt_text = _parse_frontmatter_and_body(raw)
    else:
        prompt_text = (args.prompt or "").strip()

    if not prompt_text.strip():
        raise SystemExit("提示词为空：请提供 --prompt-file 或 --prompt")

    # 确定比例（优先级：prompt file > --aspect-ratio（用户显式）> --style > 默认 4:3）
    aspect_ratio = (str(meta.get("aspect_ratio") or "").strip()
                    or (str(args.aspect_ratio or "").strip())
                    or style_ratio
                    or "4:3")

    # 确定分辨率
    image_size = (_normalize_image_size(str(meta.get("image_size") or "").strip())
                  or _normalize_image_size(args.image_size)
                  or _normalize_image_size(os.getenv("VF_IMAGE_SIZE"))
                  or _normalize_image_size(settings.get("image_size")))

    if image_size and image_size not in ("1K", "2K", "4K"):
        _eprint(f"警告：image_size={image_size} 不是 1K/2K/4K，仍将尝试提交")

    ref_images = [pathlib.Path(p).expanduser().resolve() for p in (args.reference or [])]
    for p in ref_images:
        if not p.exists() or not p.is_file():
            raise SystemExit(f"参考图不存在：{p}")

    # 确定输出路径
    if args.out:
        out_path = pathlib.Path(args.out).expanduser()
    else:
        out_root = pathlib.Path(os.path.expanduser(output_dir_str)).resolve()
        name = prompt_file.stem if prompt_file else ("generated-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
        ext = _ext_from_output_format(output_format) or ".jpg"
        out_path = out_root / (name + ext)

    _ensure_parent(out_path)

    # ===== 双引擎 Fallback =====
    # 优先级：--provider CLI > VF_PROVIDER 环境变量 > 默认 auto
    provider = (args.provider or os.getenv("VF_PROVIDER", "auto")).strip().lower()
    _eprint(f"引擎配置：provider={provider} base_url={base_url} model={model}")
    _eprint(f"api_key={_mask(api_key)} aspect_ratio={aspect_ratio} image_size={image_size or 'DEFAULT'}")
    _eprint(f"prompt（{len(prompt_text)}字）：{prompt_text[:200]}{'...' if len(prompt_text) > 200 else ''}")

    success = False
    saved_paths: list[pathlib.Path] = []
    debug_info: dict = {}

    if provider in ("auto", "yunwu"):
        is_openai_model = model.startswith("gpt-image")
        if is_openai_model:
            # gpt-image-2 等模型走 OpenAI Images 兼容格式（dall-e-3）
            _eprint(f"尝试 yunwu 引擎（OpenAI Images 格式）...")
            ok, paths, info = _generate_via_openai_images(
                prompt=prompt_text, out_path=out_path,
                aspect_ratio=aspect_ratio,
                base_url=base_url, model=model, api_key=api_key,
                timeout_s=timeout_s,
            )
        else:
            # Gemini 原生模型走 generateContent 格式
            _eprint(f"尝试 yunwu 引擎（Gemini 格式）...")
            ok, paths, info = _generate_via_gemini(
                prompt=prompt_text, out_path=out_path, aspect_ratio=aspect_ratio,
                image_size=image_size, ref_images=ref_images,
                base_url=base_url, model=model, api_key=api_key,
                timeout_s=timeout_s, max_retries=max_retries,
                retry_backoff_s=retry_backoff_s, auth_mode=auth_mode,
                api_version=api_version, output_format=output_format,
                jpg_quality=jpg_quality, save_response_json=save_response_json,
                save_thought_images=save_thought_images, prompt_file=prompt_file,
            )
        if ok:
            success = True
            saved_paths = paths
            debug_info = info
        else:
            _eprint(f"yunwu 失败：{info if isinstance(info, str) else info.get('detail', info)}")
            if provider == "yunwu":
                raise SystemExit(f"yunwu 引擎失败且不 fallback：{info}")

    if not success and provider in ("auto", "grsai"):
        model_to_use = args.model or grsai_default_model
        # 合并 URL 参考 + 本地文件 OSS 上传
        all_urls = list(args.reference_url or [])
        for img_path in ref_images:
            url = _upload_to_oss(img_path)
            if url:
                all_urls.append(url)
            else:
                _eprint(f"警告：本地参考图上传失败 {img_path}，已跳过，继续文生图")
        _eprint(f"尝试 grsai 引擎 (model={model_to_use})...")
        ok, paths, info = _generate_via_grsai(
            prompt=prompt_text, out_path=out_path,
            aspect_ratio=aspect_ratio, image_size=image_size,
            model=model_to_use, ref_urls=all_urls or None,
        )
        if ok:
            success = True
            saved_paths = paths
            debug_info = info
        else:
            _eprint(f"grsai 失败：{info}")

    if not success:
        raise SystemExit("所有生图引擎均失败")

    # 输出摘要
    engine_name = debug_info.get("engine", "unknown")
    print("生图完成")
    print(f"- 引擎: {engine_name}")
    print(f"- 保存: {len(saved_paths)} 张")
    for s in saved_paths:
        print(f"  - {s}")


if __name__ == "__main__":
    t0 = time.time()
    try:
        main()
    finally:
        _eprint(f"Done in {time.time() - t0:.2f}s")
