import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


LOGIN_TEXT = ("扫码登录", "请先同意", "重新登录", "身份校验过期", "登录")
TABLE_ROW_SELECTOR = (
    ".servicequalityIndex .yl-crud-table-wrap "
    ".el-table > .el-table__body-wrapper tbody tr"
)


@dataclass
class Counts:
    total: int
    online: int
    offline: int
    idle: int


class Notifier:
    def __init__(self, config: Dict[str, Any], logger: logging.Logger) -> None:
        self.config = config.get("notify", {})
        self.logger = logger

    def send(self, title: str, text: str) -> None:
        message = f"{title}\n{text}"
        if self.config.get("print_to_console", True):
            print(f"\n[{now_text()}] {message}\n", flush=True)
        self.logger.info(message.replace("\n", " | "))

        webhook = self.config.get("feishu_webhook") or ""
        if webhook:
            self._send_feishu(webhook, title, text)

    def _send_feishu(self, webhook: str, title: str, text: str) -> None:
        payload: Dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": f"{title}\n{text}"},
        }
        secret = self.config.get("feishu_secret") or ""
        if secret:
            timestamp = str(int(time.time()))
            string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
            sign = base64.b64encode(
                hmac.new(string_to_sign, b"", digestmod=hashlib.sha256).digest()
            ).decode("utf-8")
            payload["timestamp"] = timestamp
            payload["sign"] = sign

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            webhook,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read().decode("utf-8", errors="replace")
            self.logger.info("Feishu response: %s", body[:500])
        except Exception as exc:
            self.logger.exception("Feishu notify failed: %s", exc)


