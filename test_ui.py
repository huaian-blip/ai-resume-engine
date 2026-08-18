# -*- coding: utf-8 -*-
"""验证网页 UI 功能。"""
from playwright.sync_api import sync_playwright

errors = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.goto("http://127.0.0.1:8000/")
    pg.wait_for_timeout(600)

    # 模板下拉已加载
    tpls = pg.eval_on_selector("#tplSelect", "el => el.options.length")
    print("模板数:", tpls)
    assert tpls >= 2

    # 填写基本信息
    pg.fill("#f_name", "王五")
    pg.fill("#f_phone", "13900000000")
    pg.fill("#f_email", "wangwu@example.com")
    pg.fill("#f_intention", "高级后端工程师")
    # 工作经历（默认已有一条空条目）
    pg.fill("#workList .entry [data-f='company']", "某科技公司")
    pg.fill("#workList .entry [data-f='position']", "Java 工程师")
    pg.fill("#workList .entry [data-f='start']", "2020.07")
    pg.fill("#workList .entry [data-f='end']", "至今")
    pg.fill("#workList .entry [data-f='desc']", "负责订单系统开发，处理大量数据")
    pg.fill("#f_self", "有 5 年 Java 经验，专注高并发系统。")
    pg.fill("#f_skills", "Java, Spring Cloud")

    # 点击实时预览
    pg.click("#btnPreview")
    pg.wait_for_timeout(1200)
    src = pg.get_attribute("#preview", "src")
    print("iframe src:", src[:40])
    assert src and src.startswith("blob:"), "preview iframe not blob"

    # 进入 iframe 检查内容
    frame = pg.frame_locator("#preview")
    name = frame.locator("h1").text_content()
    print("iframe h1:", name)
    assert name == "王五"
    phone_hidden = "13900000000" not in frame.locator("body").inner_text()
    print("电话已隐藏:", phone_hidden)
    assert phone_hidden

    # 敏感标记取消勾选后预览，电话应出现
    pg.uncheck("#c_phone")
    pg.click("#btnPreview")
    pg.wait_for_timeout(1000)
    frame2 = pg.frame_locator("#preview")
    body2 = frame2.locator("body").inner_text()
    print("取消敏感后电话可见:", "13900000000" in body2)
    assert "13900000000" in body2

    print("console errors:", errors if errors else "none")
    assert not errors, errors
    b.close()
print("WEB UI CHECKS PASSED")