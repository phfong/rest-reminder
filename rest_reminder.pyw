#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
健康休息提醒程序
每45分钟提醒起来活动，显示5分钟倒计时
检测真正的锁屏/解锁动作，自动重置工作时间
"""
import tkinter as tk
from tkinter import ttk
import threading
import time
import sys
import os
import json
import ctypes
import math
import tempfile

from PIL import Image, ImageDraw, ImageFont, ImageTk

WORK_MINUTES = 45
REST_MINUTES = 5

# 配置文件路径（与脚本同目录）
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rest_reminder_config.json")


def load_config():
    """从配置文件加载设置"""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg.get("work_minutes", WORK_MINUTES), cfg.get("rest_minutes", REST_MINUTES)
    except Exception:
        pass
    return WORK_MINUTES, REST_MINUTES


def save_config(work_minutes, rest_minutes):
    """保存设置到配置文件"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"work_minutes": work_minutes, "rest_minutes": rest_minutes}, f)
    except Exception:
        pass


def is_workstation_locked():
    """检测工作站是否处于锁定状态（真正的锁屏检测）
    当用户锁定屏幕（Win+L）后，桌面切换到安全桌面，
    OpenInputDesktop 会失败，以此判断是否锁屏。
    """
    # DESKTOP_READOBJECTS = 0x0001
    hDesktop = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0001)
    if not hDesktop:
        return True  # 无法打开输入桌面 → 已锁定
    ctypes.windll.user32.CloseDesktop(hDesktop)
    return False  # 可以打开输入桌面 → 未锁定

# 防止重复启动：通过 socket 端口检查
SINGLETON_PORT = 50999


def _ensure_single_instance():
    """确保只有一个实例在运行：使用命名互斥量（Windows 下可靠方案）。
    原实现用 socket + SO_REUSEADDR 判重，在 Windows 上 SO_REUSEADDR 允许
    重复绑定同一端口，导致多开失效；这里改为互斥量。
    若已有实例，则向其监听端口发送 SHOW 以激活其窗口，然后本实例退出。
    """
    import socket
    kernel32 = ctypes.windll.kernel32
    MUTEX_NAME = "Local\\RestReminder_Singleton_Mutex"
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS → 已有实例
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(('127.0.0.1', SINGLETON_PORT))
            s.send(b'SHOW')
            s.close()
        except Exception:
            pass
        print("程序已在运行中，激活已有窗口后退出。")
        sys.exit(0)
    # 首次实例：绑定监听端口，供后续实例发 SHOW 唤醒本实例
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # 不要用 SO_REUSEADDR，否则 Windows 允许重复绑定导致单实例失效
        sock.bind(('127.0.0.1', SINGLETON_PORT))
        sock.listen(1)
        return sock
    except OSError:
        # 端口暂不可用（如前实例刚退出仍在释放）不影响本实例运行
        return None


