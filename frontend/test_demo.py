# -*- coding: utf-8 -*-
"""验证 frontend/performance-demo.html 的 6 项优化行为。"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = r"C:\Users\yuan\Documents\Tencent Files\ai-resume-engine\frontend"
path = "file:///" + BASE.replace("\\", "/") + "/performance-demo.html"

errors = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.goto(path)
    pg.wait_for_timeout(300)

    # 1) 虚拟列表
    total = pg.text_content("#st-total")
    dom = pg.text_content("#st-dom")
    print("total=%s visible rows=%s" % (total, dom))
    assert total == "10000" and int(dom) <= 25, "virtual list window wrong"

    pg.eval_on_selector("#vlist", "el => el.scrollTop = 400000")
    pg.wait_for_timeout(150)
    last = pg.eval_on_selector("#vitems .vrow:last-child", "el => el.firstChild.textContent")
    first = pg.eval_on_selector("#vitems .vrow", "el => el.firstChild.textContent")
    print("first=%s | last=%s" % (first, last))
    assert "#10000" in last and "#99" in first, "virtual list scroll failed"

    # 3) 事件委托
    pg.eval_on_selector("#vlist", "el => el.scrollTop = 0")
    pg.wait_for_timeout(150)
    pg.click("#vitems .vrow .del")
    pg.wait_for_timeout(100)
    log = pg.text_content("#cacheLog")
    # 用 codepoint 判断（避免管道编码问题）
    assert any(0x4E8B <= ord(c) <= 0x9FFF for c in log), "no chinese log"
    assert "删除" in log.encode("utf-8").decode("utf-8") or "事件" in log.encode("utf-8").decode("utf-8"), "delegation log missing"

    # 2) 图片懒加载
    pg.eval_on_selector("#imgGrid", "el => el.scrollIntoView()")
    pg.wait_for_timeout(900)
    img = pg.text_content("#st-img")
    print("lazy images:", img)
    assert int(img.split("/")[0]) >= 8, "lazy load not triggered"

    # 5) 防抖/节流
    pg.fill("#duty", "")
    pg.type("#duty", "abc", delay=30)
    pg.wait_for_timeout(50)
    raw1 = int(pg.text_content("#st-raw"))
    save1 = int(pg.text_content("#st-save"))
    pg.type("#duty", "def", delay=30)
    pg.wait_for_timeout(50)
    raw2 = int(pg.text_content("#st-raw"))
    pg.wait_for_timeout(600)
    save2 = int(pg.text_content("#st-save"))
    print("input raw=%s->%s save=%s->%s" % (raw1, raw2, save1, save2))
    assert raw2 > raw1, "input events not counted"
    assert save2 > save1, "debounce save failed"

    # 4) 缓存
    pg.click("#btnCalc")
    pg.wait_for_timeout(200)
    pg.click("#btnCalc")
    pg.wait_for_timeout(200)
    log_text = pg.text_content("#cacheLog")
    print("cache log tail:", log_text[-120:])
    assert log_text.count("匹配度=") == 2, "expected two results"
    assert "计算" in log_text and "缓存" in log_text
    assert log_text.find("计算") < log_text.find("缓存"), "first run should compute, second hit cache"
    print("console errors:", errors if errors else "none")
    assert not errors, errors
    b.close()

print("ALL CHECKS PASSED")