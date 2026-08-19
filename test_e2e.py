# -*- coding: utf-8 -*-
"""端到端验证：DeepSeek + 全部 AI/模板接口"""
import os
import sys
from pathlib import Path

# 密钥一律从环境变量 / .env 读取，禁止硬编码进源码
sys.path.insert(0, r"C:\Users\yuan\Documents\Tencent Files\ai-resume-engine")
from dotenv import load_dotenv

# 本地测试无需站点令牌：必须在 load_dotenv 之前置空，避免被 .env 覆盖开启鉴权
os.environ["API_TOKEN"] = ""
os.environ.setdefault("OPENAI_BASE_URL", "https://api.deepseek.com")
os.environ.setdefault("OPENAI_MODEL", "deepseek-v4-flash")
load_dotenv(Path(sys.path[0]) / ".env")
assert os.environ.get("OPENAI_API_KEY"), "缺少 OPENAI_API_KEY（请在 .env 或环境变量中配置）"

import app
from fastapi.testclient import TestClient

client = TestClient(app.app, base_url="http://localhost")

JD = "高级后端工程师，负责高并发交易系统架构设计与开发，要求 5 年以上 Java 经验，熟悉 Spring Cloud、MySQL、Redis、Kafka，具备分布式系统与微服务治理经验，本科及以上学历，有电商交易系统背景者优先。"

r = client.post("/parse-jd", json={"job_description": JD})
print("1) /parse-jd ->", r.status_code)
assert r.status_code == 200
parsed = r.json()
print("   job_title:", parsed.get("job_title"))
print("   required_skills:", parsed.get("hard_requirements", {}).get("required_skills"))
assert parsed.get("job_title")
assert parsed.get("hard_requirements", {}).get("required_skills")

RESUME = {
    "basic": {"name": "李四", "job_intention": "高级后端工程师"},
    "work": [
        {
            "company": "某电商公司",
            "position": "Java 开发工程师",
            "start": "2019.07",
            "end": "至今",
            "responsibilities": ["负责订单系统核心模块开发与维护"],
        }
    ],
    "education": [
        {"school": "某大学", "degree": "本科", "major": "计算机科学与技术", "graduation": "2019.06"}
    ],
    "skills": {"skills": ["Java", "Spring Cloud", "MySQL", "Redis"], "certificates": [], "languages": []},
}

r = client.post("/match-score", json={"parsed_jd": parsed, "user_resume": RESUME})
ms = r.json()
print("2) /match-score ->", r.status_code, "| total:", ms.get("total_score"))
print("   gaps[0]:", ms.get("gaps", [{}])[0])
assert r.status_code == 200
assert ms.get("total_score") is not None
assert isinstance(ms.get("gaps"), list) and ms["gaps"] and isinstance(ms["gaps"][0], dict)

r = client.post(
    "/optimize-star",
    json={
        "job_keywords": ["Java", "高并发", "微服务"],
        "experiences": [
            {"text": "负责订单模块开发，处理了大量订单数据"},
            {"text": "参与支付系统重构，提高了系统稳定性"},
        ],
    },
)
star = r.json()
print("3) /optimize-star ->", r.status_code)
for it in star.get("optimized_items", []):
    print("   ", it.get("optimized"), "(len=%d)" % it.get("word_count", -1))
    assert it.get("optimized")
    assert it.get("word_count", 0) <= 50
assert r.status_code == 200

r = client.post(
    "/optimize-self-eval",
    json={
        "job_keywords": ["Java", "高并发"],
        "self_evaluation": "我有多年 Java 经验，喜欢学习新技术。",
        "resume_highlights": ["订单系统开发", "支付重构"],
    },
)
se = r.json()
print("4) /optimize-self-eval ->", r.status_code, "|", se.get("optimized"), "| wc:", se.get("word_count"))
assert r.status_code == 200
assert se.get("optimized") and se.get("word_count", 0) <= 500

FULL = {
    "basic": {"name": "李四", "phone": "13800000000", "email": "lisi@example.com", "city": "上海", "job_intention": "高级后端工程师"},
    "education": [{"school": "某大学", "degree": "本科", "major": "计算机科学与技术", "graduation": "2019.06"}],
    "work": [
        {
            "company": "某电商公司",
            "position": "Java 开发工程师",
            "start": "2019.07",
            "end": "至今",
            "responsibilities": ["负责订单系统核心模块开发与维护", "主导支付系统重构"],
        }
    ],
    "projects": [
        {
            "name": "交易链路重构",
            "role": "核心开发",
            "start": "2023.01",
            "end": "2023.12",
            "description": ["重构支付核心链路"],
            "achievements": ["稳定性提升 99.99%"],
        }
    ],
    "skills": {"skills": ["Java", "Spring Cloud", "MySQL", "Redis", "Kafka"], "certificates": [], "languages": ["英语 CET-6"]},
    "self_evaluation": "5 年 Java 后端经验，专注高并发系统。",
    "sensitive_fields": ["phone", "email"],
}

r = client.post("/render-preview", json={"resume": FULL, "template_id": "modern", "hide_sensitive": True})
print("5) /render-preview ->", r.status_code, "| masked:", "13800000000" not in r.text)
assert r.status_code == 200 and "13800000000" not in r.text

r = client.post("/export-pdf", json={"resume": FULL, "template_id": "modern", "hide_sensitive": True})
print("6) /export-pdf ->", r.status_code, "| valid:", r.content[:4] == b"%PDF", "| size:", len(r.content))
assert r.status_code == 200 and r.content[:4] == b"%PDF"

r = client.post("/export-docx", json={"resume": FULL, "template_id": "classic", "hide_sensitive": False})
print("7) /export-docx ->", r.status_code, "| valid:", r.content[:2] == b"PK", "| size:", len(r.content))
assert r.status_code == 200 and r.content[:2] == b"PK"

print("=== END-TO-END ALL PASSED ===")