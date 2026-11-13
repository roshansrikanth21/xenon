"""Local hand-off from the CLI to the running orb.

One named pipe, ``\\\\.\\pipe\\xenon-<user SID>``, created with a DACL that
only the current user can open and with ``PIPE_REJECT_REMOTE_CLIENTS``.
Every request must also carry the per-boot secret the orb writes to
``%LOCALAPPDATA%\\Xenon\\ipc.key`` (owner-only ACL) when it starts.

The server can do exactly one thing: submit a goal to the TaskManager.
There is no message that approves anything; a card on the orb still needs
a real click. Requests are JSON ``{"secret", "goal", "source"}``; the source
a caller claims is ignored and recorded as "ipc".

The server thread sleeps inside ``ConnectNamedPipe`` (a blocking kernel
wait), so an idle orb spends no CPU on it.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import threading
from pathlib import Path
from typing import Callable

MAX_REQUEST = 8192
MAX_GOAL = 2000

PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_TYPE_MESSAGE = 0x00000004
PIPE_READMODE_MESSAGE = 0x00000002
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
ERROR_PIPE_CONNECTED = 535
INVALID_HANDLE_VALUE = -1
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
CREATE_ALWAYS = 2
FILE_ATTRIBUTE_NORMAL = 0x80
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# The whole protocol. There is deliberately no verb: a request can only carry
# a goal, and any other field (say "approve") makes it invalid.
REQUEST_FIELDS = frozenset({"secret", "goal", "source"})


def _log(message: str) -> None:
    """One line on stderr, like the rest of the orb. Never includes a secret."""
    try:
        print(f"ipc: {message}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _token_user_sid(process) -> str:
    """The user SID string of ``process``'s token, or "" when unreadable."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        return ""
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        buffer = ctypes.create_string_buffer(size.value or 256)
        if not advapi32.GetTokenInformation(token, 1, buffer, ctypes.sizeof(buffer), ctypes.byref(size)):
            return ""
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            return ""
        try:
            return str(text.value or "")
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def current_user_sid() -> str:
    """The current user's SID string ("S-1-5-21-..."), or "" off Windows."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        return _token_user_sid(kernel32.GetCurrentProcess())
    except Exception:
        return ""


def pipe_server_is_current_user(pipe_handle) -> bool:
    """True only when the process serving ``pipe_handle`` runs as this user.

    The pipe name is predictable, so another account could create it first
    and wait for the CLI to hand it the secret and the goal. The client asks
    the kernel who serves the pipe and refuses anything it cannot confirm.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
        kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        pid = wintypes.ULONG(0)
        if not kernel32.GetNamedPipeServerProcessId(pipe_handle, ctypes.byref(pid)) or not pid.value:
            return False
        process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not process:
            return False
        try:
            server_sid = _token_user_sid(process)
        finally:
            kernel32.CloseHandle(process)
        mine = current_user_sid()
        return bool(mine) and server_sid == mine
    except Exception:
        return False


def pipe_name(user: str | None = None) -> str:
    who = user if user is not None else (current_user_sid() or os.environ.get("USERNAME", "user"))
    return r"\\.\pipe\xenon-" + re.sub(r"[^A-Za-z0-9_.-]", "_", who)


def key_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "Xenon" / "ipc.key"


class _Security:
    """SECURITY_ATTRIBUTES whose DACL grants only the current user (and is protected)."""

    def __init__(self, rights: str = "GA") -> None:
        import ctypes
        from ctypes import wintypes

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", wintypes.BOOL)]

        sid = current_user_sid()
        if not sid:
            raise OSError("could not read the current user's SID")
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p
        ]
        descriptor = ctypes.c_void_p()
        # D:P = protected DACL (nothing inherited); one ACE for this user only.
        sddl = f"D:P(A;;{rights};;;{sid})"
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise OSError(ctypes.get_last_error(), "could not build the security descriptor")
        self._descriptor = descriptor
        self.attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)

    def free(self) -> None:
        import ctypes

        if self._descriptor:
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree(self._descriptor)
            self._descriptor = None


