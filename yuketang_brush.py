# -*- coding: utf-8 -*-
"""
雨课堂(主站 www.yuketang.cn)自动刷课脚本

功能:
    1. 打开 Edge, 扫码登录(首次), 会话保存在 ./yuketang_profile, 之后免登录
    2. 登录后弹出设置窗口:
       - 下拉框选择课程(自动读取个人空间全部课程, 也可手动输入)
       - 下拉框选择刷取内容类型: 视频 / 讨论 / 作业
       - 可设置定时关闭(精确到分), 到点自动停止
       - 作业需提供 AI 接口(OpenAI 兼容), 可勾选提交前人工确认
    3. 自动进入课程"学习内容"页, 遍历目标类型条目
    4. 跳过已完成的, 未完成的逐个处理:
       视频静音二倍速看完; 讨论复制同学发言发布; 作业 AI 逐题作答提交
    5. 停止方式: 定时时间到达 / Ctrl+C 手动停止 / 全部刷完自动停止

用法:
    python yuketang_brush.py                 # 正常运行(弹窗设置)
    python yuketang_brush.py --speed 2.0     # 指定视频倍速(平台UI上限通常为2.0)
    python yuketang_brush.py --debug         # 只扫描打印条目, 不实际刷
"""

import argparse
import base64
import json
import random
import re
import sys
import time
import tkinter as tk
import urllib.request
from datetime import datetime
from pathlib import Path
from tkinter import ttk

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

HOME_URL = "https://www.yuketang.cn"
INDEX_URL = f"{HOME_URL}/v2/web/index"
# 打包成 exe 时 __file__ 指向临时解压目录, 配置需放在 exe 同目录
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "yuketang_profile"

# 内容类型 -> (图标class, 徽章文字)
CONTENT_TYPES = {
    "视频": ("icon--shipin", "视频"),
    "讨论": ("icon--taolun", "讨论"),
    "作业": ("icon--zuoye", "作业"),
}

MAX_ATTEMPTS = 3          # 单条目最多尝试次数(防止无法完成的条目死循环)
AI_CONFIG_FILE = BASE_DIR / "yuketang_ai_config.json"   # AI API 配置(含密钥, 请勿分享)


class DeadlineReached(Exception):
    pass


# ---------------- 页面内执行的 JS ----------------

# 在"我的课程"页提取全部课程卡片, 打上 data-ykt-course 标记。
# 真实结构(来自页面转储): 课程卡片是 div.lesson-cardS, 课程名在卡片的 h1 里。
JS_EXTRACT_COURSES = r"""
() => {
  document.querySelectorAll('[data-ykt-course]').forEach(el => el.removeAttribute('data-ykt-course'));

  // 精确匹配: div.lesson-cardS 卡片, h1 为课程名
  const cards = [...document.querySelectorAll('div.lesson-cardS')];
  if (cards.length) {
    const out = [];
    cards.forEach((el) => {
      const h1 = el.querySelector('h1');
      const name = ((h1 ? h1.innerText : el.innerText) || '').trim();
      if (!name) return;
      el.setAttribute('data-ykt-course', out.length);
      out.push({ idx: out.length, name });
    });
    if (out.length) return out;
  }

  // 回退: 启发式搜索
  const sels = [
    '[class*=course-item]', '[class*=courseItem]',
    '[class*=course-card]', '[class*=courseCard]',
    '[class*=course-list] > div', '[class*=course_list] li',
    '[class*=course] a'
  ];
  for (const s of sels) {
    const cards2 = [];
    const seen = new Set();
    for (const el of document.querySelectorAll(s)) {
      const t = (el.innerText || '').trim();
      if (!t || t.length < 2 || t.length > 120) continue;
      const name = t.split('\n')[0].trim();
      if (!name || seen.has(name)) continue;
      seen.add(name);
      el.setAttribute('data-ykt-course', cards2.length);
      cards2.push({ idx: cards2.length, name });
    }
    if (cards2.length) return cards2;
  }
  return [];
}
"""

JS_CLICK_COURSE = r"""
(idx) => {
  const card = document.querySelector(`[data-ykt-course="${idx}"]`);
  if (!card) return false;
  (card.querySelector('a') || card).click();
  return true;
}
"""

