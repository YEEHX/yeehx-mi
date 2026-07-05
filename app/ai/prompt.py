# 来源：phase0/yeehx_phase0/prompt.py  用途：固定词表 + 视觉打标系统提示词（YEEHX 六维标签）
"""固定词表 + 视觉打标系统提示词(YEEHX 版六维标签)。

模型负责综合缩略图画面、路径、文件名、拍摄时间和元数据，返回结构化判断。
已有标签只能从系统给出的标签库里命中；新词只作为候选，人工确认后才进入标签库。
"""
from __future__ import annotations
import json

# ── 固定词表(六维里的"模型四维") ───────────────────────────
VOCAB = {
    "主体": [
        # 画面主体类别。城市和具体地标单独归 location/地标,不放这里。
        "建筑", "江河", "湖泊", "人物", "食物", "商业", "艺术品",
        "古建筑", "现代建筑", "摩天楼", "桥梁", "塔", "老街", "民居", "寺庙",
        "山", "云海", "树木", "花", "田野", "城市天际线", "车流", "灯光夜景",
        "人群", "人物背影", "情侣", "儿童", "手艺人", "戏曲民俗", "非遗手工",
        "绿幕", "AI角色素材", "物件",
    ],
    "镜头": [
        "航拍", "FPV", "广角", "中景", "特写", "延时",
        "大全景", "全景", "近景", "超广角", "长焦", "微距",
        "手持", "固定机位", "稳定器运镜", "滑轨", "车载",
        "移动延时", "升格慢动作", "一镜到底", "环绕", "跟拍",
    ],
    "氛围": [
        "震撼", "烟火气", "科技感", "文艺", "古风", "商业", "生活",
        "壮阔", "孤独", "苍凉", "浪漫", "治愈", "温暖", "宁静", "静谧",
        "空灵", "诗意", "繁华", "肃穆", "神秘", "冷峻",
    ],
    "时间": [
        # 时段
        "日出", "清晨", "白天", "上午", "正午", "下午", "黄昏", "蓝调", "夜景", "深夜",
        # 天况(并入)
        "晴天", "多云", "阴天", "雨天", "雪天", "雾", "霾",
        "晚霞", "朝霞", "火烧云", "丁达尔光", "星空", "彩虹", "雨幡", "打雷", "暴雨",
    ],
    "风格": [
        "写实", "电影感", "纪实", "动漫", "二次元", "3D", "卡通", "国风", "水墨", "油画",
        "像素", "赛博朋克", "蒸汽波", "复古胶片", "黑白", "超现实", "极简", "梦幻", "末日废土",
    ],
}
MULTI = {"主体", "镜头", "氛围", "时间", "风格"}   # 都可多选(如 时间=[黄昏,火烧云])
SINGLE: set[str] = set()


def _vocab_block() -> str:
    lines = []
    for field in ["主体", "镜头", "氛围", "时间", "风格"]:
        lines.append(f"- {field}(可多选): {' / '.join(VOCAB[field])}")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""你是影视素材打标助手,给导演的镜头素材库打结构化标签。你看到的是从素材里抽出的代表帧。

【最重要的铁律】
1. **描述字段写自然画面描述**,可以写清画面里可见的对象、人物、食物、天气、构图;如果你明确认出地标,地标名主要放到"地标"数组。
2. 固定分类字段(主体/镜头/氛围/时间/风格)**只能从下面词表里选**,不许自造同义词。词表没有的细节放进 keywords。
3. 系统会给"素材上下文"(路径、文件名、日期、设备等)和"已有标签库"。上下文是证据,不是结论;画面支持或不矛盾时才提高置信度。
4. existing_tags **只能填已有标签库中的标签名**,表示你认为应该直接挂上的标签。
5. new_candidates 极少使用,只填能跨大量素材复用的稳定新词;普通画面细节放 keywords,编号、一次性项目名、纯日期、过细文件名放 reject_terms。
6. 不确定的字段就**留空数组 []**,不要硬凑。
7. 严格只输出一个 JSON 对象,不要任何解释、前后缀、markdown 代码块。

【词表】
{_vocab_block()}

【location_check】
系统会给"路径推测地点"(文件夹/文件名先验,可能错)。你**不要**说地名,只判断画面与该先验"相符/不符/不确定":
- 画面与先验**明显矛盾** → "不符";**支持或不矛盾** → "相符";看不出 → "不确定"。

【place_guess(单独猜地点 —— 这里允许说地名)】
对每条都猜一下"可能是哪里":{{"name": 地点名, "confidence": 0~1, "reason": 一句依据}};完全看不出 → null。
place_guess 只作辅助线索,不会直接创建新地点标签。普通街景/人群/特写物品看不出地点的,给 null。

【地标(可多个,独立字段)】
画面里你**认得出的标志性建筑/地标**(如 黄鹤楼、武汉长江大桥、龟山电视塔、江汉关、东方明珠 等)列进 "地标" 数组,**可多个**(一张图里有几个就都写);认不出给空数组 []。不要把具体地标名写进"主体"。

【ai生成】
画面是不是**明显的 AI 生成/合成**(数字人、AI 虚拟头像、动漫/二次元渲染、3D CGI、明显 AI 痕迹)→ ai生成 给 true;真实拍摄的 → false;拿不准 → false。

