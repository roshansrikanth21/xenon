"""Real-hardware checks, so "it works" is not just a unit test's opinion.

The unit suite uses fakes on purpose: no test should open your microphone,
change your speaker volume, or wait on a real speech model. This module is the
other half. It touches the actual machine and prints what really happened:

    python -m xenon verify            # read-only checks
    python -m xenon verify --mic      # open the microphone for one second
    python -m xenon verify --speak    # say one sentence through SAPI
    python -m xenon verify --duck     # duck other apps for 1.5 s, then restore
    python -m xenon verify --all      # all of the above

Exit code is 0 unless a check that should work actually failed.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str

    def line(self) -> str:
        return f"{self.status:<4} {self.name:<14} {self.detail}"


def _result(name: str, status: str, detail: str) -> Check:
    return Check(name, status, detail)


def check_platform() -> Check:
    if sys.platform != "win32":
        return _result("platform", FAIL, f"{sys.platform} (Xenon targets Windows 11)")
    return _result("platform", PASS, f"Windows, Python {sys.version.split()[0]}")


def check_voice_deps() -> Check:
    missing = []
    for module in ("sounddevice", "faster_whisper"):
        try:
            __import__(module)
        except Exception:
            missing.append(module)
    if missing:
        return _result("voice deps", FAIL, "missing " + ", ".join(missing) + ' (pip install -e ".[voice]")')
    return _result("voice deps", PASS, "sounddevice + faster-whisper import")


def check_sapi(speak: bool = False) -> Check:
    from xenon.audio import SapiSpeaker, list_voices

    try:
        voices = list_voices()
    except Exception as error:
        return _result("sapi", FAIL, f"voices unavailable: {error}")
    if not voices:
        return _result("sapi", FAIL, "no SAPI voices installed")
    detail = f"{len(voices)} voice(s): {voices[0]}"
    if speak:
        try:
            SapiSpeaker().speak("Xenon audio check. If you can hear this, speech output works.")
        except Exception as error:
            return _result("sapi", FAIL, f"speak failed: {error}")
        detail += " - spoke one sentence"
    return _result("sapi", PASS, detail)


def check_microphone(record: bool = False, seconds: float = 1.0) -> Check:
    try:
        import sounddevice as sd
    except Exception as error:
        return _result("microphone", FAIL, f"sounddevice unavailable: {error}")
    try:
        inputs = [
            (index, device)
            for index, device in enumerate(sd.query_devices())
            if int(device.get("max_input_channels", 0)) > 0
        ]
    except Exception as error:
        return _result("microphone", FAIL, f"device query failed: {error}")
    if not inputs:
        return _result("microphone", FAIL, "no input device")
    detail = f"{len(inputs)} input device(s): {inputs[0][1].get('name')}"
    if not record:
        return _result("microphone", PASS, detail + " - not opened (use --mic)")
    from xenon.audio import MicrophoneSource

    source = MicrophoneSource()
    try:
        samples = []
        deadline = time.monotonic() + max(0.2, seconds)
        while time.monotonic() < deadline:
            samples.extend(source.read_frame())
    except Exception as error:
        return _result("microphone", FAIL, f"capture failed: {error}")
    finally:
        source.close()
    peak = max((abs(value) for value in samples), default=0.0)
    return _result("microphone", PASS, detail + f" - {len(samples)} samples, peak {peak:.3f}")


def check_ducker(duck: bool = False, seconds: float = 1.5) -> Check:
    from xenon.duck import NullDucker, make_ducker

    ducker = make_ducker()
    if isinstance(ducker, NullDucker):
        return _result("ducker", SKIP, "pycaw unavailable or XENON_DUCK=off")
    if not duck:
        return _result("ducker", PASS, "pycaw ready - not exercised (use --duck)")
    try:
        count = ducker.duck()
        time.sleep(max(0.2, seconds))
    finally:
        ducker.restore()
    if count == 0:
        return _result("ducker", PASS, "no other app is playing audio right now")
    return _result("ducker", PASS, f"ducked and restored {count} app session(s) for {seconds:.1f}s")


def check_uia() -> Check:
    try:
        from pywinauto.uia_defines import IUIA

        client = IUIA().ui_automation_client
        if client is None:
            return _result("uia", FAIL, "UI Automation client did not start")
    except Exception as error:
        return _result("uia", FAIL, f"UI Automation unavailable: {error}")
    return _result("uia", PASS, "UI Automation client ready")


def check_profile() -> Check:
    from xenon.field_assistant import profile_path

    path = profile_path()
    if not os.path.exists(path):
        return _result("profile", SKIP, f"no {path} yet - form suggestions stay empty until you add it")
    return _result("profile", PASS, path)


def run(speak: bool = False, record: bool = False, duck: bool = False) -> list[Check]:
    return [
        check_platform(),
        check_voice_deps(),
        check_sapi(speak=speak),
        check_microphone(record=record),
        check_ducker(duck=duck),
        check_uia(),
        check_profile(),
    ]


def report(checks: list[Check]) -> int:
    for check in checks:
        print(check.line())
    failed = [check for check in checks if check.status == FAIL]
    print()
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed or skipped; {len(failed)} failed.")
    return 1 if failed else 0
