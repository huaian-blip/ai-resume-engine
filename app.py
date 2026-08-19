"""AI 简历引擎 — FastAPI 调用示例。

运行：
    pip install -r requirements.txt
    copy .env.example .env  # 填写密钥
    uvicorn app:app --reload

接口文档：http://127.0.0.1:8000/docs
"""

import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

import jsonschema
import prompts
import template_engine
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from openai import BadRequestError, OpenAI
from pydantic import BaseModel, Field

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_DIR = BASE_DIR / "schemas"

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
)
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

app = FastAPI(title="AI 简历引擎", version="1.1.0")

# 允许的前端来源（GitHub Pages 域名）。浏览器请求携带 Origin 头，
# 命中白名单即可免令牌访问；非浏览器客户端仍须走 API_TOKEN。
ALLOWED_ORIGINS = [
    o.strip().rstrip("/")
    for o in os.getenv("ALLOWED_ORIGINS", "https://huaian-blip.github.io").split(",")
    if o.strip()
]

# 跨域白名单（收紧，不再全开）
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 可选 API 访问令牌：设置 API_TOKEN 后，除首页/文档外所有接口需携带
# Authorization: Bearer <API_TOKEN>。浏览器来源命中 ALLOWED_ORIGINS 可免令牌。
API_TOKEN = os.getenv("API_TOKEN", "").strip()

# 用量统计目录（每次 LLM 调用追加一行记录，便于核对成本）
USAGE_DIR = BASE_DIR / ".usage"
# LLM 调用速率限制：按用户 Key 滑动窗口（1 分钟），防令牌泄露后被刷爆
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))
_rate_bucket: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def _origin_allowed(origin: str) -> bool:
    return any(origin.rstrip("/") == o for o in ALLOWED_ORIGINS)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # CORS 预检请求必须放行（浏览器预检不携带自定义请求头）
    if request.method == "OPTIONS":
        return await call_next(request)
    if API_TOKEN:
        path = request.url.path
        if not (path in ("/", "/docs", "/redoc", "/openapi.json", "/usage/page") or path.startswith("/docs/")):
            # 浏览器来源命中白名单 → 免令牌（config.js 不再公开令牌）
            origin = request.headers.get("origin", "")
            if origin and _origin_allowed(origin):
                return await call_next(request)
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {API_TOKEN}":
                return Response("unauthorized", status_code=401)
    return await call_next(request)


def load_schema(name: str) -> dict:
    with open(SCHEMA_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


JD_SCHEMA = load_schema("jd_schema.json")
MATCH_SCHEMA = load_schema("match_schema.json")
STAR_SCHEMA = load_schema("star_schema.json")
SELF_EVAL_SCHEMA = load_schema("self_eval_schema.json")


def _compose_full_schema() -> dict:
    """组合解析+匹配为单次调用的 Schema（复用两个子 Schema，避免重复定义）。"""
    def _strip(s: dict) -> dict:
        return {k: v for k, v in s.items() if k not in ("$schema", "title")}

    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "FullAnalysisOutput",
        "type": "object",
        "additionalProperties": False,
        "required": ["parsed_jd", "match"],
        "properties": {"parsed_jd": _strip(JD_SCHEMA), "match": _strip(MATCH_SCHEMA)},
    }


FULL_SCHEMA = _compose_full_schema()


def _normalize(data: dict, schema: dict) -> dict:
    """按 schema 属性名对 LLM 输出做模糊键名归并（如 responsibility_keywords -> responsibility）。"""
    if not isinstance(data, dict) or not isinstance(schema, dict):
        return data
    for key, spec in schema.get("properties", {}).items():
        if key not in data:
            for k in list(data.keys()):
                if key in k:
                    data[key] = data.pop(k)
                    break
        if key in data and isinstance(data[key], dict) and isinstance(spec, dict) and "properties" in spec:
            _normalize(data[key], spec)
    return data


