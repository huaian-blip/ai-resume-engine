"""AI 简历引擎 — FastAPI 调用示例。

运行：
    pip install -r requirements.txt
    copy .env.example .env  # 填写密钥
    uvicorn app:app --reload

接口文档：http://127.0.0.1:8000/docs
"""

import json
import os
from pathlib import Path

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

app = FastAPI(title="AI 简历引擎", version="1.0.0")

# 公网部署跨域支持（demo：允许全部来源；生产建议收紧）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 可选 API 访问令牌：设置 API_TOKEN 后，除首页/文档外所有接口需携带
# Authorization: Bearer <API_TOKEN>。用于防止公网部署时额度被滥用。
API_TOKEN = os.getenv("API_TOKEN", "").strip()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # CORS 预检请求必须放行（浏览器预检不携带自定义请求头）
    if request.method == "OPTIONS":
        return await call_next(request)
    if API_TOKEN:
        path = request.url.path
        if not (path in ("/", "/docs", "/redoc", "/openapi.json") or path.startswith("/docs/")):
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
    try:
        import jsonschema
    except ImportError:
        return
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


def chat_json(
    system: str,
    user: str,
    schema: dict,
    retries: int = 1,
    llm_key: str | None = None,
    llm_base: str | None = None,
    llm_model: str | None = None,
) -> dict:
    """调用 LLM 并强制 JSON 输出。

    优先使用 json_schema 结构化输出；若服务商不支持（如 DeepSeek 仅支持
    json_object），自动降级为 json_object，并对输出做 Schema 软校验 + 字段归一化。

    每次调用使用调用方提供的 llm_key/base/model（多租户：每个用户用自己的 Key）。
    """
    llm_key = llm_key or os.getenv("OPENAI_API_KEY")
    llm_base = llm_base or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
    llm_model = llm_model or MODEL
    _client = OpenAI(api_key=llm_key, base_url=llm_base)
    last_err = None

    def _call(mode: str) -> dict:
        kwargs = {
            "model": llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.get("title", "Output"),
                    "strict": True,
                    "schema": schema,
                },
            }
            msgs = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        else:
            # 降级模式：把 Schema 注入提示词，约束字段名与结构
            schema_text = json.dumps(schema, ensure_ascii=False)
            msgs = [
                {"role": "system", "content": system},
                {"role": "user", "content": user + "\n\n必须严格按以下 JSON Schema 输出（字段名、类型、嵌套结构完全一致）：\n" + schema_text},
            ]
            kwargs["response_format"] = {"type": "json_object"}
        kwargs["messages"] = msgs
        resp = _client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("LLM 输出非 JSON 对象")
        _validate(data, schema, strict=(mode == "json_schema"))
        return data

    for mode in ("json_schema", "json_object"):
        for attempt in range(retries + 1):
            try:
                return _call(mode)
            except BadRequestError as e:
                last_err = e
                if mode == "json_schema" and "response_format" in str(e).lower():
                    break  # 服务商不支持 json_schema，降级 json_object
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
    )


@app.post("/match-score")
def match_score(req: MatchScoreRequest, request: Request):
    key, base, model = resolve_llm(request)
    return chat_json(
        prompts.MATCH_SYSTEM_PROMPT,
        prompts.MATCH_USER_PROMPT.format(
            parsed_jd=json.dumps(req.parsed_jd, ensure_ascii=False),
            user_resume=json.dumps(req.user_resume, ensure_ascii=False),
        ),
        MATCH_SCHEMA,
        llm_key=key,
        llm_base=base,
        llm_model=model,
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
    )
    result["word_count"] = len(result.get("optimized", ""))
    return result


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