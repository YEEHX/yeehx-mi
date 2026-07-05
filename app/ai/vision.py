# 来源：phase0/yeehx_phase0/vision.py  用途：调本地 ollama 视觉接口打标；支持 mock 模式（YEEHX_MOCK=1）
"""调本地 ollama 的 OpenAI 兼容视觉接口给缩略图打标。

POST {base_url}/chat/completions
  system = prompt.SYSTEM_PROMPT
  user   = 文本(地点先验/机位线索) + 素材缩略图(base64 data URI)

mock=True 或环境变量 YEEHX_MOCK=1:不连模型,返回确定性假 JSON,用于沙箱里验证整条管线机制。

错误处理铁律：本模块所有对外请求统一捕获 _REQ_ERRORS（含 OSError——
certifi 证书包失效/网络栈异常时 requests 抛的是 OSError 而非 RequestException，
曾因此把 /api/ai_search 炸成 500），永远返回 {ok: False, error: ...}，不向上抛异常。
"""
from __future__ import annotations
import base64
import json
import hashlib
import os
import re
from pathlib import Path

import requests

from app.ai import prompt as P

# requests 在 TLS 证书包缺失、DNS/套接字底层故障时抛 OSError（不是 RequestException）；
# 响应体解析偶发 ValueError。三类一起兜住，调用方只看 ok/error。
_REQ_ERRORS = (requests.RequestException, OSError, ValueError)


def _b64_data_uri(img: Path) -> str:
    raw = img.read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def _is_mock(mock: bool) -> bool:
    """判断是否走 mock 模式：参数 mock=True 或环境变量 YEEHX_MOCK=1。"""
    return mock or bool(os.environ.get("YEEHX_MOCK"))


def tag_images(images: list[Path], location_hint: str | None, cam_hint: str | None,
               cfg, model: str | None = None, mock: bool = False,
               context: dict | None = None, tag_library: dict | None = None) -> dict:
    """对素材缩略图打标,返回 {ok, json, raw, model, error}。"""
    model = model or cfg.model
    if _is_mock(mock):
        return {"ok": True, "model": f"mock:{model}", "raw": "",
                "json": _mock_json(images, location_hint, tag_library)}

    user_text = P.build_user_text(location_hint, cam_hint, context=context, tag_library=tag_library)
    content = [{"type": "text", "text": user_text}]
    try:
        for img in images:
            content.append({"type": "image_url",
                            "image_url": {"url": _b64_data_uri(img)}})
    except OSError as e:
        return {"ok": False, "model": model, "error": f"读不到缩略图: {e}", "json": None, "raw": ""}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": P.SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": cfg.temperature,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    url = f"{cfg.base_url}/chat/completions"
    try:
        r = requests.post(url, json=payload,
                          headers={"Authorization": f"Bearer {cfg.api_key}"},
                          timeout=cfg.timeout)
    except _REQ_ERRORS as e:
        return {"ok": False, "model": model, "error": f"连不上模型({url}): {e}", "json": None, "raw": ""}
    if r.status_code != 200:
        # 有些 ollama 版本不认 response_format,去掉重试一次
        payload.pop("response_format", None)
        try:
            r = requests.post(url, json=payload,
                              headers={"Authorization": f"Bearer {cfg.api_key}"},
                              timeout=cfg.timeout)
        except _REQ_ERRORS as e:
            return {"ok": False, "model": model, "error": str(e), "json": None, "raw": ""}
    if r.status_code != 200:
        return {"ok": False, "model": model, "error": f"HTTP {r.status_code}: {r.text[:300]}",
                "json": None, "raw": r.text[:1000]}

    try:
        msg = r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "model": model, "error": f"返回结构异常: {e}", "json": None, "raw": r.text[:1000]}

    parsed = P.safe_parse(msg)
    if parsed is None:
        return {"ok": False, "model": model, "error": "模型没给出合法 JSON", "json": None, "raw": msg[:1000]}
    return {"ok": True, "model": model, "error": None, "json": P.normalize(parsed), "raw": msg[:1500]}


# classify_places（纯文字判地名）已删除：全仓零调用的死代码。地点治理走
# 标签库人工确认 + place_guess 只命中已有地点（v1.3.0 防膨胀方案），不再需要它。


