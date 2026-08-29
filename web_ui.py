import json
import queue
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.sync_api import sync_playwright

from monitor import (
    AlertState,
    Counts,
    Notifier,
    TABLE_ROW_SELECTOR,
    collect_all_rows,
    count_rows,
    load_config,
    now_text,
    page_has_login_text,
    setup_logger,
    wait_until_logged_in,
)


HOST = "127.0.0.1"
PORT = 8765


class WebNotifier(Notifier):
    def __init__(self, config: Dict[str, Any], logger, events: queue.Queue) -> None:
        super().__init__(config, logger)
        self.events = events

    def send(self, title: str, text: str) -> None:
        self.events.put({"type": "alert", "time": now_text(), "title": title, "text": text})
        super().send(title, text)


class MonitorWorker(threading.Thread):
    def __init__(self, config_path: Path, events: queue.Queue) -> None:
        super().__init__(daemon=True)
        self.config_path = config_path
        self.events = events
        self.commands: queue.Queue = queue.Queue()
        self.logger = setup_logger(config_path.parent)
        self.playwright = None
        self.context = None
        self.page = None
        self.alive = True
        self.login_notice_sent = False

    def command(self, name: str) -> None:
        self.commands.put(name)

    def run(self) -> None:
        self._log("后台服务已启动")
        while self.alive:
            command = self.commands.get()
            if command == "login":
                self._login()
            elif command == "start":
                self._monitor()
            elif command == "shutdown":
                self.alive = False
            elif command == "stop":
                self._log("当前没有运行中的监控")
        self._close()
        self._log("后台服务已停止")

    def _event(self, payload: Dict[str, Any]) -> None:
        self.events.put(payload)

    def _log(self, text: str) -> None:
        self.logger.info(text)
        self._event({"type": "log", "time": now_text(), "text": text})

    def _status(self, text: str) -> None:
        self._event({"type": "status", "time": now_text(), "text": text})

    def _error(self, text: str) -> None:
        self.logger.exception(text)
        self._event({"type": "error", "time": now_text(), "text": text})

    def _counts(self, counts: Counts) -> None:
        self._event(
            {
                "type": "counts",
                "time": now_text(),
                "total": counts.total,
                "online": counts.online,
                "offline": counts.offline,
                "idle": counts.idle,
            }
        )

    def _active_page(self):
        if not self.context:
            return None
        try:
            for page in self.context.pages:
                if not page.is_closed():
                    return page
        except Exception:
            return None
        return None

    def _reset_browser(self) -> None:
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.playwright = None
        self.context = None
        self.page = None

    def _ensure_browser(self, config: Dict[str, Any]) -> None:
        active_page = self._active_page()
        if active_page:
            self.page = active_page
            return
        if self.context or self.playwright:
            self._log("检测到登录浏览器窗口已关闭，正在重新打开")
            self._reset_browser()
        browser_config = config.get("browser", {})
        user_data_dir = self.config_path.parent / browser_config.get(
            "user_data_dir", "runtime/browser-profile"
        )
        user_data_dir.mkdir(parents=True, exist_ok=True)
        self._log("正在打开极兔登录浏览器窗口")
        self._status("等待扫码")
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=bool(browser_config.get("headless", False)),
            viewport={"width": 1440, "height": 900},
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.goto(config["target_url"], wait_until="domcontentloaded", timeout=60000)

    def _login(self) -> None:
        try:
            config = load_config(self.config_path)
            self._ensure_browser(config)
            timeout = int(config.get("browser", {}).get("login_timeout_seconds", 180))
            wait_until_logged_in(self.page, timeout, self.logger)
            rows = collect_all_rows(self.page, self.logger)
            counts = count_rows(rows)
            self._counts(counts)
            self.login_notice_sent = False
            self._status("登录正常")
            self._log("登录成功，已读取坐席状态")
        except Exception as exc:
            self._status("登录失败")
            self._error(f"登录或读取失败：{exc}")

    def _is_login_problem(self, exc: Exception) -> bool:
        text = str(exc)
        login_words = ("登录", "扫码", "身份校验", "过期", "未登录")
        if any(word in text for word in login_words):
            return True
        if self.page:
            try:
                return page_has_login_text(self.page)
            except Exception:
                return False
        return False

    def _is_browser_closed_problem(self, exc: Exception) -> bool:
        text = str(exc).lower()
        closed_words = ("closed", "target page", "browser has been closed", "context")
        if any(word in text for word in closed_words):
            return True
        return self._active_page() is None and (self.context is not None or self.page is not None)

    def _wait_for_relogin(self, config: Dict[str, Any], notifier: WebNotifier) -> bool:
        self._status("等待重新扫码")
        self._log("检测到登录已失效，监控已暂停，等待重新扫码登录")
        if not self.login_notice_sent:
            notifier.send(
                "监控暂停：需要重新登录",
                "极兔内部网页登录状态已失效。请在弹出的浏览器中重新扫码登录，"
                "然后回到监控 UI 点击“登录极兔系统”。登录成功后程序会继续监控。",
            )
            self.login_notice_sent = True

        timeout = int(config.get("browser", {}).get("login_timeout_seconds", 180))
        last_auto_check = 0.0
        while True:
            try:
                command = self.commands.get(timeout=0.5)
            except queue.Empty:
                command = None

            if command == "stop":
                self._status("已停止")
                return False
            if command == "shutdown":
                self.alive = False
                self._status("已停止")
                return False
            if command == "start":
                self._log("监控已暂停，等待重新登录")
                continue
            if command != "login":
                if command is not None:
                    continue
                if time.time() - last_auto_check < 3:
                    continue
                last_auto_check = time.time()
                try:
                    if self.page and not self.page.is_closed() and not page_has_login_text(self.page):
                        self.page.wait_for_selector(TABLE_ROW_SELECTOR, timeout=3000)
                        rows = collect_all_rows(self.page, self.logger)
                        counts = count_rows(rows)
                        self._counts(counts)
                        self.login_notice_sent = False
                        self._status("监控中")
                        self._log("检测到页面已重新登录，监控继续运行")
                        return True
                except Exception:
                    continue
                continue

            try:
                self._ensure_browser(config)
                self.page.goto(config["target_url"], wait_until="domcontentloaded", timeout=60000)
                self._status("等待扫码")
                wait_until_logged_in(self.page, timeout, self.logger)
                rows = collect_all_rows(self.page, self.logger)
                counts = count_rows(rows)
                self._counts(counts)
                self.login_notice_sent = False
                self._status("监控中")
                self._log("重新登录成功，监控继续运行")
                return True
            except Exception as exc:
                self._status("重新登录失败")
                self._error(f"重新登录失败：{exc}")

    def _monitor(self) -> None:
        try:
            config = load_config(self.config_path)
            self._ensure_browser(config)
            timeout = int(config.get("browser", {}).get("login_timeout_seconds", 180))
            wait_until_logged_in(self.page, timeout, self.logger)
            interval = int(config.get("interval_seconds", 60))
            notifier = WebNotifier(config, self.logger, self.events)
            state = AlertState(config.get("thresholds", {}), notifier)
            self.login_notice_sent = False
            self._status("监控中")
            self._log(f"监控已启动，每 {interval} 秒检查一次")

            while True:
                if self._should_stop():
                    self._log("监控已停止")
                    return
                try:
                    if page_has_login_text(self.page):
                        if not self._wait_for_relogin(config, notifier):
                            self._log("监控已停止")
                            return
                        continue
                    rows = collect_all_rows(self.page, self.logger)
                    counts = count_rows(rows)
                    self._counts(counts)
                    state.evaluate(counts)
                    self._log(
                        f"检查完成：总人数 {counts.total}，在线 {counts.online}，"
                        f"离线 {counts.offline}，通话空闲 {counts.idle}"
                    )
                except Exception as exc:
                    if self._is_browser_closed_problem(exc):
                        self._status("浏览器已关闭")
                        self._log("极兔浏览器窗口已关闭，监控暂停并准备重新打开登录窗口")
                        try:
                            self._ensure_browser(config)
                            wait_until_logged_in(self.page, timeout, self.logger)
                        except Exception as login_exc:
                            if self._is_login_problem(login_exc):
                                if not self._wait_for_relogin(config, notifier):
                                    self._log("监控已停止")
                                    return
                                continue
                            self._error(f"重新打开登录窗口失败：{login_exc}")
                        continue
                    if self._is_login_problem(exc):
                        if not self._wait_for_relogin(config, notifier):
                            self._log("监控已停止")
                            return
                        continue
                    self._error(f"本轮检查失败：{exc}")
                    notifier.send("监控检查失败", f"{exc}\n程序将在下一轮自动重试。")
                    try:
                        self.page.reload(wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass

                until = time.time() + interval
                while time.time() < until:
                    if self._should_stop():
                        self._log("监控已停止")
                        return
                    time.sleep(0.3)
        except Exception as exc:
            self._error(f"启动监控失败：{exc}")

    def _should_stop(self) -> bool:
        try:
            while True:
                command = self.commands.get_nowait()
                if command == "stop":
                    self._status("已停止")
                    return True
                if command == "shutdown":
                    self.alive = False
                    self._status("已停止")
                    return True
                if command == "login":
                    self._log("浏览器已经打开")
                if command == "start":
                    self._log("监控已经在运行")
        except queue.Empty:
            return not self.alive

    def _close(self) -> None:
        self._reset_browser()


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
EVENTS: queue.Queue = queue.Queue()
WORKER: Optional[MonitorWorker] = None


def ensure_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        example = BASE_DIR / "config.example.json"
        CONFIG_PATH.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    return load_config(CONFIG_PATH)


def ensure_worker() -> MonitorWorker:
    global WORKER
    if not WORKER or not WORKER.is_alive():
        WORKER = MonitorWorker(CONFIG_PATH, EVENTS)
        WORKER.start()
    return WORKER


def html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>J&T 坐席状态监控</title>
  <style>
    :root { color-scheme: light; font-family: Arial, "PingFang SC", sans-serif; }
    body { margin: 0; background: #f5f7fb; color: #1f2937; }
    main { max-width: 1120px; margin: 0 auto; padding: 24px; }
    h1 { margin: 0 0 18px; font-size: 24px; }
    section { background: #fff; border: 1px solid #d7dde8; border-radius: 8px; padding: 18px; margin-bottom: 16px; }
    .grid { display: grid; grid-template-columns: 140px 1fr 140px 1fr; gap: 12px; align-items: center; }
    label { color: #536172; font-size: 14px; }
    input { width: 100%; box-sizing: border-box; border: 1px solid #c9d2df; border-radius: 6px; padding: 9px 10px; font-size: 14px; }
    .full { grid-column: span 3; }
    .buttons { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
    button { border: 0; border-radius: 6px; padding: 10px 14px; background: #2563eb; color: white; font-size: 14px; cursor: pointer; }
    button.secondary { background: #e8eef7; color: #1f2937; }
    button.danger { background: #dc2626; }
    .metrics { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; }
    .metric { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px; }
    .metric span { display: block; color: #64748b; font-size: 13px; }
    .metric strong { display: block; margin-top: 8px; font-size: 24px; }
    .panes { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    pre { height: 260px; overflow: auto; white-space: pre-wrap; margin: 0; background: #0f172a; color: #e5e7eb; border-radius: 8px; padding: 12px; font-size: 13px; }
    .hint { color: #64748b; font-size: 13px; margin-top: 10px; }
    @media (max-width: 820px) {
      .grid, .metrics, .panes { grid-template-columns: 1fr; }
      .full { grid-column: span 1; }
    }
  </style>
</head>
<body>
<main>
  <h1>J&T 坐席状态监控</h1>
  <section>
    <div class="grid">
      <label>飞书 Webhook</label><input id="webhook" class="full" placeholder="暂时不接飞书可以留空">
      <label>飞书 Secret</label><input id="secret" class="full" type="password" placeholder="机器人未开启签名校验可留空">
      <label>在线提醒阈值</label><input id="online" type="number" min="0">
      <label>离线异常阈值</label><input id="offline" type="number" min="0">
      <label>空闲异常阈值</label><input id="idle" type="number" min="0">
      <label>检查频率/秒</label><input id="interval" type="number" min="10">
    </div>
    <div class="buttons">
      <button class="secondary" onclick="saveConfig()">保存配置</button>
      <button onclick="sendCommand('login')">登录极兔系统</button>
      <button onclick="sendCommand('start')">开始监控</button>
      <button class="danger" onclick="sendCommand('stop')">停止监控</button>
    </div>
    <div class="hint">点击“登录极兔系统”后，会弹出浏览器窗口；扫码登录成功后，本页面会显示人数。</div>
  </section>
  <section>
    <div class="metrics">
      <div class="metric"><span>总人数</span><strong id="total">-</strong></div>
      <div class="metric"><span>在线</span><strong id="onlineCount">-</strong></div>
      <div class="metric"><span>离线</span><strong id="offlineCount">-</strong></div>
      <div class="metric"><span>通话空闲</span><strong id="idleCount">-</strong></div>
      <div class="metric"><span>登录状态</span><strong id="loginStatus" style="font-size:15px">未启动</strong></div>
      <div class="metric"><span>最近检查</span><strong id="lastCheck" style="font-size:15px">-</strong></div>
    </div>
  </section>
  <div class="panes">
    <section><h2>提醒记录</h2><pre id="alerts"></pre></section>
    <section><h2>运行日志</h2><pre id="logs"></pre></section>
  </div>
</main>
<script>
async function loadConfig() {
  const cfg = await (await fetch('/api/config')).json();
  webhook.value = cfg.notify?.feishu_webhook || '';
  secret.value = cfg.notify?.feishu_secret || '';
  online.value = cfg.thresholds?.online ?? 5;
  offline.value = cfg.thresholds?.offline ?? 5;
  idle.value = cfg.thresholds?.idle ?? 2;
  interval.value = cfg.interval_seconds ?? 60;
}
async function saveConfig() {
  const payload = {
    notify: { feishu_webhook: webhook.value.trim(), feishu_secret: secret.value.trim(), print_to_console: true },
    thresholds: { online: Number(online.value), offline: Number(offline.value), idle: Number(idle.value) },
    interval_seconds: Number(interval.value)
  };
  await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
  addLog('配置已保存');
}
async function sendCommand(name) {
  await saveConfig();
  await fetch('/api/' + name, { method: 'POST' });
  addLog('已发送命令：' + name);
}
function addLog(text) {
  logs.textContent += new Date().toLocaleString() + '  ' + text + '\\n';
  logs.scrollTop = logs.scrollHeight;
}
function addAlert(text) {
  alerts.textContent += text + '\\n\\n';
  alerts.scrollTop = alerts.scrollHeight;
}
async function poll() {
  try {
    const items = await (await fetch('/api/events')).json();
    for (const ev of items) {
      if (ev.type === 'counts') {
        total.textContent = ev.total;
        onlineCount.textContent = ev.online;
        offlineCount.textContent = ev.offline;
        idleCount.textContent = ev.idle;
        lastCheck.textContent = ev.time;
      } else if (ev.type === 'alert') {
        addAlert(ev.time + '  ' + ev.title + '\\n' + ev.text);
      } else if (ev.type === 'status') {
        loginStatus.textContent = ev.text;
      } else if (ev.type === 'error') {
        addLog(ev.time + '  ' + ev.text);
        addAlert(ev.time + '  程序异常\\n' + ev.text);
      } else if (ev.type === 'log') {
        addLog(ev.time + '  ' + ev.text);
      }
    }
  } catch (e) {
    addLog('读取后台状态失败：' + e);
  }
  setTimeout(poll, 1000);
}
loadConfig();
poll();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if self.path == "/":
            self._send(200, html().encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/config":
            self._json(ensure_config())
        elif self.path == "/api/events":
            items = []
            try:
                while len(items) < 100:
                    items.append(EVENTS.get_nowait())
            except queue.Empty:
                pass
            self._json(items)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path == "/api/config":
            length = int(self.headers.get("Content-Length", "0") or "0")
            patch = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            config = ensure_config()
            config.setdefault("notify", {}).update(patch.get("notify", {}))
            config.setdefault("thresholds", {}).update(patch.get("thresholds", {}))
            if "interval_seconds" in patch:
                config["interval_seconds"] = int(patch["interval_seconds"])
            CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            self._json({"ok": True})
        elif self.path in ("/api/login", "/api/start", "/api/stop"):
            ensure_worker().command(self.path.rsplit("/", 1)[-1])
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    ensure_config()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"UI 已启动：{url}")
    print("浏览器没有自动打开时，请手动复制上面的地址。")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    if WORKER and WORKER.is_alive():
        WORKER.command("shutdown")


if __name__ == "__main__":
    main()
