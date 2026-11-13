"""The task thread: one long-running task at a time, cancellable, with focus.

Quick asks stay on the orb's Worker; tasks (plans that act on the desktop)
run here so a slow plan never blocks an answer. The thread is created on
the first ``submit`` and then sleeps in a blocking ``queue.get``, so an orb
that never runs a task pays nothing.

Each task gets a cancel Event and, the first time it starts a child process,
one Windows Job Object with KILL_ON_JOB_CLOSE: Stop closes the job and every
child (agent-desktop, claude.exe) dies with it, mid-turn.

Conversation focus: a task blocked in ``ask()`` receives the next typed or
spoken input through ``offer_input``. Nothing that arrives that way can
approve a card; it only answers the question the task asked.
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from typing import Callable

STATES = ("queued", "running", "waiting", "done", "failed", "cancelled")
CREATE_NO_WINDOW = 0x08000000
CREATE_SUSPENDED = 0x00000004


def resume_process(process) -> bool:
    """Resume every thread of a child started with CREATE_SUSPENDED.

    Uses NtResumeProcess on the Popen handle (``process._handle``), so no
    thread enumeration is needed. False when it could not be resumed.
    """
    if sys.platform != "win32":
        return False
    handle = getattr(process, "_handle", None)
    if handle is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        return ntdll.NtResumeProcess(int(handle)) >= 0
    except Exception:
        return False


@dataclass(frozen=True)
class TaskUpdate:
    task_id: str
    title: str
    state: str          # queued | running | waiting | done | failed | cancelled
    text: str = ""
    step: int = 0
    total: int = 0


class TaskCancelled(Exception):
    """Raised inside a task when it notices it was cancelled."""


# ---------------------------------------------------------------------------
# Job Objects
# ---------------------------------------------------------------------------

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class JobObject:
    """A Windows Job Object whose children die when it is closed.

    Safe off Windows (every method is a no-op) and safe to close twice.
    """

    def __init__(self) -> None:
        self._handle = None
        self._lock = threading.Lock()

    @staticmethod
    def _kernel32():
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        return kernel32

    def create(self) -> bool:
        if sys.platform != "win32":
            return False
        with self._lock:
            if self._handle:
                return True
            import ctypes

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(name, ctypes.c_ulonglong) for name in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class BASIC_LIMIT(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32),
                ]

            class EXTENDED_LIMIT(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BASIC_LIMIT),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = self._kernel32()
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return False
            info = EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info), ctypes.sizeof(info),
            ):
                kernel32.CloseHandle(handle)
                return False
            self._handle = handle
            return True

    def assign(self, pid: int) -> bool:
        if sys.platform != "win32" or not pid:
            return False
        with self._lock:
            if not self._handle:
                return False
            kernel32 = self._kernel32()
            process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
            if not process:
                return False
            try:
                return bool(kernel32.AssignProcessToJobObject(self._handle, process))
            finally:
                kernel32.CloseHandle(process)

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
        if handle and sys.platform == "win32":
            try:
                self._kernel32().CloseHandle(handle)
            except Exception:
                pass

    @property
    def active(self) -> bool:
        return bool(self._handle)


def _com_init() -> None:
    """UI Automation on the task thread needs COM there (STA, like Tk)."""
    if sys.platform != "win32":
        return
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Choice answers
# ---------------------------------------------------------------------------

_ORDINALS = {
    "first": 1, "1st": 1, "1": 1,
    "second": 2, "2nd": 2, "two": 2, "2": 2,
    "third": 3, "3rd": 3, "three": 3, "3": 3,
    "fourth": 4, "4th": 4, "four": 4, "4": 4,
    "fifth": 5, "5th": 5, "five": 5, "5": 5,
    "sixth": 6, "6th": 6, "six": 6, "6": 6,
    "seventh": 7, "7th": 7, "seven": 7, "7": 7,
    "eighth": 8, "8th": 8, "eight": 8, "8": 8,
    "ninth": 9, "9th": 9, "nine": 9, "9": 9,
    "tenth": 10, "10th": 10, "ten": 10, "10": 10,
    "last": -1,
}
# Words that may surround an ordinal without changing which item it names.
_CHOICE_FILLER = frozenset(
    {"the", "one", "option", "number", "choice", "item", "please", "pick", "take",
     "choose", "go", "with", "i", "want", "i'll", "lets", "let's", "it's", "its", "is",
     "that", "say", "um", "uh", "ok", "okay"}
)
_NEGATIONS = frozenset({"no", "not", "don't", "dont", "never", "neither", "nor", "except", "but"})


def _choice_words(text: str) -> list[str]:
    import re

    cleaned = text.lower().replace("’", "'")
    cleaned = re.sub(r"[^\w' ]+", " ", cleaned)
    return cleaned.split()


def match_choice(answer: str, choices: list[str]) -> str | None:
    """Map a typed or spoken answer onto one of ``choices``, or None.

    Accepts the choice's own text (exact, then a unique containment or a
    single close fuzzy match) and ordinals ("first", "2", "the second one",
    "last"). Anything negated or ambiguous returns None: asking again is
    cheap, acting on the wrong item is not.
    """
    import difflib

    try:
        from xenon.untrusted import strip_invisible

        answer = strip_invisible(answer or "")
    except Exception:
        answer = answer or ""
    words = _choice_words(answer)
    if not words or not choices:
        return None
    text = " ".join(words)
    normed = [" ".join(_choice_words(choice)) for choice in choices]
    for index, norm in enumerate(normed):
        if norm and norm == text:
            return choices[index]
    if any(word in _NEGATIONS or word.endswith("n't") for word in words):
        return None

    # Ordinals: every word must be an ordinal or filler, and exactly one
    # position may be named. "one" alone means the first; next to another
    # ordinal ("the second one") it is filler.
    if all(word in _ORDINALS or word in _CHOICE_FILLER for word in words):
        positions = {_ORDINALS[word] for word in words if word in _ORDINALS}
        if not positions and "one" in words:
            positions = {1}
        if len(positions) == 1:
            position = positions.pop()
            index = len(choices) - 1 if position == -1 else position - 1
            if 0 <= index < len(choices):
                return choices[index]
        return None

    contained = [
        index for index, norm in enumerate(normed)
        if norm and ((len(text) >= 3 and text in norm) or (len(norm) >= 3 and norm in text))
    ]
    if len(contained) == 1:
        return choices[contained[0]]
    if len(contained) > 1:
        return None
    close = [
        index for index, norm in enumerate(normed)
        if norm and difflib.SequenceMatcher(None, text, norm).ratio() >= 0.8
    ]
    if len(close) == 1:
        return choices[close[0]]
    return None


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TaskContext:
    """What a running task can do: report, ask, spawn, and notice cancel."""

    def __init__(self, manager: "TaskManager", task_id: str, title: str, source: str) -> None:
        self._manager = manager
        self.task_id = task_id
        self.title = title
        self.source = source
        self.cancel = threading.Event()
        self.job = JobObject()
        self._answer_event = threading.Event()
        self._answer: str | None = None
        self._waiting = False
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def check(self) -> None:
        if self.cancel.is_set():
            raise TaskCancelled(f"{self.title} was stopped")

    def progress(self, text: str, step: int | None = None, total: int | None = None) -> None:
        self._manager._emit(TaskUpdate(self.task_id, self.title, "running", text, step or 0, total or 0))

    def ask(self, question: str, choices: list[str] | None = None, timeout_s: float = 120) -> str | None:
        """Block until Roshan answers (typed or spoken), cancel, or timeout.

        With ``choices`` the answer is mapped onto one of them ("the second
        one", "2", or the choice's own words). An answer that maps onto none
        is asked again once; a second miss returns None rather than a guess,
        because a wrong pick here can lead to acting on the wrong thing.
        """
        options = [str(choice) for choice in (choices or []) if str(choice).strip()]
        prompt = question
        for _attempt in range(2 if options else 1):
            shown = prompt if not options else f"{prompt} ({' / '.join(options)})"
            answer = self._wait_for_answer(shown, timeout_s)
            if answer is None or not options:
                return answer
            picked = match_choice(answer, options)
            if picked is not None:
                return picked
            prompt = f"Sorry, which one? {question}"
        return None

    def _wait_for_answer(self, text: str, timeout_s: float) -> str | None:
        if self.cancel.is_set():
            return None
        with self._lock:
            self._answer = None
            self._answer_event.clear()
            self._waiting = True
        self._manager._emit(TaskUpdate(self.task_id, self.title, "waiting", text))
        try:
            self._answer_event.wait(timeout_s)
        finally:
            with self._lock:
                self._waiting = False
        if self.cancel.is_set():
            return None
        answer = self._answer
        if answer is not None:
            self._manager._emit(TaskUpdate(self.task_id, self.title, "running", "got your answer"))
        return answer

    def _offer(self, text: str) -> bool:
        with self._lock:
            if not self._waiting or self._answer_event.is_set():
                return False
            self._answer = text
            self._answer_event.set()
            return True

    def _wake(self) -> None:
        self._answer_event.set()

    @property
    def waiting(self) -> bool:
        return self._waiting

    def spawn(self, argv: list[str], **popen_kwargs) -> subprocess.Popen:
        """Start a child in this task's Job Object, with no console window."""
        self.check()
        windows = sys.platform == "win32"
        if windows:
            # Started suspended, put in the Job, then resumed: a child that
            # spawns helpers at once (claude.exe -> node) cannot create a
            # grandchild outside the Job before the assignment.
            popen_kwargs["creationflags"] = (
                int(popen_kwargs.get("creationflags", 0)) | CREATE_NO_WINDOW | CREATE_SUSPENDED
            )
            self.job.create()
        process = subprocess.Popen(argv, **popen_kwargs)
        if windows and not self.job.assign(process.pid):
            # A child we cannot kill on Stop must not keep running.
            process.kill()
            raise TaskCancelled("could not put the child process under the task's control")
        if self.cancel.is_set():
            process.kill()
            raise TaskCancelled(f"{self.title} was stopped")
        if windows and not resume_process(process):
            process.kill()
            raise TaskCancelled("could not start the child process after putting it in the task's Job")
        if self.cancel.is_set():
            process.kill()
            raise TaskCancelled(f"{self.title} was stopped")
        return process


@dataclass
class _Queued:
    context: TaskContext
    fn: Callable[[TaskContext], str]


class TaskManager:
    """One task at a time on a lazily created thread.

    ``on_update`` is called on the task thread (or the submitting thread for
    queued/cancelled updates); the orb hops to Tk itself. A second submit
    while busy asks ``on_busy_choice(new_title, decide)``, where ``decide``
    takes "queue", "replace" or "cancel" (anything else cancels).
    """

    def __init__(
        self,
        on_update: Callable[[TaskUpdate], None],
        on_busy_choice: Callable[[str, Callable[[str], None]], None] | None = None,
        com_init: Callable[[], None] | None = None,
    ) -> None:
        self._on_update = on_update
        self._on_busy_choice = on_busy_choice
        self._com_init = com_init if com_init is not None else _com_init
        self._queue: "queue.Queue[_Queued | None]" = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._current: TaskContext | None = None
        self._waiting_list: list[_Queued] = []  # queued, not yet started
        self._closed = False

    # -- state -------------------------------------------------------------

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._current is not None or bool(self._waiting_list)

    @property
    def waiting_for_input(self) -> bool:
        current = self._current
        return current is not None and current.waiting

    @property
    def current_title(self) -> str:
        current = self._current
        return current.title if current is not None else ""

    @property
    def current_source(self) -> str:
        current = self._current
        return current.source if current is not None else ""

    def _emit(self, update: TaskUpdate) -> None:
        try:
            self._on_update(update)
        except Exception:
            pass

    # -- submit / cancel ---------------------------------------------------

    def submit(self, title: str, fn: Callable[[TaskContext], str], source: str = "typed") -> str:
        task_id = uuid.uuid4().hex[:12]
        item = _Queued(TaskContext(self, task_id, title, source), fn)
        with self._lock:
            if self._closed:
                raise RuntimeError("the task manager is shut down")
            busy = self._current is not None or bool(self._waiting_list)
        if busy and self._on_busy_choice is not None:
            decided = threading.Event()

            def decide(choice: str) -> None:
                if decided.is_set():
                    return  # one answer per prompt
                decided.set()
                choice = (choice or "").strip().lower()
                if choice == "queue":
                    self._enqueue(item)
                elif choice == "replace":
                    self.cancel(None)
                    self._enqueue(item)
                else:
                    self._emit(TaskUpdate(task_id, title, "cancelled", "not started"))

            try:
                self._on_busy_choice(title, decide)
            except Exception:
                decide("cancel")
            return task_id
        self._enqueue(item)
        return task_id

    def _enqueue(self, item: _Queued) -> None:
        with self._lock:
            self._waiting_list.append(item)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="xenon-tasks")
                self._thread.start()
        self._emit(TaskUpdate(item.context.task_id, item.context.title, "queued"))
        self._queue.put(item)

    def cancel(self, task_id: str | None = None) -> bool:
        """Stop a task: set its Event, kill its children, wake its ``ask``.

        ``None`` stops the current task and drops everything queued.
        """
        stopped = False
        with self._lock:
            targets: list[TaskContext] = []
            current = self._current
            if current is not None and (task_id is None or current.task_id == task_id):
                targets.append(current)
            for item in list(self._waiting_list):
                if task_id is None or item.context.task_id == task_id:
                    targets.append(item.context)
        for context in targets:
            if not context.cancel.is_set():
                context.cancel.set()
                stopped = True
            context.job.close()
            context._wake()
        return stopped

    def offer_input(self, text: str) -> bool:
        current = self._current
        return current is not None and current._offer(text)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        self.cancel(None)
        if self._thread is not None:
            self._queue.put(None)

    # -- thread ------------------------------------------------------------

    def _run(self) -> None:
        self._com_init()
        while True:
            item = self._queue.get()
            if item is None:
                return
            context = item.context
            with self._lock:
                if item in self._waiting_list:
                    self._waiting_list.remove(item)
                if context.cancel.is_set():
                    started = False
                else:
                    self._current = context
                    started = True
            if not started:
                self._emit(TaskUpdate(context.task_id, context.title, "cancelled", "stopped before it started"))
                continue
            self._emit(TaskUpdate(context.task_id, context.title, "running"))
            state, text = "done", ""
            try:
                result = item.fn(context)
                text = "" if result is None else str(result)
                if context.cancel.is_set():
                    state, text = "cancelled", text or "stopped"
            except TaskCancelled as stop:
                state, text = "cancelled", str(stop) or "stopped"
            except Exception as error:
                if context.cancel.is_set():
                    state, text = "cancelled", "stopped"
                else:
                    state, text = "failed", f"{type(error).__name__}: {error}"
            finally:
                context.job.close()
                with self._lock:
                    self._current = None
            self._emit(TaskUpdate(context.task_id, context.title, state, text))