# 扫描学习内容页中指定类型的条目。
# 真实结构: 每个学习单元是 div.leaf-detail, 类型标志是图标字体 <i class="iconfont icon--xxx">
# (伪元素渲染, 文本取不到), 标题在 .leaf-title .title, 完成度在 .progress-wrap(形如 "0%")。
JS_SCAN_ROWS = r"""
({ iconClass, badge }) => {
  document.querySelectorAll('[data-ykt]').forEach(el => el.removeAttribute('data-ykt'));

  // 精确匹配: 含指定类型图标的 leaf-detail 行
  let items = [...document.querySelectorAll('div.leaf-detail')]
    .filter(el => el.querySelector('i.' + iconClass));

  // 回退: 伪元素 content 含徽章文字的小元素向上找行
  if (!items.length) {
    const isBadge = (el) => {
      if (el.children.length === 0 && el.textContent.trim() === badge) return true;
      for (const pe of ['::before', '::after']) {
        const c = getComputedStyle(el, pe).content;
        if (c && c !== 'none' && c !== '""' && c.includes(badge)) return true;
      }
      return false;
    };
    const badges = [...document.querySelectorAll('*')].filter(el => {
      if (!isBadge(el)) return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.width < 150 && r.height > 0 && r.height < 80;
    });
    const seen = new Set();
    items = [];
    for (const b of badges) {
      let row = b;
      for (let i = 0; i < 10 && row.parentElement; i++) {
        row = row.parentElement;
        const t = (row.innerText || '').trim();
        if (t.length > 4 && t.length < 300) break;
      }
      if (!seen.has(row)) { seen.add(row); items.push(row); }
    }
  }

  return items.map((el, idx) => {
    const titleEl = el.querySelector('.leaf-title .title');
    const title = titleEl ? titleEl.innerText.trim()
                          : (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const progEl = el.querySelector('.progress-wrap');
    const prog = progEl ? progEl.innerText.replace(/\s+/g, '') : '';
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const done = /^100%/.test(prog) || /已完成|已读|已发言/.test(text);
    el.setAttribute('data-ykt', idx);
    return { idx, title: title.slice(0, 80), prog, done, text: text.slice(0, 120) };
  });
}
"""

JS_CLICK_ROW = r"""
(idx) => {
  const row = document.querySelector(`[data-ykt="${idx}"]`);
  if (!row) return false;
  const target = row.querySelector('a, [class*=title], [class*=name]') || row;
  target.click();
  return true;
}
"""

# 视频页: 设置倍速并播放, 返回视频状态。
# 注意: 雨课堂 xt-player 会劫持/重置 playbackRate, 直接赋值会被立刻改回。
# 这里每次轮询先读取 rateBefore(即上次设置后被播放器修正过的真实速率),
# 再用 HTMLMediaElement.prototype 原生 setter 尝试校正。
JS_VIDEO_CTRL = r"""
(speed) => {
  const v = document.querySelector('video');
  if (!v) return { found: false };
  const rateBefore = v.playbackRate;
  if (!v.muted) v.muted = true;   // 每轮强制静音
  if (Math.abs(v.playbackRate - speed) > 0.01) {
    try {
      const d = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'playbackRate');
      d.set.call(v, speed);
    } catch (e) {
      try { v.playbackRate = speed; } catch (e2) {}
    }
    try { v.defaultPlaybackRate = speed; } catch (e) {}
  }
  if (v.paused) v.play().catch(() => {});
  return {
    found: true,
    paused: v.paused,
    ended: v.ended,
    rate: rateBefore,
    current: v.currentTime,
    duration: v.duration || 0,
    progress: v.duration ? v.currentTime / v.duration : 0
  };
}
"""

# 通过播放器 UI 设置倍速(经 probe 实测验证有效的配方, 源自开源脚本 ScriptCat#5713):
#   1) 改造第一个 li 的 data-speed 和文本为目标倍速
#   2) 向 xt-speedbutton 派发 mousemove —— 关键步骤: 播放器的菜单点击处理器
#      是 mousemove 时才绑定的, 没有这一步任何点击都会被无视
#   3) li.click() 触发处理器, 播放器内部状态与 <video> 同步变为目标倍速
JS_SET_SPEED_UI = r"""
async (speed) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const wrap = document.getElementsByTagName('xt-speedbutton')[0];
  const list = document.getElementsByTagName('xt-speedlist')[0];
  if (!wrap || !list) return { uiFound: false };
  let li = list.querySelector('li[data-speed="' + speed + '"]')
        || list.querySelector('ul li');
  if (!li) return { uiFound: false };
  li.setAttribute('data-speed', String(speed));
  li.innerText = Number(speed).toFixed(2) + 'X';
  wrap.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: 10, clientY: 10 }));
  li.click();
  await sleep(1000);
  const v = document.querySelector('video');
  const sv = document.querySelector('xt-speedvalue');
  return { uiFound: true, clicked: (li.textContent || '').trim(),
           rate: v ? v.playbackRate : null,
           speedText: sv ? sv.textContent.trim() : null };
}
"""

# 通过播放器 UI 静音(与倍速同款配方):
# 先向 xt-volumebutton 派发 mousemove 激活处理器, 再点击 xt-icon 切换静音,
# 让播放器内部状态与 <video> 同步为静音。已是静音状态时不点击(避免反向切换)。
JS_MUTE = r"""
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const v = document.querySelector('video');
  if (!v) return { found: false };
  const wrap = document.getElementsByTagName('xt-volumebutton')[0];
  const icon = wrap ? wrap.querySelector('xt-icon') : null;
  let clicked = false;
  if (wrap && icon && !v.muted) {
    wrap.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: 10, clientY: 10 }));
    icon.click();
    clicked = true;
    await sleep(500);
  }
  if (!v.muted) v.muted = true;   // 兜底
  const vol = document.querySelector('xt-volume');
  return { found: true, clicked, muted: v.muted,
           volText: vol ? vol.textContent.trim() : null };
}
"""

# 滚动页面触发懒加载
JS_SCROLL = r"""
async () => {
  for (let y = 0; y <= document.body.scrollHeight; y += 600) {
    window.scrollTo(0, y);
    await new Promise(r => setTimeout(r, 200));
  }
  window.scrollTo(0, 0);
}
"""