def write_secret(path: Path | None = None) -> str:
    """Make a fresh secret and write it where only this user can read it."""
    path = Path(path) if path is not None else key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)
    data = secret.encode("ascii")
    if sys.platform != "win32":
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return secret
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        os.remove(path)  # CREATE_ALWAYS keeps an old file's ACL; start clean
    except OSError:
        pass
    security = _Security("FA")
    try:
        handle = kernel32.CreateFileW(str(path), GENERIC_WRITE, 0, ctypes.byref(security.attributes),
                                      CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
        if not handle or handle == wintypes.HANDLE(INVALID_HANDLE_VALUE).value:
            raise OSError(ctypes.get_last_error(), f"could not create {path}")
        try:
            written = wintypes.DWORD(0)
            if not kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None):
                raise OSError(ctypes.get_last_error(), f"could not write {path}")
        finally:
            kernel32.CloseHandle(handle)
    finally:
        security.free()
    return secret


def read_secret(path: Path | None = None) -> str | None:
    try:
        text = Path(path if path is not None else key_path()).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def handle_request(raw: bytes, secret: str, submit: Callable[[str, str], str]) -> dict:
    """Validate one request and submit its goal. The only action there is."""
    if not secret:
        return {"ok": False, "error": "the orb has no secret"}
    if len(raw) > MAX_REQUEST:
        return {"ok": False, "error": "request too large"}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"ok": False, "error": "bad json"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "bad request"}
    offered = payload.get("secret")
    if not isinstance(offered, str) or not hmac.compare_digest(offered.encode("utf-8"), secret.encode("utf-8")):
        _log("rejected a request with a wrong secret")
        return {"ok": False, "error": "wrong secret"}
    unknown = sorted(str(key)[:32] for key in payload if key not in REQUEST_FIELDS)
    if unknown:
        # Fail closed: a field we do not understand is never ignored.
        _log(f"rejected a request with unknown field(s): {', '.join(unknown)}")
        return {"ok": False, "error": "unknown field: " + ", ".join(unknown)}
    if not isinstance(payload.get("source", ""), str):
        return {"ok": False, "error": "bad request"}
    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return {"ok": False, "error": "no goal"}
    goal = goal.strip()
    if len(goal) > MAX_GOAL:
        return {"ok": False, "error": "goal too long"}
    try:
        task_id = submit(goal, "ipc")  # never what the caller claims
    except Exception as error:
        return {"ok": False, "error": f"could not start the task: {error}"}
    return {"ok": True, "task_id": str(task_id or "")}