def _validate(data: dict, schema: dict, strict: bool) -> None:
    if strict:
        jsonschema.validate(data, schema)
        return
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError:
        _normalize(data, schema)
        try:
            jsonschema.validate(data, schema)
        except jsonschema.ValidationError:
            pass  # 软校验：json_object 无法强制结构，尽力归一化后仍不符则放行


# ---------- AI 调用成本优化 ----------

# 各任务输出上限（output tokens）：仅为防失控的"保险丝"，须设宽松以容纳正常输出；
# 模型会在内容完成时自行停止（finish=stop），上限过低反而截断 JSON → 重试更费 token。
OUTPUT_LIMITS = {
    "JdParseOutput": 2000,
    "MatchScoreOutput": 4000,
    "FullAnalysisOutput": 5000,
    "StarOptimizeOutput": 6000,
    "SelfEvaluationOutput": 2000,
}
DEFAULT_MAX_TOKENS = 4000
TEMPERATURE = 0.3  # 结构化任务用低温，减少发散与重试

# 客户端缓存：同一 (key, base) 复用连接，避免每次新建握手开销
_CLIENT_CACHE: dict[tuple, OpenAI] = {}
# 响应缓存：相同输入的重复请求直接命中，零 token 消耗。
# 两层：内存 L1（快）+ SQLite L2（持久，后端重启后仍命中）。
_RESPONSE_CACHE: dict[str, dict] = {}
_RESPONSE_CACHE_MAX = int(os.getenv("RESPONSE_CACHE_MAX", "128"))
_CACHE_DB = BASE_DIR / ".cache" / "llm_cache.sqlite3"
_cache_lock = threading.Lock()


def _cache_init() -> None:
    _CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_CACHE_DB) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, data TEXT, created REAL)"
        )
        # 启动时若超出上限，先淘汰最旧的
        conn.execute(
            "DELETE FROM cache WHERE key NOT IN (SELECT key FROM cache ORDER BY created DESC LIMIT ?)",
            (_RESPONSE_CACHE_MAX,),
        )


