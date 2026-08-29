import json
import os
import queue
import threading
import time

os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Dict, Optional

from playwright.sync_api import sync_playwright

from monitor import (
    AlertState,
    Counts,
    Notifier,
    collect_all_rows,
    count_rows,
    load_config,
    now_text,
    setup_logger,
    wait_until_logged_in,
)


APP_TITLE = "J&T 坐席状态监控"
BG_COLOR = "#f5f7fb"
PANEL_COLOR = "#ffffff"
TEXT_COLOR = "#1f2937"
MUTED_COLOR = "#5f6b7a"
ENTRY_BG = "#ffffff"


class GuiNotifier(Notifier):
    def __init__(self, config: Dict[str, Any], logger, event_queue: queue.Queue) -> None:
        super().__init__(config, logger)
        self.event_queue = event_queue

    def send(self, title: str, text: str) -> None:
        self.event_queue.put(("alert", {"title": title, "text": text, "time": now_text()}))
        super().send(title, text)


class BrowserWorker(threading.Thread):
    def __init__(self, config_path: Path, event_queue: queue.Queue) -> None:
        super().__init__(daemon=True)
        self.config_path = config_path
        self.event_queue = event_queue
        self.command_queue: queue.Queue = queue.Queue()
        self.playwright = None
        self.context = None
        self.page = None
        self.logger = setup_logger(config_path.resolve().parent)
        self.running = True

    def command(self, name: str) -> None:
        self.command_queue.put(name)

    def run(self) -> None:
        self._emit_log("后台服务已启动")
        try:
            while self.running:
                command = self.command_queue.get()
                if command == "login":
                    self._login()
                elif command == "start":
                    self._monitor_loop()
                elif command == "stop":
                    self._emit_log("当前没有运行中的监控")
                elif command == "shutdown":
                    self.running = False
        except Exception as exc:
            self._emit_error(f"后台服务异常：{exc}")
        finally:
            self._close_browser()
            self._emit_log("后台服务已停止")

    def _emit_log(self, text: str) -> None:
        self.logger.info(text)
        self.event_queue.put(("log", {"time": now_text(), "text": text}))

    def _emit_error(self, text: str) -> None:
        self.logger.exception(text)
        self.event_queue.put(("error", {"time": now_text(), "text": text}))

    def _emit_counts(self, counts: Counts) -> None:
        self.event_queue.put(
            (
                "counts",
                {
                    "time": now_text(),
                    "total": counts.total,
                    "online": counts.online,
                    "offline": counts.offline,
                    "idle": counts.idle,
                },
            )
        )

    def _ensure_browser(self, config: Dict[str, Any]) -> None:
        if self.context and self.page:
            return
        base_dir = self.config_path.resolve().parent
        browser_config = config.get("browser", {})
        user_data_dir = base_dir / browser_config.get("user_data_dir", "runtime/browser-profile")
        user_data_dir.mkdir(parents=True, exist_ok=True)

        self._emit_log("正在打开浏览器，请在弹出的窗口完成扫码登录")
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
            login_timeout = int(config.get("browser", {}).get("login_timeout_seconds", 180))
            wait_until_logged_in(self.page, login_timeout, self.logger)
            rows = collect_all_rows(self.page, self.logger)
            counts = count_rows(rows)
            self._emit_counts(counts)
            self._emit_log("登录成功，已读取到坐席状态表格")
        except Exception as exc:
            self._emit_error(f"登录或读取失败：{exc}")

    def _monitor_loop(self) -> None:
        try:
            config = load_config(self.config_path)
            self._ensure_browser(config)
            login_timeout = int(config.get("browser", {}).get("login_timeout_seconds", 180))
            wait_until_logged_in(self.page, login_timeout, self.logger)

            notifier = GuiNotifier(config, self.logger, self.event_queue)
            state = AlertState(config.get("thresholds", {}), notifier)
            interval = int(config.get("interval_seconds", 60))
            self._emit_log(f"监控已启动，每 {interval} 秒检查一次")

            while True:
                if self._consume_stop_commands():
                    self._emit_log("监控已停止")
                    return

                try:
                    rows = collect_all_rows(self.page, self.logger)
                    counts = count_rows(rows)
                    self._emit_counts(counts)
                    state.evaluate(counts)
                    self._emit_log(
                        f"检查完成：总人数 {counts.total}，在线 {counts.online}，"
                        f"离线 {counts.offline}，通话空闲 {counts.idle}"
                    )
                except Exception as exc:
                    self._emit_error(f"本轮检查失败：{exc}")
                    notifier.send("监控检查失败", f"{exc}\n程序将在下一轮自动重试。")
                    try:
                        self.page.reload(wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass

                end_time = time.time() + interval
                while time.time() < end_time:
                    if self._consume_stop_commands():
                        self._emit_log("监控已停止")
                        return
                    time.sleep(0.3)
        except Exception as exc:
            self._emit_error(f"启动监控失败：{exc}")

    def _consume_stop_commands(self) -> bool:
        try:
            while True:
                command = self.command_queue.get_nowait()
                if command == "stop":
                    return True
                if command == "shutdown":
                    self.running = False
                    return True
                if command == "login":
                    self._emit_log("浏览器已经打开，无需重复登录")
                if command == "start":
                    self._emit_log("监控已经在运行")
        except queue.Empty:
            return not self.running

    def _close_browser(self) -> None:
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


class MonitorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("920x680")
        self.minsize(820, 600)
        self.configure(bg=BG_COLOR)

        self.base_dir = Path(__file__).resolve().parent
        self.config_path = self.base_dir / "config.json"
        self.config = self._load_or_create_config()
        self.event_queue: queue.Queue = queue.Queue()
        self.worker: Optional[BrowserWorker] = None

        self._setup_style()
        self._build_ui()
        self._load_form_values()
        self._poll_events()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_or_create_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            example_path = self.base_dir / "config.example.json"
            self.config_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
        return load_config(self.config_path)

    def _setup_style(self) -> None:
        self.option_add("*Font", "Arial 12")
        self.option_add("*foreground", TEXT_COLOR)
        self.option_add("*background", BG_COLOR)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG_COLOR, foreground=TEXT_COLOR, fieldbackground=ENTRY_BG)
        style.configure("TFrame", background=BG_COLOR)
        style.configure("Panel.TFrame", background=PANEL_COLOR)
        style.configure("TLabel", background=BG_COLOR, foreground=TEXT_COLOR)
        style.configure("Panel.TLabel", background=PANEL_COLOR, foreground=TEXT_COLOR)
        style.configure("Muted.TLabel", background=PANEL_COLOR, foreground=MUTED_COLOR)
        style.configure("Metric.TLabel", background=PANEL_COLOR, foreground=TEXT_COLOR, font=("Arial", 18, "bold"))
        style.configure("TLabelframe", background=PANEL_COLOR, foreground=TEXT_COLOR, bordercolor="#d7dde8")
        style.configure("TLabelframe.Label", background=PANEL_COLOR, foreground=TEXT_COLOR, font=("Arial", 12, "bold"))
        style.configure("TEntry", fieldbackground=ENTRY_BG, foreground=TEXT_COLOR, insertcolor=TEXT_COLOR)
        style.configure("TButton", background="#edf2f7", foreground=TEXT_COLOR, padding=(12, 6))
        style.map("TButton", background=[("active", "#dde7f2"), ("pressed", "#cfdbea")])

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        form = ttk.LabelFrame(self, text="配置")
        form.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        self.webhook_var = tk.StringVar()
        self.secret_var = tk.StringVar()
        self.online_var = tk.StringVar()
        self.offline_var = tk.StringVar()
        self.idle_var = tk.StringVar()
        self.interval_var = tk.StringVar()

        self._field(form, "飞书 Webhook", self.webhook_var, 0, 0, colspan=3)
        self._field(form, "飞书 Secret", self.secret_var, 1, 0, colspan=3, show="*")
        self._field(form, "在线提醒阈值", self.online_var, 2, 0)
        self._field(form, "离线异常阈值", self.offline_var, 2, 2)
        self._field(form, "空闲异常阈值", self.idle_var, 3, 0)
        self._field(form, "检查频率/秒", self.interval_var, 3, 2)

        buttons = ttk.Frame(self, style="TFrame")
        buttons.grid(row=1, column=0, padx=16, pady=8, sticky="ew")
        self.save_button = ttk.Button(buttons, text="保存配置", command=self._save_config)
        self.login_button = ttk.Button(buttons, text="登录极兔系统", command=self._login)
        self.start_button = ttk.Button(buttons, text="开始监控", command=self._start_monitor)
        self.stop_button = ttk.Button(buttons, text="停止监控", command=self._stop_monitor)
        self.save_button.pack(side="left", padx=(0, 8))
        self.login_button.pack(side="left", padx=(0, 8))
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button.pack(side="left")

        main = ttk.Frame(self, style="TFrame")
        main.grid(row=2, column=0, padx=16, pady=(8, 16), sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(1, weight=1)

        status = ttk.LabelFrame(main, text="当前状态")
        status.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        for index in range(8):
            status.columnconfigure(index, weight=1)

        self.total_value = self._metric(status, "总人数", 0)
        self.online_value = self._metric(status, "在线", 1)
        self.offline_value = self._metric(status, "离线", 2)
        self.idle_value = self._metric(status, "通话空闲", 3)
        self.last_check_value = self._metric(status, "最近检查", 4, width=18)

        alerts_frame = ttk.LabelFrame(main, text="提醒记录")
        alerts_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        alerts_frame.rowconfigure(0, weight=1)
        alerts_frame.columnconfigure(0, weight=1)
        self.alerts = tk.Text(
            alerts_frame,
            height=12,
            wrap="word",
            bg=PANEL_COLOR,
            fg=TEXT_COLOR,
            insertbackground=TEXT_COLOR,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#d7dde8",
        )
        self.alerts.grid(row=0, column=0, sticky="nsew")

        logs_frame = ttk.LabelFrame(main, text="运行日志")
        logs_frame.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
        logs_frame.rowconfigure(0, weight=1)
        logs_frame.columnconfigure(0, weight=1)
        self.logs = tk.Text(
            logs_frame,
            height=12,
            wrap="word",
            bg=PANEL_COLOR,
            fg=TEXT_COLOR,
            insertbackground=TEXT_COLOR,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#d7dde8",
        )
        self.logs.grid(row=0, column=0, sticky="nsew")

    def _field(
        self,
        parent,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
        colspan: int = 1,
        show: Optional[str] = None,
    ) -> None:
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(row=row, column=column, padx=10, pady=8, sticky="w")
        entry = ttk.Entry(parent, textvariable=variable, show=show)
        entry.grid(row=row, column=column + 1, columnspan=colspan, padx=10, pady=8, sticky="ew")

    def _metric(self, parent, label: str, column: int, width: int = 10) -> tk.StringVar:
        variable = tk.StringVar(value="-")
        box = ttk.Frame(parent, style="Panel.TFrame")
        box.grid(row=0, column=column, padx=10, pady=10, sticky="ew")
        ttk.Label(box, text=label, style="Muted.TLabel").pack(anchor="w")
        ttk.Label(box, textvariable=variable, style="Metric.TLabel", width=width).pack(anchor="w")
        return variable

    def _load_form_values(self) -> None:
        notify = self.config.get("notify", {})
        thresholds = self.config.get("thresholds", {})
        self.webhook_var.set(notify.get("feishu_webhook", ""))
        self.secret_var.set(notify.get("feishu_secret", ""))
        self.online_var.set(str(thresholds.get("online", 5)))
        self.offline_var.set(str(thresholds.get("offline", 5)))
        self.idle_var.set(str(thresholds.get("idle", 2)))
        self.interval_var.set(str(self.config.get("interval_seconds", 60)))

    def _save_config(self) -> None:
        try:
            self.config.setdefault("notify", {})
            self.config.setdefault("thresholds", {})
            self.config["notify"]["feishu_webhook"] = self.webhook_var.get().strip()
            self.config["notify"]["feishu_secret"] = self.secret_var.get().strip()
            self.config["notify"]["print_to_console"] = True
            self.config["thresholds"]["online"] = int(self.online_var.get())
            self.config["thresholds"]["offline"] = int(self.offline_var.get())
            self.config["thresholds"]["idle"] = int(self.idle_var.get())
            self.config["interval_seconds"] = int(self.interval_var.get())
            self.config_path.write_text(
                json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._append_log("配置已保存")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"保存配置失败：{exc}")

    def _ensure_worker(self) -> BrowserWorker:
        if not self.worker or not self.worker.is_alive():
            self.worker = BrowserWorker(self.config_path, self.event_queue)
            self.worker.start()
        return self.worker

    def _login(self) -> None:
        self._save_config()
        self._ensure_worker().command("login")
        self._append_log("已请求打开极兔系统登录页")

    def _start_monitor(self) -> None:
        self._save_config()
        self._ensure_worker().command("start")
        self._append_log("已请求开始监控")

    def _stop_monitor(self) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.command("stop")
            self._append_log("已请求停止监控")

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.event_queue.get_nowait()
                if event == "counts":
                    self.total_value.set(str(payload["total"]))
                    self.online_value.set(str(payload["online"]))
                    self.offline_value.set(str(payload["offline"]))
                    self.idle_value.set(str(payload["idle"]))
                    self.last_check_value.set(payload["time"])
                elif event == "alert":
                    self._append_alert(f"{payload['time']}  {payload['title']}\n{payload['text']}\n")
                elif event == "error":
                    self._append_log(f"{payload['time']}  {payload['text']}")
                    self._append_alert(f"{payload['time']}  程序异常\n{payload['text']}\n")
                elif event == "log":
                    self._append_log(f"{payload['time']}  {payload['text']}")
        except queue.Empty:
            pass
        self.after(300, self._poll_events)

    def _append_alert(self, text: str) -> None:
        self.alerts.insert("end", text + "\n")
        self.alerts.see("end")

    def _append_log(self, text: str) -> None:
        self.logs.insert("end", text + "\n")
        self.logs.see("end")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.command("shutdown")
        self.destroy()


def main() -> None:
    app = MonitorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