# 讨论页真实结构(来自页面转储):
#   发言列表: .forum-item .comment-text (同学发言正文)
#   输入框:   .prompt-input textarea
#   发布按钮: .prompt-send-btn (纸飞机图标, 无文字)
JS_DISCUSSION_EXTRACT = r"""
() => {
  const replies = [...document.querySelectorAll('.forum-item .comment-text')]
    .map(e => (e.innerText || '').trim())
    .filter(t => t.length >= 20)
    .slice(0, 30);
  return {
    replies,
    hasTextarea: !!document.querySelector('.prompt-input textarea'),
    hasSend: !!document.querySelector('.prompt-send-btn')
  };
}
"""

# 作业页(iframe-exercise)真实结构(来自页面转储):
#   题目区: .container-problem, 题型在 .item-type(如 "1.单选题")
#   选项:   ul.list-unstyled-checkbox li > label.el-checkbox > input[value="A"/"B"...]
#   按钮区: .problem-fixedbar 内 button(上一题/提交/下一题, 禁用时带 disabled)
#   弹窗:   .el-dialog__wrapper(隐藏时 display:none), 主操作是 div.rain-btn 的 .text
#   注意: 题干/选项文字是 xuetangx 加密字体, 文本提取是乱码, 必须截图给视觉模型
JS_HOMEWORK_EXTRACT = r"""
() => {
  const container = document.querySelector('.container-problem');
  if (!container) return { found: false };
  const typeEl = container.querySelector('.item-type');
  const qtype = (typeEl ? typeEl.innerText : '').trim();
  const inputs = [...container.querySelectorAll('ul li input')];
  const options = inputs.map(inp => inp.value).filter(v => /^[A-Z]$/.test(v));
  const active = document.querySelector('.problems-aside .subject-item.active');
  const total = document.querySelectorAll('.problems-aside .subject-item').length;
  const order = active ? active.getAttribute('data-order') : null;
  const btns = [...document.querySelectorAll('.problem-fixedbar button')];
  const hasNext = btns.some(b => (b.innerText || '').includes('下一题') && !b.disabled);
  // 已提交判定: 侧栏当前题状态区出现图标, 或选项被禁用, 或页面出现解析
  const sc = active ? active.querySelector('.status-container') : null;
  const sidebarDone = !!(sc && (sc.querySelector('svg, i, img') || (sc.innerText || '').trim()));
  const allDisabled = inputs.length > 0 && inputs.every(i => i.disabled);
  const hasAnalysis = /查看解析|本题解析|正确答案/.test(document.body.innerText || '');
  return { found: options.length >= 2, qtype, options, order, total, hasNext,
           submitted: sidebarDone || allDisabled || hasAnalysis };
}
"""

# 侧栏题号跳转: 点击第一道未提交的题(已进入的套题续做时直接续上进度)
JS_SIDEBAR_JUMP = r"""
() => {
  const items = [...document.querySelectorAll('.problems-aside .subject-item')];
  for (const el of items) {
    const sc = el.querySelector('.status-container');
    const done = !!(sc && (sc.querySelector('svg, i, img') || (sc.innerText || '').trim()));
    if (!done) {
      const isActive = el.classList.contains('active');
      if (!isActive) el.click();
      return { jumped: !isActive, order: el.getAttribute('data-order'), allDone: false };
    }
  }
  return { jumped: false, allDone: items.length > 0 };
}
"""


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def check_deadline(deadline_ts):
    if deadline_ts and time.time() >= deadline_ts:
        raise DeadlineReached


# ---------------- 设置弹窗 ----------------