def _cache_get(key: str):
    item = _RESPONSE_CACHE.get(key)
    if item:
        return item["data"]
    with _cache_lock, sqlite3.connect(_CACHE_DB) as conn:
        row = conn.execute(
            "SELECT data FROM cache WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    data = json.loads(row[0])
    # 命中磁盘缓存 → 提升到内存 L1
    if len(_RESPONSE_CACHE) < _RESPONSE_CACHE_MAX:
        _RESPONSE_CACHE[key] = {"data": data}
    return data


def _cache_put(key: str, data: dict) -> None:
    if _RESPONSE_CACHE_MAX <= 0:
        return
    if len(_RESPONSE_CACHE) >= _RESPONSE_CACHE_MAX:
        # FIFO 淘汰最旧的一半，足够应对重复请求场景
        for k in list(_RESPONSE_CACHE)[: len(_RESPONSE_CACHE) // 2]:
            _RESPONSE_CACHE.pop(k, None)
    _RESPONSE_CACHE[key] = {"data": data}
    with _cache_lock, sqlite3.connect(_CACHE_DB) as conn:
        conn.execute(
            "INSERT INTO cache (key, data, created) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET data = excluded.data, created = excluded.created",
            (key, json.dumps(data, ensure_ascii=False), time.time()),
        )
        conn.execute(
            "DELETE FROM cache WHERE key NOT IN (SELECT key FROM cache ORDER BY created DESC LIMIT ?)",
            (_RESPONSE_CACHE_MAX,),
        )


_cache_init()


def _get_client(key: str, base: str) -> OpenAI:
    k = (key, base)
    c = _CLIENT_CACHE.get(k)
    if c is None:
        c = OpenAI(api_key=key, base_url=base)
        _CLIENT_CACHE[k] = c
    return c


def _cache_key(llm_model: str, system: str, user: str, schema_name: str, mode: str) -> str:
    h = hashlib.sha256()
    for part in (llm_model, system, user, schema_name, mode):
        h.update(part.encode("utf-8"))
    return h.hexdigest()


def compact_schema(schema: dict) -> dict:
    """把完整 JSON Schema 压缩成 token 精简版（仅保留字段名/类型/必填，供 json_object 降级提示词使用）。

    完整 Schema 含 $schema/description/additionalProperties 等大段说明文字，
    注入提示词纯属浪费 token；这里只保留模型真正需要的结构信息。
    """
    def _compact(s: dict) -> object:
        t = s.get("type")
        if t == "object":
            return {
                "type": "object",
                "required": sorted(s.get("required", [])),
                "properties": {k: _compact(v) for k, v in s.get("properties", {}).items()},
            }
        if t == "array":
            return {"type": "array", "items": _compact(s.get("items", {}))}
        return {"type": t}

    return _compact(schema)


def _extract_json(content: str) -> str:
    """从 LLM 输出中稳健提取 JSON 文本（容忍 markdown 代码块与前后缀文字），减少解析失败重试。"""
    if not content:
        raise ValueError("LLM 输出为空")
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith(("json", "JSON")):
            text = text[4:].lstrip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("LLM 输出中未找到 JSON 对象")
    return text[start : end + 1]


def _check_rate_limit(scope: str) -> None:
    """滑动窗口限流：同一用户 Key 每分钟最多 RATE_LIMIT_PER_MIN 次 LLM 调用。"""
    now = time.time()
    with _rate_lock:
        bucket = [t for t in _rate_bucket.get(scope, []) if now - t < 60]
        if len(bucket) >= RATE_LIMIT_PER_MIN:
            raise HTTPException(status_code=429, detail=f"请求过于频繁，请稍后再试（限 {RATE_LIMIT_PER_MIN} 次/分钟）")
        bucket.append(now)
        _rate_bucket[scope] = bucket


def _log_usage(label: str, llm_key: str, llm_model: str, usage) -> None:
    """记录一次 LLM 调用的 token 用量（JSONL，按天落盘），用于成本核对。"""
    if usage is None:
        return
    USAGE_DIR.mkdir(parents=True, exist_ok=True)
    # 只存 Key 的哈希，避免明文落盘
    key_hash = hashlib.sha256((llm_key or "").encode("utf-8")).hexdigest()[:12]
    record = {
        "ts": int(time.time()),
        "label": label,
        "key_hash": key_hash,
        "model": llm_model,
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }
    with open(USAGE_DIR / f"{date.today().isoformat()}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _lean_resume(resume: dict) -> dict:
    """精简简历：匹配评分只用与 JD 相关的字段，去除自我评价/期望薪资等无关大段内容，并截断长文本。

    既减少输入 token，也降低模型受无关信息干扰的概率。
    """
    def _cap(s, n=200):
        s = (s or "").strip()
        return s[:n]

    b = resume.get("basic") or {}
    lean = {"basic": {"job_intention": _cap(b.get("job_intention"))}}
    lean["education"] = [
        {"degree": e.get("degree"), "major": _cap(e.get("major"))}
        for e in (resume.get("education") or [])[:5]
    ]
    lean["skills"] = {"skills": (resume.get("skills") or {}).get("skills", [])[:20]}
    lean["work"] = [
        {
            "position": _cap(w.get("position")),
            "responsibilities": [_cap(r, 150) for r in (w.get("responsibilities") or [])][:6],
        }
        for w in (resume.get("work") or [])[:5]
    ]
    lean["projects"] = [
        {
            "role": _cap(p.get("role")),
            "description": [_cap(d, 150) for d in (p.get("description") or [])][:6],
            "achievements": [_cap(a, 150) for a in (p.get("achievements") or [])][:4],
        }
        for p in (resume.get("projects") or [])[:5]
    ]
    return lean


def chat_json(
    system: str,
    user: str,
    schema: dict,
    retries: int = 2,
    llm_key: str | None = None,
    llm_base: str | None = None,
    llm_model: str | None = None,
    label: str = "chat",
) -> dict:
    """调用 LLM 并强制 JSON 输出。

    优先使用 json_schema 结构化输出；若服务商不支持（如 DeepSeek 仅支持
    json_object），自动降级为 json_object，并注入"紧凑版" Schema（而非完整
    Schema），同时对输出做软校验 + 字段归一化。

    成本优化：低温采样、max_tokens 输出上限、相同输入响应缓存、客户端复用。
    """
    llm_key = llm_key or os.getenv("OPENAI_API_KEY")
    llm_base = llm_base or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
    llm_model = llm_model or MODEL
    schema_name = schema.get("title", "Output")
    max_tokens = OUTPUT_LIMITS.get(schema_name, DEFAULT_MAX_TOKENS)

    def _call(mode: str) -> dict:
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs = {
            "model": llm_model,
            "messages": msgs,
            "temperature": TEMPERATURE,
            "max_tokens": max_tokens,
        }
        if mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        else:
            # 降级：注入紧凑 Schema，约束字段名与结构（完整 Schema 太浪费 token）
            schema_text = json.dumps(compact_schema(schema), ensure_ascii=False)
            kwargs["messages"] = [
                {"role": "system", "content": system},
                {"role": "user", "content": user + "\n\n请基于上述输入分析后，输出一个 JSON 对象。字段名、类型与嵌套结构必须和下面完全一致，但每个字段的值要填写分析得到的实际内容（如 job_title 应填岗位名称），不要原样重复下面的结构说明：\n" + schema_text},
            ]
            kwargs["response_format"] = {"type": "json_object"}
        resp = _client.chat.completions.create(**kwargs)
        _log_usage(label, llm_key, llm_model, resp.usage)
        data = json.loads(_extract_json(resp.choices[0].message.content))
        if not isinstance(data, dict):
            raise ValueError("LLM 输出非 JSON 对象")
        # 弱模型偶发"回显 Schema 模板"而非填写数据，判定为失败并触发重试
        if {"type", "required", "properties"} <= set(data) and data.get("type") == "object":
            raise ValueError("LLM 输出疑似回显 Schema，视为失败并重试")
        _validate(data, schema, strict=(mode == "json_schema"))
        return data

    _client = _get_client(llm_key, llm_base)

    for mode in ("json_schema", "json_object"):
        for attempt in range(retries + 1):
            key = _cache_key(llm_model, system, user, schema_name, mode)
            if attempt == 0:
                hit = _cache_get(key)
                if hit is not None:
                    return hit
            try:
                _check_rate_limit(llm_key)
            except HTTPException:
                raise  # 429 限流直接透传，不进入重试兜底
            try:
                data = _call(mode)
                _cache_put(key, data)
                return data
            except BadRequestError as e:
                if mode == "json_schema" and "response_format" in str(e).lower():
                    break  # 服务商不支持 json_schema → 降级 json_object
                raise HTTPException(status_code=502, detail=f"AI 服务调用失败: {e}")
            except Exception as e:  # noqa: BLE001
                last_err = e
    raise HTTPException(status_code=502, detail=f"AI 服务调用失败: {last_err}")


def resolve_llm(request: Request) -> tuple[str | None, str | None, str | None]:
    """从请求头解析用户自带 LLM 配置。

    返回 (api_key, base_url, model)。
    - X-LLM-Key: 用户自己的 OpenAI 兼容 API Key（必填，除非是 localhost 本地开发）
    - X-LLM-Base: API 地址，默认 DeepSeek
    - X-LLM-Model: 模型名，默认 .env 的 OPENAI_MODEL
    """
    key = (request.headers.get("x-llm-key") or "").strip()
    base = (request.headers.get("x-llm-base") or "").strip() or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
    model = (request.headers.get("x-llm-model") or "").strip() or MODEL
    # 本地开发判断：cloudflared 隧道转发后 client.host 仍是 127.0.0.1，
    # 因此用 Host 头区分（本地访问 Host 为 127.0.0.1/localhost，公网为隧道域名）。
    req_host = request.headers.get("host", "") or ""
    is_local = "127.0.0.1" in req_host or "localhost" in req_host
    if not key:
        if is_local:
            key = os.getenv("OPENAI_API_KEY", "")  # 本地开发回退开发者密钥
        if not key:
            raise HTTPException(
                status_code=400,
                detail="请先在右上角「API 设置」填写你自己的 API Key（本服务不提供、也不代用开发者的密钥）",
            )
    return key, base, model


# ---------- 请求/响应模型 ----------

class JdParseRequest(BaseModel):
    job_description: str = Field(min_length=1, description="岗位 JD 原文")


class MatchScoreRequest(BaseModel):
    parsed_jd: dict = Field(description="JD 解析结果（可先调用 /parse-jd）")
    user_resume: dict = Field(description="用户简历结构化信息")


class StarExperienceItem(BaseModel):
    text: str = Field(description="原始经历描述")


class StarOptimizeRequest(BaseModel):
    job_keywords: list[str] = Field(default_factory=list, description="岗位关键词，可为空")
    experiences: list[StarExperienceItem] = Field(description="待优化的经历条目")


class SelfEvalRequest(BaseModel):
    job_keywords: list[str] = Field(default_factory=list)
    self_evaluation: str = Field(min_length=1)
    resume_highlights: list[str] = Field(default_factory=list)


class RenderRequest(BaseModel):
    resume: dict = Field(description="简历结构化数据（basic 必含 name）")
    template_id: str = Field(default="modern", description="模板 id，见 /templates")
    hide_sensitive: bool = Field(default=False, description="生成时是否隐藏敏感字段")


def _validate_resume(resume: dict) -> str:
    """轻量校验简历数据，返回模板 id。"""
    if not isinstance(resume, dict) or not isinstance(resume.get("basic"), dict) or not resume["basic"].get("name"):
        raise HTTPException(status_code=422, detail="resume.basic.name 必填")
    return True


# ---------- 接口 ----------

@app.post("/parse-jd")
def parse_jd(req: JdParseRequest, request: Request):
    key, base, model = resolve_llm(request)
    return chat_json(
        prompts.JD_SYSTEM_PROMPT,
        prompts.JD_USER_PROMPT.format(job_description=req.job_description),
        JD_SCHEMA,
        llm_key=key,
        llm_base=base,
        llm_model=model,
        label="parse-jd",
    )


@app.post("/match-score")
def match_score(req: MatchScoreRequest, request: Request):
    key, base, model = resolve_llm(request)
    return chat_json(
        prompts.MATCH_SYSTEM_PROMPT,
        prompts.MATCH_USER_PROMPT.format(
            parsed_jd=json.dumps(req.parsed_jd, ensure_ascii=False),
            user_resume=json.dumps(_lean_resume(req.user_resume), ensure_ascii=False),
        ),
        MATCH_SCHEMA,
        llm_key=key,
        llm_base=base,
        llm_model=model,
        label="match-score",
    )


class FullAnalysisRequest(BaseModel):
    job_description: str = Field(min_length=1, description="岗位 JD 原文")
    user_resume: dict = Field(description="用户简历结构化信息")


@app.post("/full-analysis")
def full_analysis(req: FullAnalysisRequest, request: Request):
    """JD 解析 + 匹配评分，一次 LLM 调用完成（省一次往返与 JD 重发）。"""
    key, base, model = resolve_llm(request)
    return chat_json(
        prompts.FULL_SYSTEM_PROMPT,
        prompts.FULL_USER_PROMPT.format(
            job_description=req.job_description,
            user_resume=json.dumps(_lean_resume(req.user_resume), ensure_ascii=False),
        ),
        FULL_SCHEMA,
        llm_key=key,
        llm_base=base,
        llm_model=model,
        label="full-analysis",
    )


@app.post("/optimize-star")
def optimize_star(req: StarOptimizeRequest, request: Request):
    key, base, model = resolve_llm(request)
    experiences_text = "\n".join(f"{i}. {e.text}" for i, e in enumerate(req.experiences, 1))
    result = chat_json(
        prompts.STAR_SYSTEM_PROMPT,
        prompts.STAR_USER_PROMPT.format(
            job_keywords=json.dumps(req.job_keywords, ensure_ascii=False),
            experiences_text=experiences_text,
        ),
        STAR_SCHEMA,
        llm_key=key,
        llm_base=base,
        llm_model=model,
        label="optimize-star",
    )
    # 双端校验：前端实际计数，LLM 自报超限则回退为原文并标注
    for item in result.get("optimized_items", []):
        actual = len(item.get("optimized", ""))
        if actual > 50:
            item["optimized"] = item.get("original", "")
            item["word_count"] = len(item.get("original", ""))
            item["_fallback"] = "字数超限，已回退为原文"
        else:
            item["word_count"] = actual
    return result


@app.post("/optimize-self-eval")
def optimize_self_eval(req: SelfEvalRequest, request: Request):
    key, base, model = resolve_llm(request)
    result = chat_json(
        prompts.SELF_EVAL_SYSTEM_PROMPT,
        prompts.SELF_EVAL_USER_PROMPT.format(
            job_keywords=json.dumps(req.job_keywords, ensure_ascii=False),
            self_evaluation=req.self_evaluation,
            resume_highlights=json.dumps(req.resume_highlights, ensure_ascii=False),
        ),
        SELF_EVAL_SCHEMA,
        llm_key=key,
        llm_base=base,
        llm_model=model,
        label="optimize-self-eval",
    )
    result["word_count"] = len(result.get("optimized", ""))
    return result


# ---------- 用量统计 ----------

@app.get("/usage")
def usage():
    """今日 token 用量汇总（需 API_TOKEN 或白名单来源；仅统计记录到 .usage/ 的调用）。"""
    today = date.today().isoformat()
    log_file = USAGE_DIR / f"{today}.jsonl"
    total = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    by_label: dict[str, dict] = {}
    if log_file.exists():
        for line in log_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                v = rec.get(k) or 0
                total[k] += v
            total["requests"] += 1
            lbl = by_label.setdefault(
                rec.get("label", "unknown"),
                {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                lbl[k] += rec.get(k) or 0
            lbl["requests"] += 1
    return {"date": today, "total": total, "by_label": by_label}


USAGE_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 用量面板</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; background:#0f172a; color:#e2e8f0; padding:24px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:#94a3b8; font-size:13px; margin-bottom:20px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-bottom:24px; }
  .card { background:#1e293b; border:1px solid #334155; border-radius:12px; padding:14px 16px; }
  .card .label { font-size:12px; color:#94a3b8; }
  .card .value { font-size:24px; font-weight:600; margin-top:4px; font-variant-numeric:tabular-nums; }
  table { width:100%; border-collapse:collapse; background:#1e293b; border:1px solid #334155; border-radius:12px; overflow:hidden; }
  th, td { text-align:left; padding:10px 14px; font-size:13px; }
  th { background:#0b1626; color:#94a3b8; font-weight:500; }
  td { border-top:1px solid #1f2b3d; }
  tr td:first-child { font-weight:600; }
  .btn { margin-top:16px; background:#3b82f6; color:#fff; border:none; border-radius:8px; padding:8px 16px; font-size:14px; cursor:pointer; }
  .btn:hover { background:#2563eb; }
  .err { color:#f87171; margin-top:12px; font-size:13px; }
</style>
</head>
<body>
  <h1>AI 用量面板</h1>
  <div class="sub" id="sub">加载中…</div>
  <div class="cards" id="cards"></div>
  <h2 style="font-size:15px; margin:0 0 10px;">按接口</h2>
  <table>
    <thead><tr><th>接口</th><th>次数</th><th>输入 tokens</th><th>输出 tokens</th><th>合计 tokens</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <button class="btn" onclick="load()">刷新</button>
  <div class="err" id="err"></div>
<script>
function fmt(n){ return (n||0).toLocaleString(); }
function load(){
  fetch('/usage').then(function(r){ return r.json(); }).then(function(d){
    var t = d.total;
    document.getElementById('sub').textContent = d.date + ' · 记录 ' + t.requests + ' 次 LLM 调用';
    var cards = [
      ['调用次数', fmt(t.requests)],
      ['输入 tokens', fmt(t.prompt_tokens)],
      ['输出 tokens', fmt(t.completion_tokens)],
      ['合计 tokens', fmt(t.total_tokens)],
    ];
    document.getElementById('cards').innerHTML = cards.map(function(c){
      return '<div class="card"><div class="label">'+c[0]+'</div><div class="value">'+c[1]+'</div></div>';
    }).join('');
    var labels = ['接口','次数','输入','输出','合计'];
    var rows = Object.keys(d.by_label).map(function(k){
      var v = d.by_label[k];
      return '<tr><td>'+k+'</td><td>'+fmt(v.requests)+'</td><td>'+fmt(v.prompt_tokens)+'</td><td>'+fmt(v.completion_tokens)+'</td><td>'+fmt(v.total_tokens)+'</td></tr>';
    });
    document.getElementById('rows').innerHTML = rows.length ? rows.join('') : '<tr><td colspan="5">暂无记录</td></tr>';
    document.getElementById('err').textContent = '';
  }).catch(function(e){
    document.getElementById('err').textContent = '加载失败：' + e.message + '（请确认已启动后端）';
  });
}
load();
</script>
</body>
</html>"""


@app.get("/usage/page", response_class=HTMLResponse)
def usage_page():
    """用量统计的可视化面板（只读汇总，不含任何密钥）。"""
    return USAGE_PAGE_HTML


# ---------- 多模板排版输出 ----------

@app.get("/", response_class=HTMLResponse)
def index():
    """网页版界面（docs/ 目录同时用于 GitHub Pages 发布）。"""
    return (BASE_DIR / "docs" / "index.html").read_text(encoding="utf-8")


@app.get("/templates")
def templates():
    """列出可用简历模板。"""
    return {"templates": template_engine.list_templates()}


@app.post("/render-preview", response_class=HTMLResponse)
def render_preview(req: RenderRequest):
    """渲染 HTML 预览（模板适配预览的右侧实时效果）。"""
    _validate_resume(req.resume)
    try:
        tpl = template_engine.load_template(req.template_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return template_engine.render_html(req.resume, tpl, req.hide_sensitive)


@app.post("/export-pdf")
def export_pdf(req: RenderRequest):
    """导出 PDF（需已安装 playwright + chromium）。"""
    _validate_resume(req.resume)
    try:
        tpl = template_engine.load_template(req.template_id)
        pdf = template_engine.render_pdf(req.resume, tpl, req.hide_sensitive)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"PDF 生成失败，请确认已执行 playwright install chromium: {e}")
    filename = f"resume_{req.template_id}.pdf"
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/export-docx")
def export_docx(req: RenderRequest):
    """导出 Word 文档。"""
    _validate_resume(req.resume)
    try:
        tpl = template_engine.load_template(req.template_id)
        data = template_engine.render_docx(req.resume, tpl, req.hide_sensitive)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Word 生成失败: {e}")
    filename = f"resume_{req.template_id}.docx"
    return Response(content=data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)