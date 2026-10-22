#!/usr/bin/env python3
r"""Install Projectivy Launcher on an old Android TV and make it Home, or undo that.

The upstream CleanYourTV launcher needs Android 8.0 (API 26). Projectivy needs Android 6.0 (API 23),
so it is the launcher for sets this repository otherwise cannot cover. Android 6 has no way to set the
default Home from the shell (`cmd package` arrived in Android 7), so the handover is the one the
phone era used: install and start the new launcher, confirm it really is in front, disable the stock
launcher, press Home, and confirm Projectivy is still in front. If anything does not add up, the
stock launcher is enabled again before this script exits.

The APK is downloaded from the official upstream release and verified against projectivy/releases.json.
It is never bundled here: Projectivy's license does not allow redistribution.

Windows (PowerShell), from the repository root; on Linux/macOS replace the path with
tools/venv/bin/python and use `export TV_HOST=...`:

    $env:TV_HOST = "192.168.1.50"
    tools\venv\Scripts\python.exe projectivy\install.py --check        # read the TV, change nothing
    tools\venv\Scripts\python.exe projectivy\install.py --dry-run      # show the plan, change nothing
    tools\venv\Scripts\python.exe projectivy\install.py --install-only # install and start, keep Home
    tools\venv\Scripts\python.exe projectivy\install.py               # install, start, hand Home over
    tools\venv\Scripts\python.exe projectivy\install.py --rollback     # enable the stock launcher again

TV_HOST may be replaced by --host. The debloat half runs on Android 6 unchanged: see README.md.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TV_PY = ROOT / "tools" / "tv.py"
B64PUSH = ROOT / "tools" / "b64push.py"
RELEASES_PATH = Path(__file__).resolve().with_name("releases.json")
RECORD_DIR = Path.home() / ".cleanyourtv"

PACKAGE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+")
COMPONENT_RE = re.compile(r"[A-Za-z_][\w.]*/[\w.$]+")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
FAIL_RE = re.compile(r"(?i)\b(error|exception|failure|refused|denied)\b")

REMOTE_APK = "/data/local/tmp/cleanyourtv-projectivy.apk"
HOME_QUERY = ("cmd package resolve-activity --brief -a android.intent.action.MAIN "
              "-c android.intent.category.HOME")
# One connection, four spellings. Android 6 reports mCurrentFocus; newer builds also have
# mFocusedApp, and dumpsys activity names the resumed activity differently across versions.
FOCUS_QUERY = ("dumpsys window windows | grep mCurrentFocus; "
               "dumpsys window windows | grep mFocusedApp; "
               "dumpsys activity activities | grep mFocusedActivity; "
               "dumpsys activity activities | grep mResumedActivity")
KNOWN_STOCK_LAUNCHERS = (
    "com.google.android.tvlauncher",
    "com.google.android.apps.tv.launcherx",
    "com.google.android.leanbacklauncher",
    "com.mitv.tvhome",
)
PROPS = (
    "ro.product.manufacturer",
    "ro.product.model",
    "ro.build.version.release",
    "ro.build.version.sdk",
)

USAGE = """Windows (PowerShell), from the repository root:
  $env:TV_HOST = "192.168.1.50"
  tools\\venv\\Scripts\\python.exe projectivy\\install.py --check        # read the TV, change nothing
  tools\\venv\\Scripts\\python.exe projectivy\\install.py --dry-run      # show the plan, change nothing
  tools\\venv\\Scripts\\python.exe projectivy\\install.py --install-only # install and start, keep Home
  tools\\venv\\Scripts\\python.exe projectivy\\install.py               # install, start, hand Home over
  tools\\venv\\Scripts\\python.exe projectivy\\install.py --rollback    # enable the stock launcher again

Linux/macOS: export TV_HOST=192.168.1.50 and use tools/venv/bin/python in place of the path above.
"""


class InstallError(RuntimeError):
    pass


class AdbTv:
    """The same ADB-over-TCP path the rest of the repository uses, one connection per call."""

    def __init__(self, host=None, python=None, timeout=240):
        self.host = host or os.environ.get("TV_HOST") or ""
        self.python = python or default_python()
        self.timeout = timeout

    def _run(self, argv, stdin=None):
        if not self.host:
            raise InstallError("set TV_HOST to the TV's IP address, e.g. "
                               "TV_HOST=192.168.1.50 python projectivy/install.py")
        env = os.environ.copy()
        env["TV_HOST"] = self.host
        try:
            result = subprocess.run(argv, input=stdin, text=True, capture_output=True,
                                    env=env, timeout=self.timeout)
        except FileNotFoundError as e:
            raise InstallError(f"cannot run {argv[0]}: {e}")
        except subprocess.TimeoutExpired:
            raise InstallError(f"{Path(argv[1]).name} did not finish in {self.timeout:g}s; "
                               "the TV may be off or the connection may have dropped")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if "adb_shell" in detail or "No module named" in detail:
                detail += ("\nInstall the client: python -m venv tools/venv && "
                           "tools/venv/Scripts/python.exe -m pip install -r tools/requirements.txt "
                           "on Windows, tools/venv/bin/python -m pip install ... on Linux/macOS")
            raise InstallError(f"{Path(argv[1]).name} failed: {detail or 'no output'}")
        return result.stdout

    def shell(self, command):
        return self._run([self.python, "-I", str(TV_PY), command])

    def push(self, local, remote):
        try:
            return self._run([self.python, "-I", str(TV_PY), "--push", str(local), remote])
        except InstallError as first:
            # adb_shell's push is much faster, but an old adbd that refuses the data payload leaves
            # the base64 chunk upload as the fallback. Tests replace this class entirely.
            try:
                return self._run([self.python, "-I", str(B64PUSH), str(local), remote])
            except InstallError as second:
                raise InstallError(f"could not push the APK ({first}; fallback: {second})")


def default_python():
    override = os.environ.get("CYT_PYTHON")
    if override:
        return override
    for candidate in (ROOT / "tools" / "venv" / "Scripts" / "python.exe",
                      ROOT / "tools" / "venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def load_release():
    try:
        data = json.loads(RELEASES_PATH.read_text(encoding="utf-8"))
        launcher = data["launcher"]
        release = dict(launcher["releases"][0])
        release["name"] = launcher["name"]
        release["package"] = launcher["package"]
        release["home_activity"] = launcher["home_activity"]
        release["license_note"] = launcher.get("license_note", "")
    except (OSError, ValueError, KeyError, IndexError) as e:
        raise InstallError(f"{RELEASES_PATH} is not usable: {e}")
    if not PACKAGE_RE.fullmatch(release.get("package", "")):
        raise InstallError(f"{RELEASES_PATH}: not a package name: {release.get('package')!r}")
    if not COMPONENT_RE.fullmatch(f"{release['package']}/{release.get('home_activity', '')}"):
        raise InstallError(f"{RELEASES_PATH}: not a Home activity: "
                           f"{release.get('home_activity')!r}")
    if not isinstance(release.get("min_sdk"), int) or release["min_sdk"] > 23:
        raise InstallError(f"{RELEASES_PATH}: this integration needs a release that runs on "
                           f"API 23, got min_sdk {release.get('min_sdk')!r}")
    if not isinstance(release.get("size"), int) or not SHA256_RE.fullmatch(release.get("sha256", "")):
        raise InstallError(f"{RELEASES_PATH}: the release needs a size and a SHA-256")
    if not release.get("url", "").startswith("https://github.com/spocky/miproja1/releases/"):
        raise InstallError(f"{RELEASES_PATH}: the APK must come from the official Projectivy "
                           f"release page, got {release.get('url')!r}")
    return release


def probe(tv):
    props = {}
    for prop in PROPS:
        props[prop] = tv.shell(f"getprop {prop}").strip()
    raw = props.get("ro.build.version.sdk", "")
    try:
        sdk = int(raw)
    except ValueError:
        raise InstallError(f"the TV did not answer ro.build.version.sdk (got {raw!r}); "
                           "check TV_HOST and the debugging prompt on the TV")
    return props, sdk


def focused_packages(text):
    return set(re.findall(r"([A-Za-z_][\w.]*)/[\w.$]+", text))


def current_focus_packages(text):
    """The app in mCurrentFocus when the TV reports it; mResumedActivity can be behind a dialog."""
    for marker in ("mCurrentFocus", "mFocusedApp"):
        for line in text.splitlines():
            if marker in line:
                found = focused_packages(line)
                if found:
                    return found
    return focused_packages(text)


def focus_snapshot(tv):
    return tv.shell(FOCUS_QUERY)


def wait_for_focus(tv, packages, attempts=15, interval=1.0):
    if isinstance(packages, str):
        packages = (packages,)
    text = ""
    for attempt in range(max(1, attempts)):
        text = focus_snapshot(tv)
        if current_focus_packages(text) & set(packages):
            return True, text
        if attempt + 1 < attempts:
            time.sleep(interval)
    return False, text


def query_home_component(tv, sdk):
    """The component Android currently resolves Home to, when this Android has `cmd package`."""
    if sdk < 24:
        return None
    try:
        out = tv.shell(HOME_QUERY)
    except InstallError:
        return None
    for line in reversed([line.strip() for line in out.splitlines() if line.strip()]):
        if COMPONENT_RE.fullmatch(line):
            return line
    return None


def query_home_package(tv, sdk):
    component = query_home_component(tv, sdk)
    if component:
        return component.split("/", 1)[0]
    # Android 6 has no cmd package. dumpsys activity still names the process Home last started;
    # tools/disable.sh uses the same fallback for its Home guard.
    try:
        out = tv.shell("dumpsys activity | grep mHomeProcess")
    except InstallError:
        return None
    for line in out.splitlines():
        match = re.search(r"mHomeProcess:.*?[: ]([A-Za-z_][\w.]*)/", line)
        if match:
            return match.group(1)
    return None


def choose_stock(tv, sdk, requested, projectivy_package):
    """The package to disable, or None when Home already belongs to Projectivy / must be left alone."""
    if requested == "none":
        return None
    if requested:
        if not PACKAGE_RE.fullmatch(requested):
            raise InstallError(f"--stock-launcher is not a package name: {requested!r}")
        installed = tv.shell(f"pm path {requested}")
        if "package:" not in installed:
            raise InstallError(f"{requested} is not installed on the TV; check the package name")
        return requested
    home = query_home_package(tv, sdk)
    if not home:
        raise InstallError(
            "could not ask the TV which launcher owns Home (Android 6 has no `cmd package`). "
            "Pass --stock-launcher <package>, e.g. --stock-launcher com.mitv.tvhome, "
            "or --install-only to leave Home untouched.")
    if home == projectivy_package:
        print(f"Home already belongs to Projectivy ({home}); no stock launcher will be disabled.")
        return None
    if home == "android":
        raise InstallError(
            "Home is Android's launcher chooser, not a single launcher. Pick a default launcher on "
            "the TV first, or pass --stock-launcher <package>, or --install-only.")
    if home not in KNOWN_STOCK_LAUNCHERS:
        raise InstallError(
            f"Home currently belongs to '{home}', which is not a stock launcher this installer "
            "knows. Check `pm list packages | grep -i home`, then pass --stock-launcher <package> "
            "if it is the vendor launcher, or --install-only to leave Home alone.")
    return home


def verify_apk(path, release, skip_verify=False):
    path = Path(path)
    if not path.is_file():
        raise InstallError(f"no APK at {path}")
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    size = path.stat().st_size
    if skip_verify:
        print(f"warning: --skip-verify: {path} is {size} bytes, sha256 {actual} (not checked)")
        return actual
    if actual != release["sha256"] or size != release["size"]:
        raise InstallError(
            f"{path} is not the official Projectivy {release['version']} APK.\n"
            f"  expected {release['size']} bytes, sha256 {release['sha256']}\n"
            f"  got      {size} bytes, sha256 {actual}\n"
            "Download it again from the release named in projectivy/releases.json, or pass "
            "--skip-verify only if you built this APK yourself.")
    print(f"APK verified: Projectivy {release['version']} ({size} bytes, sha256 {actual[:16]}...)")
    return actual


def acquire_apk(args, release):
    if args.apk:
        return Path(args.apk).expanduser()
    cache = Path(args.cache_dir).expanduser() if args.cache_dir else RECORD_DIR / "projectivy"
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / release["url"].rsplit("/", 1)[-1]
    if dest.exists():
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        if digest == release["sha256"] and dest.stat().st_size == release["size"]:
            print(f"using the verified download at {dest}")
            return dest
        print(f"{dest} does not match the pinned release; downloading it again")
    part = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(
        release["url"],
        headers={"User-Agent": "cleanyourtv-legacy-projectivy/1.0"})
    print(f"downloading Projectivy {release['version']} from {release['url']}")
    try:
        with urllib.request.urlopen(request, timeout=120) as response, open(part, "wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    except (urllib.error.URLError, OSError) as e:
        part.unlink(missing_ok=True)
        raise InstallError(
            f"download failed: {e}\nDownload the official APK yourself and pass --apk <path>:\n"
            f"  {release['url']}")
    os.replace(part, dest)
    # A download is always checked: --skip-verify exists for a locally built APK, not for a file
    # whose whole point is that it came from the pinned official release.
    verify_apk(dest, release, False)
    return dest


def install_apk(tv, apk, release):
    print(f"pushing {apk} to {REMOTE_APK}")
    tv.push(apk, REMOTE_APK)
    try:
        out = tv.shell(f"pm install -r {REMOTE_APK}")
    finally:
        try:
            tv.shell(f"rm -f {REMOTE_APK}")
        except InstallError:
            print(f"warning: could not remove {REMOTE_APK} from the TV", file=sys.stderr)
    if "Success" not in out:
        detail = " ".join(line.strip() for line in out.splitlines() if line.strip())
        if "INSTALL_FAILED_OLDER_SDK" in out:
            raise InstallError(
                f"pm install refused the APK as too new for this TV (SDK floor in the APK is "
                f"higher than the TV's). Nothing was disabled. {detail}")
        raise InstallError(f"pm install failed; nothing was disabled: {detail or 'no output'}")
    path = tv.shell(f"pm path {release['package']}")
    if "package:" not in path:
        raise InstallError(f"pm install said Success, but `pm path {release['package']}` is empty; "
                           "the package is not usable. Nothing was disabled.")
    print(f"{release['name']} {release['version']} installed.")


def start_launcher(tv, release):
    component = f"{release['package']}/{release['home_activity']}"
    out = tv.shell(f"am start -W -n {component}")
    if "Status: ok" not in out or re.search(r"(?im)^error", out):
        detail = " ".join(line.strip() for line in out.splitlines() if line.strip())
        raise InstallError(f"the new launcher did not start, so the stock Home was left untouched "
                           f"({detail or 'no output'})")
    return out


def record_path(host):
    tag = re.sub(r"[^A-Za-z0-9._-]", "_", host or "no-host")
    return RECORD_DIR / f"projectivy-home-{tag}.json"


def recovery_path(host):
    return record_path(host).with_suffix(".txt")


def write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_record(path):
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def recovery_commands(host, stock, sdk, component=None):
    """The commands that put the stock launcher back, in the shell of the machine running this."""
    if os.name == "nt":
        # PowerShell reads the host from the environment, so the recovery file starts by setting it.
        yield '$env:TV_HOST = "%s"' % host
        prefix = r"tools\venv\Scripts\python.exe -I tools\tv.py"
    else:
        prefix = f"TV_HOST={host} tools/venv/bin/python -I tools/tv.py"
    yield f"{prefix} 'pm enable --user 0 {stock}'"
    if sdk >= 24 and component:
        yield f"{prefix} 'cmd package set-home-activity --user 0 {component}'"
    yield f"{prefix} 'input keyevent KEYCODE_HOME'"


def write_recovery(host, stock, sdk, component, reason):
    path = recovery_path(host)
    lines = [
        f"# Projectivy Home takeover on {host} did not finish: {reason}",
        "# Run these from the repository root while the TV still answers ADB:",
        *recovery_commands(host, stock, sdk, component),
        f"# Then check: {recovery_path(host).parent} and projectivy/install.py --check",
    ]
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
    except OSError as e:
        print(f"warning: could not write {path}: {e}", file=sys.stderr)
        return None


def set_stock_home(tv, sdk, stock, component=None):
    """Best effort: re-enable the stock path Android takes to its launcher after a rollback."""
    if sdk < 24:
        return
    target = component
    if not target:
        try:
            out = tv.shell(f"{HOME_QUERY} {stock}")
            for line in reversed([line.strip() for line in out.splitlines() if line.strip()]):
                if COMPONENT_RE.fullmatch(line) and line.startswith(stock + "/"):
                    target = line
                    break
        except InstallError:
            return
    if target:
        try:
            tv.shell(f"cmd package set-home-activity --user 0 {target}")
        except InstallError:
            pass


def rollback_after_failure(tv, sdk, stock, release, host, reason, component, args):
    record_file = record_path(host)
    print(f"ERROR: {reason}", file=sys.stderr)
    print(f"Putting {stock} back ...", file=sys.stderr)
    try:
        tv.shell(f"pm enable --user 0 {stock}")
        set_stock_home(tv, sdk, stock, component)
        tv.shell("input keyevent KEYCODE_HOME")
        wait_for_focus(tv, (stock, "android"), args.attempts, args.wait)
        disabled = tv.shell("pm list packages -d")
        reenabled = f"package:{stock}" not in disabled
        focus = focus_snapshot(tv)
    except InstallError as e:
        reenabled = False
        focus = ""
        print(f"ERROR: the TV stopped answering during rollback: {e}", file=sys.stderr)

    record = load_record(record_file) or {}
    record.update({
        "version": 1,
        "package": release["package"],
        "home_activity": release["home_activity"],
        "stock_launcher": stock,
        "stock_component": component,
        "sdk": sdk,
        "host": host,
        "state": "rolled-back" if reenabled else "recovery-incomplete",
        "created": record.get("created") or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    write_record(record_file, record)
    if reenabled:
        shown = ", ".join(sorted(focused_packages(focus))) or "no focused app reported"
        print(f"{stock} is enabled again; the TV reports focus on {shown}.", file=sys.stderr)
        print("The stock launcher was restored. Fix the cause, then run the installer again.",
              file=sys.stderr)
        return 1
    record["state"] = "recovery-incomplete"
    write_record(record_file, record)
    commands = recovery_commands(host, stock, sdk, component)
    text = write_recovery(host, stock, sdk, component, "Home could not be verified after rollback")
    print("ERROR: recovery is incomplete; the stock launcher may still be disabled.", file=sys.stderr)
    if text:
        print(f"The commands that finish it are in {text}; the TV still needs ADB for them.",
              file=sys.stderr)
    else:
        for command in commands:
            print(f"  {command}", file=sys.stderr)
    return 2


def take_over(tv, sdk, stock, release, args, component=None):
    host = args.host or os.environ.get("TV_HOST", "")
    record_file = record_path(host)
    record = {
        "version": 1,
        "package": release["package"],
        "home_activity": release["home_activity"],
        "stock_launcher": stock,
        "stock_component": component,
        "sdk": sdk,
        "host": host,
        "state": "in-progress",
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    # Written before the first change: a crash or a dropped connection after `pm disable-user`
    # still leaves the package name that `--rollback` needs.
    write_record(record_file, record)

    try:
        out = tv.shell(f"pm disable-user --user 0 {stock}")
        if FAIL_RE.search(out):
            raise InstallError(f"pm disable-user --user 0 {stock} was refused: "
                               f"{' '.join(out.split())}")
        disabled = tv.shell("pm list packages -d")
        if f"package:{stock}" not in disabled:
            raise InstallError(f"the TV did not report {stock} as disabled after pm disable-user")

        # Android 7+ can name the Home component. Android 6 cannot, so there the disabled stock
        # launcher is the whole switch: Projectivy must be the only app left that answers HOME.
        home_set = False
        if sdk >= 24:
            try:
                out = tv.shell(f"cmd package set-home-activity --user 0 "
                               f"{release['package']}/{release['home_activity']}")
                if FAIL_RE.search(out):
                    raise InstallError(f"cmd package set-home-activity was refused: "
                                       f"{' '.join(out.split())}")
                home_set = True
            except InstallError as e:
                print(f"warning: cmd package set-home-activity failed ({e}); "
                      "Home will fall back to the only remaining launcher.", file=sys.stderr)

        tv.shell("input keyevent KEYCODE_HOME")
        focused, focus_text = wait_for_focus(tv, release["package"], args.attempts, args.wait)
        home = query_home_package(tv, sdk) if sdk >= 24 else release["package"]
        # When Android accepted set-home-activity, Home must really name Projectivy. On a build
        # without that command, the disabled stock launcher leaves Projectivy as the only candidate,
        # and focus is the honest check.
        if not focused or (home_set and home != release["package"]):
            shown = ", ".join(sorted(focused_packages(focus_text))) or "nothing"
            raise InstallError(
                f"Home did not land on Projectivy (focus: {shown}; Home resolves to "
                f"{home or 'nothing'})")
    except InstallError as e:
        return rollback_after_failure(tv, sdk, stock, release, host, str(e), component, args)

    record.update({"state": "complete",
                   "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    write_record(record_file, record)
    print(f"Home now belongs to {release['name']} {release['version']}.")
    print(f"{stock} is disabled (reversible). Undo: python projectivy/install.py --rollback")
    print(f"Recovery record: {record_file}")
    return 0


def check(tv, sdk, props, release, args):
    host = args.host or os.environ.get("TV_HOST", "")
    print(f"TV:       {props.get('ro.product.manufacturer', '?')} "
          f"{props.get('ro.product.model', '?')}, Android "
          f"{props.get('ro.build.version.release', '?')} (SDK {sdk})")
    home = query_home_package(tv, sdk)
    installed = "package:" in tv.shell(f"pm path {release['package']}")
    version = ""
    if installed:
        version = tv.shell(f"dumpsys package {release['package']} | grep versionName").strip()
    print(f"Home:     {home or 'not reported (Android 6 cannot resolve it without cmd package)'}")
    print(f"{release['name']}: {'installed' + (f' ({version})' if version else '') if installed else 'not installed'}")
    disabled = tv.shell("pm list packages -d")
    disabled_names = {line.split(":", 1)[1].strip() for line in disabled.splitlines()
                      if line.startswith("package:")}
    stock = args.stock_launcher
    if not stock or stock == "none":
        record = load_record(record_path(host))
        stock = record.get("stock_launcher") if record else None
    if stock and stock != "none":
        state = "disabled" if stock in disabled_names else "enabled or absent"
        print(f"Stock:    {stock} ({state})")
    focus = focus_snapshot(tv)
    shown = ", ".join(sorted(focused_packages(focus))) or "nothing"
    print(f"Focus:    {shown}")
    record_file = record_path(host)
    record = load_record(record_file)
    if record:
        print(f"Record:   {record_file} ({record.get('state', 'unknown')})")
    else:
        print(f"Record:   none at {record_file}")
    return 0


def rollback(tv, sdk, args, release):
    host = args.host or os.environ.get("TV_HOST", "")
    record_file = record_path(host)
    record = load_record(record_file) or {}
    stock = args.stock_launcher
    if stock == "none":
        raise InstallError("--stock-launcher none has nothing to roll back")
    if not stock:
        stock = record.get("stock_launcher")
    if not stock:
        raise InstallError(
            "no recovery record names the stock launcher. Pass --stock-launcher <package>, "
            "e.g. --stock-launcher com.mitv.tvhome.")
    if not PACKAGE_RE.fullmatch(stock):
        raise InstallError(f"not a package name: {stock!r}")
    out = tv.shell(f"pm enable --user 0 {stock}")
    if FAIL_RE.search(out):
        raise InstallError(f"pm enable --user 0 {stock} was refused: {' '.join(out.split())}")
    set_stock_home(tv, sdk, stock, record.get("stock_component"))
    tv.shell("input keyevent KEYCODE_HOME")
    focused, focus_text = wait_for_focus(tv, (stock, "android"), args.attempts, args.wait)
    disabled = tv.shell("pm list packages -d")
    if f"package:{stock}" in disabled:
        raise InstallError(f"{stock} is still reported as disabled")
    shown = ", ".join(sorted(focused_packages(focus_text))) or "nothing"
    if record:
        record.update({"state": "rolled-back",
                       "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        write_record(record_file, record)
    print(f"{stock} is enabled again. Focus: {shown}.")
    if not focused:
        print("Android 6 cannot set a Home component from the shell. Press Home on the remote and "
              "pick the stock launcher if the TV asks; Projectivy is still installed.")
    print(f"{release['name']} was left installed (this rollback only returns Home).")
    return 0


def run(tv, args, release):
    props, sdk = probe(tv)
    if sdk < release["min_sdk"]:
        raise InstallError(
            f"this TV runs SDK {sdk}; {release['name']} {release['version']} needs Android 6.0 "
            f"(API {release['min_sdk']}) or newer. Nothing was changed.")
    if args.check:
        return check(tv, sdk, props, release, args)
    if args.rollback:
        return rollback(tv, sdk, args, release)

    host = args.host or os.environ.get("TV_HOST", "")
    stock = None
    stock_component = None
    if not args.no_takeover:
        stock = choose_stock(tv, sdk, args.stock_launcher, release["package"])
        # Read the stock component before Projectivy starts, so a rollback cannot write Projectivy
        # into the record as the thing to restore.
        stock_component = query_home_component(tv, sdk) if stock else None

    apk = None
    if args.dry_run:
        if args.apk:
            apk = Path(args.apk).expanduser()
            verify_apk(apk, release, args.skip_verify)
        print("DRY RUN: no file is pushed, installed or disabled.")
        print(f"  TV:      {props.get('ro.product.model', '?')} (SDK {sdk})")
        print(f"  APK:     {apk or release['url']}")
        print(f"  Install: pm install -r, then start "
              f"{release['package']}/{release['home_activity']}")
        if args.no_takeover:
            print("  Home:    left alone (--install-only)")
        elif stock:
            how = ("disable-user, press HOME, require Projectivy in focus; re-enable stock if not"
                   if sdk < 24 else
                   "disable-user, set-home-activity, press HOME, require Projectivy in focus")
            print(f"  Home:    {how} ({stock})")
        elif args.stock_launcher == "none":
            print("  Home:    left alone (--stock-launcher none)")
        else:
            print("  Home:    already Projectivy, nothing to disable")
        return 0

    apk = acquire_apk(args, release)
    verify_apk(apk, release, args.skip_verify)
    install_apk(tv, apk, release)
    try:
        start_launcher(tv, release)
        focused, _ = wait_for_focus(tv, release["package"], args.attempts, args.wait)
        if not focused:
            raise InstallError(
                f"{release['name']} was installed but never came to the front (it may be showing "
                "its first-run screen or a permission dialog). The stock launcher was left "
                "untouched; finish the Projectivy setup on the TV, then run this installer again.")
    except InstallError:
        raise

    if args.no_takeover:
        print(f"{release['name']} is installed and running. The stock launcher was left enabled.")
        print("Run the installer again without --install-only to hand Home over.")
        return 0
    if stock is None:
        if args.stock_launcher == "none":
            print(f"{release['name']} is installed and running; --stock-launcher none left Home "
                  "unchanged.")
        else:
            print("Nothing to disable: Home already belongs to Projectivy.")
        return 0
    return take_over(tv, sdk, stock, release, args, stock_component)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Install Projectivy Launcher on Android 6/7 and hand Home over safely.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=USAGE)
    parser.add_argument("--host", help="TV IP address (default: TV_HOST)")
    parser.add_argument("--python", help="Python with adb_shell to run tools/tv.py "
                                         "(default: tools/venv, then this interpreter)")
    parser.add_argument("--apk", help="already downloaded official APK (otherwise download it)")
    parser.add_argument("--cache-dir", help="where to keep the downloaded APK "
                                            "(default: ~/.cleanyourtv/projectivy)")
    parser.add_argument("--skip-verify", action="store_true",
                        help="accept an APK whose size and SHA-256 do not match the pinned release")
    parser.add_argument("--stock-launcher", metavar="PACKAGE",
                        help="vendor launcher to disable (e.g. com.mitv.tvhome); "
                             "'none' leaves Home alone")
    parser.add_argument("--install-only", dest="no_takeover", action="store_true",
                        help="install and start Projectivy, but do not change Home")
    parser.add_argument("--check", action="store_true", help="print what the TV reports, change nothing")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--rollback", action="store_true", help="enable the stock launcher again")
    parser.add_argument("--list-releases", action="store_true", help="print the pinned release")
    parser.add_argument("--attempts", type=int, default=15,
                        help="times to ask which app has focus (default: 15)")
    parser.add_argument("--wait", type=float, default=1.0,
                        help="seconds between focus checks (default: 1)")
    parser.add_argument("--timeout", type=float, default=240,
                        help="seconds for one ADB operation (default: 240)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        release = load_release()
        if args.list_releases:
            print(f"{release['name']} {release['version']} (versionCode {release['version_code']}), "
                  f"minSdk {release['min_sdk']}, {release['size']} bytes")
            print(f"  {release['url']}")
            print(f"  sha256 {release['sha256']}")
            print(f"  {release['license_note']}")
            return 0
        if not args.host and not os.environ.get("TV_HOST"):
            raise InstallError("set TV_HOST to the TV's IP address or pass --host")
        tv = AdbTv(host=args.host, python=args.python, timeout=args.timeout)
        return run(tv, args, release)
    except (InstallError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