def select_options_gui(courses, ai_cfg):
    """弹出设置窗口, 返回 dict(course_name, course_idx, type_label, deadline_ts, ai), 取消返回 None。"""
    names = [c["name"] for c in courses]
    result = {}

    root = tk.Tk()
    root.title("雨课堂刷课设置")
    root.geometry("480x480")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    pad = {"padx": 16, "pady": 6}
    tk.Label(root, text="雨课堂自动刷课", font=("Microsoft YaHei", 14, "bold")).pack(pady=(14, 4))

    frm = tk.Frame(root)
    frm.pack(fill="x", **pad)
    tk.Label(frm, text="选择课程:", width=10, anchor="w").grid(row=0, column=0, sticky="w")
    course_var = tk.StringVar(value=names[0] if names else "")
    cb_course = ttk.Combobox(frm, textvariable=course_var, values=names, width=34)
    cb_course.grid(row=0, column=1, sticky="w")
    if not names:
        tk.Label(frm, text="(未能自动读取课程列表, 请手动输入课程名)",
                 fg="red").grid(row=1, column=1, sticky="w")

    tk.Label(frm, text="刷取内容:", width=10, anchor="w").grid(row=2, column=0, sticky="w")
    type_var = tk.StringVar(value="视频")
    ttk.Combobox(frm, textvariable=type_var, values=list(CONTENT_TYPES.keys()),
                 state="readonly", width=34).grid(row=2, column=1, sticky="w")

    tk.Label(frm, text="定时关闭:", width=10, anchor="w").grid(row=3, column=0, sticky="w")
    deadline_on = tk.BooleanVar(value=False)
    tk.Checkbutton(frm, text="启用", variable=deadline_on).grid(row=3, column=1, sticky="w")
    deadline_var = tk.StringVar()
    tk.Entry(frm, textvariable=deadline_var, width=36).grid(row=4, column=1, sticky="w")
    tk.Label(frm, text="格式: 年-月-日 时:分, 如 2026-10-20 08:30",
             fg="gray").grid(row=5, column=1, sticky="w")

    tk.Label(frm, text="AI设置:", width=10, anchor="w",
             font=("Microsoft YaHei", 9, "bold")).grid(row=6, column=0, sticky="w")
    tk.Label(frm, text="(仅刷作业时需要)",
             fg="gray").grid(row=6, column=1, sticky="w")
    tk.Label(frm, text="API地址:", width=10, anchor="w").grid(row=7, column=0, sticky="w")
    api_url_var = tk.StringVar(value=ai_cfg.get("base_url", ""))
    tk.Entry(frm, textvariable=api_url_var, width=36).grid(row=7, column=1, sticky="w")
    tk.Label(frm, text="API密钥:", width=10, anchor="w").grid(row=8, column=0, sticky="w")
    api_key_var = tk.StringVar(value=ai_cfg.get("api_key", ""))
    tk.Entry(frm, textvariable=api_key_var, width=36, show="*").grid(row=8, column=1, sticky="w")
    tk.Label(frm, text="模型名称:", width=10, anchor="w").grid(row=9, column=0, sticky="w")
    model_var = tk.StringVar(value=ai_cfg.get("model", ""))
    tk.Entry(frm, textvariable=model_var, width=36).grid(row=9, column=1, sticky="w")

    confirm_var = tk.BooleanVar(value=True)
    tk.Checkbutton(frm, text="作业提交前人工确认(AI选完后暂停, 由你确认再提交)",
                   variable=confirm_var).grid(row=10, column=1, sticky="w")

    err_var = tk.StringVar()
    tk.Label(root, textvariable=err_var, fg="red").pack()

    btns = tk.Frame(root)
    btns.pack(pady=10)

    def on_start():
        name = course_var.get().strip()
        if not name:
            err_var.set("请填写课程名称")
            return
        deadline_ts = None
        if deadline_on.get():
            s = deadline_var.get().strip()
            try:
                deadline_ts = datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()
            except ValueError:
                err_var.set("定时关闭时间格式不正确, 应为: 2026-10-20 08:30")
                return
            if deadline_ts <= time.time():
                err_var.set("定时关闭时间必须晚于当前时间")
                return
        ai = {"base_url": api_url_var.get().strip(),
              "api_key": api_key_var.get().strip(),
              "model": model_var.get().strip()}
        if type_var.get() == "作业":
            if not all(ai.values()):
                err_var.set("刷作业需要完整填写 AI 地址、密钥和模型")
                return
            save_ai_config(ai)
        idx = next((c["idx"] for c in courses if c["name"] == name), None)
        result.update(course_name=name, course_idx=idx,
                      type_label=type_var.get(), deadline_ts=deadline_ts,
                      ai=ai, confirm_submit=confirm_var.get())
        root.destroy()

    tk.Button(btns, text="开始刷课", width=12, command=on_start).grid(row=0, column=0, padx=10)
    tk.Button(btns, text="取消", width=12, command=root.destroy).grid(row=0, column=1, padx=10)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return result or None


# ---------------- 浏览器流程 ----------------

def wait_login(page):
    page.goto(HOME_URL, wait_until="domcontentloaded")
    print()
    print("=" * 50)
    print("已在 Edge 中打开雨课堂, 请用微信扫码登录。")
    print("登录成功进入个人主页后, 回到这里按回车继续。")
    print("=" * 50)
    input(">>> 登录完成后按回车 <<<")