class IpcServer:
    """The orb's end of the pipe. ``submit(goal, origin)`` returns a task id."""

    def __init__(
        self,
        submit: Callable[[str, str], str],
        name: str | None = None,
        key_file: str | Path | None = None,
    ) -> None:
        self._submit = submit
        self.name = name or pipe_name()
        self._key_file = Path(key_file) if key_file is not None else key_path()
        self._secret = ""
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        # True once this process created the pipe and wrote the key.
        self._owner = False
        self.error = ""

    def start(self) -> bool:
        """Start the pipe thread. The key file is written only once this
        process owns the pipe, so a second orb that cannot create it never
        replaces (or, on exit, deletes) the first orb's key."""
        if sys.platform != "win32" or self._thread is not None:
            return False
        self._thread = threading.Thread(target=self._serve, daemon=True, name="xenon-ipc")
        self._thread.start()
        return True

    def _api(self):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                              wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
        kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel32.ConnectNamedPipe.restype = wintypes.BOOL
        kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        return ctypes, wintypes, kernel32

    def _serve(self) -> None:
        try:
            ctypes, wintypes, kernel32 = self._api()
            security = _Security("GA")
        except Exception as error:
            self.error = f"IPC unavailable: {error}"
            return
        invalid = wintypes.HANDLE(INVALID_HANDLE_VALUE).value
        try:
            # One instance, created once as the first: if that fails another
            # process holds the name and the orb never shares it. The same
            # handle then serves client after client (Connect, Disconnect),
            # so a slow client closing late can never make a re-create fail.
            handle = kernel32.CreateNamedPipeW(
                self.name, PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1, 4096, 4096, 0, ctypes.byref(security.attributes),
            )
            if not handle or handle == invalid:
                self.error = f"could not create the pipe (error {ctypes.get_last_error()})"
                _log(self.error)
                return
            try:
                self._secret = write_secret(self._key_file)
            except Exception as error:
                self.error = f"could not write the IPC key: {error}"
                _log(self.error)
                kernel32.CloseHandle(handle)
                return
            self._owner = True
            self._ready.set()
            try:
                while not self._stop.is_set():
                    connected = kernel32.ConnectNamedPipe(handle, None)  # blocks; no CPU while idle
                    if not connected and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
                        kernel32.DisconnectNamedPipe(handle)  # e.g. the client already left
                        continue
                    try:
                        if self._stop.is_set():
                            break
                        buffer = ctypes.create_string_buffer(MAX_REQUEST + 1)
                        read = wintypes.DWORD(0)
                        ok = kernel32.ReadFile(handle, buffer, MAX_REQUEST + 1, ctypes.byref(read), None)
                        raw = buffer.raw[: read.value] if ok else b"x" * (MAX_REQUEST + 1)
                        reply = json.dumps(handle_request(raw, self._secret, self._submit)).encode("utf-8")
                        written = wintypes.DWORD(0)
                        kernel32.WriteFile(handle, reply, len(reply), ctypes.byref(written), None)
                        kernel32.FlushFileBuffers(handle)
                    finally:
                        kernel32.DisconnectNamedPipe(handle)
            finally:
                kernel32.CloseHandle(handle)
        finally:
            self._ready.set()
            security.free()

    def wait_ready(self, timeout_s: float = 2.0) -> bool:
        """True once the pipe exists (False if it could not be created)."""
        return self._ready.wait(timeout_s) and not self.error

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        if self._owner:
            # Wake ConnectNamedPipe by connecting once; the loop then exits.
            # Only the owner does this: another orb's pipe is left alone.
            try:
                with open(self.name, "r+b", buffering=0):
                    pass
            except OSError:
                pass
        thread.join(timeout=1.0)
        if not self._owner or not self._secret:
            return  # never wrote the key, so never deletes one
        try:
            if read_secret(self._key_file) == self._secret:
                self._key_file.unlink()
        except OSError:
            pass


def exchange_raw(target: str, request: bytes, verify_server: bool = True) -> dict | None:
    """Send one raw request down ``target`` and return the decoded reply.

    Blocking; ``send_task`` runs it on a short-lived thread with a timeout.
    Before anything is written the pipe's server must be this same user.
    """
    try:
        with open(target, "r+b", buffering=0) as pipe:
            if verify_server:
                import msvcrt

                if not pipe_server_is_current_user(msvcrt.get_osfhandle(pipe.fileno())):
                    _log("refused to talk to a pipe served by another account")
                    return None
            pipe.write(request)
            reply = pipe.read(MAX_REQUEST)
        decoded = json.loads(reply.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def send_task(
    goal: str,
    timeout_s: float = 2.0,
    name: str | None = None,
    key_file: str | Path | None = None,
) -> str | None:
    """Ask a running orb to start ``goal``. Returns its task id, or None.

    None means no orb answered (not running, no key, wrong secret, timeout).
    """
    if sys.platform != "win32":
        return None
    secret = read_secret(Path(key_file) if key_file is not None else None)
    if not secret:
        return None
    target = name or pipe_name()
    result: dict = {}

    request = json.dumps({"secret": secret, "goal": goal, "source": "cli"}).encode("utf-8")

    def exchange() -> None:
        reply = exchange_raw(target, request)
        if reply:
            result.update(reply)

    worker = threading.Thread(target=exchange, daemon=True, name="xenon-ipc-client")
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive() or not result.get("ok"):
        return None
    task_id = str(result.get("task_id") or "")
    return task_id or None
