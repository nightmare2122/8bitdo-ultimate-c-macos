"""Service tests: the real padbridged logic driving the mock engine, as a normal user, in temp dirs.

    python3 -m unittest discover -s tests -v        (from padbridge/host)
"""
import asyncio
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from padbridge import config  # noqa: E402
from padbridge.client import Client, ConnectError, ServiceError  # noqa: E402
from padbridge.config import Paths  # noqa: E402
from padbridge.daemon import Service  # noqa: E402
from padbridge.inhibit import FakeInhibitor  # noqa: E402
from padbridge.system import FakeSystem, identity_from_main_conf  # noqa: E402

MOCK = os.path.join(ROOT, "mock", "mock_engine.py")
MAC = "AA:BB:CC:DD:EE:01"


class ConfigTests(unittest.TestCase):
    def test_defaults_when_missing_or_broken(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "config.json")
        self.assertEqual(config.load(path), config.DEFAULTS)
        with open(path, "w") as f:
            f.write("{not json")
        self.assertEqual(config.load(path), config.DEFAULTS)

    def test_invalid_values_fall_back_individually(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "config.json")
        with open(path, "w") as f:
            json.dump({"rate": 5, "lid_closed": True, "identity": "ps5", "bogus": 1}, f)
        s = config.load(path)
        self.assertEqual(s["rate"], 250)
        self.assertTrue(s["lid_closed"])
        self.assertEqual(s["identity"], "xbox-one")

    def test_validate(self):
        self.assertEqual(config.validate({"rate": 500, "grab": False}), {"rate": 500, "grab": False})
        for bad in ({"rate": True}, {"rate": 1001}, {"rate": "250"}, {"lid_closed": "yes"}, {"nope": 1},
                    {"identity": "ds4"}, {"inflight": 9}, {"name": ""}, {"name": "x" * 30}):
            with self.assertRaises(config.SettingsError, msg=bad):
                config.validate(bad)
        self.assertEqual(config.validate({"identity": "ds4"}, allow_identity=True), {"identity": "ds4"})
        self.assertEqual(config.validate({"identity": "xbox"}, allow_identity=True), {"identity": "xbox-one"})
        with self.assertRaises(config.SettingsError):
            config.validate({"identity": "xbox-series"}, allow_identity=True)
        self.assertEqual(config.validate({"name": " Pad "}), {"name": "Pad"})

    def test_save_roundtrip(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "sub", "config.json")
        config.save(path, {**config.DEFAULTS, "rate": 333})
        self.assertEqual(config.load(path)["rate"], 333)

    def test_identity_from_main_conf(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "main.conf")
        self.assertIsNone(identity_from_main_conf(path))
        for text, want in (("[General]\nDeviceID = usb:054C:09CC:0100\n", "ds4"),
                           ("[General]\nName = X\nDeviceID = usb:045E:02FD:0903\n", "xbox-one"),
                           ("[General]\n#PadBridgeIdentity = ds4\nDeviceID = usb:045E:02FD:0903\n", "ds4"),
                           ("[General]\n#PadBridgeIdentity = xbox\n", "xbox-one"),
                           ("[General]\n#DeviceID = usb:054C:09CC:0100\n", None)):
            with open(path, "w") as f:
                f.write(text)
            self.assertEqual(identity_from_main_conf(path), want)


class ServiceTestCase(unittest.IsolatedAsyncioTestCase):
    scenario = "normal"
    settings = None
    paired = None

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pb")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = Paths(run_dir=os.path.join(self.tmp, "run"), config_dir=os.path.join(self.tmp, "etc"),
                           engine_dir=os.path.join(self.tmp, "engine"), engine=MOCK)
        os.makedirs(self.paths.engine_dir)
        if self.settings:
            config.save(self.paths.config_file, {**config.DEFAULTS, **self.settings})
        if self.paired:
            with open(self.paths.host_file, "w") as f:
                f.write(self.paired)
            with open(self.paths.keys_file, "w") as f:
                json.dump({self.paired: {"key": "AB" * 16, "type": 4}, "11:22:33:44:55:66": {"key": "CD" * 16, "type": 4}}, f)
        os.environ["MOCK_SCENARIO"] = self.scenario
        os.environ["MOCK_PAIR_AFTER"] = "1"
        self.system = FakeSystem(setup_delay=0.3)
        self.inhibitor = FakeInhibitor()
        self.svc = Service(self.paths, self.system, self.inhibitor, stop_timeout=3, tick=0.1, start_grace=2.5)
        self.runner = asyncio.create_task(self.svc.run())
        await self.until(lambda: os.path.exists(self.paths.socket), 3, "socket")

    async def asyncTearDown(self):
        await self.svc.shutdown()
        self.runner.cancel()
        try:
            await self.runner
        except (asyncio.CancelledError, Exception):
            pass

    async def until(self, cond, timeout=8.0, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            await asyncio.sleep(0.05)
        self.fail(f"timed out waiting for {what}; state={self.svc.state()} log={[e['text'] for e in self.svc.logs][-8:]}")

    async def call(self, cmd, **args):
        """A request over the real socket, from a thread (the client blocks)."""
        def go():
            c = Client(self.paths.socket, timeout=10)
            try:
                return c.call(cmd, **args)
            finally:
                c.close()
        return await asyncio.to_thread(go)

    def engine(self):
        return self.svc.status()["engine"] or {}


class NormalTests(ServiceTestCase):
    async def test_runs_engine_and_pairs_a_mac(self):
        await self.until(lambda: self.svc.state() == "running", what="running")
        st = await self.call("status")
        self.assertEqual(st["engine"]["pid"], st["service"]["engine_pid"])
        await self.until(lambda: (self.engine().get("controller") or {}).get("connected"), what="controller")
        await self.until(lambda: (self.engine().get("host") or {}).get("connected"), what="Mac connected")
        await self.until(lambda: self.svc.status()["service"]["paired"]["name"] == "MacBook Pro", what="alias")
        self.assertEqual(open(self.paths.host_file).read(), MAC)
        st = await self.call("status")
        self.assertEqual(st["service"]["paired"]["address"], MAC)
        self.assertGreater(st["engine"]["stats"]["reports_per_s"], 200)
        self.assertTrue(any("Mac AA:BB:CC:DD:EE:01 connected" in e["text"] for e in st["log"]))

    async def test_log_since_returns_only_new_entries(self):
        await self.until(lambda: self.svc.state() == "running")
        st = await self.call("status")
        st2 = await self.call("status", log_since=st["log_seq"])
        self.assertTrue(all(e["seq"] > st["log_seq"] for e in st2["log"]))
        self.assertLess(len(st2["log"]), len(st["log"]))

    async def test_stop_and_start_persist(self):
        await self.until(lambda: self.svc.state() == "running")
        pid = self.svc.proc.pid
        await self.call("stop")
        self.assertIsNone(self.svc.proc)
        self.assertEqual(self.svc.state(), "stopped")
        self.assertFalse(config.load(self.paths.config_file)["bridging"])
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        await asyncio.sleep(0.5)
        self.assertIsNone(self.svc.proc, "a stopped service must not respawn the engine")
        await self.call("start")
        await self.until(lambda: self.svc.state() == "running", what="running again")
        self.assertTrue(config.load(self.paths.config_file)["bridging"])

    async def test_restart_gives_new_engine(self):
        await self.until(lambda: self.svc.state() == "running")
        pid = self.svc.proc.pid
        await self.call("restart")
        await self.until(lambda: self.svc.proc and self.svc.proc.pid != pid and self.svc.state() == "running",
                         what="new engine")
        self.assertEqual(self.svc.restarts, 0, "a requested restart is not a crash")

    async def test_engine_killed_is_restarted(self):
        await self.until(lambda: self.svc.state() == "running")
        pid = self.svc.proc.pid
        os.kill(pid, 9)
        await self.until(lambda: self.svc.restarts == 1, what="crash noticed")
        await self.until(lambda: self.svc.state() == "running" and self.svc.proc.pid != pid, what="respawn")

    async def test_lid_closed_inhibitor_follows_setting_and_bridging(self):
        await self.until(lambda: self.svc.state() == "running")
        self.assertFalse(self.inhibitor.active)
        await self.call("set_settings", settings={"lid_closed": True})
        await self.until(lambda: self.inhibitor.active, 2, "inhibitor")
        self.assertTrue((await self.call("status"))["service"]["inhibit"]["active"])
        await self.call("stop")
        self.assertFalse(self.inhibitor.active, "stopped bridging must release the lock")
        await self.call("start")
        await self.until(lambda: self.inhibitor.active, 2, "inhibitor again")
        await self.call("set_settings", settings={"lid_closed": False})
        self.assertFalse(self.inhibitor.active)
        self.assertEqual(self.inhibitor.history, ["acquire", "release", "acquire", "release"])

    async def test_settings_rate_restarts_engine(self):
        await self.until(lambda: self.svc.state() == "running")
        pid = self.svc.proc.pid
        result = await self.call("set_settings", settings={"rate": 500, "grab": False})
        self.assertEqual(result["rate"], 500)
        await self.until(lambda: self.svc.proc and self.svc.proc.pid != pid, what="restart")
        args = self.svc.engine_argv()
        self.assertIn("--no-grab", args)
        self.assertEqual(args[args.index("--rate") + 1], "500")
        await self.until(lambda: (self.engine().get("stats") or {}).get("reports_per_s", 0) > 400, what="new rate")
        self.assertEqual(config.load(self.paths.config_file)["rate"], 500)

    async def test_lid_toggle_does_not_restart_engine(self):
        await self.until(lambda: self.svc.state() == "running")
        pid = self.svc.proc.pid
        await self.call("set_settings", settings={"lid_closed": True})
        await asyncio.sleep(0.5)
        self.assertEqual(self.svc.proc.pid, pid)

    async def test_bad_requests(self):
        with self.assertRaises(ServiceError):
            await self.call("set_settings", settings={"rate": 5})
        with self.assertRaises(ServiceError):
            await self.call("set_settings", settings={"identity": "ds4"})
        with self.assertRaises(ServiceError):
            await self.call("apply_identity", identity="switch")
        with self.assertRaises(ServiceError):
            await self.call("format_disk")

        def raw():
            s = socket.socket(socket.AF_UNIX)
            s.connect(self.paths.socket)
            s.sendall(b"this is not json\n[1,2]\n")
            data = b""
            while data.count(b"\n") < 2:
                data += s.recv(4096)
            s.close()
            return [json.loads(x) for x in data.splitlines()]
        replies = await asyncio.to_thread(raw)
        self.assertEqual([r["ok"] for r in replies], [False, False])
        # the service survived
        self.assertTrue((await self.call("ping"))["pong"])

    async def test_socket_permissions(self):
        mode = stat.S_IMODE(os.stat(self.paths.socket).st_mode)
        self.assertEqual(mode, 0o660)

    async def test_usb_kept_awake_while_running_then_restored(self):
        await self.until(lambda: self.svc.state() == "running")
        await self.until(lambda: "/fake/usb/controller" in self.svc.pinned, what="controller pinned")
        self.assertEqual(self.system.power["/fake/usb/bluetooth"], "on")
        await self.call("stop")
        await self.until(lambda: not self.svc.pinned, 2, "unpinned")
        self.assertEqual(self.system.power["/fake/usb/bluetooth"], "auto")
        self.assertEqual(self.system.power["/fake/usb/controller"], "auto")

    async def test_start_at_boot(self):
        r = await self.call("set_boot", enabled=True)
        self.assertTrue(r["start_at_boot"])
        self.assertIn(("set_boot", True), self.system.calls)
        self.assertTrue((await self.call("status"))["service"]["start_at_boot"])
        await self.call("set_boot", enabled=False)
        self.assertFalse((await self.call("status"))["service"]["start_at_boot"])

    async def test_shutdown_cleans_up(self):
        await self.call("set_settings", settings={"lid_closed": True})
        await self.until(lambda: self.svc.state() == "running" and self.inhibitor.active)
        pid = self.svc.proc.pid
        await self.svc.shutdown()
        self.assertFalse(self.inhibitor.active)
        self.assertFalse(os.path.exists(self.paths.socket))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with self.assertRaises(ConnectError):
            Client(self.paths.socket).call("ping")


class PairedTests(ServiceTestCase):
    paired = MAC
    settings = {"desktop_user": "user"}

    async def test_forget_removes_pairing_and_restarts(self):
        await self.until(lambda: (self.engine().get("host") or {}).get("connected"), what="connected")
        pid = self.svc.proc.pid
        r = await self.call("forget")
        self.assertEqual(r["forgot"], MAC)
        self.assertIn(("bt_remove", MAC), self.system.calls)
        self.assertFalse(os.path.exists(self.paths.host_file))
        with open(self.paths.keys_file) as f:
            self.assertEqual(list(json.load(f)), ["11:22:33:44:55:66"], "only the forgotten Mac's key goes")
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.keys_file).st_mode), 0o600)
        await self.until(lambda: self.svc.proc and self.svc.proc.pid != pid, what="restart")

    async def test_apply_identity(self):
        await self.until(lambda: self.svc.state() == "running")
        r = await self.call("apply_identity", identity="ds4")
        self.assertTrue(r["started"])
        self.assertEqual((await self.call("status"))["service"]["state"], "applying")
        with self.assertRaises(ServiceError):
            await self.call("apply_identity", identity="xbox-one")
        await self.until(lambda: not self.svc.busy, 5, "setup finished")
        self.assertIn(("setup", "ds4", "user"), self.system.calls)
        self.assertIn(("bt_remove", MAC), self.system.calls)
        self.assertFalse(os.path.exists(self.paths.host_file), "the Mac must pair again after a switch")
        self.assertEqual(config.load(self.paths.config_file)["identity"], "ds4")
        # the engine stays stopped until the Mac has forgotten the device, or the Mac reconnects at once
        st = await self.call("status")
        self.assertEqual(st["service"]["state"], "hold")
        self.assertIsNone(self.svc.proc)
        await asyncio.sleep(0.5)
        self.assertIsNone(self.svc.proc)
        self.assertTrue(st["service"]["last_action"]["ok"])
        self.assertEqual(st["service"]["setup"]["identity"], "ds4")
        await self.call("start")
        await self.until(lambda: (self.engine().get("identity") or {}).get("id") == "ds4", what="engine as ds4")
        self.assertEqual(self.engine()["mode"], "ds4")
        args = self.svc.engine_argv()
        self.assertEqual(args[args.index("--mode") + 1], "ds4")

    async def test_apply_identity_failure_keeps_old_identity(self):
        await self.until(lambda: self.svc.state() == "running")
        self.system.fail_setup = True
        await self.call("apply_identity", identity="ds4")
        await self.until(lambda: not self.svc.busy, 5, "setup finished")
        st = await self.call("status")
        self.assertFalse(st["service"]["last_action"]["ok"])
        self.assertIn("bluetoothd not found", st["service"]["last_action"]["message"])
        self.assertEqual(st["settings"]["identity"], "xbox-one")
        self.assertIsNone(st["service"]["hold"])
        self.assertTrue(os.path.exists(self.paths.host_file), "a failed switch must not forget the Mac")
        await self.until(lambda: self.svc.state() == "running", what="engine back")


class IdentityTests(ServiceTestCase):
    async def test_status_carries_addendum_keys(self):
        await self.until(lambda: (self.engine().get("controller") or {}).get("connected"), what="controller")
        st = await self.call("status")
        self.assertEqual(st["engine"]["controller"]["vid"], "2dc8")
        self.assertEqual(st["engine"]["bt_name"], "8BitDo Ultimate C")
        self.assertEqual([i["id"] for i in st["service"]["identities"]], ["xbox-one", "ds4"])
        await self.until(lambda: (self.engine().get("stats") or {}).get("link_ms"), what="radio delivery stats")
        self.assertTrue(all(i["supported"] for i in st["service"]["identities"]))

    async def test_engine_flags_and_paused_identity(self):
        argv = self.svc.engine_argv()
        self.assertEqual(argv[argv.index("--mode") + 1], "xbox-one")
        self.assertEqual(argv[argv.index("--inflight") + 1], "2")
        self.assertNotIn("--name", argv)
        await self.until(lambda: self.svc.state() == "running")
        pid = self.svc.proc.pid
        await self.call("set_settings", settings={"name": "Couch Pad", "inflight": 0})
        await self.until(lambda: self.svc.proc and self.svc.proc.pid != pid, what="restart for new flags")
        argv = self.svc.engine_argv()
        self.assertEqual(argv[argv.index("--name") + 1], "Couch Pad")
        self.assertEqual(argv[argv.index("--inflight") + 1], "0")
        await self.until(lambda: self.engine().get("bt_name") == "Couch Pad", what="engine uses the name")
        with self.assertRaises(ServiceError):
            await self.call("apply_identity", identity="xbox-series")
        self.assertIsNone(self.svc.busy)


class BindFailTests(ServiceTestCase):
    scenario = "bindfail"

    async def test_backoff_and_error_message(self):
        await self.until(lambda: self.svc.restarts >= 2, 6, "two failures")
        st = await self.call("status")
        self.assertEqual(st["service"]["state"], "waiting")
        self.assertIn("cannot bind HID channels", st["service"]["last_exit"]["message"])
        self.assertEqual(st["service"]["last_exit"]["code"], 1)
        gap = st["service"]["retry_at"] - st["service"]["last_exit"]["time"]
        self.assertGreaterEqual(round(gap), 2, "second failure waits longer than the first")
        await self.call("stop")
        self.assertIsNone(self.svc.retry_at)
        self.assertEqual(self.svc.state(), "stopped")


class CrashTests(ServiceTestCase):
    scenario = "crash"

    async def test_traceback_summarised(self):
        await self.until(lambda: self.svc.restarts >= 1, 8, "crash")
        self.assertEqual(self.svc.last_exit["message"], "OSError: [Errno 104] Connection reset by peer")


class HangTests(ServiceTestCase):
    scenario = "hang"

    async def test_stale_status_is_reported(self):
        await self.until(lambda: self.svc.state() == "unresponsive", 8, "unresponsive")
        self.assertFalse((await self.call("status"))["service"]["status_fresh"])


class NoSetupTests(ServiceTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.system.identity, self.system.dropin = None, False

    async def test_reports_setup_missing(self):
        st = await self.call("status")
        self.assertEqual(st["service"]["setup"], {"identity": None, "applied": False})
        await self.call("apply_identity", identity="ds4")
        await self.until(lambda: not self.svc.busy, 5)
        st = await self.call("status")
        self.assertEqual(st["service"]["setup"], {"identity": "ds4", "applied": True})


if __name__ == "__main__":
    unittest.main()
