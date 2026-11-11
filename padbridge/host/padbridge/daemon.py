"""padbridged: the root service that runs the PadBridge engine and answers the GUI.

    python3 -m padbridge.daemon [--run-dir DIR] [--config-dir DIR] [--engine-dir DIR] [--engine PATH]
                                [--fake-system] [--fake-inhibit]

* Supervises the engine (ds4_bt_bridge.py) as a child process with --status-file, restarting it with
  backoff when it dies, and stopping it when the user stops bridging.
* Serves a control socket (JSON lines) at <run dir>/control.sock, mode 0660 root:padbridge. Connections
  are also checked with SO_PEERCRED: root, the service's own user, or members of the padbridge group.
* Holds a logind inhibitor while "keep bridging with the lid closed" is on, and keeps the Bluetooth
  radio's and the controller's USB devices out of autosuspend while the engine runs.
* Persists settings in <config dir>/config.json.

--fake-system and --fake-inhibit replace every system-changing action (ds4_bt_setup.sh, bluetoothctl,
systemctl, sysfs, logind) with recorders, so the service can run unprivileged against the mock engine.
"""
import argparse
import asyncio
import collections
import grp
import json
import os
import pwd
import re
import signal
import socket
import struct
import sys
import time

from . import GROUP, VERSION
from . import config
from .config import Paths, SettingsError
from .inhibit import FakeInhibitor, LogindInhibitor
from .system import FakeSystem, RealSystem

ENGINE_TS = re.compile(r"^\d\d:\d\d:\d\d ")
EXCEPTION = re.compile(r"^[A-Za-z_.]*(Error|Exception|Exit)\b.*: ")
BACKOFF = (1, 2, 4, 8, 15, 30)
STABLE_AFTER = 60          # an engine that ran this long resets the crash backoff
STALE_AFTER = 3.0          # status.json older than this (engine writes every 0.5 s) is stale
MAX_LINE = 1 << 20


