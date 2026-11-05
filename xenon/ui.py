"""Minimal always-on-top command box for asking Xenon without a terminal.

The presenter is separate from Tk so its behavior is testable; the Tk shell is
thin and only handles layout and thread hops.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from typing import Callable

Ask = Callable[[str], str]


class CommandBoxModel:
    def __init__(self, ask: Ask) -> None:
        self._ask = ask
        self.status = "idle"
        self.last_error = ""

    def submit(self, question: str) -> str | None:
        question = question.strip()
        if not question:
            return None
        self.status = "working"
        try:
            answer = self._ask(question)
        except Exception as error:
            self.last_error = str(error)
            self.status = "error"
            return None
        self.last_error = ""
        self.status = "done"
        return answer


class CommandBox:
    def __init__(self, ask: Ask, title: str = "Xenon", topmost: bool = True) -> None:
        self.model = CommandBoxModel(ask)
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("560x380")
        self.root.attributes("-topmost", bool(topmost))

        self._output = tk.Text(self.root, height=16, wrap="word", state="disabled")
        self._output.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        entry_row = tk.Frame(self.root)
        entry_row.pack(fill="x", padx=8, pady=(0, 4))
        self._entry = tk.Entry(entry_row)
        self._entry.pack(side="left", fill="x", expand=True)
        self._entry.bind("<Return>", self._on_submit)
        tk.Button(entry_row, text="Ask", command=self._on_submit).pack(side="right", padx=(6, 0))

        self._status = tk.Label(self.root, text="idle", anchor="w")
        self._status.pack(fill="x", padx=8, pady=(0, 8))

    def _on_submit(self, event=None) -> None:
        question = self._entry.get()
        if not question.strip():
            return
        self._entry.delete(0, tk.END)
        self._append(f"you: {question}")
        self._status.config(text="working")
        threading.Thread(target=self._work, args=(question,), daemon=True).start()

    def _work(self, question: str) -> None:
        answer = self.model.submit(question)
        self.root.after(0, lambda: self._render(answer))

    def _render(self, answer: str | None) -> None:
        if answer is None:
            self._append(f"xenon: error: {self.model.last_error}")
        else:
            self._append(f"xenon: {answer}")
        self._status.config(text=self.model.status)

    def _append(self, line: str) -> None:
        self._output.config(state="normal")
        self._output.insert(tk.END, line + "\n\n")
        self._output.see(tk.END)
        self._output.config(state="disabled")

    def run(self) -> None:
        self.root.mainloop()


class VoiceStatusModel:
    """Testable state for the voice indicator (no Tk)."""

    COLORS = {
        "idle": "#9ca3af",
        "listening": "#22c55e",
        "heard": "#3b82f6",
        "working": "#f59e0b",
        "speaking": "#a855f7",
        "error": "#ef4444",
    }

    def __init__(self) -> None:
        self.state = "idle"
        self.detail = ""

    def set(self, state: str, detail: str = "") -> None:
        self.state = state
        self.detail = detail

    @property
    def color(self) -> str:
        return self.COLORS.get(self.state, "#9ca3af")


class VoiceIndicator:
    """Small always-on-top window showing whether Xenon is listening/working."""

    def __init__(self, title: str = "Xenon voice", topmost: bool = True) -> None:
        self.model = VoiceStatusModel()
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("420x200+750+420")
        self.root.attributes("-topmost", bool(topmost))
        self.root.resizable(False, False)
        self.root.configure(bg="#111827")

        row = tk.Frame(self.root)
        row.pack(fill="x", padx=10, pady=(10, 4))
        self._dot = tk.Label(row, text="\u25cf", font=("Segoe UI", 22), fg=self.model.color, bg="#111827")
        self._dot.pack(side="left")
        self._state = tk.Label(row, text=self.model.state, font=("Segoe UI", 13, "bold"), fg="#f9fafb", bg="#111827")
        self._state.pack(side="left", padx=(8, 0))

        self._detail = tk.Label(self.root, text="waiting for wake word", anchor="w", wraplength=390, justify="left", fg="#d1d5db", bg="#111827")
        self._detail.pack(fill="x", padx=10, pady=(0, 8))

        tk.Button(self.root, text="Stop", command=self._on_close).pack(pady=(0, 8))

        self._poll()

    def set(self, state: str, detail: str = "") -> None:
        """Thread-safe state update."""
        self._queue.put((state, detail))

    def _poll(self) -> None:
        changed = False
        while not self._queue.empty():
            state, detail = self._queue.get()
            self.model.set(state, detail)
            changed = True
        if changed:
            self._dot.config(fg=self.model.color)
            self._state.config(text=self.model.state)
            self._detail.config(text=self.model.detail)
        try:
            self.root.after(100, self._poll)
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run_mainloop(self) -> None:
        self.root.mainloop()
