
import hashlib
import json
import re
import time
from subprocess import TimeoutExpired

from gui import FILES, adb, capture, install, snapshot, wait
from inspect_apks import INPUTS, PACKAGE
from report import CASES, OUT, initial, save

REMOTE = "/sdcard/Download"


def ready_device():
    for _ in range(45):
        try:
            if (adb("shell", "getprop", "sys.boot_completed") == "1"
                    and "package:android" in adb("shell", "pm", "list", "packages", "android")):
                break
        except (RuntimeError, TimeoutExpired):
            pass
        time.sleep(2)
    else:
        raise TimeoutError("Android or Package Manager did not finish starting")

    if adb("shell", "getprop", "ro.build.version.sdk") != "34":
        raise RuntimeError("API 34 required")

    (OUT / "device.txt").write_text(adb("shell", "getprop"))
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "wm", "dismiss-keyguard")
    adb("shell", "settings", "put", "system", "screen_off_timeout", "1800000")



def ready():
    ready_device()
    installed = set(
        adb("shell", "pm", "list", "packages", "-s")
        .replace("package:", "")
        .splitlines()
    )
    files = next((p for p in sorted(FILES) if p in installed), None)
    if not files:
        raise RuntimeError("System DocumentsUI package missing")

    # Android may attribute a Downloads-provider launch
    # to a different source.
    for p in sorted({
        files,
        "com.android.providers.downloads.ui",
        "com.android.providers.downloads"
    } & installed):
        adb("shell", "appops", "set", p, "REQUEST_INSTALL_PACKAGES", "allow")
        (OUT / f"source-{p}.txt").write_text(
            adb("shell", "appops", "get", p, "REQUEST_INSTALL_PACKAGES")
        )

    adb("shell", "mkdir", "-p", REMOTE)

    for _, filename, *_ in CASES:
        remote = f"{REMOTE}/{filename}"
        apk = INPUTS / filename
        adb("push", str(apk), remote)

        if adb("shell", "sha256sum", remote).split()[0] != hashlib.sha256(
            apk.read_bytes()
        ).hexdigest():
            raise ValueError("Device input differs: " + filename)

    return files


def installed(folder, label):
    packages = adb(
        "shell", "pm", "list", "packages", "--user", "0", PACKAGE
    )

    if f"package:{PACKAGE}" not in packages.splitlines():
        return {"version": None, "sha256": None}

    info = adb("shell", "dumpsys", "package", PACKAGE)
    (folder / f"package-{label}.txt").write_text(info)

    match = re.search(r"\bversionCode=(\d+)", info)

    paths = adb("shell", "pm", "path", PACKAGE).splitlines()
    base = next(
        (
            p[8:] for p in paths
            if p.startswith("package:") and p.endswith("/base.apk")
        ),
        None
    )

    if not match or not base:
        raise RuntimeError("Installed package metadata unavailable")

    return {
        "version": int(match[1]),
        "sha256": adb("shell", "sha256sum", base).split()[0]
    }


def mismatch(log):
    return next(
        (
            line for line in log.splitlines()
            if PACKAGE in line
            and re.search(
                r"\b(?:PackageManager|PackageInstallerSession)\s*:",
                line
            )
            and re.search(
                r"signatures? (?:do not|does not) match"
                r"|INSTALL_FAILED_UPDATE_INCOMPATIBLE",
                line,
                re.I
            )
        ),
        None
    )


def diagnose(folder):
    for name, cmd in (
        ("windows", ("shell", "dumpsys", "window", "windows")),
        ("activities", ("shell", "dumpsys", "activity", "activities"))
    ):
        try:
            (folder / f"{name}.txt").write_text(adb(*cmd))
        except Exception as e:
            (folder / f"{name}-error.txt").write_text(str(e))

    try:
        snapshot(folder)
    except Exception as e:
        (folder / "ui-error.txt").write_text(str(e))


def stage(row, case, records, files, previous):
    id_, filename, _, _, pre, post = case

    folder = OUT / id_
    folder.mkdir(exist_ok=True)

    row["input"] = records[filename]
    row["reason"] = None

    before = installed(folder, "before")
    row["before"] = before

    original_hash = records["sample-v1-a.apk"]["apk_sha256"]

    if before["version"] != pre or (
        pre and before["sha256"] != original_hash
    ):
        row["reason"] = (
            "Исходное состояние пакета не соответствует протоколу."
        )
        return

    # Configure rotation before sending HOME,
    # then wait until the launcher is visible.
    adb(
        "shell", "settings", "put",
        "system", "accelerometer_rotation", "0"
    )

    # Two success dialogs can be identical.
    # Present the update in landscape.
    adb(
        "shell", "settings", "put",
        "system", "user_rotation",
        "1" if id_ == "1.3" else "0"
    )

    adb("shell", "input", "keyevent", "KEYCODE_HOME")

    wait(
        folder,
        lambda ns: True if any(
            n.get("package") in {
                "com.google.android.apps.nexuslauncher",
                "com.android.launcher3",
                "com.android.launcher3.quickstep",
            }
            for n in ns
        ) else None,
        "home-ready",
        seconds=30,
    )

    adb("logcat", "-b", "all", "-c")

    wanted = "failure" if id_ == "1.2" else "success"
    errors = []
    observed = None

    try:
        observed, node = install(
            folder, filename, files, pre is not None
        )

        message = node.get("text")

        row["installer"] = dict(
            state=observed,
            message=message,
            package=node.get("package")
        )

        digest, source = capture(
            folder, (observed, message), previous
        )

        previous.add(digest)

        row.update(
            screenshot=f"{id_}/result.png",
            png_sha256=digest,
            png_valid=True,
            capture_source=source
        )

    except Exception as e:
        errors.append(f"GUI: {e}")
        diagnose(folder)

    log = adb("logcat", "-b", "all", "-d", "-v", "threadtime")
    (folder / "logcat.txt").write_text(log)

    after = installed(folder, "after")
    row["after"] = after

    expected_hash = (
        records["sample-v2-a.apk"]["apk_sha256"]
        if post == 2
        else original_hash
    )

    retained = (
        after["version"] == post
        and after["sha256"] == expected_hash
    )

    rejection = mismatch(log) if wanted == "failure" else None
    row["signature_error"] = rejection

    technical = retained and (
        wanted == "success" or bool(rejection)
    )

    row["technical"] = (
        "Подтверждено" if technical else "Не подтверждено"
    ) + f"; versionCode={after['version']}."

    row["technical_verified"] = technical

    if not technical:
        errors.append(
            "Не подтверждены ожидаемая версия, хеш "
            "установленного APK или причина отклонения."
        )

    if observed and observed != wanted:
        errors.append("Установщик показал противоположный исход.")

    if observed is not None and (
        observed != wanted or not retained
    ):
        row["status"] = "FAIL"

    elif technical and observed == wanted and row["png_valid"]:
        row["status"] = "PASS"

    row["reason"] = " ".join(errors) or None


def main():
    rows = initial("Ожидает запуска.")
    save(rows)

    try:
        records = json.loads(
            (OUT / "verified.json").read_text()
        )
        files = ready()

    except Exception as e:
        save(initial(str(e)))
        raise

    previous = set()

    for row, case in zip(rows, CASES):
        try:
            stage(row, case, records, files, previous)

        except Exception as e:
            row["reason"] = str(e)
            diagnose(OUT / case[0])

        save(rows)

        print(
            row["id"],
            row["status"],
            row["reason"],
            flush=True
        )

    return 0 if all(
        r["status"] == "PASS" for r in rows
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