class Service:
    def __init__(self, paths, system, inhibitor, python=None, stop_timeout=5.0, tick=0.5, start_grace=15.0):
        self.paths = paths
        self.system = system
        self.inhibitor = inhibitor
        self.python = python or sys.executable
        self.stop_timeout = stop_timeout
        self.tick_s = tick
        self.start_grace = start_grace
        self.settings = config.load(paths.config_file)
        self.proc = None
        self.engine_started = None
        self.expect_exit = False
        self.failures = 0
        self.restarts = 0
        self.retry_at = None
        self.last_exit = None
        self.last_lines = collections.deque(maxlen=8)
        self.busy = None             # "identity" while ds4_bt_setup.sh runs
        self.hold = None             # "forget-on-mac": engine kept stopped until the Mac has forgotten the device
        self.last_bt_name = None
        self.last_action = None
        self.engine_status = None
        self.closing = False
        self.wake = asyncio.Event()
        self.logs = collections.deque(maxlen=500)
        self.seq = 0
        self.boot = None
        self.boot_checked = 0.0
        self.pinned = {}             # usb dir -> previous power/control value
        self.alias = (None, None)    # (address, name)
        self.inhibit_logged = None
        self.server = None
        self.tasks = []

    # ------------------------------------------------------------------ log
    def log(self, text, source="service"):
        self.seq += 1
        self.logs.append({"seq": self.seq, "time": time.time(), "source": source, "text": text})
        if source == "service":
            print(f"padbridged: {text}", flush=True)

    # ------------------------------------------------------------------ engine
    def identities(self):
        """What the GUI offers: the engine's own list when it reports one, else our table.
        Xbox Series X|S is paused (user decision) and never offered, even if an engine lists it."""
        reported = (self.engine_status or {}).get("identities")
        out = []
        source = reported if isinstance(reported, list) and reported else config.IDENTITY_TABLE
        for item in source:
            if isinstance(item, dict) and item.get("id") in config.IDENTITY_INFO:
                out.append({**config.IDENTITY_INFO[item["id"]], **item, "supported": True})
        return out

    def engine_argv(self):
        identity, _ = self.system.setup_state()
        mode = identity if identity in config.IDENTITIES else self.settings["identity"]
        argv = [self.python, self.paths.engine, "--status-file", self.paths.status_file,
                "--mode", mode, "--rate", str(self.settings["rate"])]
        argv += ["--inflight", str(self.settings["inflight"])]
        if self.settings.get("name"):
            argv += ["--name", self.settings["name"]]
        if not self.settings["grab"]:
            argv.append("--no-grab")
        return argv

    async def spawn(self):
        os.makedirs(self.paths.run_dir, exist_ok=True)
        try:
            os.remove(self.paths.status_file)
        except FileNotFoundError:
            pass
        env = dict(os.environ, PYTHONUNBUFFERED="1", PADBRIDGE_ENGINE_DIR=self.paths.engine_dir)
        argv = self.engine_argv()
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, cwd=self.paths.engine_dir,
                env=env, limit=MAX_LINE)
        except OSError as e:
            self.proc = None
            self.log(f"Could not start the engine: {e}")
            self.last_lines.append(str(e))
            return False
        self.engine_started = time.time()
        self.engine_status = None
        self.last_lines.clear()
        self.log(f"Engine started as {argv[argv.index('--mode') + 1]} at {self.settings['rate']} reports/s (pid {self.proc.pid})")
        return True

    async def read_output(self, proc):
        while True:
            try:
                line = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if not text:
                continue
            text = ENGINE_TS.sub("", text, count=1)
            self.last_lines.append(text)
            self.log(text, source="engine")

    async def wait_wake(self, timeout=None):
        try:
            await asyncio.wait_for(self.wake.wait(), timeout)
        except TimeoutError:
            pass
        self.wake.clear()

    def poke(self):
        self.wake.set()

    async def supervise(self):
        while not self.closing:
            if not self.settings["bridging"] or self.busy or self.hold:
                await self.wait_wake()
                continue
            if self.retry_at and time.time() < self.retry_at:
                await self.wait_wake(self.retry_at - time.time())
                continue
            self.retry_at = None
            if not await self.spawn():
                self.schedule_retry(None)
                continue
            proc = self.proc
            reader = asyncio.create_task(self.read_output(proc))
            code = await proc.wait()
            try:
                await asyncio.wait_for(reader, 2)
            except TimeoutError:
                reader.cancel()
            uptime = time.time() - (self.engine_started or time.time())
            self.proc = None
            self.engine_status = None
            if self.expect_exit or self.closing:
                self.expect_exit = False
                self.failures = 0
                continue
            if uptime >= STABLE_AFTER:
                self.failures = 0
            self.schedule_retry(code)

    def schedule_retry(self, code):
        self.failures += 1
        self.restarts += 1
        delay = BACKOFF[min(self.failures, len(BACKOFF)) - 1]
        self.retry_at = time.time() + delay
        message = self.failure_message()
        self.last_exit = {"code": code, "time": time.time(), "message": message}
        self.log(f"The engine stopped{'' if code is None else f' (exit code {code})'}. "
                 f"Trying again in {delay} s.")

    def failure_message(self):
        """The most telling line the engine printed before it died."""
        lines = [line.strip() for line in self.last_lines if line.strip()]
        for line in reversed(lines):          # a Python exception says the most ("OSError: ...")
            if EXCEPTION.match(line):
                return line
        for line in reversed(lines):
            if not line.startswith(("Traceback", "File ")):
                return line
        return lines[-1] if lines else None

    async def stop_engine(self):
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        self.expect_exit = True
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), self.stop_timeout)
        except TimeoutError:
            self.log("The engine did not stop in time; killing it")
            proc.kill()
            await proc.wait()
        # let the supervisor observe the exit before anyone respawns
        for _ in range(100):
            if self.proc is not proc:
                break
            await asyncio.sleep(0.01)

    # ------------------------------------------------------------------ periodic housekeeping
    def read_engine_status(self):
        if self.proc is None:
            self.engine_status = None
            return
        try:
            with open(self.paths.status_file) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.get("pid") == self.proc.pid:
            self.engine_status = data
            if data.get("bt_name"):
                self.last_bt_name = data["bt_name"]   # still known while the engine is stopped

    def status_fresh(self):
        st = self.engine_status
        return bool(st) and time.time() - float(st.get("updated_at") or 0) < STALE_AFTER

    def want_inhibit(self):
        return self.settings["lid_closed"] and self.settings["bridging"] and not self.closing

    def update_inhibitor(self):
        if self.want_inhibit():
            if not self.inhibitor.active:
                ok = self.inhibitor.acquire()
                if ok and self.inhibit_logged != "on":
                    self.log("Holding the lid-switch and sleep inhibitor: the laptop stays awake with the lid closed")
                    self.inhibit_logged = "on"
                elif not ok and self.inhibit_logged != "error":
                    self.log(f"Could not keep the laptop awake: {self.inhibitor.error}")
                    self.inhibit_logged = "error"
        elif self.inhibitor.active:
            self.inhibitor.release()
            self.log("Released the inhibitor: closing the lid suspends the laptop again")
            self.inhibit_logged = None

    def update_usb_power(self):
        running = self.proc is not None and not self.closing
        if running:
            ctrl = ((self.engine_status or {}).get("controller") or {}).get("path")
            for d in self.system.usb_dirs(ctrl):
                if d in self.pinned:
                    continue
                old = self.system.read_power(d)
                if old is None:
                    continue
                if old != "on" and not self.system.write_power(d, "on"):
                    continue
                self.pinned[d] = old
        else:
            for d, old in list(self.pinned.items()):
                if old != "on":
                    self.system.write_power(d, old)
                del self.pinned[d]

    def paired_address(self):
        try:
            with open(self.paths.host_file) as f:
                addr = f.read().strip()
                if addr:
                    return addr
        except OSError:
            pass
        host = (self.engine_status or {}).get("host") or {}
        return host.get("address")

    async def refresh_alias(self, addr):
        name = await self.system.bt_alias(addr)
        if self.alias[0] == addr:
            self.alias = (addr, name)

    async def refresh_boot(self):
        self.boot_checked = time.time()
        self.boot = await self.system.boot_enabled()

    async def housekeeping(self):
        while not self.closing:
            self.read_engine_status()
            self.update_inhibitor()
            self.update_usb_power()
            addr = self.paired_address()
            if addr != self.alias[0]:
                self.alias = (addr, None)
                if addr:
                    asyncio.create_task(self.refresh_alias(addr))
            if time.time() - self.boot_checked > 30:
                asyncio.create_task(self.refresh_boot())
            await asyncio.sleep(self.tick_s)

    # ------------------------------------------------------------------ state for clients
    def state(self):
        if self.busy:
            return "applying"
        if self.hold:
            return "hold"
        if not self.settings["bridging"]:
            return "stopped"
        if self.proc is None:
            return "waiting" if self.retry_at else "starting"
        if self.status_fresh():
            return "running"
        if time.time() - (self.engine_started or 0) < self.start_grace:
            return "starting"
        return "unresponsive"

    def status(self, log_since=0):
        sys_identity, dropin = self.system.setup_state()
        self.read_engine_status()
        return {
            "service": {
                "version": VERSION,
                "state": self.state(),
                "busy": self.busy,
                "hold": self.hold,
                "bt_name": self.settings.get("name") or self.last_bt_name,
                "engine_pid": self.proc.pid if self.proc else None,
                "engine_started_at": self.engine_started if self.proc else None,
                "status_fresh": self.status_fresh(),
                "restarts": self.restarts,
                "retry_at": self.retry_at,
                "last_exit": self.last_exit,
                "last_action": self.last_action,
                "inhibit": {"wanted": self.want_inhibit(), "active": self.inhibitor.active,
                            "error": self.inhibitor.error},
                "usb_awake": sorted(self.pinned),
                "start_at_boot": self.boot,
                "setup": {"identity": sys_identity, "applied": dropin},
                "identities": self.identities(),
                "paired": {"address": self.paired_address(), "name": self.alias[1]},
                "fake_system": bool(getattr(self.system, "fake", False)),
                "now": time.time(),
            },
            "settings": dict(self.settings),
            "engine": self.engine_status,
            "log": [e for e in self.logs if e["seq"] > log_since],
            "log_seq": self.seq,
        }

    # ------------------------------------------------------------------ commands
    def save(self):
        config.save(self.paths.config_file, self.settings)

    async def cmd_status(self, log_since=0, **_):
        return self.status(int(log_since or 0))

    async def cmd_ping(self, **_):
        return {"pong": True, "version": VERSION}

    async def cmd_start(self, **_):
        """Also ends the hold after an identity switch ("I forgot the device on my Mac")."""
        if self.hold:
            self.hold = None
            self.log("Starting the bridge again; connect from the Mac to pair")
        self.retry_at, self.failures = None, 0
        if not self.settings["bridging"]:
            self.settings["bridging"] = True
            self.save()
            self.log("Bridging started")
        self.poke()
        return self.status()["service"]

    async def cmd_stop(self, **_):
        if self.settings["bridging"]:
            self.settings["bridging"] = False
            self.save()
            self.log("Bridging stopped")
        self.retry_at = None
        await self.stop_engine()
        self.update_inhibitor()
        self.poke()
        return self.status()["service"]

    async def cmd_restart(self, **_):
        if self.busy:
            raise SettingsError("Wait until the identity switch finishes.")
        if self.hold:
            return await self.cmd_start()
        self.log("Restarting the engine")
        self.retry_at, self.failures = None, 0
        if not self.settings["bridging"]:
            return await self.cmd_start()
        await self.stop_engine()
        self.poke()
        return self.status()["service"]

    async def cmd_set_settings(self, settings=None, **_):
        if not isinstance(settings, dict):
            raise SettingsError("set_settings needs a settings object.")
        changes = config.validate(settings)
        bridging = changes.pop("bridging", None)
        before = dict(self.settings)
        self.settings.update(changes)
        self.save()
        for key in changes:
            if before[key] != changes[key]:
                self.log(f"Setting {key} changed to {json.dumps(changes[key])}")
        self.update_inhibitor()
        if any(before[k] != self.settings[k] for k in ("rate", "grab", "inflight", "name")) and self.proc:
            await self.stop_engine()
            self.poke()
        if bridging is True:
            await self.cmd_start()
        elif bridging is False:
            await self.cmd_stop()
        return dict(self.settings)

    def drop_saved_key(self, addr):
        """The engine restores pairings from ds4_bt_keys.json at startup; a forgotten Mac must leave it too."""
        path = self.paths.keys_file
        try:
            with open(path) as f:
                keys = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(keys, dict):
            return
        drop = [k for k in keys if not addr or k.upper() == addr.upper()]
        if not drop:
            return
        for k in drop:
            del keys[k]
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(keys, f)
        os.replace(tmp, path)

    async def forget_mac(self):
        """Forget the paired Mac on this side. Call with the engine stopped."""
        addr = self.paired_address()
        try:
            os.remove(self.paths.host_file)
        except FileNotFoundError:
            pass
        except OSError as e:
            raise SettingsError(f"Could not forget the Mac: {e}") from None
        try:
            self.drop_saved_key(addr)
        except OSError as e:
            self.log(f"Could not remove the saved link key: {e}")
        if addr:
            code, out = await self.system.bt_remove(addr)
            if code != 0 and "not available" not in out:
                self.log(f"bluetoothctl could not remove {addr}: {out.strip()}")
        self.alias = (None, None)
        return addr

    async def cmd_forget(self, **_):
        if self.busy:
            raise SettingsError("Wait until the identity switch finishes.")
        running = self.proc is not None
        await self.stop_engine()          # the engine must not reconnect while we forget
        addr = await self.forget_mac()
        self.log(f"Forgot {addr}. Pair again from the Mac." if addr else "No Mac was paired")
        if running:
            self.poke()
        return {"forgot": addr}

    async def cmd_apply_identity(self, identity=None, **_):
        identity = config.validate({"identity": identity}, allow_identity=True)["identity"]
        if self.busy:
            raise SettingsError("An identity switch is already running.")
        self.busy = "identity"
        self.poke()
        asyncio.create_task(self.apply_identity(identity))
        return {"started": True, "identity": identity}

    async def apply_identity(self, identity):
        name = config.IDENTITY_NAMES[identity]
        self.log(f"Switching to {name}: stopping the engine and reconfiguring Bluetooth")
        try:
            await self.stop_engine()
            code, out = await self.system.run_setup(self.paths.setup_script, identity,
                                                    self.settings.get("desktop_user"))
            for line in out.strip().splitlines()[-6:]:
                self.log(line, source="setup")
            if code == 0:
                await self.forget_mac()
                self.settings["identity"] = identity
                self.save()
                self.hold = "forget-on-mac"
                self.log(f"Now appearing as {name}. The bridge stays stopped until the Mac has forgotten "
                         f"the controller (System Settings > Bluetooth > Forget This Device).")
                self.last_action = {"name": "identity", "ok": True, "time": time.time(),
                                    "message": f"Now appearing as {name}"}
            else:
                self.log(f"Bluetooth setup failed (exit code {code})")
                self.last_action = {"name": "identity", "ok": False, "time": time.time(),
                                    "message": (out.strip().splitlines() or ["Bluetooth setup failed"])[-1]}
        except Exception as e:  # never leave the service stuck in "applying"
            self.log(f"Identity switch failed: {e}")
            self.last_action = {"name": "identity", "ok": False, "time": time.time(), "message": str(e)}
        finally:
            self.busy = None
            self.retry_at, self.failures = None, 0
            self.poke()

    async def cmd_set_boot(self, enabled=None, **_):
        if not isinstance(enabled, bool):
            raise SettingsError("set_boot needs enabled: true or false.")
        code, out = await self.system.set_boot(enabled)
        await self.refresh_boot()
        if code != 0:
            raise SettingsError(out.strip().splitlines()[-1] if out.strip() else "systemctl failed")
        self.log("PadBridge starts with the laptop" if enabled else "PadBridge no longer starts with the laptop")
        return {"start_at_boot": self.boot}

    COMMANDS = {"status", "ping", "start", "stop", "restart", "set_settings", "forget", "apply_identity",
                "set_boot"}

    async def dispatch(self, req):
        cmd = req.get("cmd")
        if cmd not in self.COMMANDS:
            raise SettingsError(f"Unknown command {cmd!r}.")
        args = {k: v for k, v in req.items() if k not in ("cmd", "id")}
        return await getattr(self, f"cmd_{cmd}")(**args)

    # ------------------------------------------------------------------ socket
    def allowed(self, sock):
        if not hasattr(socket, "SO_PEERCRED"):
            return True   # not Linux (development); the socket's file mode still applies
        try:
            _pid, uid, gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                                  struct.calcsize("3i")))
        except OSError:
            return False
        if uid in (0, os.geteuid()):
            return True
        try:
            group_gid = grp.getgrnam(GROUP).gr_gid
            if gid == group_gid:
                return True
            user = pwd.getpwuid(uid).pw_name
            return group_gid in os.getgrouplist(user, gid)
        except KeyError:
            return False

    async def handle_client(self, reader, writer):
        sock = writer.get_extra_info("socket")
        if sock is not None and not self.allowed(sock):
            writer.write(b'{"id": null, "ok": false, "error": "Not allowed: join the padbridge group."}\n')
            await writer.drain()
            writer.close()
            return
        try:
            while not self.closing:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    break
                if not line:
                    break
                rid = None
                try:
                    req = json.loads(line)
                    if not isinstance(req, dict):
                        raise ValueError("request must be a JSON object")
                    rid = req.get("id")
                    reply = {"id": rid, "ok": True, "result": await self.dispatch(req)}
                except (SettingsError, ValueError) as e:
                    reply = {"id": rid, "ok": False, "error": str(e)}
                except Exception as e:
                    self.log(f"Internal error: {e!r}")
                    reply = {"id": rid, "ok": False, "error": f"Internal error: {e}"}
                writer.write(json.dumps(reply).encode() + b"\n")
                await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()

    async def start_server(self):
        os.makedirs(self.paths.run_dir, exist_ok=True)
        path = self.paths.socket
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        old = os.umask(0o117)
        try:
            self.server = await asyncio.start_unix_server(self.handle_client, path, limit=MAX_LINE)
        finally:
            os.umask(old)
        os.chmod(path, 0o660)
        if os.geteuid() == 0:
            try:
                os.chown(path, 0, grp.getgrnam(GROUP).gr_gid)
            except KeyError:
                self.log(f"Group {GROUP} does not exist; only root can use the control socket")

    # ------------------------------------------------------------------ lifecycle
    async def run(self):
        await self.start_server()
        self.log(f"PadBridge service {VERSION} ready on {self.paths.socket}")
        identity, applied = self.system.setup_state()
        if not applied:
            self.log("Bluetooth is not set up for PadBridge yet; choose an identity to set it up")
        self.tasks = [asyncio.create_task(self.supervise()), asyncio.create_task(self.housekeeping())]
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await self.shutdown()

    async def shutdown(self):
        self.closing = True
        self.poke()
        if self.server:
            self.server.close()
        await self.stop_engine()
        self.update_inhibitor()
        self.update_usb_power()
        for t in self.tasks:
            t.cancel()
        try:
            os.remove(self.paths.socket)
        except OSError:
            pass
        self.log("PadBridge service stopped")


def main(argv=None):
    p = argparse.ArgumentParser(prog="padbridged", description="PadBridge host service")
    p.add_argument("--run-dir")
    p.add_argument("--config-dir")
    p.add_argument("--engine-dir")
    p.add_argument("--engine", help="engine script (default: <engine dir>/ds4_bt_bridge.py)")
    p.add_argument("--python", help="interpreter for the engine (default: this one)")
    p.add_argument("--fake-system", action="store_true", help="record system changes instead of making them")
    p.add_argument("--fake-inhibit", action="store_true", help="do not talk to logind")
    args = p.parse_args(argv)
    paths = Paths.from_env(args.run_dir, args.config_dir, args.engine_dir, args.engine)
    if not args.fake_system and os.geteuid() != 0:
        sys.exit("padbridged must run as root (or pass --fake-system for development).")
    system = FakeSystem() if args.fake_system else RealSystem()
    inhibitor = FakeInhibitor() if (args.fake_inhibit or args.fake_system) else LogindInhibitor()
    service = Service(paths, system, inhibitor, python=args.python)
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
