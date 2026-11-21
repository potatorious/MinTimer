import ctypes as c
from ctypes import wintypes as w
from dataclasses import dataclass, replace
from pathlib import Path
from time import perf_counter
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import traceback

from . import settings
from .audio import Clips, DEFAULT_NAMES, Mixer, RATE, WaveOutput
from .taskbar import APP_NAME, TaskbarTab, identify_process

ICON = Path(__file__).resolve().parents[1] / "assets" / "smile.ico"
BAR = "#41413f"
HOVER = "#585854"
INK = "#f6f5ee"
DIALOG = "#202123"
CARD = "#2d2e31"
BUTTON = "#3c3d41"
TEXT = "#f0f0eb"
YELLOW = "#ffda3d"
FONT = "Consolas"
DRAG_AREA, PLAY_BUTTON, RESET_BUTTON = 0, 1, 2

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_SYSMENU, WS_MINIMIZEBOX = 0x80000, 0x20000
WS_EX_APPWINDOW, WS_EX_TOOLWINDOW, WS_EX_TOPMOST = 0x40000, 0x80, 0x08
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x01, 0x02, 0x04
SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x10, 0x20, 0x40
SW_HIDE = 0

user = c.WinDLL("user32")
gdi = c.WinDLL("gdi32")
user.GetAncestor.argtypes = [w.HWND, w.UINT]
user.GetAncestor.restype = w.HWND
user.GetWindowRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
user.MoveWindow.argtypes = [w.HWND, c.c_int, c.c_int, c.c_int, c.c_int, w.BOOL]
user.SetWindowRgn.argtypes = [w.HWND, w.HRGN, w.BOOL]
user.GetDpiForWindow.argtypes = [w.HWND]
user.GetDpiForWindow.restype = w.UINT
user.GetWindowLongW.argtypes = [w.HWND, c.c_int]
user.GetWindowLongW.restype = w.LONG
user.SetWindowLongW.argtypes = [w.HWND, c.c_int, w.LONG]
user.ShowWindow.argtypes = [w.HWND, c.c_int]
user.SetWindowPos.argtypes = [w.HWND, w.HWND, c.c_int, c.c_int, c.c_int, c.c_int, w.UINT]
user.SetForegroundWindow.argtypes = [w.HWND]
gdi.CreateRoundRectRgn.argtypes = [c.c_int] * 6
gdi.CreateRoundRectRgn.restype = w.HRGN
gdi.DeleteObject.argtypes = [w.HGDIOBJ]
user.MonitorFromRect.argtypes = [c.POINTER(w.RECT), w.DWORD]
user.MonitorFromRect.restype = w.HANDLE


class MonitorInfo(c.Structure):
    _fields_ = [
        ("size", w.DWORD), ("monitor", w.RECT), ("work", w.RECT), ("flags", w.DWORD),
    ]


user.GetMonitorInfoW.argtypes = [w.HANDLE, c.POINTER(MonitorInfo)]


def work_area(x, y, width=1, height=1):
    bounds = w.RECT(x, y, x + width, y + height)
    monitor = user.MonitorFromRect(c.byref(bounds), 2)
    info = MonitorInfo(c.sizeof(MonitorInfo))
    if not user.GetMonitorInfoW(monitor, c.byref(info)):
        raise OSError("화면 위치를 확인할 수 없습니다.")
    return info.work


def show_on_taskbar(hwnd):
    extended = user.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style = user.GetWindowLongW(hwnd, GWL_STYLE)
    if extended & WS_EX_APPWINDOW and not extended & WS_EX_TOOLWINDOW and style & WS_MINIMIZEBOX:
        return
    # Windows detects the new taskbar style when the borderless window is shown again.
    user.ShowWindow(hwnd, SW_HIDE)
    user.SetWindowLongW(hwnd, GWL_EXSTYLE, (extended | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW)
    user.SetWindowLongW(hwnd, GWL_STYLE, style | WS_SYSMENU | WS_MINIMIZEBOX)
    user.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                      | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW)


