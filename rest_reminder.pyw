#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""健康休息提醒程序（v2）

相对旧版的改动：

1. 任务栏图标数字刷新
   - 修正 ctypes 句柄类型：旧版 LoadImageW / GetParent / SetClassLongPtrW 没声明
     restype/argtypes，64 位 Python 下句柄会被截成 32 位，调用时好时坏（刷新“不灵”的主因之一）；
   - 旧 HICON 会 DestroyIcon 释放，不再每次刷新泄漏 2 个句柄；
   - 只在窗口“最小化”时做隐藏/重显来重建任务栏按钮（Win11 的 Explorer 不认 WM_SETICON
     的实时更新），窗口可见时只走 WM_SETICON —— 旧版在休息期间为防弹窗把刷新整个关掉了，
     所以休息倒计时那几分钟数字是冻住的；现在两种模式都会刷新，
     同时不会再出现“休息时窗口重新弹到前面”的老问题。
2. 忙着的时候隐藏弹窗
   - 新增“隐藏”按钮 + Esc：最小化到任务栏，工作/休息计时继续，任务栏数字继续走；
   - 提醒弹出 3 秒后若没人碰它，自动收起到任务栏（界面可勾选关闭）；
   - 弹出后 120ms 就把前台交还给原来的窗口，不再占用键盘焦点 2 秒。
3. 计时
   - 去掉“后台线程 sleep(1) + root.after(0)”的跨线程写法，改成主线程 250ms 的
     monotonic 时钟循环，不漂移，也不会在退出时和线程打架；
   - 锁屏期间暂停计时，解锁且离开超过 10 秒才重置（UAC 安全桌面不会误重置）；
   - 系统休眠/挂起超过 2 分钟按“离开”处理，重置工作计时。
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import select
import socket
import sys
import tempfile
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk

from PIL import Image, ImageDraw, ImageFont

app_name = "健康休息提醒"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
WORK_MINUTES = 45
REST_MINUTES = 5

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rest_reminder_config.json")

WIN_W, WIN_H = 440, 366
TICK_MS = 250                 # 主循环间隔
LOCK_POLL_INTERVAL = 1.0      # 锁屏检测间隔（秒）
LOCK_MIN_AWAY = 10.0          # 锁屏短于这个时长视为误报（UAC 等），不重置
SLEEP_GAP = 120.0             # 两次循环间隔超过这个值视为系统休眠

SINGLETON_PORT = 50999
MUTEX_NAME = "Local\\RestReminder_Singleton_Mutex"

ICON_WORK_BG = "#4caf50"
ICON_WORK_EDGE = "#1b5e20"
ICON_REST_BG = "#ff9800"
ICON_REST_EDGE = "#bf360c"
ICO_SIZES = [(256, 256), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]

GA_ROOT = 2
WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
GCLP_HICON = -14
GCLP_HICONSM = -34
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SW_HIDE = 0
SW_SHOWMINNOACTIVE = 7

ERROR_ALREADY_EXISTS = 183

DEFAULT_CONFIG = {
    "work_minutes": WORK_MINUTES,
    "rest_minutes": REST_MINUTES,
    "auto_hide": True,
}

# ---------------------------------------------------------------------------
# Win32 绑定：必须声明 restype / argtypes，否则 64 位下句柄会被 ctypes 截断
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.ShowWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = (wintypes.HWND,)
user32.IsIconic.restype = wintypes.BOOL
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindow.restype = wintypes.BOOL
user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.LoadImageW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT)
user32.LoadImageW.restype = wintypes.HANDLE
user32.DestroyIcon.argtypes = (wintypes.HICON,)
user32.DestroyIcon.restype = wintypes.BOOL
user32.OpenInputDesktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.CloseDesktop.argtypes = (wintypes.HANDLE,)
user32.CloseDesktop.restype = wintypes.BOOL

if ctypes.sizeof(ctypes.c_void_p) == 8:
    SetClassLongPtr = user32.SetClassLongPtrW
    SetClassLongPtr.restype = ctypes.c_void_p
    SetClassLongPtr.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_void_p)
else:  # pragma: no cover - 32 位 Python
    SetClassLongPtr = user32.SetClassLongW
    SetClassLongPtr.restype = ctypes.c_ulong
    SetClassLongPtr.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ulong)

kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL


class FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT),
                ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.UINT),
                ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.UINT)]


user32.FlashWindowEx.argtypes = (ctypes.POINTER(FLASHWINFO),)
user32.FlashWindowEx.restype = wintypes.BOOL

_MUTEX_HANDLE = None  # 保持引用，进程存活期间一直持有


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in ("work_minutes", "rest_minutes"):
            value = data.get(key)
            if isinstance(value, int) and value > 0:
                cfg[key] = value
        if isinstance(data.get("auto_hide"), bool):
            cfg["auto_hide"] = data["auto_hide"]
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
    except Exception:
        pass


def is_workstation_locked():
    """真正的锁屏检测：锁屏后输入桌面切到安全桌面，OpenInputDesktop 会失败。"""
    hdesk = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    if not hdesk:
        return True
    user32.CloseDesktop(hdesk)
    return False


def render_icon_image(text, bg_color, edge_color):
    """生成 256x256 圆角彩色图标，数字居中；描一圈深色边，16px 下也能看清。"""
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=44, fill=bg_color)

    font_size = 176 if len(text) <= 2 else 124
    font = None
    for font_name in ("segoeuib.ttf", "arialbd.ttf", "seguisb.ttf"):
        try:
            font = ImageFont.truetype(font_name, font_size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    stroke = 8 if len(text) <= 2 else 5
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    pos = ((size - width) / 2 - bbox[0], (size - height) / 2 - bbox[1])
    draw.text(pos, text, font=font, fill="white",
              stroke_width=stroke, stroke_fill=edge_color)
    return img


def _ensure_single_instance():
    """命名互斥量防多开；已有实例则通过本地端口通知它显示窗口，然后本实例退出。"""
    global _MUTEX_HANDLE
    _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not _MUTEX_HANDLE:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        try:
            s = socket.create_connection(("127.0.0.1", SINGLETON_PORT), timeout=1.0)
            s.sendall(b"SHOW")
            s.close()
        except OSError:
            pass
        print("程序已在运行中，激活已有窗口后退出。")
        sys.exit(0)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # 不使用 SO_REUSEADDR：Windows 下它允许重复绑定同一端口，会让单实例失效
        sock.bind(("127.0.0.1", SINGLETON_PORT))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------
class RestReminderApp:
    def __init__(self, sock=None):
        self._sock = sock
        self._activate_q = queue.SimpleQueue()

        self.root = tk.Tk()
        self.root.withdraw()  # 先隐藏，布局完成后再显示
        self.root.title(app_name)
        self.root.resizable(False, False)
        self.root.geometry(f"{WIN_W}x{WIN_H}")
        self.root.attributes("-topmost", False)
        self.center_window()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # ---- 配置与状态（只在主线程读写）----
        cfg = load_config()
        self.work_minutes = int(cfg["work_minutes"])
        self.rest_minutes = int(cfg["rest_minutes"])
        self.auto_hide_default = bool(cfg["auto_hide"])

        self.work_seconds = self.work_minutes * 60
        self.countdown_seconds = self.rest_minutes * 60
        self.is_resting = False
        self.running = True

        # 计时 / 锁屏
        self._acc = 0.0              # 秒累加器
        self._last_ts = time.monotonic()
        self._last_lock_check = 0.0
        self._locked = False
        self._locked_since = 0.0
        self._job = None

        # 图标
        self._ico_path = os.path.join(tempfile.gettempdir(), f"rest_reminder_{os.getpid()}.ico")
        self._icon_key = None
        self._hicons = (None, None)
        self._last_iconic = None
        self._icon_fail_until = 0.0

        # 渲染缓存
        self._last_timer_text = None
        self._last_progress = None

        # 字体和颜色
        self.title_font = ("Microsoft YaHei", 18, "bold")
        self.timer_font = ("Consolas", 46, "bold")
        self.btn_font = ("Microsoft YaHei", 11, "bold")
        self.status_font = ("Microsoft YaHei", 12, "bold")

        self.work_bg = "#e8f5e9"
        self.rest_bg = "#fff3e0"
        self.work_fg = "#2e7d32"
        self.rest_fg = "#e65100"
        self.btn_rest_color = "#1565c0"
        self.btn_skip_color = "#4a4a4a"
        self.btn_reset_color = "#455a64"
        self.btn_hide_color = "#607d8b"
        self.text_color = "#333333"
        self.muted_color = "#5f6b66"

        self.build_ui()

        # Esc / Ctrl+H 隐藏（焦点在本窗口时生效）
        self.root.bind("<Escape>", self._hide_window)
        self.root.bind("<Control-h>", self._hide_window)

        self.root.deiconify()
        self._sync_icon(force=True)
        self._render()

        self._start_singleton_listener()

    # ------------------------------------------------------------------
    # 单实例：接收新实例的激活请求（只往队列里放标记，不碰 Tk）
    # ------------------------------------------------------------------
    def _start_singleton_listener(self):
        sock = self._sock
        if not sock:
            return

        def _listen():
            while self.running:
                try:
                    ready, _, _ = select.select([sock], [], [], 0.5)
                    if not ready:
                        continue
                    conn, _ = sock.accept()
                    try:
                        data = conn.recv(1024)
                    finally:
                        conn.close()
                    if data.strip() == b"SHOW":
                        self._activate_q.put(True)
                except OSError:
                    break  # socket 已关闭（程序退出）
                except Exception:
                    time.sleep(0.2)

        threading.Thread(target=_listen, daemon=True, name="singleton-listener").start()

    def _drain_activation(self):
        activated = False
        while True:
            try:
                self._activate_q.get_nowait()
                activated = True
            except queue.Empty:
                break
        if activated and self.running:
            self._show_window()

    # ------------------------------------------------------------------
    # 窗口 / 主题
    # ------------------------------------------------------------------
    def center_window(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - WIN_W) // 2)
        y = max(0, (sh - WIN_H) // 2)
        self.root.geometry(f"{WIN_W}x{WIN_H}+{x}+{y}")

    def build_ui(self):
        self.main_frame = tk.Frame(self.root, bg=self.work_bg)
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        self.title_label = tk.Label(
            self.main_frame, text="工作中...", font=self.title_font,
            bg=self.work_bg, fg=self.work_fg)
        self.title_label.pack(pady=(20, 6))

        self.timer_label = tk.Label(
            self.main_frame, text="00:00", font=self.timer_font,
            bg=self.work_bg, fg=self.work_fg)
        self.timer_label.pack(pady=(0, 4))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("green.Horizontal.TProgressbar",
                        troughcolor="#c8e6c9", background="#4caf50",
                        lightcolor="#4caf50", darkcolor="#4caf50")
        style.configure("orange.Horizontal.TProgressbar",
                        troughcolor="#ffe0b2", background="#ff9800",
                        lightcolor="#ff9800", darkcolor="#ff9800")

        self.progress = ttk.Progressbar(
            self.main_frame, style="green.Horizontal.TProgressbar",
            length=350, mode="determinate")
        self.progress.pack(pady=(4, 10))
        self.progress["maximum"] = max(1, self.work_seconds)
        self.progress["value"] = self.work_seconds

        self.status_label = tk.Label(
            self.main_frame, text="", font=self.status_font,
            bg=self.work_bg, fg=self.text_color)
        self.status_label.pack(pady=(0, 8))

        # ---- 设置行 ----
        self.setting_frame = tk.Frame(self.main_frame, bg=self.work_bg)
        self.setting_frame.pack(pady=(0, 10))

        vcmd = (self.root.register(self._validate_minutes), "%P")

        self.work_caption = tk.Label(self.setting_frame, text="工作：",
                                     font=("Microsoft YaHei", 10),
                                     bg=self.work_bg, fg=self.text_color)
        self.work_caption.pack(side=tk.LEFT)

        self.work_entry = tk.Entry(self.setting_frame, font=("Microsoft YaHei", 10),
                                   width=4, justify=tk.CENTER, validate="key",
                                   validatecommand=vcmd)
        self.work_entry.insert(0, str(self.work_minutes))
        self.work_entry.pack(side=tk.LEFT)

        self.rest_caption = tk.Label(self.setting_frame, text="分钟    休息：",
                                     font=("Microsoft YaHei", 10),
                                     bg=self.work_bg, fg=self.text_color)
        self.rest_caption.pack(side=tk.LEFT)

        self.rest_entry = tk.Entry(self.setting_frame, font=("Microsoft YaHei", 10),
                                   width=4, justify=tk.CENTER, validate="key",
                                   validatecommand=vcmd)
        self.rest_entry.insert(0, str(self.rest_minutes))
        self.rest_entry.pack(side=tk.LEFT)

        self.minutes_caption = tk.Label(self.setting_frame, text="分钟",
                                        font=("Microsoft YaHei", 10),
                                        bg=self.work_bg, fg=self.text_color)
        self.minutes_caption.pack(side=tk.LEFT)

        self.apply_btn = tk.Button(
            self.setting_frame, text="应用", font=("Microsoft YaHei", 9, "bold"),
            bg="#43a047", fg="white", activebackground="#2e7d32",
            activeforeground="white", relief=tk.FLAT,
            padx=10, pady=3, cursor="hand2", command=self.apply_settings)
        self.apply_btn.pack(side=tk.LEFT, padx=(10, 0))

        # ---- 按钮行 ----
        self.btn_frame = tk.Frame(self.main_frame, bg=self.work_bg)
        self.btn_frame.pack(pady=(2, 8))

        self.rest_now_btn = tk.Button(
            self.btn_frame, text="立刻休息", font=self.btn_font,
            bg=self.btn_rest_color, fg="white",
            activebackground="#0d47a1", activeforeground="white",
            disabledforeground="#90caf9", relief=tk.FLAT,
            padx=14, pady=8, cursor="hand2", command=self.start_rest)
        self.rest_now_btn.pack(side=tk.LEFT, padx=4)

        self.skip_btn = tk.Button(
            self.btn_frame, text="跳过休息", font=self.btn_font,
            bg=self.btn_skip_color, fg="white",
            activebackground="#333333", activeforeground="white",
            disabledforeground="#9e9e9e", relief=tk.FLAT,
            padx=14, pady=8, cursor="hand2",
            command=self.skip_rest, state=tk.DISABLED)
        self.skip_btn.pack(side=tk.LEFT, padx=4)

        self.reset_btn = tk.Button(
            self.btn_frame, text="重置计时", font=self.btn_font,
            bg=self.btn_reset_color, fg="white",
            activebackground="#263238", activeforeground="white",
            disabledforeground="#90a4ae", relief=tk.FLAT,
            padx=14, pady=8, cursor="hand2", command=self.reset_timer)
        self.reset_btn.pack(side=tk.LEFT, padx=4)

        self.hide_btn = tk.Button(
            self.btn_frame, text="隐藏", font=self.btn_font,
            bg=self.btn_hide_color, fg="white",
            activebackground="#37474f", activeforeground="white",
            disabledforeground="#b0bec5", relief=tk.FLAT,
            padx=14, pady=8, cursor="hand2", command=self._hide_window)
        self.hide_btn.pack(side=tk.LEFT, padx=4)

        # ---- 底栏：自动收起选项 + 快捷键提示 ----
        self.foot_frame = tk.Frame(self.main_frame, bg=self.work_bg)
        self.foot_frame.pack(pady=(0, 10))

        self.auto_hide_var = tk.BooleanVar(value=self.auto_hide_default)
        self.auto_hide_chk = tk.Checkbutton(
            self.foot_frame, text="3 秒后自动收起", variable=self.auto_hide_var,
            command=self._on_auto_hide_toggle,
            bg=self.work_bg, fg=self.muted_color, activebackground=self.work_bg,
            activeforeground=self.text_color, selectcolor="white",
            font=("Microsoft YaHei", 9), bd=0, highlightthickness=0,
            cursor="hand2")
        self.auto_hide_chk.pack(side=tk.LEFT)

        self.hint_label = tk.Label(
            self.foot_frame, text="Esc 隐藏 · 任务栏数字继续计时",
            font=("Microsoft YaHei", 9), bg=self.work_bg, fg=self.muted_color)
        self.hint_label.pack(side=tk.LEFT, padx=(12, 0))

        # 切换主题时统一改背景色的控件
        self._theme_widgets = [
            self.main_frame, self.title_label, self.timer_label, self.status_label,
            self.setting_frame, self.work_caption, self.rest_caption,
            self.minutes_caption, self.btn_frame, self.foot_frame,
            self.auto_hide_chk, self.hint_label,
        ]

    def _apply_theme(self, bg):
        for widget in self._theme_widgets:
            try:
                widget.configure(bg=bg)
            except tk.TclError:
                pass
        try:
            self.auto_hide_chk.configure(activebackground=bg)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # 任务栏图标
    # ------------------------------------------------------------------
    def _top_hwnd(self):
        """Tk 的 winfo_id 是内层子窗口，任务栏按钮对应最外层 wrapper。"""
        try:
            hwnd = user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
            return hwnd or 0
        except Exception:
            return 0

    def _sync_icon(self, force=False):
        seconds = self.countdown_seconds if self.is_resting else self.work_seconds
        minutes = max(0, math.ceil(max(0, seconds) / 60))
        self._refresh_icon(minutes, self.is_resting, force)

    def _refresh_icon(self, minutes, resting, force=False):
        key = (int(minutes), bool(resting))
        if not force and key == self._icon_key:
            return
        if not force and time.monotonic() < self._icon_fail_until:
            return  # 上一次失败后短暂退避，避免每 250ms 反复重试
        hwnd = self._top_hwnd()
        if not hwnd:
            return  # 窗口还没建好，等下一轮
        try:
            bg = ICON_REST_BG if resting else ICON_WORK_BG
            edge = ICON_REST_EDGE if resting else ICON_WORK_EDGE
            img = render_icon_image(str(minutes), bg, edge)
            img.save(self._ico_path, format="ICO", sizes=ICO_SIZES)
            if self._install_icons(hwnd):
                self._icon_key = key
                self._cycle_taskbar_button(hwnd)
            else:
                self._icon_fail_until = time.monotonic() + 2.0
        except Exception:
            self._icon_fail_until = time.monotonic() + 2.0

    def _install_icons(self, hwnd):
        hbig = user32.LoadImageW(None, self._ico_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        hsmall = user32.LoadImageW(None, self._ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if not hbig and not hsmall:
            return False
        if hbig:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hbig)
            SetClassLongPtr(hwnd, GCLP_HICON, hbig)
        if hsmall:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hsmall)
            SetClassLongPtr(hwnd, GCLP_HICONSM, hsmall)

        # 只释放我们自己创建过的旧句柄
        old_big, old_small = self._hicons
        for hicon in (old_big, old_small):
            if hicon and hicon not in (hbig, hsmall):
                user32.DestroyIcon(hicon)
        self._hicons = (hbig, hsmall)
        return True

    def _cycle_taskbar_button(self, hwnd):
        """Win11 的 Explorer 只在任务栏按钮被重建时才重读图标。

        只在窗口“已最小化”时做隐藏/重显：
        - 最小化时没有可见窗口，重显不会把窗口弹到前台（旧版的“又弹出来”问题）；
        - 窗口可见时直接用 WM_SETICON，不折腾；
        - 重显用 SW_SHOWMINNOACTIVE，保持最小化且不抢焦点。
        """
        try:
            if not user32.IsWindowVisible(hwnd):
                return
            if not user32.IsIconic(hwnd):
                return
            user32.ShowWindow(hwnd, SW_HIDE)
            user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
        except Exception:
            pass

    def _track_window_state(self):
        """最小化/恢复时强制重贴一次图标，保证任务栏上是最新数字。"""
        hwnd = self._top_hwnd()
        if not hwnd:
            return
        iconic = bool(user32.IsIconic(hwnd))
        if iconic != self._last_iconic:
            self._last_iconic = iconic
            self._sync_icon(force=True)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self):
        self._last_ts = time.monotonic()
        self._last_lock_check = self._last_ts
        self.root.after(120, lambda: self._sync_icon(force=True))
        self._job = self.root.after(TICK_MS, self._loop)
        self.root.mainloop()

    def _loop(self):
        if not self.running:
            return
        now = time.monotonic()
        dt = now - self._last_ts
        self._last_ts = now

        if dt > SLEEP_GAP:
            # 休眠/长时间挂起：这段时间不算干活
            self._reset_after_absence("检测到系统休眠，工作时间已重置")
            dt = 0.0
        elif dt > 5.0:
            dt = 1.0  # 中等卡顿，不把整段空档算进倒计时

        self._drain_activation()
        self._sample_workstation(now)

        if not self._locked:
            self._acc += dt
            if self._acc >= 1.0:
                whole = int(self._acc)
                self._acc -= whole
                self._advance(whole)

        self._render()
        self._track_window_state()
        if self.running:
            self._job = self.root.after(TICK_MS, self._loop)

    def _sample_workstation(self, now):
        if now - self._last_lock_check < LOCK_POLL_INTERVAL:
            return
        self._last_lock_check = now
        locked = is_workstation_locked()
        if locked and not self._locked:
            self._locked = True
            self._locked_since = now
        elif not locked and self._locked:
            self._locked = False
            # 短暂的锁屏（UAC 安全桌面、屏保一闪）不重置
            if now - self._locked_since >= LOCK_MIN_AWAY:
                self._reset_after_absence("欢迎回来，工作时间已重置")

    def _advance(self, seconds):
        if self.is_resting:
            self.countdown_seconds -= seconds
            if self.countdown_seconds <= 0:
                self.countdown_seconds = 0
                self._do_end_rest()
        else:
            self.work_seconds -= seconds
            if self.work_seconds <= 0:
                self.work_seconds = 0
                self._do_start_rest()

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def _render(self):
        if self.is_resting:
            seconds = max(0, self.countdown_seconds)
        else:
            seconds = max(0, self.work_seconds)

        text = f"{seconds // 60:02d}:{seconds % 60:02d}"
        if text != self._last_timer_text:
            self._last_timer_text = text
            self.timer_label.config(text=text)
            if self.is_resting:
                self.status_label.config(text=f"站起来活动一下吧！剩余 {text}")

        if seconds != self._last_progress:
            self._last_progress = seconds
            self.progress["value"] = seconds

        self._sync_icon()

    # ------------------------------------------------------------------
    # 模式切换
    # ------------------------------------------------------------------
    def _switch_to_rest_ui(self):
        bg = self.rest_bg
        fg = self.rest_fg
        self._apply_theme(bg)
        self.title_label.config(text="该活动了！", fg=fg)
        self.timer_label.config(fg=fg)
        self.status_label.config(fg=fg, text="站起来活动一下吧！")
        self.progress.configure(style="orange.Horizontal.TProgressbar")
        self.progress["maximum"] = max(1, self.rest_minutes * 60)
        self.rest_now_btn.config(state=tk.DISABLED, text="休息中...")
        self.skip_btn.config(state=tk.NORMAL)
        self._last_timer_text = None
        self._last_progress = None
        self._icon_key = None
        self._sync_icon(force=True)
        self._render()

    def _switch_to_work_ui(self):
        bg = self.work_bg
        fg = self.work_fg
        self._apply_theme(bg)
        self.title_label.config(text="工作中...", fg=fg)
        self.timer_label.config(fg=fg)
        self.status_label.config(fg=self.text_color, text="")
        self.progress.configure(style="green.Horizontal.TProgressbar")
        self.progress["maximum"] = max(1, self.work_seconds)
        self.rest_now_btn.config(state=tk.NORMAL, text="立刻休息")
        self.skip_btn.config(state=tk.DISABLED)
        self.root.attributes("-topmost", False)
        self._last_timer_text = None
        self._last_progress = None
        self._icon_key = None
        self._sync_icon(force=True)
        self._render()

    def _do_start_rest(self):
        if self.is_resting:
            return
        self.is_resting = True
        self.countdown_seconds = self.rest_minutes * 60
        self._switch_to_rest_ui()
        self._notify_rest_start()

    def _do_end_rest(self):
        self.is_resting = False
        self.work_seconds = self.work_minutes * 60
        self._switch_to_work_ui()

    def _reset_after_absence(self, message):
        self._acc = 0.0
        self.is_resting = False
        self.work_seconds = self.work_minutes * 60
        self.countdown_seconds = self.rest_minutes * 60
        self._switch_to_work_ui()
        self.status_label.config(text=message)

    # ------------------------------------------------------------------
    # 提醒弹出 & 隐藏
    # ------------------------------------------------------------------
    def _notify_rest_start(self):
        """开始休息：置顶 3 秒引起注意，随后（没人碰的话）自动收起。

        置顶会让窗口成为前台，所以 120ms 后立刻把前台还给之前的窗口，
        避免用户正在打字时被吞掉 2 秒的按键。
        """
        try:
            prev_fg = user32.GetForegroundWindow()
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.lift()
        except tk.TclError:
            return

        self.root.after(120, lambda: self._restore_foreground(prev_fg))
        self.root.after(3000, self._rest_popup_settle)

    def _restore_foreground(self, hwnd):
        try:
            if not hwnd or not user32.IsWindow(hwnd):
                return
            # 只有当焦点还在我们窗口上时才归还，避免抢走用户此刻点的其他窗口
            if user32.GetForegroundWindow() == self._top_hwnd():
                user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def _rest_popup_settle(self):
        """弹出 3 秒后：取消置顶；若用户没点过窗口且开启了自动收起，则最小化。"""
        if not self.running:
            return
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass
        if not self.is_resting or not self.auto_hide_var.get():
            return
        hwnd = self._top_hwnd()
        if hwnd and user32.GetForegroundWindow() != hwnd:
            self._hide_window()

    def _hide_window(self, event=None):
        """隐藏到任务栏：计时和任务栏数字继续，点任务栏按钮可恢复。"""
        if not self.running:
            return "break"
        try:
            self.root.attributes("-topmost", False)
            self.root.iconify()
        except tk.TclError:
            pass
        return "break"

    def _show_window(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self._sync_icon(force=True)
        except tk.TclError:
            return
        self._flash_taskbar()

    def _flash_taskbar(self):
        """闪烁任务栏按钮引起注意（不弹窗、不抢焦点）。"""
        try:
            hwnd = self._top_hwnd()
            if not hwnd:
                return
            fwi = FLASHWINFO()
            fwi.cbSize = ctypes.sizeof(fwi)
            fwi.hwnd = hwnd
            fwi.dwFlags = 0x00000003 | 0x0000000C  # FLASHW_ALL | FLASHW_TIMERNOFG
            fwi.uCount = 5
            fwi.dwTimeout = 0
            user32.FlashWindowEx(ctypes.byref(fwi))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 用户操作
    # ------------------------------------------------------------------
    def start_rest(self):
        self._do_start_rest()

    def skip_rest(self):
        self._do_end_rest()

    def reset_timer(self):
        if self.is_resting:
            self.countdown_seconds = self.rest_minutes * 60
            self._switch_to_rest_ui()
        else:
            self.work_seconds = self.work_minutes * 60
            self._switch_to_work_ui()

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------
    def _validate_minutes(self, value):
        if value == "":
            return True
        return value.isdigit() and len(value) <= 3

    def _on_auto_hide_toggle(self):
        self.auto_hide_default = bool(self.auto_hide_var.get())
        save_config(self._current_config())

    def _current_config(self):
        return {
            "work_minutes": self.work_minutes,
            "rest_minutes": self.rest_minutes,
            "auto_hide": bool(self.auto_hide_var.get()),
        }

    def apply_settings(self):
        try:
            work_mins = min(999, max(1, int(self.work_entry.get())))
            rest_mins = min(999, max(1, int(self.rest_entry.get())))
        except ValueError:
            return

        self.work_minutes = work_mins
        self.rest_minutes = rest_mins

        self.work_entry.delete(0, tk.END)
        self.work_entry.insert(0, str(work_mins))
        self.rest_entry.delete(0, tk.END)
        self.rest_entry.insert(0, str(rest_mins))

        save_config(self._current_config())

        if self.is_resting:
            self.countdown_seconds = rest_mins * 60
            self._switch_to_rest_ui()
            self.status_label.config(text=f"休息时间已设为 {rest_mins} 分钟")
        else:
            self.work_seconds = work_mins * 60
            self._switch_to_work_ui()
            self.status_label.config(
                text=f"工作时间 {work_mins} 分钟 | 休息时间 {rest_mins} 分钟")

    # ------------------------------------------------------------------
    # 退出
    # ------------------------------------------------------------------
    def on_close(self):
        self.running = False
        if self._job:
            try:
                self.root.after_cancel(self._job)
            except tk.TclError:
                pass

        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

        for hicon in self._hicons:
            if hicon:
                try:
                    user32.DestroyIcon(hicon)
                except Exception:
                    pass
        self._hicons = (None, None)

        try:
            if os.path.exists(self._ico_path):
                os.remove(self._ico_path)
        except OSError:
            pass

        try:
            self.root.destroy()
        except tk.TclError:
            pass


def main():
    sock = _ensure_single_instance()
    app = RestReminderApp(sock)
    app.run()


if __name__ == "__main__":
    main()