def extract_courses(page):
    """进入我的课程页, 提取全部课程名。失败时转储 HTML 便于排查。"""
    page.goto(INDEX_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    scroll_page(page)
    try:
        courses = page.evaluate(JS_EXTRACT_COURSES)
    except Exception as e:
        log(f"读取课程列表出错: {e}")
        courses = []
    if courses:
        log(f"读取到 {len(courses)} 门课程")
    else:
        log("未能自动读取课程列表(可在弹窗中手动输入课程名)")
        try:
            dump = BASE_DIR / "yuketang_debug_index.html"
            dump.write_text(page.content(), encoding="utf-8")
            log(f"课程页 HTML 已保存到 {dump.name}, 可发给我适配选择器")
        except Exception:
            pass
    return courses


def goto_course(page, ctx, course_name, course_idx):
    """进入指定课程的学习内容页, 返回课程页面对象。"""
    log(f"进入课程: {course_name}")
    course_page = None
    try:
        if course_idx is not None:
            clicked = page.evaluate(JS_CLICK_COURSE, course_idx)
        else:
            clicked = False
        if not clicked:
            page.get_by_text(course_name, exact=False).first.click(timeout=8000)
        try:
            with ctx.expect_page(timeout=8000) as pinfo:
                pass
            course_page = pinfo.value
            course_page.wait_for_load_state("domcontentloaded")
            log("课程在新标签页打开, 已切换")
        except PWTimeout:
            course_page = page
        course_page.wait_for_timeout(3000)
    except Exception:
        print()
        print(f"未能自动进入课程《{course_name}》。")
        print("请在浏览器中手动打开该课程的【学习内容】页面,")
        input("打开后回到这里按回车 <<<")
        course_page = ctx.pages[-1]
        course_page.wait_for_timeout(1000)

    try:
        course_page.get_by_text("学习内容", exact=True).first.click(timeout=3000)
        course_page.wait_for_timeout(2000)
    except Exception:
        pass
    return course_page


def scroll_page(page):
    try:
        page.evaluate(JS_SCROLL)
    except Exception:
        pass


def scan_rows(page, icon_class, badge):
    for frame in page.frames:
        try:
            rows = frame.evaluate(JS_SCAN_ROWS, {"iconClass": icon_class, "badge": badge})
        except Exception:
            continue
        if rows:
            return frame, rows
    return None, []


def click_row(page, idx):
    for frame in page.frames:
        try:
            if frame.evaluate(JS_CLICK_ROW, idx):
                return True
        except Exception:
            continue
    return False


def debug_dump(page):
    print("----- 诊断信息 -----")
    print(f"当前URL: {page.url}")
    for i, frame in enumerate(page.frames):
        try:
            txt = (frame.evaluate("() => document.body ? document.body.innerText : ''") or "")
            txt = " ".join(txt.split())
            print(f"frame[{i}] url={frame.url[:80]}")
            print(f"  文本前300字: {txt[:300]}")
            if "studycontent" in frame.url or "lms" in frame.url:
                html = frame.evaluate("() => document.body ? document.body.innerHTML : ''")
                dump = BASE_DIR / f"yuketang_debug_frame{i}.html"
                dump.write_text(html, encoding="utf-8")
                print(f"  >>> 该 frame 的 HTML 已保存到: {dump.name}")
        except Exception as e:
            print(f"frame[{i}] 读取失败: {e}")
    print("--------------------")


def open_item(list_page, ctx, idx):
    """点击条目, 返回 (条目页面, 是否本页跳转); 失败返回 None。"""
    if not click_row(list_page, idx):
        log("  点击条目失败")
        return None
    try:
        with ctx.expect_page(timeout=8000) as pinfo:
            pass
        return pinfo.value, False
    except PWTimeout:
        return list_page, True


def watch_video(list_page, ctx, idx, speed, deadline_ts):
    """看完一个视频(二倍速), 返回是否成功。"""
    opened = open_item(list_page, ctx, idx)
    if not opened:
        return False
    video_page, same_tab = opened
    video_page.wait_for_load_state("domcontentloaded")
    video_page.wait_for_timeout(3000)

    def video_status():
        for frame in video_page.frames:
            try:
                st = frame.evaluate(JS_VIDEO_CTRL, speed)
                if st.get("found"):
                    return st
            except Exception:
                continue
        return {"found": False}

    deadline = time.time() + 30
    while time.time() < deadline:
        check_deadline(deadline_ts)
        if video_status().get("found"):
            break
        video_page.wait_for_timeout(1500)
    else:
        log("  未找到 <video> 元素, 关闭并跳过")
        if not same_tab:
            video_page.close()
        return False

    # 通过播放器 UI 设置倍速(probe 验证过的配方: 改 li -> mousemove 激活 -> click)
    speed_set = False
    for frame in video_page.frames:
        try:
            if not frame.evaluate("() => !!document.querySelector('video')"):
                continue
            r = frame.evaluate(JS_SET_SPEED_UI, speed)
        except Exception:
            continue
        if r.get("clicked"):
            extra = []
            if r.get("rate"):
                extra.append(f"实际速率 x{r['rate']:.2f}")
            if r.get("speedText"):
                extra.append(f"播放器显示 {r['speedText']}")
            log(f"  已设置倍速: {r['clicked']}"
                + (f" ({', '.join(extra)})" if extra else ""))
            speed_set = bool(r.get("rate")) and abs(r["rate"] - speed) < 0.01
            break
        if r.get("uiFound") is False:
            continue

    # 通过播放器 UI 静音(同款配方: mousemove 激活 -> 点击音量图标)
    for frame in video_page.frames:
        try:
            if not frame.evaluate("() => !!document.querySelector('video')"):
                continue
            rm = frame.evaluate(JS_MUTE)
        except Exception:
            continue
        if rm.get("found"):
            log(f"  已静音(音量显示 {rm.get('volText') or '-'})")
            break

    log(f"  开始播放(目标倍速 x{speed})")
    st = video_status()
    actual_rate = st.get("rate") or 1.0
    if abs(actual_rate - speed) > 0.01:
        log(f"  提示: 当前实际速率 x{actual_rate:.2f}, 倍速未生效, 按实际速率等待播完")
        # 倍速设置失败时转储播放器页面 HTML, 便于适配真实的倍速控件结构
        if not speed_set:
            try:
                for frame in video_page.frames:
                    has_video = frame.evaluate("() => !!document.querySelector('video')")
                    if has_video:
                        dump = BASE_DIR / "yuketang_debug_video.html"
                        dump.write_text(
                            frame.evaluate("() => document.documentElement.outerHTML"),
                            encoding="utf-8")
                        log(f"  播放器页面已保存到 {dump.name}, 可发给开发者适配倍速控件")
                        break
            except Exception:
                pass
    start = time.time()
    # 按实际速率计算超时, 避免速率未生效时提前超时
    max_wait = (st.get("duration") or 3600) / max(actual_rate, 0.5) + 300

    while True:
        check_deadline(deadline_ts)
        st = video_status()
        if not st.get("found"):
            video_page.wait_for_timeout(2000)
            continue
        cur, dur = st["current"], st["duration"]
        if dur:
            log(f"  播放中 {cur:6.1f}s / {dur:6.1f}s ({st['progress']*100:4.1f}%)")
        if st["ended"] or (dur and st["progress"] >= 0.985):
            break
        if time.time() - start > max_wait:
            log("  等待超时, 按已看完处理并继续")
            break
        video_page.wait_for_timeout(4000)

    video_page.wait_for_timeout(4000)  # 等平台记录进度
    if not same_tab:
        video_page.close()
    list_page.wait_for_timeout(1000)
    return True


# ---------------- AI 接口 ----------------

def load_ai_config():
    try:
        return json.loads(AI_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"base_url": "https://api.deepseek.com/v1", "api_key": "", "model": "deepseek-chat"}


def save_ai_config(cfg):
    try:
        AI_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log(f"保存AI配置失败: {e}")


def ai_chat(cfg, system, user):
    """OpenAI 兼容的 chat/completions 调用(DeepSeek/月之暗面/OpenAI 等通用)。"""
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps({
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
        }).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
        })
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"].strip()