def suggest_tag_merges(tags: list[dict], cfg, model: str | None = None, mock: bool = False,
                       max_suggestions: int = 80) -> dict:
    """Use a text model to suggest duplicate tag merges.

    The model returns names only; callers must resolve and filter against the
    current local tag table before storing suggestions.
    """
    model = model or cfg.model
    max_suggestions = max(1, min(int(max_suggestions or 80), 160))
    items = [_tag_for_prompt(t) for t in tags if (t.get("name") or "").strip()]
    if not items:
        return {"ok": True, "model": model, "json": {"suggestions": []}, "raw": ""}
    if _is_mock(mock):
        return {"ok": True, "model": f"mock:{model}",
                "json": {"suggestions": _mock_merge_suggestions(items, max_suggestions)}, "raw": ""}

    sys = (
        "你是中文影视素材标签库的标签治理助手。你只负责发现真正重复、同义、错别字、空格/中英文写法差异的标签。"
        "不要合并上下位、包含关系、风格相近但不等价、地点层级不同、对象数量不同的词。"
        "例如: 武汉 和 武汉大学 不合并; 建筑 和 古建筑 不合并; 人物 和 人群 不合并; 夜景 和 晚霞 不合并。"
        "只输出 JSON。"
    )
    user = (
        "从下面标签列表里找可以把 source 并入 target 的建议。target 应该是更规范、更常用的现有标签名。"
        f"尽量系统地检查，最多返回 {max_suggestions} 条。每条建议必须给 confidence(0-1) 和一句 reason。没有把握就不要凑数。"
        "输出格式: {\"suggestions\":[{\"source\":\"旧标签名\",\"target\":\"目标标签名\",\"confidence\":0.86,\"reason\":\"...\"}]}\n"
        f"标签列表:{json.dumps(items, ensure_ascii=False)}"
    )
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        "temperature": 0.0,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    url = f"{cfg.base_url}/chat/completions"
    try:
        r = requests.post(url, json=payload, headers={"Authorization": f"Bearer {cfg.api_key}"}, timeout=cfg.timeout)
        if r.status_code != 200:
            payload.pop("response_format", None)
            r = requests.post(url, json=payload, headers={"Authorization": f"Bearer {cfg.api_key}"}, timeout=cfg.timeout)
    except _REQ_ERRORS as e:
        return {"ok": False, "model": model, "error": f"连不上模型({url}): {e}", "json": None, "raw": ""}
    if r.status_code != 200:
        return {"ok": False, "model": model, "error": f"HTTP {r.status_code}: {r.text[:300]}", "json": None, "raw": r.text[:1000]}
    try:
        msg = r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "model": model, "error": f"返回结构异常: {e}", "json": None, "raw": r.text[:1000]}
    obj = P.safe_parse(msg)
    if not isinstance(obj, dict):
        return {"ok": False, "model": model, "error": "模型没给出合法 JSON", "json": None, "raw": msg[:1000]}
    return {"ok": True, "model": model, "error": None,
            "json": _normalize_merge_response(obj, max_suggestions), "raw": msg[:1500]}