【输出 JSON 模板(严格按此结构,字段都要在)】
{{
  "主体": ["古建筑", "城市天际线"],
  "镜头": ["航拍", "大全景"],
  "氛围": ["震撼", "古风"],
  "时间": ["黄昏", "火烧云"],
  "风格": ["写实"],
  "keywords": ["飞檐", "暖光", "江景"],
  "地标": ["黄鹤楼"],
  "ai生成": false,
  "location_check": "相符",
  "place_guess": {{"name": "某古塔/古建筑群", "confidence": 0.5, "reason": "山顶多层飞檐塔楼,临江"}},
  "existing_tags": [{{"name": "航拍", "confidence": 0.9, "evidence": "image"}}, {{"name": "黄昏", "confidence": 0.82, "evidence": "image+path"}}],
  "new_candidates": [{{"name": "城市天际线", "category": "内容", "confidence": 0.78, "reason": "可复用的画面内容"}}],
  "reject_terms": [{{"name": "260303", "reason": "像日期或编号,不适合作为标签"}}],
  "描述": "黄昏时段的航拍城市江景,画面中飞檐古建筑位于前景,远处有城市天际线与江面,天空呈现火烧云和暖色霞光。",
  "confidence": 0.86
}}
"""


def build_user_text(path_location_hint, cam_hint, context: dict | None = None,
                    tag_library: dict | None = None) -> str:
    """拼用户消息。顺序有讲究（v1.8.1）：标签库是整批打标全程不变的大块
    （几百词+别名，数千 token），必须放最前——ollama/llama.cpp 的前缀缓存按
    "与上一条请求的最长公共前缀"复用 KV，静态块在前 = 每条素材只需重算
    后面几行变项和图片；旧版把逐素材变化的先验放前面，等于每条都把整段
    词表重新 prefill 一遍，本地大模型打标因此慢一大截。"""
    parts = []
    if tag_library:
        parts.append("已有标签库(只能从这里填 existing_tags):" + json.dumps(tag_library, ensure_ascii=False))
    if path_location_hint:
        parts.append(f"路径推测地点(仅先验,可能错,你只做相符判断、别说地名):「{path_location_hint}」")
    else:
        parts.append("路径无地点先验。location_check 给 \"不确定\"。")
    if cam_hint:
        parts.append(f"机位线索(参考):{cam_hint}")
    if context:
        parts.append("素材上下文(证据,不是结论):" + json.dumps(context, ensure_ascii=False))
    parts.append("现在只输出 JSON。")
    return "\n".join(parts)


def normalize(obj: dict) -> dict:
    out = dict(obj or {})
    for field in MULTI:
        vals = out.get(field, [])
        if isinstance(vals, str):
            vals = [vals]
        if field in VOCAB:
            vals = [v for v in vals if v in VOCAB[field]]
        out[field] = vals
    if not isinstance(out.get("keywords"), list):
        out["keywords"] = [out["keywords"]] if out.get("keywords") else []
    if out.get("location_check") not in ("相符", "不符", "不确定"):
        out["location_check"] = "不确定"
    try:
        out["confidence"] = max(0.0, min(1.0, float(out.get("confidence", 0))))
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    out["描述"] = str(out.get("描述", "")).strip()
    pg = out.get("place_guess")
    if isinstance(pg, dict) and pg.get("name"):
        try:
            conf = max(0.0, min(1.0, float(pg.get("confidence", 0))))
        except (TypeError, ValueError):
            conf = 0.0
        out["place_guess"] = {"name": str(pg.get("name")).strip(), "confidence": conf,
                              "reason": str(pg.get("reason", "")).strip()}
    else:
        out["place_guess"] = None
    lm = out.get("地标")
    if isinstance(lm, str):
        lm = [lm]
    out["地标"] = [str(x).strip() for x in lm if str(x).strip()] if isinstance(lm, list) else []
    out["ai生成"] = bool(out.get("ai生成"))
    existing = out.get("existing_tags") or out.get("existing_tag_hits") or []
    norm_existing = []
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, str):
                norm_existing.append({"name": item.strip(), "confidence": 1.0, "evidence": ""})
            elif isinstance(item, dict):
                name = str(item.get("name") or item.get("tag") or "").strip()
                if not name:
                    continue
                try:
                    conf = max(0.0, min(1.0, float(item.get("confidence", 1.0))))
                except (TypeError, ValueError):
                    conf = 1.0
                norm_existing.append({"name": name, "confidence": conf,
                                      "evidence": str(item.get("evidence") or "").strip()})
    out["existing_tags"] = norm_existing
    new_candidates = []
    raw_candidates = out.get("new_candidates") or []
    if isinstance(raw_candidates, list):
        for item in raw_candidates:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            try:
                conf = max(0.0, min(1.0, float(item.get("confidence", 0.5))))
            except (TypeError, ValueError):
                conf = 0.5
            new_candidates.append({"name": name, "category": str(item.get("category") or "未分类").strip(),
                                   "confidence": conf, "reason": str(item.get("reason") or "").strip()})
    out["new_candidates"] = new_candidates
    rejects = out.get("reject_terms") or []
    out["reject_terms"] = rejects if isinstance(rejects, list) else []
    known = {"主体", "镜头", "氛围", "时间", "风格", "keywords", "地标", "ai生成", "location_check",
             "place_guess", "描述", "confidence", "existing_tags", "new_candidates", "reject_terms"}
    return {k: v for k, v in out.items() if k in known}


def safe_parse(text: str):
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    a, b = t.find("{"), t.rfind("}")
    if a != -1 and b != -1 and b > a:
        t = t[a:b + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None