HOMEWORK_SYSTEM = (
    "你是课程作业答题助手。根据题目和选项选出最可能正确的答案。"
    "只返回选项字母, 单选/判断返回一个字母(如 A), 多选返回多个字母(如 A,C), "
    "不要输出任何解释。"
)

HW_VISION_PROMPT = (
    "图片中是一道课程作业题(可能为单选/多选/判断题)。请仔细阅读图片中的题目和选项, "
    "选出最可能正确的答案。只返回选项字母, 单选/判断返回一个字母(如 A), "
    "多选返回多个字母并用英文逗号分隔(如 A,C), 不要输出任何解释。"
)


def ai_chat_vision(cfg, prompt, image_bytes):
    """OpenAI 兼容的视觉模型调用(图片走 base64 data URL)。"""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps({
            "model": cfg["model"],
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/png;base64," + b64}},
                ],
            }],
            "temperature": 0.2,
        }).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
        })
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"].strip()


# ---------------- 讨论模块 ----------------

def dump_page(page, name):
    """转储所有实质 frame 的 HTML(含 iframe 嵌套页), 便于离线适配。"""
    base = name.rsplit(".html", 1)[0]
    for i, frame in enumerate(page.frames):
        try:
            html = frame.evaluate("() => document.documentElement.outerHTML")
            if len(html) > 5000:
                path = BASE_DIR / f"{base}_f{i}.html"
                path.write_text(html, encoding="utf-8")
                log(f"  frame[{i}] ({frame.url[:60]}) 已保存到 {path.name}")
        except Exception:
            continue


def do_discussion(list_page, ctx, idx, deadline_ts):
    """完成一条讨论: 复制讨论区其他同学的发言并发布(不用 AI)。"""
    opened = open_item(list_page, ctx, idx)
    if not opened:
        return False
    page, same_tab = opened
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(4000)
    check_deadline(deadline_ts)

    # 等发言列表加载(最多 20s)
    data = None
    for _ in range(10):
        for frame in page.frames:
            try:
                d = frame.evaluate(JS_DISCUSSION_EXTRACT)
                if d and d.get("hasTextarea") and d.get("hasSend"):
                    data = (frame, d)
                    break
            except Exception:
                continue
        if data:
            break
        page.wait_for_timeout(2000)
    if not data:
        log("  未找到讨论输入框/发布按钮, 讨论页结构待适配")
        dump_page(page, "yuketang_debug_discussion.html")
        if not same_tab:
            page.close()
        return False

    frame, d = data
    replies = d["replies"]
    if not replies:
        log("  讨论区还没有同学发言(≥20字), 跳过, 下轮重试")
        if not same_tab:
            page.close()
        return False

    content = random.choice(replies)
    log(f"  选用同学发言: {content[:50]}...")

    try:
        ta = frame.locator(".prompt-input textarea")
        ta.first.click()
        ta.first.fill(content)
    except Exception as e:
        log(f"  填写回复框失败: {e}")
        if not same_tab:
            page.close()
        return False
    page.wait_for_timeout(800)

    try:
        frame.locator(".prompt-send-btn").first.click(timeout=3000)
        log("  已发布讨论")
    except Exception:
        log("  点击发布按钮失败, 请人工检查")
        dump_page(page, "yuketang_debug_discussion.html")
    page.wait_for_timeout(2500)
    if not same_tab:
        page.close()
    list_page.wait_for_timeout(1000)
    return True


# ---------------- 作业模块 ----------------

def get_exercise_frame(page):
    """找到作业 iframe(题目在 /v2/web/iframe-exercise/ 里)。"""
    for frame in page.frames:
        if "iframe-exercise" in frame.url:
            return frame
    for frame in page.frames:
        try:
            if frame.evaluate("() => !!document.querySelector('.container-problem')"):
                return frame
        except Exception:
            continue
    return None


