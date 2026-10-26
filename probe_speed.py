# -*- coding: utf-8 -*-
"""
倍速问题诊断探针 v2(一次性调试工具)

验证开源刷课脚本(ScriptCat 5713)的倍速方案及其变体:
  方案G: 改造第一个 li 的 data-speed -> 向 xt-speedbutton 派发 mousemove -> li.click()
  方案H: 先派发 mousemove 激活菜单, 再用 Playwright 真实鼠标 hover+click
  方案I: 先派发 mousemove 激活菜单, 再用 page.mouse 坐标点击

运行: python probe_speed.py   (运行期间不要动鼠标, 约 1 分钟)
"""

import json
import time

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from yuketang_brush import (
    log, wait_login, goto_course, scroll_page, scan_rows, click_row,
    PROFILE_DIR, CONTENT_TYPES,
)

OUT_DIR = PROFILE_DIR.parent / "probe_results"
SPEED = 2.0
PROBE_LOG = []


def record(name, data):
    PROBE_LOG.append({"probe": name, **data})
    log(f"[{name}] {json.dumps(data, ensure_ascii=False)}")


def get_frame_with_video(page):
    for frame in page.frames:
        try:
            if frame.evaluate("() => !!document.querySelector('video')"):
                return frame
        except Exception:
            continue
    return None


def get_state(page):
    frame = get_frame_with_video(page)
    if not frame:
        return {"found": False}
    return frame.evaluate("""() => {
        const v = document.querySelector('video');
        const s = document.querySelector('xt-speedvalue');
        return { found: true, rateProp: v.playbackRate, paused: v.paused,
                 current: v.currentTime, speedText: s ? s.textContent.trim() : null };
    }""")


def measure_effective_rate(page, seconds=4):
    s1 = get_state(page)
    t1 = time.time()
    time.sleep(seconds)
    s2 = get_state(page)
    t2 = time.time()
    if not s1.get("found") or not s2.get("found"):
        return None
    return (s2["current"] - s1["current"]) / (t2 - t1)


def shot(page, name):
    try:
        page.screenshot(path=str(OUT_DIR / f"{name}.png"))
    except Exception:
        pass


# ScriptCat 脚本的原版配方
JS_RECIPE = r"""
async (speed) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const wrap = document.getElementsByTagName('xt-speedbutton')[0];
  const list = document.getElementsByTagName('xt-speedlist')[0];
  if (!wrap || !list) return { ok: false, reason: 'no wrap/list' };
  let li = list.querySelector('li[data-speed="' + speed + '"]')
        || list.querySelector('ul li');
  if (!li) return { ok: false, reason: 'no li' };
  li.setAttribute('data-speed', String(speed));
  li.innerText = Number(speed).toFixed(2) + 'X';
  wrap.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: 10, clientY: 10 }));
  li.click();
  await sleep(1000);
  const v = document.querySelector('video');
  const sv = document.querySelector('xt-speedvalue');
  return { ok: true, rate: v ? v.playbackRate : null,
           speedText: sv ? sv.textContent.trim() : null };
}
"""

# 只派发 mousemove 激活菜单(供方案 H/I 前置使用)
JS_ACTIVATE = r"""
() => {
  const wrap = document.getElementsByTagName('xt-speedbutton')[0];
  if (!wrap) return false;
  wrap.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: 10, clientY: 10 }));
  return true;
}
"""


def check_success(page, tag):
    st = get_state(page)
    st["effRate"] = measure_effective_rate(page)
    record(tag, st)
    return st.get("effRate") and st["effRate"] > 1.5


def main():
    OUT_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="msedge",
            headless=False,
            no_viewport=True,
            args=["--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            wait_login(page)
            course_page = goto_course(page, ctx, "科研伦理与学术规范", None)
            scroll_page(course_page)
            icon, badge = CONTENT_TYPES["视频"]
            _, rows = scan_rows(course_page, icon, badge)
            todo = [r for r in rows if not r["done"]]
            if not todo:
                log("没有未完成的视频条目, 无法探测")
                return
            r = todo[0]
            log(f"打开视频 #{r['idx']} 用于探测")
            click_row(course_page, r["idx"])
            try:
                with ctx.expect_page(timeout=8000) as pinfo:
                    pass
                vp = pinfo.value
            except PWTimeout:
                vp = course_page
            vp.wait_for_load_state("domcontentloaded")
            vp.wait_for_timeout(5000)

            frame = get_frame_with_video(vp)
            if not frame:
                log("未找到 video 元素")
                return
            frame.evaluate("() => { const v = document.querySelector('video'); v.muted = true; v.play().catch(()=>{}); }")
            vp.wait_for_timeout(2000)
            record("初始状态", get_state(vp))

            # ---------- 方案G: ScriptCat 原版配方 ----------
            try:
                res = frame.evaluate(JS_RECIPE, SPEED)
                record("G_recipe调用", res)
                shot(vp, "G_after")
                if check_success(vp, "G_效果"):
                    log(">>> 方案G 成功! 倍速已生效")
                    (OUT_DIR / "probe_log.txt").write_text(
                        "\n".join(json.dumps(e, ensure_ascii=False) for e in PROBE_LOG),
                        encoding="utf-8")
                    input("按回车关闭浏览器 <<<")
                    return
            except Exception as e:
                record("G_error", {"error": str(e)})

            # ---------- 方案H: mousemove 激活 + Playwright 真实 hover+click ----------
            try:
                frame.evaluate(JS_ACTIVATE)
                vp.wait_for_timeout(300)
                btn = frame.locator("xt-speedbutton")
                li = frame.locator(f'xt-speedlist li[data-speed="{SPEED:g}"]')
                btn.hover(timeout=3000)
                vp.wait_for_timeout(600)
                shot(vp, "H_menu")
                li.first.click(timeout=3000)
                vp.wait_for_timeout(1500)
                shot(vp, "H_after")
                if check_success(vp, "H_效果"):
                    log(">>> 方案H 成功! 倍速已生效")
                    (OUT_DIR / "probe_log.txt").write_text(
                        "\n".join(json.dumps(e, ensure_ascii=False) for e in PROBE_LOG),
                        encoding="utf-8")
                    input("按回车关闭浏览器 <<<")
                    return
            except Exception as e:
                record("H_error", {"error": str(e)})

            # ---------- 方案I: mousemove 激活 + page.mouse 坐标点击 ----------
            try:
                frame.evaluate(JS_ACTIVATE)
                vp.wait_for_timeout(300)
                btn.hover(timeout=3000)
                vp.wait_for_timeout(600)
                box = li.first.bounding_box()
                record("I_li位置", {"box": box})
                if box:
                    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                    vp.mouse.move(cx, cy, steps=5)
                    vp.wait_for_timeout(400)
                    vp.mouse.down()
                    vp.wait_for_timeout(150)
                    vp.mouse.up()
                    vp.wait_for_timeout(1500)
                    shot(vp, "I_after")
                    if check_success(vp, "I_效果"):
                        log(">>> 方案I 成功! 倍速已生效")
                        (OUT_DIR / "probe_log.txt").write_text(
                            "\n".join(json.dumps(e, ensure_ascii=False) for e in PROBE_LOG),
                            encoding="utf-8")
                        input("按回车关闭浏览器 <<<")
                        return
            except Exception as e:
                record("I_error", {"error": str(e)})

            log(">>> 三个方案均未生效")
            (OUT_DIR / "probe_log.txt").write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in PROBE_LOG),
                encoding="utf-8")
            input("按回车关闭浏览器 <<<")
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