def parse_command(query: str, tag_library: dict, cfg, model: str | None = None,
                  selected: list[str] | None = None) -> dict:
    """自然语言指令 → {action, tags, keywords, scope, export{type,target}, note}。
    action 白名单只有 search / export（导出=复制）；解析层不具备、也永远不给
    删除/移动/改写原素材的动作。mock 模式按规则解析，确定性。"""
    model = model or cfg.model
    query = (query or "").strip()
    if _is_mock(False):
        flat = [n for names in (tag_library or {}).values() for n in (names or [])]
        m_scope = re.search(r"在\s*([^\s，。,]+?)\s*(?:里|内|中)", query)
        m_target = re.search(r"导出到\s*([^\s，。,]+)", query)
        # 标签匹配前剥掉 目标路径 和 范围名，避免路径/文件夹名里的字撞标签
        q_for_tags = re.sub(r"导出到\s*[^\s，。,]+", "", query)
        if m_scope:
            q_for_tags = q_for_tags.replace(m_scope.group(0), "")
        hits = [n for n in flat if n and n in q_for_tags]
        action = "export" if ("导出" in query or "复制到" in query) else "search"
        return {"ok": True, "model": f"mock:{model}", "error": None,
                "json": {"action": action,
                         "tags": list(dict.fromkeys((selected or []) + hits)),
                         "keywords": "" if hits else query,
                         "scope": m_scope.group(1) if m_scope else "",
                         "export": {"type": "original", "target": m_target.group(1) if m_target else ""},
                         "note": "mock 解析"}}

    sys = ("你是影视素材库的检索/导出指令解析器。把用户的自然语言拆成 JSON："
           "action——只有两种：\"search\"（找素材）或 \"export\"（用户明确说要 导出/复制/拷贝到某处 才用）；"
           "tags——只能从给定标签库里选（含义贴近才选，宁缺毋滥）；"
           "keywords——标签库覆盖不了、但适合全文搜索的词（空格分隔，没有给空字符串）；"
           "scope——用户限定的范围文件夹/盘的名字或路径片段（如\"02_视频\"\"AI资产\"），没限定给空字符串；"
           "export——{type,target}：type 默认 original（复制原素材），用户提到 图片/帧/截图 用 image，提到 清单 用 manifest；"
           "target 是目标文件夹路径：\"桌面\"理解为 ~/Desktop，\"下载\"为 ~/Downloads，子文件夹拼在后面；说不清就给空字符串；"
           "note——一句话复述你的理解。只输出一个 JSON 对象。")
    parts = []
    if selected:
        parts.append("用户已选条件（必须保留在 tags 里，在此基础上叠加）：" + json.dumps(selected, ensure_ascii=False))
    parts.append("标签库（按类目）：" + json.dumps(tag_library, ensure_ascii=False))
    parts.append(f"用户指令：{query}")
    parts.append('输出格式：{"action":"export","tags":["武当山","雪","航拍"],"keywords":"空镜",'
                 '"scope":"02_视频","export":{"type":"original","target":"~/Desktop/武当山备选"},'
                 '"note":"在02_视频里找武当山雪景航拍空镜，复制原素材到桌面武当山备选"}')
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": sys}, {"role": "user", "content": "\n".join(parts)}],
        "temperature": 0.0, "stream": False, "response_format": {"type": "json_object"},
    }
    url = f"{cfg.base_url}/chat/completions"
    try:
        r = requests.post(url, json=payload, headers={"Authorization": f"Bearer {cfg.api_key}"},
                          timeout=cfg.timeout)
        if r.status_code != 200:
            payload.pop("response_format", None)
            r = requests.post(url, json=payload, headers={"Authorization": f"Bearer {cfg.api_key}"},
                              timeout=cfg.timeout)
    except _REQ_ERRORS as e:
        return {"ok": False, "model": model, "error": f"连不上模型({url}): {e}", "json": None}
    if r.status_code != 200:
        return {"ok": False, "model": model, "error": f"HTTP {r.status_code}: {r.text[:200]}", "json": None}
    try:
        msg = r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as e:
        return {"ok": False, "model": model, "error": f"返回结构异常: {e}", "json": None}
    obj = P.safe_parse(msg)
    if not isinstance(obj, dict):
        return {"ok": False, "model": model, "error": "模型没给出合法 JSON", "json": None}
    tags = obj.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    exp = obj.get("export") if isinstance(obj.get("export"), dict) else {}
    etype = str(exp.get("type") or "original").strip()
    return {"ok": True, "model": model, "error": None, "json": {
        "action": "export" if str(obj.get("action") or "").strip() == "export" else "search",
        "tags": [str(t).strip() for t in tags if str(t).strip()][:12],
        "keywords": str(obj.get("keywords") or "").strip()[:80],
        "scope": str(obj.get("scope") or "").strip()[:80],
        "export": {"type": etype if etype in ("original", "image", "manifest") else "original",
                   "target": str(exp.get("target") or "").strip()[:300]},
        "note": str(obj.get("note") or "").strip()[:120],
    }}


def ping(cfg) -> tuple[bool, str]:
    """探活:列模型。返回 (可用?, 说明)。"""
    ok, names, msg = list_models(cfg)
    if ok:
        return True, f"在线，已加载模型: {', '.join(names) or '(空)'}"
    return False, msg


