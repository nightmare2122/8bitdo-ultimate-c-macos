"""systemd-logind inhibitor locks: keep the laptop awake with the lid closed while it bridges.

We take one "block" lock on handle-lid-switch:sleep:idle from org.freedesktop.login1.Manager.Inhibit.
logind keeps it for as long as we hold the returned file descriptor; closing the fd releases it.

Why this is enough on GNOME (checked against gnome-settings-daemon's plugins/power/gsd-power-manager.c):
  * Lid close: logind performs HandleLidSwitch itself. gsd-power only takes its own handle-lid-switch lock
    while an external monitor is connected, and its do_lid_closed_action() never suspends (it may lock
    the screen). logind always honours low-level handle-lid-switch locks, whatever LidSwitchIgnoreInhibited
    says, so with our lock held a closed lid does nothing.
  * Automatic suspend: gsd-power's idle timer asks logind to suspend. A root-held "sleep" block lock makes
    logind refuse that unless the caller may override inhibitors (polkit
    org.freedesktop.login1.suspend-ignore-inhibit, auth_admin_keep by default), so the request fails.
  * "idle" stops logind's own IdleAction= if one is configured.
"""
import os
import shutil
import subprocess

WHAT = "handle-lid-switch:sleep:idle"
WHO = "PadBridge"
WHY = "Bridging a controller to a Mac with the lid closed"


class LogindInhibitor:
    def __init__(self):
        self.fd = None
        self.proc = None       # systemd-inhibit fallback
        self.error = None

    @property
    def active(self):
        return self.fd is not None or (self.proc is not None and self.proc.poll() is None)

    def acquire(self):
        if self.active:
            return True
        self.error = None
        try:
            import dbus
            bus = dbus.SystemBus()
            mgr = dbus.Interface(bus.get_object("org.freedesktop.login1", "/org/freedesktop/login1"),
                                 "org.freedesktop.login1.Manager")
            self.fd = mgr.Inhibit(WHAT, WHO, WHY, "block").take()
            bus.close()
            return True
        except ImportError:
            pass
        except Exception as e:  # dbus.DBusException and friends
            self.error = f"logind refused the inhibitor: {e}"
        # Fallback: systemd-inhibit holds the same lock for as long as its child runs.
        tool = shutil.which("systemd-inhibit")
        if tool:
            try:
                self.proc = subprocess.Popen(
                    [tool, f"--what={WHAT}", f"--who={WHO}", f"--why={WHY}", "--mode=block", "sleep", "infinity"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.error = None
                return True
            except OSError as e:
                self.error = f"could not run systemd-inhibit: {e}"
        elif not self.error:
            self.error = "neither python3-dbus nor systemd-inhibit is available"
        return False

    def release(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None


class FakeInhibitor:
    def __init__(self):
        self.active = False
        self.error = None
        self.history = []

    def acquire(self):
        if not self.active:
            self.history.append("acquire")
        self.active = True
        return True

    def release(self):
        if self.active:
            self.history.append("release")
        self.active = False