def keep_above(hwnd, pinned):
    current = bool(user.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
    if current != pinned:
        user.SetWindowPos(hwnd, HWND_TOPMOST if pinned else HWND_NOTOPMOST, 0, 0, 0, 0,
                          SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def replay_path():
    # Google Material Replay (assets/material-replay.svg), same path as the C# version.
    points = [(12, 5), (12, 1), (7, 6), (12, 11), (12, 7)]

    def curve(control1, control2, end):
        start = points[-1]
        for step in range(1, 7):
            t = step / 6
            a = 1 - t
            points.append(tuple(
                a**3 * start[i] + 3 * a*a*t * control1[i] + 3 * a*t*t * control2[i] + t**3 * end[i]
                for i in range(2)
            ))

    curve((15.31, 7), (18, 9.69), (18, 13))
    curve((18, 16.31), (15.31, 19), (12, 19))
    curve((8.69, 19), (6, 16.31), (6, 13))
    points.append((4, 13))
    curve((4, 17.42), (7.58, 21), (12, 21))
    curve((16.42, 21), (20, 17.42), (20, 13))
    curve((20, 8.58), (16.42, 5), (12, 5))
    return [(139 + 0.75 * x, 5 + 0.75 * y) for x, y in points]


REPLAY = replay_path()


def timeframe(elapsed):
    hours, remainder = divmod(int(elapsed), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def audio_message(error):
    if isinstance(error, ValueError):
        return str(error)
    return "음원을 열거나 재생하지 못했습니다. 파일과 Windows 출력 장치를 확인해 주세요."


class TimerWindow:
    def __init__(self, root):
        self.root = root
        self.channels = settings.load_sounds()
        self.clips = Clips()
        self.device = None
        self.dialog = None
        self.slider_images = None
        self.started = None
        self.base = self.last_elapsed = 0.0
        self.hover = self.pressed = None
        self.drag = None
        self.last_drawing = None
        self.ui_timer = None
        self.closed = False
        root.title(APP_NAME)
        root.iconbitmap(default=str(ICON))
        root.overrideredirect(True)
        root.resizable(False, False)
        root.configure(bg=BAR)
        area = work_area(0, 0)
        self.x, self.y, self.zoom, pinned = settings.load_window((area.right - 190, area.top + 40))
        self.pinned = tk.BooleanVar(root, pinned)
        self.size_value = tk.IntVar(root, round(self.zoom * 100))
        root.attributes("-topmost", pinned)
        self.bar = tk.Canvas(root, highlightthickness=0, borderwidth=0, background=BAR)
        self.bar.pack(fill="both", expand=True)
        self.resize()
        self.taskbar = TaskbarTab(self.hwnd, ICON)
        show_on_taskbar(self.hwnd)
        self.taskbar.register()
        self.build_menu()
        self.bar.bind("<ButtonPress-1>", self.press)
        self.bar.bind("<ButtonRelease-1>", self.release)
        self.bar.bind("<Motion>", self.move)
        self.bar.bind("<Leave>", self.leave)
        self.bar.bind("<Button-3>", self.show_menu)
        root.bind("<space>", lambda _: self.toggle())
        root.bind("<Key-r>", lambda _: self.reset())
        root.bind("<Key-R>", lambda _: self.reset())
        root.bind("<Escape>", lambda _: self.close())
        root.bind("<FocusIn>", self.redirect_focus)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.tick()

    def build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=False, font=(FONT, 10))
        self.menu.add_command(label="시작 / 일시정지", accelerator="Space", command=self.toggle)
        self.menu.add_command(label="초기화", accelerator="R", command=self.reset)
        self.menu.add_separator()
        self.menu.add_checkbutton(label="항상 위에 표시", variable=self.pinned, command=self.pin)
        size_menu = tk.Menu(self.menu, tearoff=False, font=(FONT, 10))
        for size in (100, 125, 150, 200):
            size_menu.add_radiobutton(label=f"{size}%", variable=self.size_value, value=size, command=self.change_size)
        self.menu.add_cascade(label="크기", menu=size_menu)
        self.menu.add_command(label="소리 설정…", command=self.edit_sounds)
        self.sound_menu = self.menu.index("end")
        self.menu.add_separator()
        self.menu.add_command(label="종료", accelerator="Esc", command=self.close)

    def show_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def resize(self):
        self.width, self.height = int(164 * self.zoom), int(28 * self.zoom)
        self.root.geometry(f"{self.width}x{self.height}")
        self.root.update_idletasks()
        self.hwnd = user.GetAncestor(self.root.winfo_id(), 2)
        self.position(self.x, self.y, clamp=True)
        keep_above(self.hwnd, self.pinned.get())
        diameter = round(20 * self.zoom)
        region = gdi.CreateRoundRectRgn(0, 0, self.width, self.height, diameter, diameter)
        if region and not user.SetWindowRgn(self.hwnd, region, True):
            gdi.DeleteObject(region)
        self.last_drawing = None
        self.draw()

    def position(self, x, y, clamp=False):
        if clamp:
            area = work_area(x, y, self.width, self.height)
            x = max(area.left, min(x, area.right - self.width))
            y = max(area.top, min(y, area.bottom - self.height))
        self.x, self.y = x, y
        user.MoveWindow(self.hwnd, x, y, self.width, self.height, True)

    def save_position(self):
        try:
            settings.save_window(self.x, self.y, self.zoom, self.pinned.get())
        except OSError:
            pass

    def pin(self):
        pinned = self.pinned.get()
        self.root.attributes("-topmost", pinned)
        keep_above(self.hwnd, pinned)
        self.save_position()

    def redirect_focus(self, event):
        if event.widget is self.root and self.dialog is not None and self.dialog.window.winfo_exists():
            self.dialog.reveal()

    def change_size(self):
        self.zoom = self.size_value.get() / 100
        self.resize()
        self.save_position()

    def hit(self, event):
        if not (0 <= event.x < self.width and 0 <= event.y < self.height):
            return None
        x = event.x / self.zoom
        if x < 103:
            return DRAG_AREA
        return PLAY_BUTTON if x < 133 else RESET_BUTTON

    def press(self, event):
        self.root.focus_force()
        self.pressed = self.hit(event)
        if self.pressed == DRAG_AREA:
            self.drag = event.x_root, event.y_root, self.x, self.y

    def move(self, event):
        if self.drag is not None:
            pointer_x, pointer_y, window_x, window_y = self.drag
            dx, dy = event.x_root - pointer_x, event.y_root - pointer_y
            if abs(dx) + abs(dy) > 3:
                self.position(window_x + dx, window_y + dy)
            return
        self.hover = self.hit(event)
        self.bar.configure(cursor="fleur" if self.hover == DRAG_AREA else "hand2")
        self.draw()

    def leave(self, _):
        self.hover = None
        self.draw()

    def release(self, event):
        pressed, self.pressed = self.pressed, None
        if self.drag is not None:
            self.drag = None
            self.position(self.x, self.y, clamp=True)
            self.save_position()
        elif pressed == self.hit(event):
            if pressed == PLAY_BUTTON:
                self.toggle()
            elif pressed == RESET_BUTTON:
                self.reset()

    def elapsed(self):
        if self.started is None:
            return self.base
        if self.device is not None:
            try:
                self.last_elapsed = max(self.last_elapsed, self.base + self.device.frames_played() / RATE)
                return self.last_elapsed
            except OSError:
                self.stop_output()
                self.base, self.started = self.last_elapsed, perf_counter()
                self.menu.entryconfigure(self.sound_menu, label="소리 설정… (출력 확인 필요)")
        self.last_elapsed = self.base + perf_counter() - self.started
        return self.last_elapsed

    def stop_output(self):
        if self.device is not None:
            self.device.close()
            self.device = None

    def start_output(self):
        clips = [None] * 3
        failed = False
        for index, channel in enumerate(self.channels):
            if channel.enabled:
                try:
                    clips[index] = self.clips.get(index, channel.path)
                except (OSError, ValueError):
                    failed = True
        if any(clip is not None for clip in clips):
            start_frame = int(self.base * RATE)
            mixer = Mixer(clips, [channel.volume for channel in self.channels], start_frame)
            try:
                self.device = WaveOutput(mixer)
                self.base = start_frame / RATE
            except OSError:
                failed = True
        self.menu.entryconfigure(self.sound_menu, label="소리 설정… (음원 확인 필요)" if failed else "소리 설정…")
        self.started = perf_counter()

    def toggle(self):
        if self.started is not None:
            self.base = self.elapsed()
            self.started = None
            self.stop_output()
        else:
            self.start_output()
        self.draw()

    def reset(self):
        self.started = None
        self.base = self.last_elapsed = 0.0
        self.stop_output()
        self.draw()

    def edit_sounds(self):
        if self.dialog is not None:
            self.dialog.reveal()
            return
        if self.started is not None:
            self.base, self.started = self.elapsed(), perf_counter()
            self.stop_output()
        self.dialog = SoundDialog(self)

    def end_dialog(self, channels):
        self.dialog = None
        if channels is not None:
            self.channels = channels
            self.menu.entryconfigure(self.sound_menu, label="소리 설정…")
        if not self.closed and self.started is not None:
            self.base = self.elapsed()
            self.start_output()

    def draw(self):
        text = timeframe(self.elapsed())
        playing = self.started is not None
        state = text, playing, self.hover
        if state == self.last_drawing:
            return
        self.last_drawing = state
        self.bar.delete("all")
        zoom = self.zoom
        if self.hover in (PLAY_BUTTON, RESET_BUTTON):
            x = 103 if self.hover == PLAY_BUTTON else 134
            rounded = (x, 8, x, 2, x + 6, 2, x + 22, 2, x + 28, 2, x + 28, 8,
                       x + 28, 20, x + 28, 26, x + 22, 26, x + 6, 26, x, 26, x, 20)
            self.bar.create_polygon([p * zoom for p in rounded], smooth=True, fill=HOVER)
        self.bar.create_text(8 * zoom, 14 * zoom, text=text, anchor="w", fill=INK,
                             font=(FONT, -round((16 if len(text) > 8 else 18) * zoom)))
        if playing:
            for x in (111, 118):
                self.bar.create_rectangle(x * zoom, 8 * zoom, (x + 3) * zoom, 20 * zoom,
                                          outline="", fill=INK)
        else:
            self.bar.create_polygon([p * zoom for p in (112, 7, 112, 21, 123, 14)], fill=INK)
        self.bar.create_polygon([coordinate * zoom for point in REPLAY for coordinate in point], fill=INK)

    def tick(self):
        if self.pinned.get():
            keep_above(self.hwnd, True)
        self.draw()
        self.ui_timer = self.root.after(16, self.tick)

    def close(self):
        self.closed = True
        if self.dialog is not None:
            self.dialog.close()
        if self.ui_timer is not None:
            self.root.after_cancel(self.ui_timer)
        self.stop_output()
        self.save_position()
        self.taskbar.close()
        self.root.destroy()


@dataclass
class SoundRow:
    enabled: tk.BooleanVar
    volume_text: tk.StringVar
    file_label: tk.Label
    preview_button: tk.Button


class SoundDialog:
    SLIDER_WIDTH = 230
    SLIDER_HEIGHT = 26

    def __init__(self, owner):
        self.owner = owner
        self.channels = [replace(channel) for channel in owner.channels]
        self.device = self.playing = self.poll_id = None
        self.window = window = tk.Toplevel(owner.root)
        window.withdraw()
        window.title("소리 설정 · Mini Timer")
        window.iconbitmap(default=str(ICON))
        window.transient(owner.root)
        window.attributes("-topmost", owner.pinned.get())
        window.resizable(False, False)
        window.configure(bg=DIALOG)
        self.scale = max(1, user.GetDpiForWindow(owner.hwnd) / 96)
        self.font = (FONT, -self.px(13))
        self.rows: list[SoundRow] = []
        self.configure_controls()
        area = work_area(owner.x, owner.y, owner.width, owner.height)
        width, height = self.px(420), self.px(296)
        window.geometry(f"{width}x{height}")
        for index, name in enumerate(("매초", "매분", "매시간")):
            self.add_row(index, name)
        self.place(self.button(window, "취소", self.close), 238, 258, 82, 26)
        self.place(self.button(window, "저장", self.save, YELLOW, DIALOG), 326, 258, 82, 26)
        window.bind("<Escape>", lambda _: self.close())
        window.bind("<Return>", lambda _: self.save())
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.deiconify()
        window.update_idletasks()
        self.hwnd = user.GetAncestor(window.winfo_id(), 2)
        outer = w.RECT()
        user.GetWindowRect(self.hwnd, c.byref(outer))
        outer_width, outer_height = outer.right - outer.left, outer.bottom - outer.top
        gap = self.px(8)
        x = max(area.left, min(owner.x + owner.width - outer_width, area.right - outer_width))
        y = owner.y + owner.height + gap
        if y + outer_height > area.bottom:
            y = max(area.top, owner.y - outer_height - gap)
        user.MoveWindow(self.hwnd, x, y, outer_width, outer_height, True)
        window.grab_set()
        self.reveal()

    def reveal(self):
        if self.window.state() != "normal":
            self.window.deiconify()
        keep_above(self.hwnd, self.owner.pinned.get())
        self.window.lift()
        user.SetForegroundWindow(self.hwnd)
        self.window.focus_force()

    def px(self, value):
        return round(value * self.scale)

    def place(self, widget, x, y, width, height):
        widget.place(x=self.px(x), y=self.px(y), width=self.px(width), height=self.px(height))

    def configure_controls(self):
        if self.owner.slider_images is not None:
            return
        style = ttk.Style(self.window)
        style.theme_use("clam")
        style.configure("Mini.TCheckbutton", background=CARD, foreground=TEXT, font=self.font)
        style.map("Mini.TCheckbutton", background=[("active", CARD)], foreground=[("active", TEXT)])
        style.configure("Mini.Horizontal.TScale", background=CARD, troughcolor=CARD)
        rail = tk.PhotoImage(master=self.window, width=self.px(self.SLIDER_WIDTH),
                             height=self.px(self.SLIDER_HEIGHT))
        rail.put("#e4e4e1", to=(0, self.px(11), rail.width(), self.px(15)))
        marker_inset = rail.width() // 2 - self.px(2)
        rail.put("#e63838", to=(marker_inset, 0, rail.width() - marker_inset, self.px(15)))
        thumb = tk.PhotoImage(master=self.window, width=self.px(10), height=self.px(18))
        tip_start = self.px(12)
        thumb.put("#087cc5", to=(0, 0, thumb.width(), tip_start))
        for row in range(tip_start, thumb.height()):
            inset = (row - tip_start) * (thumb.width() // 2) // (thumb.height() - tip_start)
            thumb.put("#087cc5", to=(inset, row, thumb.width() - inset, row + 1))
        self.owner.slider_images = rail, thumb
        style.element_create("Mini.Scale.trough", "image", rail, sticky="ew")
        style.element_create("Mini.Scale.slider", "image", thumb)
        style.layout("Mini.Horizontal.TScale", [
            ("Mini.Scale.trough", {"sticky": "ew", "children": [("Mini.Scale.slider", {"side": "left", "sticky": ""})]}),
        ])

    def button(self, owner, text, action, background=BUTTON, foreground=TEXT):
        return tk.Button(owner, text=text, command=action, font=self.font, bg=background, fg=foreground,
                         activebackground=HOVER if background == BUTTON else "#f5cf38",
                         activeforeground=foreground, borderwidth=0, highlightthickness=0, cursor="hand2")

    def add_row(self, index, name):
        channel = self.channels[index]
        card = tk.Frame(self.window, bg=CARD)
        self.place(card, 12, 12 + index * 80, 396, 72)
        enabled = tk.BooleanVar(card, channel.enabled)
        volume_text = tk.StringVar(card, f"{channel.volume}%")
        file_label = tk.Label(card, font=self.font, bg=CARD, fg=TEXT, anchor="w")
        self.place(ttk.Checkbutton(card, text=name, variable=enabled, style="Mini.TCheckbutton"), 10, 7, 92, 26)
        slider = ttk.Scale(card, from_=0, to=settings.MAX_VOLUME, orient="horizontal", style="Mini.Horizontal.TScale",
                           value=channel.volume, command=lambda value: self.change_volume(index, value))
        self.place(slider, 106, 7, self.SLIDER_WIDTH, self.SLIDER_HEIGHT)
        self.place(tk.Label(card, textvariable=volume_text, font=self.font, bg=CARD, fg=TEXT, anchor="e"),
                   340, 7, 46, 26)
        self.place(file_label, 10, 39, 112, 26)
        self.place(self.button(card, "파일 선택", lambda: self.choose(index, name)), 128, 39, 82, 26)
        self.place(self.button(card, "기본", lambda: self.set_file(index, "")), 216, 39, 82, 26)
        preview_button = self.button(card, "▶", lambda: self.preview(index))
        self.place(preview_button, 304, 39, 82, 26)
        self.rows.append(SoundRow(enabled, volume_text, file_label, preview_button))
        self.update_file(index)

    def update_file(self, index):
        name = Path(self.channels[index].path).name or DEFAULT_NAMES[index]
        self.rows[index].file_label.configure(text=name if len(name) <= 14 else f"{name[:7]}…{name[-6:]}", fg=TEXT)

    def choose(self, index, name):
        path = filedialog.askopenfilename(parent=self.window, title=f"{name} 알림 음원 선택",
                                          filetypes=[("오디오 파일", "*.wav *.mp3 *.wma"), ("모든 파일", "*.*")])
        if path:
            self.set_file(index, path)

    def set_file(self, index, path):
        if index == self.playing:
            self.stop_preview()
        self.channels[index].path = path
        self.update_file(index)

    def change_volume(self, index, value):
        value = round(float(value))
        self.channels[index].volume = value
        if index < len(self.rows):
            self.rows[index].volume_text.set(f"{value}%")
        if self.device is not None:
            self.device.mixer.volumes[index] = value

    def preview(self, index):
        stop = self.playing == index
        self.stop_preview()
        if stop:
            return
        channel = self.channels[index]
        self.update_file(index)
        try:
            clip = self.owner.clips.get(index, channel.path)
            if not channel.volume:
                return
            clips = [None] * 3
            clips[index] = clip
            mixer = Mixer(clips, [channel.volume for channel in self.channels])
            mixer.trigger(index)
            self.device = WaveOutput(mixer)
            self.playing = index
            self.rows[index].preview_button.configure(text="■")
            self.poll_id = self.window.after(20, self.poll, len(clip))
        except (OSError, ValueError) as error:
            self.rows[index].file_label.configure(fg="#ffb582")
            messagebox.showinfo("음원 확인", audio_message(error), parent=self.window)

    def poll(self, length):
        self.poll_id = None
        try:
            if self.device.frames_played() < length:
                self.poll_id = self.window.after(20, self.poll, length)
                return
        except OSError as error:
            messagebox.showinfo("음원 확인", audio_message(error), parent=self.window)
        self.stop_preview()

    def stop_preview(self):
        if self.poll_id is not None:
            self.window.after_cancel(self.poll_id)
            self.poll_id = None
        if self.device is not None:
            self.device.close()
            self.device = None
        if self.playing is not None:
            self.rows[self.playing].preview_button.configure(text="▶")
            self.playing = None

    def save(self):
        for index, channel in enumerate(self.channels):
            channel.enabled = self.rows[index].enabled.get()
            if channel.enabled and channel.path and not Path(channel.path).is_file():
                messagebox.showinfo("음원 확인", "켜진 알림의 음원 파일이 없습니다.", parent=self.window)
                return
        try:
            settings.save_sounds(self.channels)
        except OSError:
            messagebox.showerror("저장 실패", "설정을 저장하지 못했습니다. 폴더 접근 권한을 확인해 주세요.", parent=self.window)
            return
        self.close(self.channels)

    def close(self, channels=None):
        self.stop_preview()
        self.window.destroy()
        self.owner.end_dialog(channels)


def main():
    user.SetProcessDPIAware()
    identify_process()
    try:
        root = tk.Tk()
        TimerWindow(root)
        root.mainloop()
    except Exception:
        try:
            settings.DATA.mkdir(parents=True, exist_ok=True)
            (settings.DATA / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        user.MessageBoxW(None, "Mini Timer를 열지 못했습니다. MiniTimerPython 폴더의 error.log를 확인해 주세요.",
                         "Mini Timer", 0x10)
