"""Blocking client for the control socket (JSON lines). Used by the GUI's worker thread and padbridgectl.

Request:  {"id": 1, "cmd": "status", ...args}\n
Reply:    {"id": 1, "ok": true, "result": {...}}\n   or   {"id": 1, "ok": false, "error": "message"}\n
"""
import errno
import json
import os
import socket

from .config import INSTALL_DIR, UNIT_PATH, socket_path


class ServiceError(Exception):
    """The service answered with ok: false."""


class ConnectError(Exception):
    """Could not reach the service. `kind` is one of: not_installed, not_running, permission, other."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def service_installed():
    return os.path.exists(UNIT_PATH) or os.path.isdir(INSTALL_DIR) or bool(os.environ.get("PADBRIDGE_SOCKET"))


class Client:
    def __init__(self, path=None, timeout=15.0):
        self.path = path or socket_path()
        self.timeout = timeout
        self.sock = None
        self.buf = b""
        self.next_id = 1

    def connect(self):
        if self.sock:
            return
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.path)
        except OSError as e:
            s.close()
            if e.errno in (errno.EACCES, errno.EPERM):
                raise ConnectError("permission", str(e)) from None
            if e.errno in (errno.ENOENT, errno.ECONNREFUSED):
                kind = "not_running" if service_installed() else "not_installed"
                raise ConnectError(kind, str(e)) from None
            raise ConnectError("other", str(e)) from None
        self.sock, self.buf = s, b""

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def call(self, cmd, **args):
        self.connect()
        req = {"id": self.next_id, "cmd": cmd, **args}
        self.next_id += 1
        try:
            self.sock.sendall(json.dumps(req).encode() + b"\n")
            while b"\n" not in self.buf:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise ConnectionResetError("the service closed the connection")
                self.buf += chunk
        except OSError as e:
            self.close()
            if isinstance(e, PermissionError):
                raise ConnectError("permission", str(e)) from None
            raise ConnectError("not_running", str(e)) from None
        line, self.buf = self.buf.split(b"\n", 1)
        reply = json.loads(line)
        if not reply.get("ok"):
            raise ServiceError(reply.get("error") or "The service reported an error.")
        return reply.get("result")
