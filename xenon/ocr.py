"""Local, offline OCR fallback for windows UI Automation cannot describe.

Windows 10 and 11 ship an OCR engine through WinRT (``Windows.Media.Ocr``).
Windows PowerShell 5.1 can drive it without any extra package, so an Electron
or game window that exposes no accessible text can still be read locally. No
image ever leaves the machine.

The engine is intentionally a *fallback*: UI Automation is faster and keeps
structure, and a configured vision model can describe diagrams that OCR
cannot. ``VisionChain`` tries providers in order and returns the first real
description.
"""
from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from typing import Protocol

from xenon.screen import CapturedFrame, WindowSnapshot
from xenon.vision import VisionUnavailable

DEFAULT_TIMEOUT_S = 20.0
CREATE_NO_WINDOW = 0x08000000

PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Path = $env:XENON_OCR_IMAGE
if ([string]::IsNullOrWhiteSpace($Path)) {
    Write-Error 'XENON_OCR_IMAGE is not set'
    exit 2
}

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    Write-Error 'no OCR language pack is installed'
    exit 3
}
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$result.Text
"""


class OcrRunner(Protocol):
    def __call__(self, image: bytes) -> str: ...


class WindowsOcr:
    """Implements ``screen.VisionProvider`` with the built-in Windows OCR."""

    def __init__(
        self,
        runner: OcrRunner | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._runner = runner
        self._timeout_s = timeout_s

    def describe(
        self, frame: CapturedFrame | None, snapshot: WindowSnapshot
    ) -> str:
        if frame is None:
            raise VisionUnavailable("no screenshot was captured for local OCR")
        if not frame.image:
            raise VisionUnavailable("screenshot was empty for local OCR")

        try:
            text = self._runner(frame.image) if self._runner else self._run(frame.image)
        except VisionUnavailable:
            raise
        except Exception as error:
            raise VisionUnavailable(f"local OCR failed: {error}") from error

        text = (text or "").strip()
        if not text:
            raise VisionUnavailable("local OCR found no readable text")
        return text

    def _run(self, image: bytes) -> str:
        encoded = base64.b64encode(PS_SCRIPT.encode("utf-16-le")).decode("ascii")
        path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
                handle.write(image)
                path = handle.name
            env = dict(os.environ, XENON_OCR_IMAGE=path)
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    encoded,
                ],
                capture_output=True,
                timeout=self._timeout_s,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                env=env,
            )
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise VisionUnavailable(
                "local OCR is unavailable" + (f": {detail}" if detail else "")
            )
        return completed.stdout.decode("utf-8", errors="replace")


class VisionChain:
    """Try several vision providers, returning the first real description."""

    def __init__(self, providers: list[object]) -> None:
        self._providers = [provider for provider in providers if provider is not None]

    def describe(
        self, frame: CapturedFrame | None, snapshot: WindowSnapshot
    ) -> str:
        if not self._providers:
            raise VisionUnavailable("no vision or OCR provider is configured")

        reasons: list[str] = []
        for provider in self._providers:
            name = type(provider).__name__
            try:
                text = provider.describe(frame, snapshot)  # type: ignore[attr-defined]
            except Exception as error:
                reasons.append(f"{name}: {error}")
                continue
            if text and text.strip():
                return text.strip()
            reasons.append(f"{name}: returned no text")
        raise VisionUnavailable("; ".join(reasons))


def local_ocr_enabled() -> bool:
    value = os.environ.get("XENON_OCR", "1").strip().lower()
    return value not in {"0", "off", "false", "no"}