def list_models(cfg) -> tuple[bool, list[str], str]:
    """读取 OpenAI-compatible /models,返回可选模型列表。"""
    try:
        r = requests.get(f"{cfg.base_url}/models",
                         headers={"Authorization": f"Bearer {cfg.api_key}"}, timeout=10)
        if r.status_code == 200:
            names = [m.get("id", "") for m in r.json().get("data", [])]
            return True, [n for n in names if n], "在线"
        return False, [], f"HTTP {r.status_code}"
    except _REQ_ERRORS as e:
        return False, [], str(e)


# ── mock:确定性假数据,沙箱验证用 ──
def _tag_for_prompt(t: dict) -> dict:
    out = {"name": str(t.get("name") or "").strip(), "category": t.get("category") or "未分类"}
    aliases = [str(a).strip() for a in (t.get("aliases") or []) if str(a).strip()]
    if aliases:
        out["aliases"] = aliases[:8]
    return out


_MERGE_NORM_RE = re.compile(r"[\s·・,，、/\\_\-—:：()（）\[\]【】]+")


def _merge_norm(value: str) -> str:
    return _MERGE_NORM_RE.sub("", str(value or "")).casefold()


def _mock_merge_suggestions(items: list[dict], limit: int = 40) -> list[dict]:
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for item in items:
        key = _merge_norm(item["name"])
        if not key:
            continue
        prev = seen.get(key)
        if prev and prev.get("category") == item.get("category") and prev["name"] != item["name"]:
            source, target = sorted([item["name"], prev["name"]], key=len, reverse=True)
            out.append({"source": source, "target": target, "confidence": 0.9, "reason": "名称规范化后完全一致"})
        else:
            seen[key] = item
    return out[:limit]


def _normalize_merge_response(obj: dict, limit: int = 80) -> dict:
    raw = obj.get("suggestions") or obj.get("merges") or []
    out = []
    if not isinstance(raw, list):
        return {"suggestions": out}
    for item in raw:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or item.get("from") or "").strip()
        target = str(item.get("target") or item.get("to") or "").strip()
        if not source or not target or source == target:
            continue
        try:
            conf = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0
        out.append({
            "source": source,
            "target": target,
            "confidence": max(0.0, min(1.0, conf)),
            "reason": str(item.get("reason") or "").strip()[:160],
        })
    return {"suggestions": out[:limit]}


def _mock_json(images: list[Path], location_hint: str | None, tag_library: dict | None = None) -> dict:
    seed = int(hashlib.md5((str(images[0]) if images else "x").encode()).hexdigest(), 16)
    pick = lambda lst, n: [lst[(seed >> (i * 3)) % len(lst)] for i in range(n)]
    flat_tags = [n for names in (tag_library or {}).values() for n in (names or [])]
    confidence = 0.86 if (location_hint and "黄鹤楼" in location_hint) else round(0.5 + (seed % 50) / 100, 2)
    obj = {
        "主体": pick(P.VOCAB["主体"], 2),
        "镜头": pick(P.VOCAB["镜头"], 2),
        "氛围": pick(P.VOCAB["氛围"], 1),
        "时间": pick(P.VOCAB["时间"], 2),
        "风格": pick(P.VOCAB["风格"], 1),
        "地标": (["黄鹤楼"] if (location_hint and "黄鹤楼" in location_hint) else []),
        "ai生成": bool(location_hint and "数字人" in str(location_hint)),
        "keywords": ["测试", "mock"],
        "location_check": "不确定" if not location_hint else ("相符", "不符", "不确定")[seed % 3],
        "place_guess": ({"name": location_hint, "confidence": 0.6, "reason": "mock一致"}
                        if (location_hint and seed % 2 == 0)
                        else ({"name": "光谷", "confidence": 0.55, "reason": "mock不一致"}
                              if seed % 3 == 0 else None)),
        "existing_tags": [{"name": flat_tags[seed % len(flat_tags)], "confidence": 0.8, "evidence": "mock"}]
                         if flat_tags else [],
        "new_candidates": [{"name": "测试候选词", "category": "内容", "confidence": 0.7, "reason": "mock"}],
        "reject_terms": [],
        "描述": f"[MOCK] 占位描述,{len(images)} 帧,用于验证管线,不代表真实画面",
        "confidence": confidence,
    }
    return P.normalize(obj)
