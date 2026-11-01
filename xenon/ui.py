"""Minimal always-on-top command box for asking Xenon without a terminal.

The presenter is separate from Tk so its behavior is testable; the Tk shell is
thin and only handles layout and thread hops.
"""
from __future__ import annotations

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
