#!/usr/bin/env python3
"""
linux.do 每日活跃 —— 浏览帖子 + 上报阅读时长。

设计依据（参数来自 Chankin026/linuxdo-v2ex-checkin 的实测实现，已逐行核对）：
  - CSRF:        GET  /session/csrf        -> {"csrf": "..."}
  - 帖子号:      DOM [data-post-number] + article[id^="post_"]，最多取 6 个
  - 上报:        POST /topics/timings      (application/x-www-form-urlencoded)
                 payload = {timings[<n>]: <ms>, ..., topic_time: <ms>, topic_id: <id>}
                 headers = Discourse-Background/Logged-In/Present: true, X-CSRF-Token, X-Requested-With
  - 浏览节奏:    最多 10 帖；每帖最多 10 次滚动(550-650px)；每次停 2-4 秒；3% 随机早退；到底部早退
  - 点赞:        30% 概率
  - 登录判据:    GET /session/current.json -> current_user.username

与上游项目的关键差异：
  上游为了在「裸 VPS + 机房 IP」上从零建立可信会话，引入了 CloakBrowser（200MB 自编译
  Chromium）+ YesCaptcha 付费打码 + 自动 git 更新，且代码里没有 proxy 参数。
  本脚本复用本机「持久化 profile（已登录）+ 代理」，因此这些全都不需要。

用法：
    python linuxdo_daily.py                # 正常执行
    python linuxdo_daily.py --dry-run      # 只浏览不上报（调试用）
    python linuxdo_daily.py --topics 3     # 只浏览 3 个帖子（快速验证）
    python linuxdo_daily.py --headful      # 显示浏览器窗口
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 配置
DEFAULT_CFG = {
    "chrome_path": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "user_data_dir": r"C:\Users\shen_ovo\.dsh\chrome-ldc-clean",
    "proxy": "http://127.0.0.1:65532",
    "headless": True,
    "topic_list_url": "https://linux.do/latest",
    "home_url": "https://linux.do/",
    "max_topics": 10,
    "max_scrolls": 10,
    "scroll_min": 550,
    "scroll_max": 650,
    "dwell_min": 2.0,
    "dwell_max": 4.0,
    "random_exit_p": 0.03,
    "like_p": 0.30,
    "settle_after_open": 4.0,
    "timeout_ms": 60000,
}


def load_cfg() -> dict:
    p = os.path.join(HERE, "config.json")
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                user = json.load(f)
            # 复用已存在的 config.json 里的 proxy / browser 段
            if "proxy" in user and isinstance(user["proxy"], dict):
                cfg["proxy"] = user["proxy"].get("server", cfg["proxy"])
            b = user.get("browser", {})
            cfg["chrome_path"] = b.get("chrome_path", cfg["chrome_path"])
            cfg["user_data_dir"] = b.get("user_data_dir", cfg["user_data_dir"])
            cfg["headless"] = b.get("headless", cfg["headless"])
        except Exception:
            pass
    return cfg


def parse_cookies_env(raw: str) -> dict:
    """解析 LINUXDO_COOKIES，支持三种格式：
       1) base64(JSON)          —— 推荐，避免引号被 shell 吃掉
       2) JSON 对象字符串        {"_t": "...", "_forum_session": "..."}
       3) Cookie 头字符串        _t=xxx; _forum_session=yyy
    """
    if not raw:
        return {}
    raw = raw.strip()

    # 1) base64
    try:
        import base64
        dec = base64.b64decode(raw, validate=True).decode("utf-8")
        obj = json.loads(dec)
        if isinstance(obj, dict):
            return {str(k): str(v) for k, v in obj.items() if v}
    except Exception:
        pass

    # 2) JSON
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return {str(k): str(v) for k, v in obj.items() if v}
        except Exception:
            pass

    # 3) Cookie 头
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
    return out


def preflight(cfg: dict) -> tuple[bool, str]:
    """启动前置检查。返回 (通过, 说明)。

    两种模式：
      - 本地模式：必须有可用代理（linux.do 在国内被墙）
      - 云端模式（GitHub Actions）：直连，不需要代理，只检查目标可达性
    """
    import socket
    from urllib.parse import urlparse

    proxy = cfg.get("proxy")

    if not proxy:
        # 云端模式：直接检查 linux.do 是否可达
        try:
            with socket.create_connection(("linux.do", 443), timeout=15):
                pass
        except Exception as e:
            return False, f"无法连接 linux.do:443（{type(e).__name__}）"
        return True, "直连模式，linux.do 可达"

    # 本地模式：检查代理端口 + 出网
    try:
        u = urlparse(proxy)
        host, port = u.hostname or "127.0.0.1", u.port or 7890
    except Exception:
        return False, f"代理地址无法解析: {proxy}"

    try:
        with socket.create_connection((host, port), timeout=5):
            pass
    except Exception as e:
        return False, (f"代理 {host}:{port} 连不上（{type(e).__name__}）。\n"
                       f"请先启动代理客户端，再运行本脚本。")

    return True, f"代理 {proxy} 可用"


# ---------------------------------------------------------------- 结果
@dataclass
class Stats:
    ok: bool = False
    username: str | None = None
    trust_level: int | None = None
    unread: int | None = None
    topics_found: int = 0
    topics_planned: int = 0
    topics_done: int = 0
    scrolls: int = 0
    likes: int = 0
    timings_ok: int = 0
    timings_fail: int = 0
    total_ms: int = 0
    error: str = ""
    details: list = field(default_factory=list)

    def summary(self) -> str:
        if not self.ok:
            return f"❌ 执行失败：{self.error}"
        mins = self.total_ms / 60000
        return (f"✅ linux.do 每日活跃完成\n"
                f"用户：{self.username}（TL{self.trust_level}）\n"
                f"浏览：{self.topics_done}/{self.topics_planned} 帖"
                f"（列表共 {self.topics_found}）\n"
                f"滚动：{self.scrolls} 次   点赞：{self.likes} 次\n"
                f"阅读时长上报：成功 {self.timings_ok} / 失败 {self.timings_fail}\n"
                f"耗时：{mins:.1f} 分钟\n"
                f"未读通知：{self.unread}")


# ---------------------------------------------------------------- 核心
class DailyRunner:
    def __init__(self, cfg: dict, dry_run: bool = False, headful: bool = False,
                 cookies: dict | None = None):
        self.cfg = cfg
        self.dry_run = dry_run
        self.headful = headful
        self.cookies = cookies or {}
        self.stats = Stats()

    # ---------- 工具 ----------
    def log(self, msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    @staticmethod
    def wait_cf(page, rounds: int = 25, click_at: int = 4) -> bool:
        """等 Cloudflare 挑战结算；必要时点一次复选框。"""
        for i in range(rounds):
            try:
                t = page.title()
            except Exception:
                return False
            if not any(k in t for k in ("Just a moment", "请稍候", "Attention")):
                return True
            if i == click_at:
                try:
                    for sel in ("input[type=checkbox]", ".cf-turnstile",
                                "iframe[src*='challenges.cloudflare.com']"):
                        el = page.query_selector(sel)
                        if el:
                            b = el.bounding_box()
                            if b:
                                page.mouse.click(b["x"] + b["width"] / 2,
                                                 b["y"] + b["height"] / 2)
                                break
                except Exception:
                    pass
            time.sleep(2)
        return False

    @staticmethod
    def topic_id_from_url(url: str) -> str:
        m = re.search(r"/t/(?:[^/?#]+/)?(\d+)(?:[/?#]|$)", url or "")
        return m.group(1) if m else ""

    # ---------- 页面内 JS ----------
    JS_TOPIC_LINKS = """
    () => {
      const out = new Set();
      document.querySelectorAll('a[href*="/t/"]').forEach(a => {
        const m = (a.getAttribute('href') || '').match(/^\\/t\\/(?:[^\\/?#]+\\/)?(\\d+)/);
        if (m) out.add(m[1]);
      });
      return Array.from(out);
    }
    """

    JS_TIMING_CONTEXT = """
    () => {
      const direct = Array.from(document.querySelectorAll('[data-post-number]'))
        .map(el => parseInt((el.getAttribute('data-post-number') || '').trim(), 10))
        .filter(Number.isFinite);
      const fromArticle = Array.from(document.querySelectorAll('article[id^="post_"]'))
        .map(el => {
          const m = String(el.id || '').match(/^post_(\\d+)$/);
          return m ? parseInt(m[1], 10) : NaN;
        })
        .filter(Number.isFinite);
      return {
        url: location.href,
        post_numbers: Array.from(new Set(direct.concat(fromArticle))).slice(0, 6)
      };
    }
    """

    JS_AT_BOTTOM = """
    () => window.scrollY + window.innerHeight >= document.body.scrollHeight - 4
    """

    # ---------- 浏览单个帖子 ----------
    def browse_topic(self, page) -> dict:
        cfg = self.cfg
        scrolls = 0
        exit_reason = "max_scrolls"
        t0 = time.monotonic()

        for _ in range(cfg["max_scrolls"]):
            dist = random.randint(cfg["scroll_min"], cfg["scroll_max"])
            try:
                page.evaluate(f"() => window.scrollBy(0, {dist})")
            except Exception as e:
                self.log(f"    滚动失败: {e}")
                break
            scrolls += 1

            if random.random() < cfg["random_exit_p"]:
                exit_reason = "random_exit"
                break

            try:
                if page.evaluate(self.JS_AT_BOTTOM):
                    exit_reason = "bottom"
                    break
            except Exception:
                pass

            time.sleep(random.uniform(cfg["dwell_min"], cfg["dwell_max"]))

        duration_ms = max(1000, int((time.monotonic() - t0) * 1000))
        return {"scrolls": scrolls, "exit_reason": exit_reason,
                "duration_ms": duration_ms, "final_url": page.url}

    # ---------- 上报阅读时长 ----------
    def report_timings(self, page, duration_ms: int) -> bool:
        # 1) 取 CSRF
        csrf = ""
        try:
            r = page.request.get("https://linux.do/session/csrf",
                                 headers={"Accept": "application/json, text/javascript, */*; q=0.01",
                                          "X-Requested-With": "XMLHttpRequest"},
                                 timeout=20000)
            if r.status == 200:
                csrf = str((r.json() or {}).get("csrf") or "")
        except Exception as e:
            self.log(f"    取 CSRF 失败: {e}")
        if not csrf:
            self.log("    没拿到 CSRF token，跳过上报")
            return False

        # 2) 取帖子号与 topic_id
        try:
            ctx = page.evaluate(self.JS_TIMING_CONTEXT) or {}
        except Exception:
            ctx = {}
        topic_id = self.topic_id_from_url(ctx.get("url") or page.url)
        post_numbers = ctx.get("post_numbers") or [1]
        if not topic_id:
            self.log("    未能从 URL 识别 topic_id，跳过上报")
            return False

        # 3) 构造表单
        payload = {f"timings[{n}]": duration_ms for n in post_numbers[:6]}
        payload["topic_time"] = duration_ms
        payload["topic_id"] = topic_id

        if self.dry_run:
            self.log(f"    [dry-run] 将上报 topic_id={topic_id} posts={post_numbers} "
                     f"time={duration_ms}ms")
            return True

        try:
            r = page.request.post(
                "https://linux.do/topics/timings",
                headers={
                    "Accept": "*/*",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Discourse-Background": "true",
                    "Discourse-Logged-In": "true",
                    "Discourse-Present": "true",
                    "X-CSRF-Token": csrf,
                    "X-Requested-With": "XMLHttpRequest",
                    "X-Silence-Logger": "true",
                },
                data=urlencode(payload),
                timeout=25000,
            )
            if r.status == 200:
                self.log(f"    ✅ 上报成功 topic_id={topic_id} posts={post_numbers} "
                         f"time={duration_ms}ms")
                return True
            self.log(f"    ⚠️ 上报失败 status={r.status} body={r.text()[:150]}")
            return False
        except Exception as e:
            self.log(f"    ⚠️ 上报异常: {e}")
            return False

    # ---------- 点赞 ----------
    def maybe_like(self, page) -> bool:
        if random.random() >= self.cfg["like_p"]:
            return False
        time.sleep(random.uniform(1.2, 2.8))
        # Discourse 点赞按钮
        for sel in ("button[title='点赞']", "button[title='Like']",
                    "button.toggle-like", ".like-button", "button[class*='like']"):
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    cls = (btn.get_attribute("class") or "").lower()
                    if "has-like" in cls or "liked" in cls:
                        return False  # 已点过
                    btn.click(timeout=6000)
                    for _ in range(10):
                        time.sleep(1)
                        c2 = (btn.get_attribute("class") or "").lower()
                        if "has-like" in c2 or "liked" in c2:
                            return True
                    return False
            except Exception:
                continue
        return False

    # ---------- 主流程 ----------
    def run(self) -> Stats:
        from playwright.sync_api import sync_playwright

        cfg = self.cfg
        t_start = time.monotonic()

        with sync_playwright() as p:
            # ---- 启动上下文：本地 profile 模式 或 云端 cookie 模式 ----
            # launch_* 与 new_context 接受的参数不同，分开构造：
            #   launch_*    -> headless / args / proxy / ignore_default_args
            #   new_context -> locale / timezone_id / viewport
            launch_kw = dict(
                headless=(not self.headful) and cfg["headless"],
                args=["--disable-blink-features=AutomationControlled",
                      "--no-first-run", "--no-default-browser-check"],
                ignore_default_args=["--enable-automation"],
            )
            ctx_kw = dict(
                locale="zh-CN", timezone_id="Asia/Shanghai",
                viewport={"width": 1400, "height": 900},
            )
            if cfg.get("proxy"):
                launch_kw["proxy"] = {"server": cfg["proxy"],
                                      "bypass": "localhost,127.0.0.1"}

            if cfg.get("user_data_dir") and os.path.isdir(cfg["user_data_dir"]):
                # 本地模式：复用已登录的持久化 profile
                self.log(f"模式: 本地 profile  {cfg['user_data_dir']}")
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=cfg["user_data_dir"],
                    executable_path=cfg.get("chrome_path") or None,
                    **launch_kw, **ctx_kw)
            else:
                # 云端模式（GitHub Actions）：无持久化目录，靠注入 cookie
                self.log("模式: 云端（注入 cookie）")
                browser = p.chromium.launch(**launch_kw)
                ctx = browser.new_context(**ctx_kw)
                if self.cookies:
                    payload = []
                    for name, value in self.cookies.items():
                        for dom in (".linux.do", "linux.do"):
                            payload.append({
                                "name": name, "value": value,
                                "domain": dom, "path": "/",
                                "secure": True, "httpOnly": False,
                                "sameSite": "Lax",
                            })
                    ctx.add_cookies(payload)
                    self.log(f"已注入 {len(self.cookies)} 个 cookie "
                             f"({', '.join(sorted(self.cookies))})")

            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            try:
                # ---- 1. 打开首页（让 CF 结算）----
                self.log("1/5 打开 linux.do")
                page.goto(cfg["home_url"], wait_until="domcontentloaded",
                          timeout=cfg["timeout_ms"])
                self.wait_cf(page)
                time.sleep(3)
                self.log(f"    标题: {page.title()[:50]}")

                # ---- 2. 确认登录 ----
                self.log("2/5 确认登录态")
                r = page.request.get("https://linux.do/session/current.json", timeout=30000)
                if r.status != 200:
                    self.stats.error = f"登录态查询失败 HTTP {r.status}"
                    ctx.close()
                    return self.stats
                j = r.json()
                u = j.get("current_user") or {}
                if not u.get("username"):
                    self.stats.error = "未登录（profile 里没有有效会话）"
                    ctx.close()
                    return self.stats
                self.stats.username = u["username"]
                self.stats.trust_level = u.get("trust_level")
                self.stats.unread = j.get("unread_notifications")
                self.log(f"    ✅ {u['username']}  TL{u.get('trust_level')}  "
                         f"未读={j.get('unread_notifications')}")

                # ---- 3. 收集主题列表 ----
                self.log("3/5 收集主题列表")
                page.goto(cfg["topic_list_url"], wait_until="domcontentloaded",
                          timeout=cfg["timeout_ms"])
                self.wait_cf(page)
                time.sleep(4)
                ids = page.evaluate(self.JS_TOPIC_LINKS) or []
                self.stats.topics_found = len(ids)
                if not ids:
                    self.stats.error = "未从列表页找到任何主题"
                    ctx.close()
                    return self.stats
                plan = min(cfg["max_topics"], len(ids))
                picked = random.sample(ids, plan)
                self.stats.topics_planned = plan
                self.log(f"    发现 {len(ids)} 个主题，计划浏览 {plan} 个")

                # ---- 4. 逐帖浏览 + 上报 ----
                for i, tid in enumerate(picked, 1):
                    url = f"https://linux.do/t/{tid}"
                    self.log(f"4/5 [{i}/{plan}] 浏览 {url}")
                    tp = ctx.new_page()
                    try:
                        tp.goto(url, wait_until="domcontentloaded",
                                timeout=cfg["timeout_ms"])
                        self.wait_cf(tp)
                        time.sleep(cfg["settle_after_open"])
                        res = self.browse_topic(tp)
                        self.stats.scrolls += res["scrolls"]
                        self.log(f"    滚动 {res['scrolls']} 次，"
                                 f"结束原因={res['exit_reason']}，"
                                 f"停留 {res['duration_ms']/1000:.1f}s")
                        ok = self.report_timings(tp, res["duration_ms"])
                        if ok:
                            self.stats.timings_ok += 1
                        else:
                            self.stats.timings_fail += 1
                        if self.maybe_like(tp):
                            self.stats.likes += 1
                            self.log("    👍 已点赞")
                        self.stats.topics_done += 1
                    except Exception as e:
                        self.log(f"    ⚠️ 该帖处理失败: {type(e).__name__}: {e}")
                    finally:
                        try:
                            tp.close()
                        except Exception:
                            pass

                # ---- 5. 汇总 ----
                self.log("5/5 完成")
                self.stats.total_ms = int((time.monotonic() - t_start) * 1000)
                self.stats.ok = True
                return self.stats

            finally:
                try:
                    ctx.close()
                except Exception:
                    pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只浏览不向上游上报")
    ap.add_argument("--topics", type=int, default=None, help="本次浏览几个帖子")
    ap.add_argument("--headful", action="store_true", help="显示浏览器窗口")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = ap.parse_args()

    cfg = load_cfg()
    if args.topics:
        cfg["max_topics"] = args.topics

    # ---- 环境变量覆盖（GitHub Actions 用）----
    cookies = {}
    env_cookies = os.environ.get("LINUXDO_COOKIES", "").strip()
    if env_cookies:
        cookies = parse_cookies_env(env_cookies)
        print(f"[env] LINUXDO_COOKIES 解析出 {len(cookies)} 个 cookie: "
              f"{', '.join(sorted(cookies)) or '(空)'}")
        if not cookies:
            print("[env] ⚠️ cookie 解析失败，请检查格式")

    # 云端模式：有 cookie 且显式要求不走代理时，清空 proxy
    if os.environ.get("LINUXDO_NO_PROXY", "").lower() in ("1", "true", "yes"):
        cfg["proxy"] = ""
    if os.environ.get("LINUXDO_HEADLESS", "").lower() in ("0", "false", "no"):
        cfg["headless"] = False
    if os.environ.get("LINUXDO_MAX_TOPICS", "").isdigit():
        cfg["max_topics"] = int(os.environ["LINUXDO_MAX_TOPICS"])

    # ---- 前置检查 ----
    ok, info = preflight(cfg)
    print(f"[preflight] {info}")
    if not ok:
        st = Stats(ok=False, error=info)
        if args.json:
            print(json.dumps({"ok": False, "error": info}, ensure_ascii=False, indent=2))
        print()
        print("=" * 60)
        print(st.summary())
        print("=" * 60)
        return 2

    runner = DailyRunner(cfg, dry_run=args.dry_run, headful=args.headful,
                         cookies=cookies)
    st = runner.run()

    if args.json:
        print(json.dumps({
            "ok": st.ok, "username": st.username, "trust_level": st.trust_level,
            "topics_found": st.topics_found, "topics_done": st.topics_done,
            "scrolls": st.scrolls, "likes": st.likes,
            "timings_ok": st.timings_ok, "timings_fail": st.timings_fail,
            "total_ms": st.total_ms, "error": st.error,
        }, ensure_ascii=False, indent=2))
    else:
        print()
        print("=" * 60)
        print(st.summary())
        print("=" * 60)

    # 供外部（定时任务）读取
    try:
        with open(os.path.join(HERE, "linuxdo-last-run.json"), "w", encoding="utf-8") as f:
            json.dump({
                "ok": st.ok, "username": st.username, "trust_level": st.trust_level,
                "topics_done": st.topics_done, "topics_planned": st.topics_planned,
                "topics_found": st.topics_found, "scrolls": st.scrolls,
                "likes": st.likes, "timings_ok": st.timings_ok,
                "timings_fail": st.timings_fail, "total_ms": st.total_ms,
                "error": st.error, "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return 0 if st.ok else 1


if __name__ == "__main__":
    sys.exit(main())
