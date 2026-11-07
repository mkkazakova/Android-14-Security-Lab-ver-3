
"""Operate stock Files and Package Installer on an English API 34 AVD."""

import hashlib
import io
import re
import subprocess
import tempfile
from pathlib import Path
import time
import xml.etree.ElementTree as ET

from PIL import Image


INSTALLERS = {
    "com.android.packageinstaller",
    "com.google.android.packageinstaller"
}

FILES = {
    "com.android.documentsui",
    "com.google.android.documentsui"
}

REMOTE_XML = "/data/local/tmp/signature-ui.xml"


def adb(*args, binary=False, timeout=25):
    p = subprocess.run(
        ["adb", *args],
        capture_output=True,
        timeout=timeout
    )

    if p.returncode:
        raise RuntimeError(
            f"adb {args}: "
            f"{(p.stdout + p.stderr)[-1200:].decode(errors='replace')}"
        )

    return (
        p.stdout
        if binary
        else p.stdout.decode(errors="replace").strip()
    )


def snapshot(folder):
    adb("shell", "rm", "-f", REMOTE_XML)

    adb(
        "shell", "uiautomator", "dump",
        REMOTE_XML,
        timeout=14
    )

    xml = adb(
        "exec-out", "cat", REMOTE_XML,
        binary=True
    )

    nodes = list(ET.fromstring(xml).iter("node"))

    (folder / "latest.xml").write_bytes(xml)

    return nodes


def wait(folder, condition, name, seconds=65):
    deadline = time.monotonic() + seconds
    detail = ""

    while time.monotonic() < deadline:
        try:
            nodes = snapshot(folder)
            result = condition(nodes)

            if result is not None:
                (folder / f"{name}.xml").write_bytes(
                    (folder / "latest.xml").read_bytes()
                )
                return result

            detail = repr([
                (n.get("package"), n.get("text"))
                for n in nodes
                if n.get("text")
            ][-14:])

        except (
            RuntimeError,
            subprocess.TimeoutExpired,
            ET.ParseError
        ) as e:
            detail = str(e)

        time.sleep(0.6)

    raise TimeoutError(f"{name}: {detail}")


def find(nodes, packages, words):
    return next(
        (
            n for n in nodes
            if n.get("package") in packages
            and n.get("enabled") == "true"
            and n.get("text", "").casefold() in words
        ),
        None
    )


def bounds(node):
    values = list(map(
        int,
        re.findall(r"-?\d+", node.get("bounds", ""))
    ))

    if (
        len(values) != 4
        or values[2] <= values[0]
        or values[3] <= values[1]
    ):
        raise ValueError("Invalid UI bounds")

    return values


