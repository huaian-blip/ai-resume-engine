# -*- coding: utf-8 -*-
"""验证 GitHub Pages 站点全链路（UI -> 隧道后端 -> AI）。"""
from playwright.sync_api import sync_playwright

URL = "https://huaian-blip.github.io/ai-resume-engine/"
errors = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("requestfailed", lambda req: errors.append("REQFAIL " + req.url))
    pg.goto(URL, wait_until="networkidle", timeout=60000)

    tpls = pg.eval_on_selector("#tplSelect", "el => el.options.length")
    print("模板数(经隧道加载):", tpls)
    assert tpls >= 2, "templates not loaded cross-origin"

    # 模拟外部用户：先在 API 设置填写自己的 Key
    pg.fill("#llmKey", "sk-a8a1ee4990b848a0a529fede1d5aca1b")
    pg.click("text=保存")
    pg.wait_for_selector("#apiStatus", timeout=5000)
    print("apiStatus:", pg.locator("#apiStatus").text_content())
    assert "已配置" in pg.locator("#apiStatus").text_content()

    pg.fill("#f_name", "公网测试")
    pg.fill("#f_intention", "后端工程师")
    pg.fill("#workList .entry [data-f='company']", "测试公司")
    pg.fill("#workList .entry [data-f='position']", "工程师")
    pg.fill("#workList .entry [data-f='desc']", "负责系统开发")
    pg.click("#btnPreview")
    pg.wait_for_timeout(2500)
    frame = pg.frame_locator("#preview")
    print("iframe h1:", frame.locator("h1").text_content())

    # AI 功能真实调用（DeepSeek）
    pg.fill("#jd", "招聘高级后端工程师，要求熟悉 Java、Spring Cloud，有高并发经验，本科以上学历")
    pg.click("text=解析 JD + 匹配打分")
    pg.wait_for_selector("#matchScore .score", timeout=60000)
    print("匹配分:", pg.locator("#matchScore .score").text_content())

    print("console errors:", errors if errors else "none")
    assert not errors, errors
    b.close()
print("PAGES E2E PASSED")