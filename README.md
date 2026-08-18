# AI 简历引擎

AI 简历生成软件核心后端：岗位分析匹配、STAR 法则自动优化经历、多模板排版输出（HTML/PDF/Word），配套前端性能优化演示页与完整 PRD 文档。

## 功能

- **岗位 JD 解析**：`/parse-jd` 结构化输出硬/软要求与关键词
- **岗位匹配打分**：`/match-score` 四维加权评分 + 缺口补强建议
- **STAR 优化**：`/optimize-star` 单条 ≤50 字，S/T/A/R 要素 + 量化建议，双端字数校验
- **自我评价优化**：`/optimize-self-eval` ≤500 字
- **多模板输出**：`/templates` `/render-preview` `/export-pdf` `/export-docx`，敏感字段可隐藏
- **前端性能演示**：`frontend/performance-demo.html`（虚拟列表/懒加载/事件委托/缓存/防抖节流/CSS 优化）

## 目录结构

```
ai-resume-engine/
├── app.py               # FastAPI 入口
├── prompts.py           # AI 提示词（防注入定界符）
├── template_engine.py   # 模板渲染引擎（HTML/PDF/docx）
├── schemas/             # JSON Schema（JD/匹配/STAR/自我评价/简历）
├── templates/           # 简历模板 DSL
├── frontend/            # 前端性能优化演示页 + 自动化验证脚本
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

## 快速开始

### 方式一：本地运行（需 Python 3.10+）

```bash
pip install -r requirements.txt
copy .env.example .env      # 填入你的 LLM API Key
uvicorn app:app --reload
# PDF 导出需浏览器：playwright install chromium
```

### 方式二：Docker

```bash
docker compose up -d --build
# 访问 http://localhost:8000/docs
```

## 配置（.env）

```env
# 支持任何 OpenAI 兼容服务
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://ark.cn-beijing.volces.com/api/v3   # 示例：豆包/火山方舟
OPENAI_MODEL=doubao-seed-2-1-pro-260628                    # 账号已开通的模型 ID
```

> 豆包/火山方舟注意：模型 ID 必须使用账号**已开通**的模型（控制台 → 开通管理），旧模型 ID 会返回 404。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/templates` | 模板列表 |
| POST | `/parse-jd` | 岗位 JD 解析 |
| POST | `/match-score` | 岗位匹配打分 |
| POST | `/optimize-star` | STAR 优化经历 |
| POST | `/optimize-self-eval` | 自我评价优化 |
| POST | `/render-preview` | HTML 实时预览 |
| POST | `/export-pdf` | PDF 导出 |
| POST | `/export-docx` | Word 导出 |

完整交互式文档：启动后访问 `/docs`。

## 安全

- `.env` 已加入 `.gitignore`，**禁止提交任何密钥**
- LLM 输入防提示注入（`<<< >>>` 定界符 + 系统指令声明）
- 敏感字段隐藏：`resume.sensitive_fields` 标记字段导出时打码为 `***`
- **多租户 API Key**：AI 接口通过 `X-LLM-Key` / `X-LLM-Base` / `X-LLM-Model` 请求头使用调用方自己的密钥；公网部署时若不带 `X-LLM-Key` 直接返回 400（不代用开发者密钥），仅 localhost 本地开发回退 `.env` 密钥
- **站点令牌**：设置 `API_TOKEN` 后所有接口需 `Authorization: Bearer <API_TOKEN>`，防止公网额度被滥用（与前端 `docs/config.js` 保持一致）

## 文档

产品设计见 `AI简历软件-用户信息输入模块-PRD.md`（核心功能总规划、技术栈、开发步骤、AI 提示词、前端性能优化方案）。