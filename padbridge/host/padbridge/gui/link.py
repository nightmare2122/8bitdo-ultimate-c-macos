"""Talks to padbridged from a worker thread and hands results to the GTK main loop."""
import queue
import threading
import time

from gi.repository import GLib

from ..client import Client, ConnectError, ServiceError

POLL_S = 0.25          # status poll while connected
RETRY_S = 1.5          # reconnect interval while the service is unreachable


class ServiceLink:
    """on_status(status) / on_error(kind, message) are called on the main loop.

    status["log"] holds only the entries that are new since the previous poll."""

    def __init__(self, on_status, on_error):
        self.on_status = on_status
        self.on_error = on_error
        self.jobs = queue.Queue()
        self.log_seq = 0
        self.stopped = False
        self.thread = threading.Thread(target=self._run, name="padbridge-link", daemon=True)
        self.thread.start()

    def call(self, cmd, callback=None, **args):
        """Queue a command. callback(result, error_message) runs on the main loop."""
        self.jobs.put((cmd, args, callback))

    def stop(self):
        self.stopped = True
        self.jobs.put(None)

    def _deliver(self, fn, *args):
        GLib.idle_add(lambda: (fn(*args), False)[1])

    def _run(self):
        client = Client(timeout=10)
        delay = 0
        while not self.stopped:
            try:
                job = self.jobs.get(timeout=delay) if delay else self.jobs.get_nowait()
            except queue.Empty:
                job = False
            if job is None:
                break
            try:
                if job:
                    cmd, args, cb = job
                    try:
                        result = client.call(cmd, **args)
                        if cb:
                            self._deliver(cb, result, None)
                    except ServiceError as e:
                        if cb:
                            self._deliver(cb, None, str(e))
                    continue
                st = client.call("status", log_since=self.log_seq)
                if st["log_seq"] < self.log_seq:      # the service restarted: take its whole log
                    self.log_seq = 0
                    st = client.call("status", log_since=0)
                    st["log_reset"] = True
                self.log_seq = st["log_seq"]
                self._deliver(self.on_status, st)
                delay = POLL_S
            except ConnectError as e:
                client.close()
                self.log_seq = 0
                if job:
                    cmd, args, cb = job
                    if cb:
                        self._deliver(cb, None, "PadBridge's service isn't reachable.")
                self._deliver(self.on_error, e.kind, str(e))
                delay = RETRY_S
            except Exception as e:  # malformed reply etc.: report, keep going
                client.close()
                self._deliver(self.on_error, "other", str(e))
                delay = RETRY_S
                time.sleep(0.1)
        client.close()