def click_dialog_action(frame, texts):
    """点击可见 el-dialog 弹窗里的指定文字按钮, 返回是否点到。"""
    for t in texts:
        try:
            loc = frame.locator(".el-dialog__wrapper:visible").get_by_text(t, exact=True)
            if loc.count():
                loc.first.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


def do_homework(list_page, ctx, idx, ai_cfg, deadline_ts, confirm_submit=False):
    """完成一套作业: 截图题干 -> 视觉AI作答 -> 点选 -> (可选人工确认) -> 提交 -> 下一题。"""
    opened = open_item(list_page, ctx, idx)
    if not opened:
        return False
    page, same_tab = opened
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(4000)

    # 等作业 iframe 加载出题目
    frame = None
    for _ in range(10):
        check_deadline(deadline_ts)
        f = get_exercise_frame(page)
        if f:
            try:
                if f.evaluate("() => !!document.querySelector('.container-problem')"):
                    frame = f
                    break
            except Exception:
                pass
        page.wait_for_timeout(2000)
    if not frame:
        log("  未找到作业题目区域, 结构待适配")
        dump_page(page, "yuketang_debug_homework.html")
        if not same_tab:
            page.close()
        return False

    # 续做支持: 跳到第一道未提交的题(中途退出后重新进入时从第1题开始的问题)
    try:
        jump = frame.evaluate(JS_SIDEBAR_JUMP)
        if jump.get("allDone"):
            log("  本套作业全部题目已提交, 跳过")
            if not same_tab:
                page.close()
            list_page.wait_for_timeout(1000)
            return True
        if jump.get("jumped"):
            log(f"  续做: 跳到第一道未提交的题(第 {jump.get('order')} 题)")
            page.wait_for_timeout(2500)
    except Exception:
        pass

    for q_no in range(1, 100):
        check_deadline(deadline_ts)
        try:
            data = frame.evaluate(JS_HOMEWORK_EXTRACT)
        except Exception:
            data = None
        if not data or not data.get("found"):
            log("  未识别到题目(可能本套已做完)")
            break

        pos = f"{data['order'] or q_no}/{data['total'] or '?'}"
        log(f"  第 {pos} 题 [{data['qtype'] or '未知题型'}] 选项: {','.join(data['options'])}")

        # 保险: 当前题若已提交过, 直接翻下一题
        if data.get("submitted"):
            log("  本题已提交过, 跳到下一题")
            try:
                nxt = frame.locator(".problem-fixedbar button", has_text="下一题").first
                if nxt.get_attribute("disabled") is not None:
                    log("  已是最后一题, 本套作业结束")
                    break
                nxt.click(timeout=3000)
                page.wait_for_timeout(1500)
                continue
            except Exception:
                log("  没有下一题, 本套作业结束")
                break

        # 截图题干区域发给视觉模型(题目文字是加密字体, 只能截图识别)
        try:
            img = frame.locator(".container-problem").first.screenshot()
        except Exception as e:
            log(f"  题目截图失败: {e}")
            break
        answer = ai_chat_vision(ai_cfg, HW_VISION_PROMPT, img)
        letters = [l for l in re.findall(r"[A-Z]", answer.upper()) if l in data["options"]]
        log(f"  AI答案: {answer.strip()[:20]} -> {letters}")

        if confirm_submit:
            print(f"  [人工确认] 第 {pos} 题 AI 建议: {letters}")
            s = input("  按回车采用并提交; 输入 s 跳过本题(不作答); 输入 q 结束本套作业 >>> ").strip().lower()
            if s == "q":
                log("  用户结束本套作业")
                break
            if s == "s":
                # 未勾任何选项时直接点下一题, 不会触发"帮我提交"挽留弹窗
                log("  跳过本题(不作答)")
                try:
                    frame.locator(".problem-fixedbar button",
                                  has_text="下一题").first.click(timeout=3000)
                    page.wait_for_timeout(1500)
                    continue
                except Exception:
                    log("  没有下一题, 本套作业结束")
                    break
        if not letters:
            log("  答案解析失败, 跳过本题(不作答)")
            try:
                frame.locator(".problem-fixedbar button",
                              has_text="下一题").first.click(timeout=3000)
                page.wait_for_timeout(1500)
                continue
            except Exception:
                break

        # 精确点选 label:has(input[value="X"])(真实点击, Element UI 正常响应)
        for l in letters:
            try:
                frame.locator(
                    f'.container-problem label:has(input[value="{l}"])').first.click(timeout=3000)
            except Exception as e:
                log(f"  点选 {l} 失败: {e}")
            page.wait_for_timeout(400)
        page.wait_for_timeout(500)

        # 提交(按钮在未选答案时是 disabled)
        try:
            frame.locator(".problem-fixedbar button",
                          has_text="提交").first.click(timeout=3000)
        except Exception as e:
            log(f"  点击提交失败: {e}(可能未选中答案)")
        page.wait_for_timeout(1500)
        # 弹窗处理: 空答案拦截 -> 取消; 提交成功/挽留 -> 确定/帮我提交
        click_dialog_action(frame, ["取消", "确定", "帮我提交"])
        page.wait_for_timeout(1000)

        # 提交后重新读取题号: 平台可能已自动翻题, 自动翻了就不再点"下一题"(防跳题)
        cur_order = data.get("order")
        try:
            new_data = frame.evaluate(JS_HOMEWORK_EXTRACT)
        except Exception:
            new_data = None
        if new_data and new_data.get("found") \
                and str(new_data.get("order")) != str(cur_order):
            log(f"  已提交, 进入第 {new_data.get('order')} 题")
            continue

        # 未自动翻题时手动点"下一题"; 按钮置灰说明已是最后一题
        try:
            nxt = frame.locator(".problem-fixedbar button", has_text="下一题").first
            if nxt.get_attribute("disabled") is not None:
                log("  已是最后一题, 本套作业结束")
                break
            nxt.click(timeout=3000)
            page.wait_for_timeout(1500)
        except Exception:
            log("  没有下一题, 本套作业结束")
            break

    page.wait_for_timeout(2000)
    if not same_tab:
        page.close()
    list_page.wait_for_timeout(1000)
    return True


