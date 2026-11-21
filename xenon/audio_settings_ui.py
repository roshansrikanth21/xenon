"""Small window for choosing the microphone and speaker Xenon uses.

The audio loop reads these choices at startup, so the window says to restart
the orb afterwards. ``xenon audio`` does the same thing without a UI.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from xenon import audio_settings


class AudioSettingsWindow:
    def __init__(self) -> None:
        self._root = tk.Tk()
        self._root.title("Xenon audio")
        self._root.geometry("640x330")
        self._root.minsize(520, 300)
        self._results: queue.Queue[tuple[str, str]] = queue.Queue()

        style = ttk.Style(self._root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        frame = ttk.Frame(self._root, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="Microphone and speaker",
            font=("Segoe UI Semibold", 14),
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text="Xenon listens on the input and speaks through the output. "
                 "The choice is saved for the next start.",
            foreground="#555",
        ).pack(anchor="w", pady=(2, 14))

        self._inputs = audio_settings.input_devices()
        self._outputs = audio_settings.output_devices()
        current = audio_settings.load()
        current_input = audio_settings.resolve_input(current.input) if current.input else None

        ttk.Label(frame, text="Microphone").pack(anchor="w")
        self._input_var = tk.StringVar(value=self._input_label(current_input))
        self._input_box = ttk.Combobox(
            frame,
            textvariable=self._input_var,
            values=[self._label(index, name) for index, name in self._inputs],
            state="readonly",
        )
        self._input_box.pack(fill="x", pady=(2, 12))

        ttk.Label(frame, text="Speaker").pack(anchor="w")
        self._output_var = tk.StringVar(
            value=current.output or self._default_output_label()
        )
        self._output_box = ttk.Combobox(
            frame,
            textvariable=self._output_var,
            values=["System default"] + self._outputs,
            state="readonly",
        )
        self._output_box.pack(fill="x", pady=(2, 16))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Test", command=self._test).pack(side="left")
        ttk.Button(buttons, text="Save", command=self._save).pack(side="left", padx=8)
        ttk.Button(buttons, text="Close", command=self._root.destroy).pack(side="right")

        self._status = ttk.Label(frame, text="", foreground="#555")
        self._status.pack(anchor="w", pady=(14, 0))
        self._root.after(120, self._drain)

    def _label(self, index: int, name: str) -> str:
        return f"{index}: {name}"

    def _input_label(self, index: int | None) -> str:
        for device_index, name in self._inputs:
            if device_index == index:
                return self._label(device_index, name)
        return "System default"

    def _default_output_label(self) -> str:
        from xenon.audio import default_output

        return default_output() or "System default"

    def _selected_input(self) -> tuple[str, int | None]:
        text = self._input_var.get()
        if text == "System default":
            return "", None
        index_text, _, name = text.partition(": ")
        try:
            index = int(index_text)
        except ValueError:
            return "", None
        return name.strip(), index

    def _selected_output(self) -> str:
        text = self._output_var.get()
        return "" if text == "System default" else text

    def _save(self) -> None:
        name, index = self._selected_input()
        chosen = audio_settings.AudioSettings(input=name, output=self._selected_output())
        path = audio_settings.save(chosen)
        where = f"{name} (#{index})" if index is not None else "system default"
        self._status.configure(text=f"Saved {where} -> {path}. Restart the orb to use it.")

    def _test(self) -> None:
        name, index = self._selected_input()
        output = self._selected_output()
        self._status.configure(text="Speaking and listening...")
        threading.Thread(
            target=self._run_test,
            args=(name or None, output or None),
            daemon=True,
            name="xenon-audio-test",
        ).start()

    def _run_test(self, input_name: str | None, output_name: str | None) -> None:
        try:
            _, peak = audio_settings.test(input_name, output_name, seconds=0.8)
            heard = "no signal" if peak is None else f"peak {peak:.3f}"
            self._results.put(("ok", f"Heard the test tone on the selected microphone ({heard})."))
        except Exception as error:
            self._results.put(("error", f"Test failed: {error}"))

    def _drain(self) -> None:
        try:
            while True:
                kind, text = self._results.get_nowait()
                self._status.configure(text=text)
        except queue.Empty:
            pass
        self._root.after(120, self._drain)

    def run(self) -> None:
        self._root.mainloop()


if __name__ == "__main__":
    AudioSettingsWindow().run()