class RestReminderApp:
    def __init__(self):
        # 确保 tkinter 只创建一个根窗口
        self.root = tk.Tk()
        self.root.withdraw()  # 先隐藏，初始化完成后再显示

        self.root.title("健康休息提醒")
        self.root.geometry("420x410")
        self.root.resizable(False, False)

        self.center_window()

        # 工作时不置顶，可以放到后台
        self.root.attributes('-topmost', False)

        # 状态变量 — 只在主线程读取和修改
        self.is_resting = False
        self.countdown_seconds = 0
        self.work_minutes, self.rest_minutes = load_config()
        self.work_seconds = self.work_minutes * 60
        self.running = True
        self._was_locked = False  # 用于检测从锁屏恢复
        self._last_taskbar_key = None  # 避免重复生成相同图标

        # 字体和颜色
        self.title_font = ("Microsoft YaHei", 18, "bold")
        self.timer_font = ("Consolas", 48, "bold")
        self.btn_font = ("Microsoft YaHei", 11, "bold")
        self.status_font = ("Microsoft YaHei", 13, "bold")

        self.work_bg = "#e8f5e9"
        self.rest_bg = "#fff3e0"
        self.work_fg = "#2e7d32"
        self.rest_fg = "#e65100"
        self.btn_rest_color = "#1565c0"
        self.btn_skip_color = "#4a4a4a"
        self.btn_reset_color = "#455a64"
        self.text_color = "#333333"

        self.build_ui()
        self.update_display()

        # 后台线程只负责倒计时，不碰UI
        self.timer_thread = threading.Thread(target=self._timer_loop, daemon=True)
        self.timer_thread.start()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 初始化完成后显示窗口
        self.root.deiconify()

        # 先初始化监听 socket 占位，main() 里再绑定实际 socket
        self._sock = None
        # 启动单实例监听（接收其他实例的激活请求）
        self._start_singleton_listener()

    def _start_singleton_listener(self):
        """监听来自新实例的激活请求"""
        import select
        def _listen():
            while self.running:
                sock = self._sock
                if not sock:
                    # main() 会在实例化后绑定实际 socket，先稍等
                    time.sleep(0.1)
                    continue
                try:
                    ready, _, _ = select.select([sock], [], [], 0.5)
                    if ready:
                        conn, _ = sock.accept()
                        data = conn.recv(1024)
                        conn.close()
                        if data == b'SHOW':
                            self.root.after(0, self._flash_taskbar)
                except Exception:
                    time.sleep(0.2)
        self._listen_thread = threading.Thread(target=_listen, daemon=True)
        self._listen_thread.start()

    def _get_top_hwnd(self):
        """从 winfo_id 向上找到真正的顶层窗口句柄（任务栏按钮对应的窗口）"""
        try:
            user32 = ctypes.windll.user32
            hwnd = self.root.winfo_id()
            if not hwnd:
                return 0
            for _ in range(8):
                parent = user32.GetParent(hwnd)
                if not parent:
                    break
                hwnd = parent
            return hwnd
        except Exception:
            return 0

    def _update_taskbar_icon(self, mins, is_resting, force=False, cycle_button=True, cycle_force=False):
        """用 PIL 生成带剩余分钟数的任务栏图标，并通过 Tk iconphoto 刷新
        force=True 时跳过 key 缓存，强制重新生成（用于自愈 / 模式切换）
        cycle_button=True 时额外通过“更新类图标 + 隐藏/非激活重显”强制任务栏按钮
        重建重绘 —— 这是修复 Win10/11 后台窗口任务栏图标不刷新的关键。
        cycle_force=True 时跳过按钮重建的去抖（仅模式切换主刷新使用）。"""
        key = (mins, is_resting)
        if not force and key == self._last_taskbar_key:
            return  # 和上次一样，无需重复生成

        bg_color = "#ff9800" if is_resting else "#4caf50"  # 橙色/绿色
        size = 256
        img = Image.new("RGB", (size, size), bg_color)
        draw = ImageDraw.Draw(img)

        # 画白色分钟数字
        text = str(mins)
        try:
            font = ImageFont.truetype("seguiemj.ttf", 144)
        except Exception:
            try:
                font = ImageFont.truetype("segoeui.ttf", 144)
            except Exception:
                font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = (size - tw) // 2 - bbox[0]
        ty = (size - th) // 2 - bbox[1]
        draw.text((tx, ty), text, fill="white", font=font)

        img16 = img.resize((16, 16), Image.LANCZOS)
        img32 = img.resize((32, 32), Image.LANCZOS)

        try:
            photo_small = ImageTk.PhotoImage(img16)
            photo_big = ImageTk.PhotoImage(img32)
            self._icon_ref = (photo_small, photo_big)
            self.root.iconphoto(False, photo_small, photo_big)
            self.root.update_idletasks()
            # 通知资源管理器任务栏按钮需要重绘（配合上面办法增强效果）
            self._force_taskbar_redraw()
            self._last_taskbar_key = key
            if cycle_button:
                self._refresh_taskbar_button(img, force=cycle_force)
        except Exception:
            pass

    def _refresh_taskbar_button(self, img, force=False):
        """Win10/11 修复：任务栏按钮图标在窗口位于后台/未激活时不会自动刷新。
        原理：Windows 资源管理器会缓存任务栏按钮图标，只有按钮被“重建”才会重读。
        做法：
          1) 把新图标同时写进窗口类图标（SetClassLongPtr GCLP_HICON / HICONSM）；
          2) 隐藏窗口（SW_HIDE）后再以“不激活”方式重新显示
             （SW_SHOWNOACTIVATE；若已最小化则用 SW_SHOWMINNOACTIVE 保持最小化），
             从而强制资源管理器重建任务栏按钮并重读图标。
        关键：SW_SHOWNOACTIVATE 不会抢走用户当前窗口的焦点。
        force=True 时跳过 1.5 秒去抖（用于模式切换的主刷新）；
        去抖用于吸收切换瞬间 update_display 等路径的重复触发，避免图标刷新多次。
        """
        try:
            now = time.time()
            if not force and hasattr(self, '_last_button_cycle_ts') \
                    and now - self._last_button_cycle_ts < 1.5:
                return  # 去抖：1.5 秒内不重复隐藏/重显
            user32 = ctypes.windll.user32
            hwnd = self._get_top_hwnd()
            if not hwnd:
                return
            # 把 PIL 图像存成临时 .ico 再加载为 HICON（稳定可靠）
            ico_path = os.path.join(tempfile.gettempdir(), "rest_reminder_taskbar.ico")
            img.save(ico_path, format="ICO", sizes=[(256, 256), (48, 48), (32, 32), (16, 16)])
            GCLP_HICON = -14
            GCLP_HICONSM = -34
            LR_LOADFROMFILE = 0x0010
            hbig = user32.LoadImageW(None, ico_path, 1, 256, 256, LR_LOADFROMFILE)
            hsmall = user32.LoadImageW(None, ico_path, 1, 32, 32, LR_LOADFROMFILE)
            if hbig:
                user32.SetClassLongPtrW(hwnd, GCLP_HICON, hbig)
            if hsmall:
                user32.SetClassLongPtrW(hwnd, GCLP_HICONSM, hsmall)
            # 隐藏 + 重新显示（重建任务栏按钮）
            user32.ShowWindow(hwnd, 0)  # SW_HIDE
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 7)  # SW_SHOWMINNOACTIVE：保持最小化
            else:
                user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE：不抢焦点
            self._last_button_cycle_ts = now  # 只有真正重建成功才更新时间戳
        except Exception:
            pass

    def _force_taskbar_redraw(self):
        """通过 SetWindowPos(SWP_FRAMECHANGED) 强制资源管理器刷新任务栏按钮图标。"""
        try:
            user32 = ctypes.windll.user32
            hwnd = self._get_top_hwnd()
            if not hwnd:
                return
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            SWP_FRAMECHANGED = 0x0020
            user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            )
        except Exception:
            pass

    def _refresh_taskbar_now(self, cycle_button=True, cycle_force=False):
        """立刻用当前状态强制刷新一次任务栏图标（用于模式切换后的多重试）
        cycle_button=True 表示重建任务栏按钮（隐藏/重显）；
        模式切换的重试应传 cycle_button=False，只做静默兜底重绘，避免重复刷新。"""
        if not self.running:
            return
        if self.is_resting:
            icon_mins = math.ceil(self.countdown_seconds / 60)
        else:
            icon_mins = math.ceil(self.work_seconds / 60)
        self._update_taskbar_icon(icon_mins, self.is_resting, force=True,
                                  cycle_button=cycle_button, cycle_force=cycle_force)

    def center_window(self):
        self.root.update_idletasks()
        w, h = 420, 410
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def build_ui(self):
        self.main_frame = tk.Frame(self.root, bg=self.work_bg)
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        self.title_label = tk.Label(
            self.main_frame, text="工作中...",
            font=self.title_font, bg=self.work_bg, fg=self.work_fg
        )
        self.title_label.pack(pady=(25, 10))

        self.timer_label = tk.Label(
            self.main_frame, text="45:00",
            font=self.timer_font, bg=self.work_bg, fg=self.work_fg
        )
        self.timer_label.pack(pady=(5, 10))

        # 进度条
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("green.Horizontal.TProgressbar",
                        troughcolor="#c8e6c9", background="#4caf50",
                        lightcolor="#4caf50", darkcolor="#4caf50")
        style.configure("orange.Horizontal.TProgressbar",
                        troughcolor="#ffe0b2", background="#ff9800",
                        lightcolor="#ff9800", darkcolor="#ff9800")

        self.progress = ttk.Progressbar(
            self.main_frame, style="green.Horizontal.TProgressbar",
            length=350, mode='determinate'
        )
        self.progress.pack(pady=(5, 15))
        self.progress['maximum'] = self.work_seconds
        self.progress['value'] = self.work_seconds

        self.status_label = tk.Label(
            self.main_frame, text="",
            font=self.status_font, bg=self.work_bg, fg=self.text_color
        )
        self.status_label.pack(pady=(0, 10))

        # ---- 设置行 ----
        setting_frame = tk.Frame(self.main_frame, bg=self.work_bg)
        setting_frame.pack(pady=(0, 10))

        vcmd = (self.root.register(self._validate_minutes), '%P')

        tk.Label(setting_frame, text="工作：", font=("Microsoft YaHei", 10),
                 bg=self.work_bg, fg=self.text_color).pack(side=tk.LEFT)

        self.work_entry = tk.Entry(setting_frame, font=("Microsoft YaHei", 10),
                                   width=4, justify=tk.CENTER, validate='key',
                                   validatecommand=vcmd)
        self.work_entry.insert(0, str(self.work_minutes))
        self.work_entry.pack(side=tk.LEFT)

        tk.Label(setting_frame, text="分钟    休息：", font=("Microsoft YaHei", 10),
                 bg=self.work_bg, fg=self.text_color).pack(side=tk.LEFT)

        self.rest_entry = tk.Entry(setting_frame, font=("Microsoft YaHei", 10),
                                   width=4, justify=tk.CENTER, validate='key',
                                   validatecommand=vcmd)
        self.rest_entry.insert(0, str(self.rest_minutes))
        self.rest_entry.pack(side=tk.LEFT)

        tk.Label(setting_frame, text="分钟", font=("Microsoft YaHei", 10),
                 bg=self.work_bg, fg=self.text_color).pack(side=tk.LEFT)

        self.apply_btn = tk.Button(
            setting_frame, text="应用", font=("Microsoft YaHei", 9, "bold"),
            bg="#43a047", fg="white", activebackground="#2e7d32",
            activeforeground="white", relief=tk.FLAT,
            padx=12, pady=3, cursor="hand2",
            command=self.apply_settings
        )
        self.apply_btn.pack(side=tk.LEFT, padx=(10, 0))

        # ---- 按钮行 ----
        btn_frame = tk.Frame(self.main_frame, bg=self.work_bg)
        btn_frame.pack(pady=(5, 20))

        self.rest_now_btn = tk.Button(
            btn_frame, text="立刻休息", font=self.btn_font,
            bg=self.btn_rest_color, fg="white",
            activebackground="#0d47a1", activeforeground="white",
            disabledforeground="#90caf9", relief=tk.FLAT,
            padx=20, pady=8, cursor="hand2",
            command=self.start_rest
        )
        self.rest_now_btn.pack(side=tk.LEFT, padx=8)

        self.skip_btn = tk.Button(
            btn_frame, text="跳过休息", font=self.btn_font,
            bg=self.btn_skip_color, fg="white",
            activebackground="#333333", activeforeground="white",
            disabledforeground="#9e9e9e", relief=tk.FLAT,
            padx=20, pady=8, cursor="hand2",
            command=self.skip_rest, state=tk.DISABLED
        )
        self.skip_btn.pack(side=tk.LEFT, padx=8)

        self.reset_btn = tk.Button(
            btn_frame, text="重置计时", font=self.btn_font,
            bg=self.btn_reset_color, fg="white",
            activebackground="#263238", activeforeground="white",
            disabledforeground="#90a4ae", relief=tk.FLAT,
            padx=20, pady=8, cursor="hand2",
            command=self.reset_timer
        )
        self.reset_btn.pack(side=tk.LEFT, padx=8)

        # 保存 btn_frame 引用以便变色
        self.btn_frame = btn_frame

    def update_display(self):
        """更新界面（只在主线程调用）"""
        if not self.running:
            return

        if self.is_resting:
            mins, secs = divmod(self.countdown_seconds, 60)
            timer_text = f"{mins:02d}:{secs:02d}"
            self.timer_label.config(text=timer_text)
            self.progress['value'] = self.countdown_seconds
            # 只在休息时显示提示，平时留空
            if self.status_label.cget("text") == "" or "剩余" in self.status_label.cget("text"):
                self.status_label.config(
                    text=f"站起来活动一下吧！剩余 {timer_text}"
                )
            icon_mins = math.ceil(self.countdown_seconds / 60)
            # 休息期间不做隐藏/重显刷新（cycle_button=False），避免窗口再次弹出
            self._update_taskbar_icon(icon_mins, True, cycle_button=False)
        else:
            mins, secs = divmod(self.work_seconds, 60)
            timer_text = f"{mins:02d}:{secs:02d}"
            self.timer_label.config(text=timer_text)
            self.progress['value'] = self.work_seconds
            icon_mins = math.ceil(self.work_seconds / 60)
            self._update_taskbar_icon(icon_mins, False)

        # 自愈：每 20 秒重设一次图标，绕过 key 缓存（但不再隐藏重显，避免频繁闪动）
        now = time.time()
        if not hasattr(self, '_last_selfheal') or now - self._last_selfheal > 20:
            self._last_selfheal = now
            self._update_taskbar_icon(icon_mins, self.is_resting, force=True, cycle_button=False)

        # 每200ms刷新一次
        self._display_job = self.root.after(200, self.update_display)

    def _timer_loop(self):
        """后台线程：每秒触发主线程 tick，同时检测锁屏/解锁"""
        while self.running:
            time.sleep(1)
            if not self.running:
                break
            self.root.after(0, self._tick)

    def _tick(self):
        """主线程中执行：每秒倒计时 + 锁屏恢复检测"""
        if not self.running:
            return

        # ---- 真正锁屏/解锁检测 ----
        locked = is_workstation_locked()
        if locked:
            self._was_locked = True
        elif self._was_locked:
            # 从锁定 → 解锁，说明用户主动解锁回来了
            self._was_locked = False
            self._on_wake_from_lock()

        if self.is_resting:
            self.countdown_seconds -= 1
            if self.countdown_seconds <= 0:
                self._do_end_rest()
        else:
            self.work_seconds -= 1
            if self.work_seconds <= 0:
                self._do_start_rest()

    def _on_wake_from_lock(self):
        """检测到锁屏后解锁，重置工作时间"""
        self.is_resting = False
        self.work_seconds = self.work_minutes * 60
        self._switch_to_work_ui()
        self.status_label.config(text="欢迎回来！工作时间已重置")

    def _do_start_rest(self):
        """在主线程中切换到休息模式"""
        if self.is_resting:
            return
        self.is_resting = True
        self.countdown_seconds = self.rest_minutes * 60
        self._switch_to_rest_ui()
        self._notify_rest_start()

    def _do_end_rest(self):
        """在主线程中切换到工作模式"""
        self.is_resting = False
        self.work_seconds = self.work_minutes * 60
        self._switch_to_work_ui()

    # ---- UI 切换方法 ----
    def _switch_to_rest_ui(self):
        color = self.rest_bg
        fg_color = self.rest_fg
        self.main_frame.config(bg=color)
        self.title_label.config(text="该活动了！", bg=color, fg=fg_color)
        self.timer_label.config(bg=color, fg=fg_color)
        self.status_label.config(bg=color, fg=fg_color)
        self.progress.configure(style="orange.Horizontal.TProgressbar")
        self.progress['maximum'] = self.rest_minutes * 60
        self.progress['value'] = self.rest_minutes * 60
        self.rest_now_btn.config(state=tk.DISABLED, text="休息中...")
        self.skip_btn.config(state=tk.NORMAL)
        self.btn_frame.config(bg=color)
        self._last_taskbar_key = None  # 模式切换后强制刷新图标
        # 模式切换：0ms 重建任务栏按钮一次（force 绕过去抖）；
        # 500ms / 2000ms 只做静默兜底重绘（不重建按钮），避免图标刷新多次
        self.root.after(0, lambda: self._refresh_taskbar_now(True, True))
        self.root.after(500, lambda: self._refresh_taskbar_now(False))
        self.root.after(2000, lambda: self._refresh_taskbar_now(False))

    def _switch_to_work_ui(self):
        color = self.work_bg
        fg_color = self.work_fg
        self.main_frame.config(bg=color)
        self.title_label.config(text="工作中...", bg=color, fg=fg_color)
        self.timer_label.config(text="", bg=color, fg=fg_color)
        self.status_label.config(text="", bg=color, fg=fg_color)
        self.progress.configure(style="green.Horizontal.TProgressbar")
        self.progress['maximum'] = self.work_seconds
        self.progress['value'] = self.work_seconds
        self.rest_now_btn.config(state=tk.NORMAL, text="立刻休息")
        self.skip_btn.config(state=tk.DISABLED)
        self.btn_frame.config(bg=color)
        self.root.attributes('-topmost', False)
        self._last_taskbar_key = None  # 模式切换后强制刷新图标
        # 模式切换：0ms 重建任务栏按钮一次（force 绕过去抖）；
        # 500ms / 2000ms 只做静默兜底重绘（不重建按钮），避免图标刷新多次
        self.root.after(0, lambda: self._refresh_taskbar_now(True, True))
        self.root.after(500, lambda: self._refresh_taskbar_now(False))
        self.root.after(2000, lambda: self._refresh_taskbar_now(False))

    def _notify_rest_start(self):
        """休息开始：短暂置顶引起注意，2秒后回落到普通层并恢复原前台窗口。

        置顶期间窗口会成为前台窗口（这是让窗口出现在最前面的必要条件），
        2秒后取消置顶并用 SetForegroundWindow 把前台交还给之前的窗口，
        用户可立即操作原窗口，无需先点击提醒窗口。
        """
        try:
            user32 = ctypes.windll.user32
            # 记住当前前台窗口，2秒后恢复它
            prev_fg = user32.GetForegroundWindow()
            self.root.deiconify()
            self.root.attributes('-topmost', True)
            self.root.lift()
            self.root.after(2000, lambda: self._clear_topmost(prev_fg))
        except Exception:
            pass

    def _clear_topmost(self, prev_fg=0):
        """取消置顶，回到普通层，恢复之前的前台窗口。

        关键：不用 SW_HIDE→SW_SHOWNOACTIVATE（会造成窗口闪现再次弹出），
        而是直接取消 topmost 标志，然后把前台交还给置顶前的窗口。
        """
        try:
            self.root.attributes('-topmost', False)
            self.root.update_idletasks()  # 确保 topmost 已实际移除
            user32 = ctypes.windll.user32
            if prev_fg and user32.IsWindow(prev_fg):
                user32.SetForegroundWindow(prev_fg)
        except Exception:
            try:
                self.root.attributes('-topmost', False)
            except Exception:
                pass

    def _flash_taskbar(self):
        """闪烁任务栏按钮引起注意（不弹出窗口、不抢焦点），用于重复启动唤醒旧实例"""
        try:
            hwnd = self._get_top_hwnd()
            if not hwnd:
                return

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint),
                            ("hwnd", ctypes.c_void_p),
                            ("dwFlags", ctypes.c_uint),
                            ("uCount", ctypes.c_uint),
                            ("dwTimeout", ctypes.c_uint)]
            FLASHW_ALL = 0x00000003        # 同时闪烁标题栏与任务栏按钮
            FLASHW_TIMERNOFG = 0x0000000C  # 持续闪烁直到窗口获得焦点
            fwi = FLASHWINFO()
            fwi.cbSize = ctypes.sizeof(fwi)
            fwi.hwnd = hwnd
            fwi.dwFlags = FLASHW_ALL | FLASHW_TIMERNOFG
            fwi.uCount = 5
            fwi.dwTimeout = 0
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(fwi))
        except Exception:
            pass

    # ---- 用户操作 ----
    def start_rest(self):
        """用户手动开始休息"""
        self._do_start_rest()

    def skip_rest(self):
        """跳过休息"""
        self._do_end_rest()

    def reset_timer(self):
        """重置当前状态的倒计时"""
        if self.is_resting:
            self.countdown_seconds = self.rest_minutes * 60
            self._switch_to_rest_ui()
        else:
            self.work_seconds = self.work_minutes * 60
            self._switch_to_work_ui()

    def _validate_minutes(self, value):
        """验证输入框只允许数字"""
        if value == "":
            return True
        return value.isdigit() and len(value) <= 3

    def apply_settings(self):
        """应用用户设置的工作和休息时间"""
        try:
            work_mins = int(self.work_entry.get())
            if work_mins < 1:
                work_mins = 1
            elif work_mins > 999:
                work_mins = 999
            rest_mins = int(self.rest_entry.get())
            if rest_mins < 1:
                rest_mins = 1
            elif rest_mins > 999:
                rest_mins = 999
        except ValueError:
            return

        self.work_minutes = work_mins
        self.rest_minutes = rest_mins

        # 更新输入框显示
        self.work_entry.delete(0, tk.END)
        self.work_entry.insert(0, str(work_mins))
        self.rest_entry.delete(0, tk.END)
        self.rest_entry.insert(0, str(rest_mins))

        save_config(work_mins, rest_mins)  # 持久化保存

        if self.is_resting:
            self.countdown_seconds = rest_mins * 60
            self._switch_to_rest_ui()
            self.status_label.config(text=f"休息时间已设为 {rest_mins} 分钟")
        else:
            self.work_seconds = work_mins * 60
            self._switch_to_work_ui()
            self.status_label.config(text=f"工作时间 {work_mins} 分钟 | 休息时间 {rest_mins} 分钟")

    def on_close(self):
        """关闭程序"""
        self.running = False
        try:
            self.root.after_cancel(self._display_job)
        except Exception:
            pass
        # 释放单实例锁
        if hasattr(self, '_sock') and self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        # 清理临时图标文件
        try:
            ico_path = os.path.join(tempfile.gettempdir(), "rest_reminder_taskbar.ico")
            if os.path.exists(ico_path):
                os.remove(ico_path)
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    _singleton_sock = _ensure_single_instance()
    app = RestReminderApp()
    app._sock = _singleton_sock  # 保存 socket 引用，关闭时释放
    app.run()


if __name__ == "__main__":
    main()
