"""The music widget's settings window.

Every control writes ``music_widget.json`` as soon as it changes; the running
widget notices the new file within half a second and redraws, so the widget
on the desktop is the live preview. Opened from the orb's tray menu, or on its
own with ``python -m xenon music-widget --settings``.
"""
from __future__ import annotations

import tkinter as tk
from dataclasses import asdict
from tkinter import colorchooser, ttk

from . import music_settings as ms

BG = "#121318"
PANEL = "#1a1c23"
FG = "#e9eaf0"
MUTED = "#9a9db0"
ACCENT = "#8fd3ff"

_LABELS = {
    "rays": "Rays", "dots": "Dots", "wave": "Wave", "blocks": "Blocks", "none": "None",
    "glow": "Glowing disc", "vinyl": "Vinyl record", "square": "Album cover",
    "right": "Orb right, lyrics left", "left": "Orb left, lyrics right",
    "poster": "Poster (big + small words)", "reveal": "Reveal (word by word)", "karaoke": "Karaoke (fill colour)",
    "stack": "Stack (previous line above)", "fade": "Fade (whole line)",
    "upper": "UPPERCASE", "original": "As written", "center": "Centred",
    "album": "From album art", "white": "White", "ice": "Ice", "aurora": "Aurora",
    "sunset": "Sunset", "violet": "Violet", "rose": "Rose", "custom": "Custom colour",
}


_MOTION_LABELS = {"wipe": "Sweep across the word", "fade": "Fade in", "pop": "Pop"}
_LINE_LABELS = {"slide": "Slide up", "fade": "Fade", "cut": "Instant"}

_DEMO_LINES = [(0, "Say my name under the lights"), (2600, "One more time tonight"),
               (4800, "Hold on to me"), (7000, "")]


class LyricPreview:
    """A small live preview of the lyric animation on the Lyrics tab: a
    demo verse on a loop, drawn by the same animator the widget uses with
    the settings as they are right now."""

    W, H = 500, 170
    LOOP_MS = 7600

    def __init__(self, parent, settings_now) -> None:
        import time

        from . import music_scene as scene

        self._scene = scene
        self._settings_now = settings_now
        self._anim = scene.LyricAnimator()
        self._start = time.monotonic()
        self._photo = None
        self._drawn = None
        self._job = None
        frame = tk.Frame(parent, bg="#07080b", highlightthickness=1, highlightbackground="#2a2d38")
        frame.pack(fill="x", pady=(0, 10))
        self._label = tk.Label(frame, bg="#07080b", width=self.W, height=self.H, bd=0)
        self._label.pack()
        self._tick()

    def _tick(self) -> None:
        import time

        try:
            from PIL import Image, ImageTk
        except ImportError:
            return
        st = self._settings_now()
        pos = int((time.monotonic() - self._start) * 1000) % self.LOOP_MS
        box = self._scene.Box(0, 0, self.W, self.H)
        try:
            key, img = self._anim.frame(_DEMO_LINES, pos, self.LOOP_MS, box, st.accent_rgb((255, 190, 90)),
                                        420, st)
        except Exception:
            key, img = None, None
        if key != self._drawn:
            self._drawn = key
            back = Image.new("RGBA", (self.W, self.H), (7, 8, 11, 255))
            if img is not None:
                back.alpha_composite(img)
            self._photo = ImageTk.PhotoImage(back.convert("RGB"))
            self._label.configure(image=self._photo)
        try:
            self._job = self._label.after(max(15, int(1000 / max(20, st.lyric_fps))), self._tick)
        except tk.TclError:
            self._job = None

    def stop(self) -> None:
        if self._job is not None:
            try:
                self._label.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None