class AlertState:
    def __init__(self, thresholds: Dict[str, int], notifier: Notifier) -> None:
        self.thresholds = thresholds
        self.notifier = notifier
        self.last_online_notified: Optional[int] = None
        self.offline_abnormal = False
        self.last_offline_notified: Optional[int] = None
        self.idle_abnormal = False
        self.last_idle_notified: Optional[int] = None

    def evaluate(self, counts: Counts) -> None:
        online_threshold = int(self.thresholds.get("online", 5))
        offline_threshold = int(self.thresholds.get("offline", 5))
        idle_threshold = int(self.thresholds.get("idle", 2))

        if counts.online >= online_threshold:
            if counts.online != self.last_online_notified:
                self.notifier.send(
                    "在线人数提醒",
                    f"当前已有 {counts.online} 人在线，阈值为 {online_threshold} 人。"
                    f"\n总人数 {counts.total}，离线 {counts.offline}，通话空闲 {counts.idle}。",
                )
                self.last_online_notified = counts.online
        else:
            self.last_online_notified = None

        if counts.offline >= offline_threshold:
            if (not self.offline_abnormal) or counts.offline != self.last_offline_notified:
                self.notifier.send(
                    "离线人数异常",
                    f"当前已有 {counts.offline} 人离线，阈值为 {offline_threshold} 人。"
                    f"\n总人数 {counts.total}，在线 {counts.online}，通话空闲 {counts.idle}。",
                )
                self.last_offline_notified = counts.offline
            self.offline_abnormal = True
        else:
            if self.offline_abnormal:
                self.notifier.send(
                    "离线人数恢复",
                    f"当前离线人数已恢复到 {counts.offline} 人，低于阈值 {offline_threshold} 人。",
                )
            self.offline_abnormal = False
            self.last_offline_notified = None

        if counts.idle <= idle_threshold:
            if (not self.idle_abnormal) or counts.idle != self.last_idle_notified:
                self.notifier.send(
                    "空闲人数异常",
                    f"当前通话栏为空闲的人数为 {counts.idle} 人，阈值为小于等于 {idle_threshold} 人。"
                    f"\n总人数 {counts.total}，在线 {counts.online}，离线 {counts.offline}。",
                )
                self.last_idle_notified = counts.idle
            self.idle_abnormal = True
        else:
            if self.idle_abnormal:
                self.notifier.send(
                    "空闲人数恢复",
                    f"当前通话栏为空闲的人数已恢复到 {counts.idle} 人，高于阈值 {idle_threshold} 人。",
                )
            self.idle_abnormal = False
            self.last_idle_notified = None


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"未找到配置文件：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def setup_logger(base_dir: Path) -> logging.Logger:
    log_dir = base_dir / "runtime" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("jt-seat-monitor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    log_file = log_dir / "monitor.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


def page_has_login_text(page) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    return any(text in body for text in LOGIN_TEXT) and "坐席实时状态" not in body


def wait_until_logged_in(page, timeout_seconds: int, logger: logging.Logger) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not page_has_login_text(page):
            try:
                page.wait_for_selector(TABLE_ROW_SELECTOR, timeout=5000)
                return
            except PlaywrightTimeoutError:
                pass
        logger.info("等待扫码登录或页面加载...")
        time.sleep(3)
    raise RuntimeError("等待登录超时。请确认已扫码登录，并能看到坐席实时状态表格。")


def read_page_rows(page) -> List[Dict[str, str]]:
    rows = page.eval_on_selector_all(
        TABLE_ROW_SELECTOR,
        """
        rows => rows.map(row => {
          const cells = Array.from(row.querySelectorAll('td')).map(td => (td.innerText || '').trim());
          return {
            region: cells[2] || '',
            network: cells[3] || '',
            line: cells[4] || '',
            effectiveNum: cells[5] || '',
            onlineType: cells[6] || '',
            answerType: cells[7] || '',
            callType: cells[8] || ''
          };
        }).filter(row => row.onlineType || row.callType)
        """,
    )
    return rows


def first_page(page) -> None:
    for _ in range(20):
        disabled = page.evaluate(
            """
            () => {
              const buttons = Array.from(document.querySelectorAll('.servicequalityIndex .el-pagination .btn-prev'))
                .filter(el => el.offsetParent !== null);
              const el = buttons[buttons.length - 1];
              if (!el) return true;
              return el.disabled || el.classList.contains('disabled');
            }
            """
        )
        if disabled:
            return
        page.evaluate(
            """
            () => {
              const buttons = Array.from(document.querySelectorAll('.servicequalityIndex .el-pagination .btn-prev'))
                .filter(el => el.offsetParent !== null);
              const el = buttons[buttons.length - 1];
              if (el) el.click();
            }
            """
        )
        page.wait_for_timeout(800)


def has_next_page(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const buttons = Array.from(document.querySelectorAll('.servicequalityIndex .el-pagination .btn-next'))
                    .filter(el => el.offsetParent !== null);
                  const el = buttons[buttons.length - 1];
                  if (!el) return false;
                  return !(el.disabled || el.classList.contains('disabled'));
                }
                """
            )
        )
    except Exception:
        return False


def next_page(page) -> None:
    page.evaluate(
        """
        () => {
          const buttons = Array.from(document.querySelectorAll('.servicequalityIndex .el-pagination .btn-next'))
            .filter(el => el.offsetParent !== null);
          const el = buttons[buttons.length - 1];
          if (el) el.click();
        }
        """
    )
    page.wait_for_timeout(1200)


def collect_all_rows(page, logger: logging.Logger) -> List[Dict[str, str]]:
    if page_has_login_text(page):
        raise RuntimeError("页面显示登录失效，请重新扫码登录。")

    page.wait_for_selector(TABLE_ROW_SELECTOR, timeout=15000)
    first_page(page)

    all_rows: List[Dict[str, str]] = []
    seen_pages = 0
    while True:
        seen_pages += 1
        page_rows = read_page_rows(page)
        all_rows.extend(page_rows)
        if not has_next_page(page):
            break
        if seen_pages >= 100:
            raise RuntimeError("翻页超过 100 页，已停止，避免进入异常循环。")
        next_page(page)

    logger.info("读取完成：%s 页，%s 行", seen_pages, len(all_rows))
    return all_rows


def count_rows(rows: List[Dict[str, str]]) -> Counts:
    online = sum(1 for row in rows if row.get("onlineType") == "在线")
    offline = sum(1 for row in rows if row.get("onlineType") == "离线")
    idle = sum(1 for row in rows if row.get("callType") == "空闲")
    return Counts(total=len(rows), online=online, offline=offline, idle=idle)


def run_monitor(config_path: Path) -> None:
    base_dir = config_path.resolve().parent
    config = load_config(config_path)
    logger = setup_logger(base_dir)
    notifier = Notifier(config, logger)
    state = AlertState(config.get("thresholds", {}), notifier)

    browser_config = config.get("browser", {})
    user_data_dir = base_dir / browser_config.get("user_data_dir", "runtime/browser-profile")
    user_data_dir.mkdir(parents=True, exist_ok=True)
    interval = int(config.get("interval_seconds", 60))
    target_url = config["target_url"]

    logger.info("启动监控，检查间隔 %s 秒", interval)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=bool(browser_config.get("headless", False)),
            viewport={"width": 1440, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        wait_until_logged_in(page, int(browser_config.get("login_timeout_seconds", 180)), logger)

        try:
            while True:
                try:
                    rows = collect_all_rows(page, logger)
                    counts = count_rows(rows)
                    logger.info(
                        "统计结果：总人数=%s 在线=%s 离线=%s 空闲=%s",
                        counts.total,
                        counts.online,
                        counts.offline,
                        counts.idle,
                    )
                    state.evaluate(counts)
                except Exception as exc:
                    logger.exception("本轮检查失败：%s", exc)
                    notifier.send("监控检查失败", f"{exc}\n程序将在 {interval} 秒后重试。")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                time.sleep(interval)
        finally:
            context.close()


def run_discovery(config_path: Path) -> None:
    base_dir = config_path.resolve().parent
    config = load_config(config_path)
    logger = setup_logger(base_dir)
    target_url = config["target_url"]
    browser_config = config.get("browser", {})
    user_data_dir = base_dir / browser_config.get("user_data_dir", "runtime/browser-profile")
    user_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=False,
            viewport={"width": 1440, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        wait_until_logged_in(page, int(browser_config.get("login_timeout_seconds", 180)), logger)
        rows = collect_all_rows(page, logger)
        counts = count_rows(rows)

        output_dir = base_dir / "runtime" / "discovery"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = {
            "time": now_text(),
            "counts": counts.__dict__,
            "sample_rows": rows[:10],
            "notes": {
                "endpoint_from_static_js": "POST {GATEWAY.SER}/seatedStateRecord/page",
                "fields": {
                    "online": "onlineType",
                    "answer": "answerType",
                    "call": "callType",
                },
            },
        }
        output_file = output_dir / "discovery.json"
        output_file.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(output, ensure_ascii=False, indent=2))
        logger.info("勘察结果已保存：%s", output_file)
        context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="J&T 坐席实时状态监控")
    parser.add_argument("command", choices=["monitor", "discover"])
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path

    if args.command == "monitor":
        run_monitor(config_path)
    else:
        run_discovery(config_path)


if __name__ == "__main__":
    main()