# ---------------- 主流程 ----------------

def main():
    parser = argparse.ArgumentParser(description="雨课堂自动刷课")
    parser.add_argument("--speed", type=float, default=2.0, help="视频倍速, 默认 2.0")
    parser.add_argument("--debug", action="store_true", help="只扫描打印条目, 不实际刷")
    args = parser.parse_args()

    log("停止方式: 定时时间到达 / 全部刷完 / 在此窗口按 Ctrl+C")

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
            courses = extract_courses(page)

            opts = select_options_gui(courses, load_ai_config())
            if not opts:
                log("已取消, 退出")
                return

            course_name = opts["course_name"]
            type_label = opts["type_label"]
            deadline_ts = opts["deadline_ts"]
            ai_cfg = opts["ai"]
            icon_class, badge = CONTENT_TYPES[type_label]

            log(f"目标课程: 《{course_name}》  内容类型: {type_label}  视频倍速: x{args.speed}")
            if deadline_ts:
                log(f"定时关闭: {datetime.fromtimestamp(deadline_ts):%Y-%m-%d %H:%M}")

            course_page = goto_course(page, ctx, course_name, opts["course_idx"])

            attempts = {}
            round_no = 0
            while True:
                check_deadline(deadline_ts)
                round_no += 1
                scroll_page(course_page)
                _, rows = scan_rows(course_page, icon_class, badge)
                if not rows:
                    log(f"未识别到任何'{type_label}'条目。")
                    debug_dump(course_page)
                    print("请确认当前页面是课程的【学习内容】页。")
                    print("在浏览器里调整好页面后按回车重新扫描; 输入 q 回车退出。")
                    s = input(">>> ").strip().lower()
                    if s == "q":
                        break
                    course_page = ctx.pages[-1]
                    continue

                total = len(rows)
                done_cnt = sum(1 for r in rows if r["done"])
                log(f"第 {round_no} 轮扫描: 共 {total} 个{type_label}, 已完成 {done_cnt}")

                if args.debug:
                    for r in rows:
                        mark = "[已完成]" if r["done"] else "[未完成]"
                        print(f"  {mark} #{r['idx']} {r['title']} {r['prog']}")
                    break

                todo = [r for r in rows
                        if not r["done"] and attempts.get(r["idx"], 0) < MAX_ATTEMPTS]
                if not todo:
                    if done_cnt == total:
                        log(f"全部{type_label}都已完成, 结束。")
                    else:
                        log(f"剩余 {total - done_cnt} 个条目多次尝试仍未完成, "
                            "可能有弹题或异常, 请手动检查。结束。")
                    break

                # 每轮只处理一个, 处理完刷新列表重新扫描, 状态最可靠
                # 尝试次数以条目序号 idx 为键(重扫后顺序稳定; 不能用标题, 大量条目同名)
                r = todo[0]
                attempts[r["idx"]] = attempts.get(r["idx"], 0) + 1
                log(f"处理 #{r['idx']}: {r['title'] or r['text'][:60]}")
                try:
                    if type_label == "视频":
                        watch_video(course_page, ctx, r["idx"], args.speed, deadline_ts)
                    elif type_label == "讨论":
                        do_discussion(course_page, ctx, r["idx"], deadline_ts)
                    else:
                        do_homework(course_page, ctx, r["idx"], ai_cfg,
                                    deadline_ts, opts.get("confirm_submit", False))
                except DeadlineReached:
                    raise
                except Exception as e:
                    log(f"  处理异常: {e} (跳过, 刷新后重扫)")

                # 关掉多余标签页, 刷新列表页
                try:
                    for pg in ctx.pages:
                        if pg != course_page and not pg.is_closed():
                            pg.close()
                    course_page.bring_to_front()
                    course_page.reload(wait_until="domcontentloaded")
                    course_page.wait_for_timeout(3000)
                    course_page.get_by_text("学习内容", exact=True).first.click(timeout=3000)
                    course_page.wait_for_timeout(2000)
                except Exception:
                    pass

            input("按回车关闭浏览器 <<<")
        except DeadlineReached:
            log("到达设定的定时关闭时间, 自动停止。已完成的进度会被平台保留。")
            input("按回车关闭浏览器 <<<")
        finally:
            ctx.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止(用户中断)。已完成的进度会被平台保留。")
        sys.exit(0)