class SettingsWindow:
    def __init__(self, parent: tk.Misc | None = None, on_change=None) -> None:
        self._own_root = parent is None
        self.top = tk.Tk() if parent is None else tk.Toplevel(parent)
        self.top.title("Xenon music widget")
        self.top.configure(bg=BG)
        self.top.geometry("560x780")
        self.top.minsize(500, 640)
        self._on_change = on_change
        self._settings = ms.load()
        self._vars: dict[str, tk.Variable] = {}
        self._labels_for: dict[str, dict] = {}
        self._pending = None
        self._style()
        self._build()
        try:
            self.top.lift()
            self.top.focus_force()
        except tk.TclError:
            pass

    # -- look -------------------------------------------------------------------
    def _style(self) -> None:
        style = ttk.Style(self.top)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, font=("Segoe UI", 10))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(14, 6))
        style.map("TNotebook.Tab", background=[("selected", BG)], foreground=[("selected", FG)])
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Head.TLabel", font=("Segoe UI Semibold", 15))
        style.configure("Section.TLabel", foreground=ACCENT, font=("Segoe UI Semibold", 10))
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TButton", background=PANEL, foreground=FG, borderwidth=0, padding=(12, 6))
        style.map("TButton", background=[("active", "#262935")])
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG, arrowcolor=FG,
                        bordercolor="#2a2d38", lightcolor=PANEL, darkcolor=PANEL)
        style.map("TCombobox",
                  fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)],
                  selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", FG)],
                  background=[("readonly", PANEL), ("active", "#262935")])
        style.configure("Horizontal.TScale", background=BG, troughcolor=PANEL)
        self.top.option_add("*TCombobox*Listbox.background", PANEL)
        self.top.option_add("*TCombobox*Listbox.foreground", FG)

    def _build(self) -> None:
        head = ttk.Frame(self.top, padding=(18, 14, 18, 4))
        head.pack(fill="x")
        ttk.Label(head, text="Music widget", style="Head.TLabel").pack(anchor="w")
        ttk.Label(head, text="Changes apply live on your desktop.   Ctrl+Alt+Shift+M floats it over windows.",
                  style="Muted.TLabel").pack(anchor="w")

        book = ttk.Notebook(self.top)
        book.pack(fill="both", expand=True, padx=12, pady=8)
        look = self._page(book, "Look")
        viz = self._page(book, "Visualizer")
        lyr = self._page(book, "Lyrics")
        audio = self._page(book, "Audio")
        system = self._page(book, "System")

        self._choice(look, "accent", "Colour theme", ms.ACCENTS)
        self._colour(look)
        self._choice(look, "disc", "Album art", ms.DISCS)
        self._choice(look, "side", "Layout", ms.SIDES)
        self._slider(look, "orb_size", "Orb size", 0.6, 1.4)
        self._slider(look, "opacity", "Opacity", 0.15, 1.0)
        self._slider(look, "backdrop", "Backdrop shade", 0.0, 1.0)
        self._check(look, "halo", "Soft light behind the disc")
        self._check(look, "show_card", "Now-playing card")

        self._choice(viz, "visualizer", "Style", ms.VISUALIZERS)
        self._slider(viz, "bar_count", "Density", 36, 200, integer=True)
        self._slider(viz, "ray_length", "Reach", 0.3, 2.0)
        self._slider(viz, "glow", "Glow", 0.0, 2.0)
        self._slider(viz, "bounce", "Beat bounce", 0.0, 2.0)
        self._slider(viz, "corner_glow", "Edge glow (beat)", 0.0, 2.0)
        self._check(viz, "glow_bands", "Edges follow bass, mids and highs")
        self._choice(viz, "glow_shape", "Glow along", ms.GLOW_SHAPES,
                     {"edges": "All four edges", "corners": "Corners only"})
        self._slider(viz, "spin_speed", "Spin speed (deg/s, 0 = still)", 0.0, 60.0)

        self._preview = LyricPreview(lyr, lambda: self._settings)
        self._check(lyr, "show_lyrics", "Show synced lyrics")
        self._choice(lyr, "lyric_style", "Style", ms.LYRIC_STYLES)
        self._slider(lyr, "lyric_size", "Size", 0.5, 1.6)
        self._choice(lyr, "lyric_case", "Case", ms.LYRIC_CASES)
        self._choice(lyr, "lyric_align", "Alignment", ms.LYRIC_ALIGNS, {"left": "Left", "center": "Centred"})
        self._slider(lyr, "unsung_opacity", "Unsung words", 0.0, 1.0)
        self._check(lyr, "show_upcoming", "Show the next line")
        ttk.Label(lyr, text="Animation", style="Section.TLabel").pack(anchor="w", pady=(12, 2))
        self._choice(lyr, "lyric_motion", "Word highlight", ms.LYRIC_MOTIONS, _MOTION_LABELS)
        self._choice(lyr, "line_motion", "Line change", ms.LINE_MOTIONS, _LINE_LABELS)
        self._slider(lyr, "lyric_lift", "Word lift", 0.0, 2.0)
        self._slider(lyr, "lyric_fps", "Smoothness (fps)", 20, 60, integer=True)
        self._slider(lyr, "lyric_offset", "Sync (s, + = sooner)", -2.0, 2.0)
        self._check(lyr, "follow_voice", "Follow the singing (time words by the voice)")

        self._slider(audio, "sensitivity", "Sensitivity", 0.3, 3.0)
        self._slider(audio, "smoothing", "Smoothing", 0.0, 0.95)
        ttk.Label(audio, text="Reads whatever plays through your speakers (WASAPI loopback).\n"
                              "Nothing is recorded or sent anywhere.",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(10, 0))

        self._check(system, "start_with_orb", "Start with the orb")
        self._check(system, "show_dock", "Corner dock (controls + recent songs)")
        self._slider(system, "idle_minutes", "Sleep after no music (min, 0 = never)", 0.0, 60.0)
        self._check(system, "suggestions", "Suggest similar songs in the dock (asks Deezer)")
        self._slider(system, "fps", "Frame rate", 10, 60, integer=True)
        self._check(system, "pause_when_covered", "Pause when windows cover the desktop")
        ttk.Label(system, text=f"Settings file:\n{ms.settings_path()}", style="Muted.TLabel",
                  justify="left").pack(anchor="w", pady=(14, 0))

        foot = ttk.Frame(self.top, padding=(14, 0, 14, 12))
        foot.pack(fill="x")
        ttk.Button(foot, text="Reset to defaults", command=self._reset).pack(side="left")
        ttk.Button(foot, text="Video look", command=self._video_look).pack(side="left", padx=(8, 0))
        ttk.Button(foot, text="Done", command=self.close).pack(side="right")
        self.top.protocol("WM_DELETE_WINDOW", self.close)

    def _page(self, book: ttk.Notebook, title: str) -> ttk.Frame:
        frame = ttk.Frame(book, padding=(16, 12))
        book.add(frame, text=title)
        return frame

    # -- controls ---------------------------------------------------------------
    def _row(self, parent, label: str) -> ttk.Frame:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=5)
        ttk.Label(row, text=label, width=18).pack(side="left")
        return row

    def _choice(self, parent, key: str, label: str, options, labels: dict | None = None) -> None:
        row = self._row(parent, label)
        labels = labels or _LABELS
        names = [labels.get(o, o) for o in options]
        var = tk.StringVar(value=labels.get(getattr(self._settings, key), getattr(self._settings, key)))
        box = ttk.Combobox(row, values=names, textvariable=var, state="readonly")
        box.pack(side="left", fill="x", expand=True)
        lookup = dict(zip(names, options))
        box.bind("<<ComboboxSelected>>", lambda _e: self._set(key, lookup[var.get()]))
        self._vars[key] = var
        self._labels_for[key] = labels

    def _slider(self, parent, key: str, label: str, lo, hi, integer: bool = False) -> None:
        row = self._row(parent, label)
        value = getattr(self._settings, key)
        var = tk.DoubleVar(value=value)
        shown = tk.StringVar(value=self._fmt(value, integer))
        ttk.Label(row, textvariable=shown, width=6, style="Muted.TLabel").pack(side="right")

        def moved(_v):
            v = var.get()
            v = int(round(v)) if integer else round(v, 2)
            shown.set(self._fmt(v, integer))
            self._set(key, v)

        ttk.Scale(row, from_=lo, to=hi, variable=var, command=moved).pack(side="left", fill="x", expand=True)
        self._vars[key] = var

    @staticmethod
    def _fmt(value, integer: bool) -> str:
        return str(int(value)) if integer else f"{value:.2f}"

    def _check(self, parent, key: str, label: str) -> None:
        var = tk.BooleanVar(value=getattr(self._settings, key))
        ttk.Checkbutton(parent, text=label, variable=var,
                        command=lambda: self._set(key, bool(var.get()))).pack(anchor="w", pady=5)
        self._vars[key] = var

    def _colour(self, parent) -> None:
        row = self._row(parent, "Custom colour")
        swatch = tk.Label(row, width=4, bg=self._settings.custom_color, relief="flat")
        swatch.pack(side="left", padx=(0, 8))

        def pick():
            chosen = colorchooser.askcolor(self._settings.custom_color, parent=self.top)[1]
            if chosen:
                swatch.configure(bg=chosen)
                self._set("custom_color", chosen)
                self._set("accent", "custom")
                if "accent" in self._vars:
                    self._vars["accent"].set(_LABELS["custom"])

        ttk.Button(row, text="Pick…", command=pick).pack(side="left")
        self._swatch = swatch

    # -- state ------------------------------------------------------------------
    def _set(self, key: str, value) -> None:
        self._settings = ms.coerce({**asdict(self._settings), key: value})
        if self._pending is not None:
            self.top.after_cancel(self._pending)
        # Debounce slider drags: write the file at most every 120 ms.
        self._pending = self.top.after(120, self._save)

    def _save(self) -> None:
        self._pending = None
        try:
            ms.save(self._settings)
        except OSError as error:
            print(f"could not save music widget settings: {error}", flush=True)
            return
        if self._on_change is not None:
            self._on_change(self._settings)

    def _video_look(self) -> None:
        self._apply(ms.video_look(self._settings))

    def _reset(self) -> None:
        self._apply(ms.MusicSettings())

    def _apply(self, settings) -> None:
        self._settings = settings
        self._save()
        for key, var in self._vars.items():
            value = getattr(self._settings, key)
            if isinstance(var, tk.StringVar):
                var.set(self._labels_for.get(key, _LABELS).get(value, value))
            else:
                var.set(value)
        self._swatch.configure(bg=self._settings.custom_color)

    def close(self) -> None:
        self._preview.stop()
        if self._pending is not None:
            self.top.after_cancel(self._pending)
            self._save()
        try:
            self.top.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        if self._own_root:
            self.top.mainloop()


def open_settings(parent: tk.Misc | None = None, on_change=None) -> SettingsWindow:
    return SettingsWindow(parent, on_change)


__all__ = ["SettingsWindow", "open_settings"]