def tap(node):
    x, y, right, bottom = bounds(node)

    adb(
        "shell", "input", "tap",
        str((x + right) // 2),
        str((y + bottom) // 2)
    )


def outcome(nodes):
    """
    Recognize real terminal status text within
    an Android Package Installer UI.

    The APK label and the Done button are not guaranteed
    to be present on every failure screen.
    """

    for node in nodes:
        if node.get("package") not in INSTALLERS:
            continue

        value = (
            node.get("text", "")
            .strip()
            .casefold()
            .rstrip(".! ")
        )

        resource = (
            node.get("resource-id", "")
            .split("/")[-1]
            .casefold()
        )

        if (
            value.startswith("app not installed")
            or resource.startswith("install_failed")
        ):
            return "failure", node

        if (
            value == "app installed"
            or resource == "install_success"
        ):
            return "success", node

    return None


def check_png(data, node):
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG":
            raise ValueError("Expected PNG")
        image.verify()

    with Image.open(io.BytesIO(data)) as image:
        image.load()

        x, y, r, b = bounds(node)

        if not (
            0 <= x < r <= image.width
            and 0 <= y < b <= image.height
        ):
            raise ValueError(
                "Message bounds outside capture"
            )

        region = image.convert("L").crop((x, y, r, b))
        lo, hi = region.getextrema()

        if (
            hi - lo < 32
            or sum(region.histogram()[lo:hi - 15]) < 10
        ):
            raise ValueError(
                "System message area is blank or hidden"
            )


def capture(folder, state, previous):
    errors = []

    for attempt in range(3):
        try:
            before = wait(
                folder,
                outcome,
                "capture-before",
                12
            )

            if (
                before[0],
                before[1].get("text")
            ) != state:
                raise ValueError(
                    "Installer message changed"
                )

            source = "adb exec-out screencap -p"

            if attempt == 2:
                source = "adb emu screenrecord screenshot"

                with tempfile.TemporaryDirectory() as tmp:
                    response = adb(
                        "emu", "screenrecord",
                        "screenshot", tmp
                    )

                    files = list(
                        Path(tmp).glob("*.png")
                    )

                    if len(files) != 1:
                        raise ValueError(
                            "Emulator capture unavailable: "
                            + response
                        )

                    data = files[0].read_bytes()

            else:
                data = adb(
                    "exec-out", "screencap", "-p",
                    binary=True
                )

            check_png(data, before[1])

            after = wait(
                folder,
                outcome,
                "capture-after",
                12
            )

            if (
                (
                    after[0],
                    after[1].get("text")
                ) != state
                or bounds(after[1]) != bounds(before[1])
                or before[1].get("package")
                != after[1].get("package")
            ):
                raise ValueError(
                    "Screen changed during capture"
                )

            digest = hashlib.sha256(data).hexdigest()

            if digest in previous:
                raise ValueError("Duplicate PNG")

            (folder / "result.png").write_bytes(data)

            return digest, source

        except (
            RuntimeError,
            OSError,
            ValueError,
            subprocess.TimeoutExpired
        ) as e:
            errors.append(str(e))
            time.sleep(1)

    raise RuntimeError(
        "Capture: " + " / ".join(errors)
    )


def open_apk(folder, filename, files):
    uri = (
        "content://com.android.externalstorage.documents"
        "/document/primary%3ADownload"
    )

    def open_folder(mime, location):
        # am start -W can hang on a cold AVD.
        # Verify activity visibility separately.

        try:
            result = adb(
                "shell", "am", "start",
                "-a", "android.intent.action.VIEW",
                "-t", mime,
                "-d", location,
                "-p", files,
                timeout=15
            )

        except subprocess.TimeoutExpired as exc:
            # Intent may have been accepted
            # even if ADB timed out.
            (folder / "directory-launch-timeout.txt").write_text(
                str(exc)
            )
            return

        if "Error:" in result or "Exception" in result:
            raise RuntimeError(result)

    def file_row(ns):
        return find(
            ns,
            {files},
            {filename.casefold()}
        )

    try:
        open_folder(
            "vnd.android.document/directory",
            uri
        )

        row = wait(
            folder,
            file_row,
            "files",
            20
        )

    except (
        RuntimeError,
        TimeoutError,
        subprocess.TimeoutExpired
    ) as e:
        (folder / "directory-fallback.txt").write_text(
            str(e)
        )

        open_folder(
            "vnd.android.document/root",
            "content://com.android.externalstorage.documents"
            "/root/primary"
        )

        downloads = wait(
            folder,
            lambda ns: find(
                ns, {files}, {"download"}
            ),
            "storage-root"
        )

        tap(downloads)

        row = wait(
            folder,
            file_row,
            "files"
        )

    tap(row)



def install(folder, filename, files, update):
    open_apk(folder, filename, files)

    def prompt(ns):
        terminal = outcome(ns)

        if terminal is not None:
            return "terminal", terminal

        for label in (
            ("update", "install")
            if update
            else ("install",)
        ):
            button = find(
                ns,
                INSTALLERS,
                {label}
            )

            if button is not None:
                return "install", button

        settings = find(
            ns,
            INSTALLERS,
            {"settings"}
        )

        if settings is not None:
            return "settings", settings

        return None

    action, button = wait(
        folder,
        prompt,
        "prompt"
    )

    if action == "terminal":
        return button

    if action == "settings":
        tap(button)

        def toggle(ns):
            if find(
                ns,
                {"com.android.settings"},
                {"allow from this source"}
            ) is not None:
                return next(
                    (
                        n for n in ns
                        if n.get("package") == "com.android.settings"
                        and n.get("checkable") == "true"
                    ),
                    None
                )

        switch = wait(
            folder,
            toggle,
            "source-settings"
        )

        if switch.get("checked") != "true":
            tap(switch)

        adb(
            "shell", "input",
            "keyevent", "BACK"
        )

        action, button = wait(
            folder,
            prompt,
            "prompt-authorized"
        )

        if action != "install":
            raise RuntimeError(
                "Source authorization did not enable installation"
            )

    tap(button)

    return wait(
        folder,
        outcome,
        "terminal",
        90
    )
