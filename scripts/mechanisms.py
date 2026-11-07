"""System-only visual evidence for the isolated experiments on an English API 34 AVD."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from gui import FILES, INSTALLERS, adb, bounds, check_png, find, open_apk, snapshot, tap, wait
from inspect_apks import PACKAGE
from report import OUT
from run import diagnose, installed, push_download_apk, ready_device

SETTINGS = {"com.android.settings"}
PRIVACY_SETTINGS = {"com.android.settings", "com.android.permissioncontroller", "com.google.android.permissioncontroller"}
RECORDER = "org.fossify.voicerecorder"
MANAGER = "org.fossify.filemanager"
APK = "sample-v1-a.apk"
TITLES = {"source": "2. Установка из неизвестного источника", "microphone": "3. Доступ к микрофону",
          "usb": "8. USB-отладка"}


class Contrary(RuntimeError):
    """A directly observed contradiction, distinct from missing evidence."""


def text_node(nodes, owners, test):
    return next((n for n in nodes if n.get("package") in owners and test(n.get("text", "").casefold())), None)


def switch_row(nodes, title, owners=None):
    """Find the switch inside the preference with this label.

    Pixel Android 14 hosts Privacy Controls in PermissionController rather
    than in the Settings app, so callers must select the expected owners.
    """
    owners = SETTINGS if owners is None else owners
    label = text_node(nodes, owners, lambda value: value == title)
    if label is None:
        return None
    for row in reversed(nodes):
        if label not in row.iter():
            continue
        controls = [n for n in row.iter()
                    if n.get("package") in owners and n.get("checkable") == "true"]
        if len(controls) == 1:
            return label, controls[0]
        if len(controls) > 1:
            break
    return None


def switch_condition(title, checked, owners=None):
    def match(nodes):
        row = switch_row(nodes, title, owners)
        if row and row[1].get("checked") == str(checked).lower():
            return row[0]
        return None
    return match


def visible_button(ns, pkg, resource):
    return next((n for n in ns if n.get("package") == pkg and
                 n.get("resource-id", "").endswith("/"+resource) and n.get("enabled") == "true"), None)


def start(action, *args):
    result = adb("shell", "am", "start", "-a", action, *args)
    if "Error:" in result or "Exception" in result:
        raise RuntimeError(result)


def home(folder):
    adb("shell", "input", "keyevent", "HOME")
    wait(folder, lambda ns: next((n for n in ns if "launcher" in n.get("package", "")), None), "home-ready", 35)


def png_bytes(attempt):
    if attempt < 2:
        return adb("exec-out", "screencap", "-p", binary=True), "adb screencap"
    with tempfile.TemporaryDirectory() as tmp:
        adb("emu", "screenrecord", "screenshot", tmp)
        files = list(Path(tmp).glob("*.png"))
        if len(files) != 1:
            raise ValueError("Emulator did not produce one PNG")
        return files[0].read_bytes(), "emulator screenshot"


def evidence(folder, name, match, report):
    errors = []
    for attempt in range(3):
        try:
