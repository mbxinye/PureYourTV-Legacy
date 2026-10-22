"""The Android 6 Home handover, against a simulated TV.

Android 6 has no `cmd package set-home-activity`, so the only way to make Projectivy Home is to
disable the stock launcher after Projectivy is running, press Home, and check that Projectivy is
still in front. These tests pin the order that keeps a TV usable (install, start, verify, disable,
verify) and the rollback when any step does not land: a TV left with no Home can only be rescued
over ADB.

Run from the repository root: python3 -m unittest discover -s tests -v
"""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "com.spocky.projengmenu"
HOME_ACTIVITY = "com.spocky.projengmenu.ui.home.MainActivity"
STOCK = "com.mitv.tvhome"

SPEC = importlib.util.spec_from_file_location("projectivy_install",
                                              ROOT / "projectivy" / "install.py")
install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install)


class FakeTv:
    """Enough of a TV for the installer's shell commands, with the state it changes."""

    def __init__(self, sdk=23, start_ok=True, start_focus=True, takeover_ok=True,
                 disable_fails=False, enable_fails=False, set_home_fails=False,
                 install_output="Success\n"):
        self.sdk = sdk
        self.start_ok = start_ok
        self.start_focus = start_focus
        self.takeover_ok = takeover_ok
        self.disable_fails = disable_fails
        self.enable_fails = enable_fails
        self.set_home_fails = set_home_fails
        self.install_output = install_output
        self.packages = {STOCK: True, PACKAGE: False}
        self.focus = STOCK
        self.home_package = STOCK
        self.home_component = f"{STOCK}/.MainActivity"
        self.commands = []
        self.pushed = []

    def push(self, local, remote):
        self.pushed.append((str(local), remote))
        return "pushed\n"

    def shell(self, command):
        self.commands.append(command)
        if command.startswith("getprop ro.build.version.sdk"):
            return f"{self.sdk}\n"
        if command.startswith("getprop "):
            return "Xiaomi\n"
        if command.startswith("pm path "):
            package = command.split()[2]
            return f"package:/data/app/{package}-1/base.apk\n" if self.packages.get(package) else ""
        if "set-home-activity" in command:
            if self.set_home_fails:
                return "Error: Unknown command: set-home-activity\n"
            self.home_package = PACKAGE
            self.home_component = f"{PACKAGE}/.MainActivity"
            return ""
        if "resolve-activity" in command:
            return self.home_component + "\n"
        if "mHomeProcess" in command:
            return f"  mHomeProcess: ProcessRecord{{6f2b 1234:{self.home_package}/u0a12}}\n"
        if command.startswith("am start"):
            if not self.start_ok:
                return ("Starting: Intent { cmp=com.spocky.projengmenu/.ui.home.MainActivity }\n"
                        "Error: Activity not started, unable to resolve Intent\n")
            if self.start_focus:
                self.focus = PACKAGE
            return ("Starting: Intent { cmp=com.spocky.projengmenu/.ui.home.MainActivity }\n"
                    "Status: ok\nActivity: com.spocky.projengmenu/.ui.home.MainActivity\n")
        if "mCurrentFocus" in command or "dumpsys activity activities" in command:
            return f"  mCurrentFocus=Window{{1a2b u0 {self.focus}/.MainActivity}}\n"
        if command.startswith("pm install"):
            if self.install_output != "Success\n":
                return self.install_output
            self.packages[PACKAGE] = True
            return "Success\n"
        if command.startswith("pm disable-user"):
            if self.disable_fails:
                return "Error: java.lang.SecurityException\n"
            self.packages[command.split()[-1]] = False
            return ""
        if command.startswith("pm enable"):
            if self.enable_fails:
                return "Error: java.lang.SecurityException\n"
            self.packages[command.split()[-1]] = True
            return ""
        if command.startswith("pm list packages -d"):
            return "".join(f"package:{name}\n"
                           for name, enabled in self.packages.items() if not enabled)
        if command.startswith("input keyevent KEYCODE_HOME"):
            if self.packages.get(STOCK):
                self.focus = STOCK
            else:
                self.focus = PACKAGE if self.takeover_ok else STOCK
            return ""
        if command.startswith("dumpsys package"):
            return "    versionName=4.72\n"
        return ""


class ProjectivyInstallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.records = self.base / "records"
        self.old_record_dir = install.RECORD_DIR
        install.RECORD_DIR = self.records
        self.addCleanup(setattr, install, "RECORD_DIR", self.old_record_dir)

        self.payload = b"a fake but pinned APK"
        self.apk = self.base / "Projectivy.apk"
        self.apk.write_bytes(self.payload)
        self.release = dict(install.load_release())
        self.release.update(size=len(self.payload),
                            sha256=hashlib.sha256(self.payload).hexdigest())

    def options(self, **overrides):
        values = dict(host="192.168.1.50", python=None, apk=str(self.apk),
                      cache_dir=str(self.base / "cache"), skip_verify=False,
                      stock_launcher=None, no_takeover=False, check=False, dry_run=False,
                      rollback=False, list_releases=False, attempts=2, wait=0, timeout=10)
        values.update(overrides)
        return argparse.Namespace(**values)

    def run_install(self, tv, **overrides):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = install.run(tv, self.options(**overrides), self.release)
            except install.InstallError as e:
                code = 1
                err.write(str(e))
        return code, out.getvalue(), err.getvalue()

    def record(self):
        path = install.record_path("192.168.1.50")
        return json.loads(path.read_text()) if path.exists() else None

    def test_android_6_takes_home_without_cmd_package(self):
        tv = FakeTv(sdk=23)
        code, out, err = self.run_install(tv)
        self.assertEqual(code, 0, err)
        commands = tv.commands
        start = next(i for i, c in enumerate(commands) if c.startswith("am start"))
        disable = next(i for i, c in enumerate(commands) if c.startswith("pm disable-user"))
        home = next(i for i, c in enumerate(commands) if c.startswith("input keyevent"))
        self.assertLess(start, disable, "the new launcher starts before the stock Home is disabled")
        self.assertLess(disable, home)
        self.assertFalse(any("set-home-activity" in c for c in commands),
                         "Android 6 has no cmd package set-home-activity")
        self.assertEqual(tv.focus, PACKAGE)
        self.assertFalse(tv.packages[STOCK])
        self.assertTrue(tv.packages[PACKAGE])
        self.assertEqual(self.record()["state"], "complete")
        self.assertIn("Home now belongs to Projectivy", out)

    def test_sdk_below_projectivy_is_refused_before_anything_changes(self):
        tv = FakeTv(sdk=22)
        code, _, err = self.run_install(tv)
        self.assertEqual(code, 1)
        self.assertIn("API 23", err)
        self.assertEqual(tv.commands, [
            "getprop ro.product.manufacturer",
            "getprop ro.product.model",
            "getprop ro.build.version.release",
            "getprop ro.build.version.sdk",
        ])
        self.assertEqual(tv.pushed, [])
        self.assertIsNone(self.record())

    def test_start_failure_leaves_the_stock_launcher_enabled(self):
        tv = FakeTv(sdk=23, start_ok=False)
        code, _, err = self.run_install(tv)
        self.assertEqual(code, 1)
        self.assertIn("did not start", err)
        self.assertFalse(any("pm disable-user" in c for c in tv.commands))
        self.assertFalse(any("input keyevent" in c for c in tv.commands))
        self.assertIsNone(self.record())

    def test_projectivy_that_starts_but_never_gets_focus_is_not_trusted(self):
        tv = FakeTv(sdk=23, start_focus=False)
        code, _, err = self.run_install(tv)
        self.assertEqual(code, 1)
        self.assertIn("never came to the front", err)
        self.assertFalse(any("pm disable-user" in c for c in tv.commands))
        self.assertIsNone(self.record())

    def test_install_failure_leaves_the_stock_launcher_enabled(self):
        tv = FakeTv(sdk=23, install_output="Failure [INSTALL_FAILED_INVALID_APK]\n")
        code, _, err = self.run_install(tv)
        self.assertEqual(code, 1)
        self.assertIn("pm install failed", err)
        self.assertFalse(any("pm disable-user" in c for c in tv.commands))
        self.assertFalse(tv.packages[PACKAGE])

    def test_home_that_does_not_land_on_projectivy_is_rolled_back(self):
        tv = FakeTv(sdk=23, takeover_ok=False)
        code, _, err = self.run_install(tv)
        self.assertEqual(code, 1)
        self.assertIn("Home did not land on Projectivy", err)
        self.assertTrue(tv.packages[STOCK], "the stock launcher is enabled again")
        self.assertIn("is enabled again", err)
        self.assertEqual(self.record()["state"], "rolled-back")
        self.assertFalse(install.recovery_path("192.168.1.50").exists())

    def test_failed_rollback_writes_the_commands_that_finish_it(self):
        tv = FakeTv(sdk=23, takeover_ok=False, enable_fails=True)
        code, _, err = self.run_install(tv)
        self.assertEqual(code, 2)
        self.assertIn("recovery is incomplete", err)
        self.assertFalse(tv.packages[STOCK])
        self.assertEqual(self.record()["state"], "recovery-incomplete")
        recovery = install.recovery_path("192.168.1.50").read_text()
        self.assertIn(f"pm enable --user 0 {STOCK}", recovery)
        self.assertIn("input keyevent KEYCODE_HOME", recovery)

    def test_android_7_also_sets_the_home_activity(self):
        tv = FakeTv(sdk=24)
        code, _, err = self.run_install(tv)
        self.assertEqual(code, 0, err)
        commands = tv.commands
        start = next(i for i, c in enumerate(commands) if c.startswith("am start"))
        disable = next(i for i, c in enumerate(commands) if c.startswith("pm disable-user"))
        set_home = next(i for i, c in enumerate(commands) if "set-home-activity" in c)
        self.assertLess(start, disable)
        self.assertLess(disable, set_home)
        self.assertEqual(self.record()["state"], "complete")

    def test_android_7_without_set_home_activity_falls_back_to_focus(self):
        # `cmd package` exists on Android 7, but some vendor builds do not implement
        # set-home-activity. With the stock launcher disabled and Projectivy in focus, Home works;
        # the installer must not roll that back just because the command printed an error.
        tv = FakeTv(sdk=24, set_home_fails=True)
        code, _, err = self.run_install(tv)
        self.assertEqual(code, 0, err)
        self.assertIn("set-home-activity failed", err)
        self.assertFalse(tv.packages[STOCK])
        self.assertEqual(tv.focus, PACKAGE)
        self.assertEqual(self.record()["state"], "complete")

    def test_install_only_does_not_touch_home(self):
        tv = FakeTv(sdk=23)
        code, out, err = self.run_install(tv, no_takeover=True)
        self.assertEqual(code, 0, err)
        self.assertIn("stock launcher was left enabled", out)
        self.assertFalse(any("pm disable-user" in c for c in tv.commands))
        self.assertFalse(any("input keyevent" in c for c in tv.commands))
        self.assertIsNone(self.record())

    def test_stock_launcher_none_installs_without_touching_home(self):
        tv = FakeTv(sdk=23)
        code, out, err = self.run_install(tv, stock_launcher="none")
        self.assertEqual(code, 0, err)
        self.assertIn("--stock-launcher none left Home unchanged", out)
        self.assertFalse(any("pm disable-user" in c for c in tv.commands))
        self.assertIsNone(self.record())

    def test_focus_ignores_a_resumed_activity_behind_a_dialog(self):
        text = ("  mCurrentFocus=Window{1a2b u0 android/com.android.internal.app.ResolverActivity}\n"
                "  mResumedActivity: ActivityRecord{3c4d u0 "
                "com.spocky.projengmenu/.ui.home.MainActivity t12}\n")
        self.assertEqual(install.current_focus_packages(text), {"android"})

    def test_dry_run_changes_nothing(self):
        tv = FakeTv(sdk=23)
        code, out, err = self.run_install(tv, dry_run=True)
        self.assertEqual(code, 0, err)
        self.assertIn("DRY RUN", out)
        self.assertEqual(tv.pushed, [])
        for mutating in ("pm install", "pm disable-user", "pm enable", "input keyevent",
                         "am start", "set-home-activity"):
            self.assertFalse(any(c.startswith(mutating) or mutating in c for c in tv.commands),
                             f"dry run sent {mutating}")
        self.assertIsNone(self.record())

    def test_check_reports_without_changing_anything(self):
        tv = FakeTv(sdk=23)
        code, out, err = self.run_install(tv, check=True)
        self.assertEqual(code, 0, err)
        self.assertIn("SDK 23", out)
        self.assertIn("not installed", out)
        self.assertIn("Home:", out)
        for mutating in ("pm install", "pm disable-user", "pm enable", "input keyevent",
                         "am start", "set-home-activity"):
            self.assertFalse(any(mutating in c for c in tv.commands))

    def test_rollback_re_enables_the_recorded_stock_launcher(self):
        tv = FakeTv(sdk=23)
        code, _, err = self.run_install(tv)
        self.assertEqual(code, 0, err)
        # The TV after the handover: stock disabled, Projectivy running.
        later = FakeTv(sdk=23)
        later.packages = {STOCK: False, PACKAGE: True}
        later.focus = PACKAGE
        later.home_package = PACKAGE
        later.home_component = f"{PACKAGE}/.MainActivity"
        code, out, err = self.run_install(later, rollback=True)
        self.assertEqual(code, 0, err)
        self.assertTrue(later.packages[STOCK])
        self.assertEqual(later.focus, STOCK)
        self.assertFalse(any("pm disable-user" in c for c in later.commands))
        self.assertIn("is enabled again", out)
        self.assertEqual(self.record()["state"], "rolled-back")

    def test_a_tampered_apk_is_refused(self):
        self.release["sha256"] = "0" * 64
        tv = FakeTv(sdk=23)
        code, _, err = self.run_install(tv)
        self.assertEqual(code, 1)
        self.assertIn("not the official Projectivy", err)
        self.assertEqual(tv.pushed, [])
        self.assertFalse(any("pm install" in c for c in tv.commands))
        self.assertFalse(any("pm disable-user" in c for c in tv.commands))

    def test_unknown_home_needs_the_stock_package_named(self):
        tv = FakeTv(sdk=23)
        tv.packages = {"com.vendor.weirdlauncher": True}
        tv.home_package = "com.vendor.weirdlauncher"
        code, _, err = self.run_install(tv)
        self.assertEqual(code, 1)
        self.assertIn("not a stock launcher", err)
        self.assertIn("--stock-launcher", err)
        self.assertEqual(tv.pushed, [])

    def test_stock_launcher_can_be_named_for_a_vendor_we_do_not_know(self):
        tv = FakeTv(sdk=23)
        tv.packages = {"com.vendor.weirdlauncher": True, PACKAGE: False}
        tv.home_package = "com.vendor.weirdlauncher"
        tv.home_component = "com.vendor.weirdlauncher/.Home"
        code, _, err = self.run_install(tv, stock_launcher="com.vendor.weirdlauncher")
        self.assertEqual(code, 0, err)
        self.assertFalse(tv.packages["com.vendor.weirdlauncher"])
        self.assertEqual(tv.focus, PACKAGE)


class ReleaseMetadataTest(unittest.TestCase):
    def test_the_pinned_release_is_the_official_android_6_build(self):
        release = install.load_release()
        self.assertEqual(release["version"], "4.72")
        self.assertEqual(release["package"], PACKAGE)
        self.assertEqual(release["home_activity"], HOME_ACTIVITY)
        self.assertEqual(release["min_sdk"], 23)
        self.assertLessEqual(release["min_sdk"], 23, "Android 6.0 is API 23")
        self.assertTrue(release["url"].startswith(
            "https://github.com/spocky/miproja1/releases/download/4.72/"))
        self.assertRegex(release["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(release["size"], 11432750)
        self.assertIn("not part of CleanYourTV", release["license_note"])

    def test_the_apk_is_not_bundled(self):
        self.assertEqual(list((ROOT / "projectivy").glob("*.apk")), [],
                         "Projectivy's license does not allow redistributing the APK")


if __name__ == "__main__":
    unittest.main()
