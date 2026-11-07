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
from run import diagnose, installed, ready_device

SETTINGS = {"com.android.settings"}
RECORDER = "org.fossify.voicerecorder"
MANAGER = "org.fossify.filemanager"
APK = "sample-v1-a.apk"
TITLES = {"source": "2. Установка из неизвестного источника", "microphone": "3. Доступ к микрофону",
          "usb": "8. USB-отладка"}


class Contrary(RuntimeError):
    """A directly observed contradiction, distinct from missing evidence."""


def text_node(nodes, owners, test):
    return next((n for n in nodes if n.get("package") in owners and test(n.get("text", "").casefold())), None)


def switch_row(nodes, title):
    """Find the switch in the smallest Settings preference containing this exact label."""
    label = text_node(nodes, SETTINGS, lambda s: s == title)
    if label is None:
        return None
    for row in reversed(nodes):
        if label not in row.iter():
            continue
        controls = [n for n in row.iter() if n.get("package") in SETTINGS and n.get("checkable") == "true"]
        if len(controls) == 1:
            return label, controls[0]
        if len(controls) > 1:
            break
    return None


def switch_condition(title, checked):
    def match(nodes):
        row = switch_row(nodes, title)
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
            before = wait(folder, match, name+"-before", 12)
            data, origin = png_bytes(attempt)
            check_png(data, before)
            after = wait(folder, match, name+"-after", 12)
            if any(before.get(k) != after.get(k) for k in ("package", "text", "bounds", "resource-id")):
                raise ValueError("System element changed during capture")
            digest = hashlib.sha256(data).hexdigest()
            if any(i["sha256"] == digest for i in report["images"]):
                raise ValueError("Duplicate PNG")
            path = folder/(name+".png")
            path.write_bytes(data)
            report["images"].append(dict(path=str(path.relative_to(OUT)), sha256=digest,
                                         package=before.get("package"), text=before.get("text"), source=origin))
            return
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
    raise RuntimeError(f"{name}: " + " / ".join(errors))


def third_party(pkg, folder):
    record = next(r for r in json.loads(Path("apks/external.json").read_text()) if r["package"] == pkg)
    with tempfile.TemporaryDirectory() as tmp:
        apk = Path(tmp)/"release.apk"
        with urlopen(record["url"], timeout=90) as response:
            apk.write_bytes(response.read())
        if hashlib.sha256(apk.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError("External APK SHA-256 mismatch")
        sdk = Path(os.environ["ANDROID_HOME"])/"build-tools/35.0.0"
        for command in ([sdk/"apksigner","verify","--print-certs",apk], [sdk/"aapt","dump","badging",apk]):
            result = subprocess.check_output(command, text=True)
            (folder/(command[0].name+".txt")).write_text(result)
        if not re.search(r"^package: name='"+re.escape(pkg)+r"' .*versionName='"+re.escape(record["version"])+r"'", result, re.M):
            raise ValueError("External APK identity mismatch")
        adb("install", str(apk), timeout=100)
    (folder/"external.json").write_text(json.dumps(record, indent=2))
    return record


def off_warning(ns):
    if find(ns, INSTALLERS, {"settings"}) is None:
        return None
    return text_node(ns, INSTALLERS, lambda s: ("from this source" in s or "unknown apps" in s)
                     and ("allow" in s or "install" in s))


def open_manager(folder):
    start("android.intent.action.VIEW", "-t", "resource/folder", "-d",
          "file:///storage/emulated/0/Download", "-p", MANAGER)
    def locate(ns):
        file = find(ns, {MANAGER}, {APK, APK.removesuffix(".apk")})
        if file is not None:
            return "file", file
        directory = find(ns, {MANAGER}, {"download"})
        if directory is not None:
            return "directory", directory
        return None
    action, item = wait(folder, locate, "manager-files")
    tap(item)
    if action == "directory":
        tap(wait(folder, lambda ns: find(ns, {MANAGER}, {APK}), "manager-download"))


def sample_state(folder, label):
    listing = adb("shell","pm","list","packages","--user","0").splitlines()
    if "package:android" not in listing:
        raise RuntimeError("Не получен список пакетов пользователя 0")
    if "package:"+PACKAGE not in listing:
        return {"version":None,"sha256":None}
    state = installed(folder,label)
    if state["version"] is None:
        raise RuntimeError("Состояние пакета изменилось во время проверки")
    return state


def source_case(folder, r):
    records = json.loads((OUT/"verified.json").read_text())
    if sample_state(folder, "before")["version"] is not None:
        raise RuntimeError("Пакет уже установлен до опыта")
    remote = "/sdcard/Download/"+APK
    adb("shell","mkdir","-p","/sdcard/Download")
    adb("push", "apks/"+APK, remote)
    if adb("shell","sha256sum",remote).split()[0] != records[APK]["apk_sha256"]:
        raise ValueError("Device APK differs from verified input")
    system = adb("shell","pm","list","packages","-s").splitlines()
    files = next((p for p in sorted(FILES) if "package:"+p in system), None)
    candidates = [p for p in (files, MANAGER) if p]
    # A privileged Files build may have no per-source switch or may bypass the unknown-source gate.
    for src in candidates:
        trial = folder/src
        trial.mkdir()
        try:
            if src == MANAGER:
                third_party(src, trial)
                adb("shell","appops","set","--uid",src,"MANAGE_EXTERNAL_STORAGE","allow")
            adb("shell","appops","set",src,"REQUEST_INSTALL_PACKAGES","allow")
            start("android.settings.MANAGE_UNKNOWN_APP_SOURCES","-d","package:"+src)
            row = wait(trial, lambda ns: switch_row(ns,"allow from this source"), "source-setting", 22)
            if row[1].get("enabled") != "true":
                raise RuntimeError("Источник не имеет редактируемого переключателя")
            if row[1].get("checked") == "true":
                tap(row[1])
            off = switch_condition("allow from this source",False)
            wait(trial,off,"source-off")
            appop = adb("shell","appops","get",src,"REQUEST_INSTALL_PACKAGES")
            (trial/"appop.txt").write_text(appop)
            if not re.search(r"REQUEST_INSTALL_PACKAGES:\s*(?:default|deny|ignore|errored)\b",appop,re.I) and "No operations" not in appop:
                raise RuntimeError("AppOp does not confirm disabled source: "+appop)
            label = "file manager" if src == MANAGER else "files"
            header_before = text_node(snapshot(trial),SETTINGS,lambda t:t == label) is not None
            evidence(trial,"2.1",off,r)
            home(trial)
            adb("logcat","-b","all","-c")
            if src == MANAGER:
                open_manager(trial)
            else:
                open_apk(trial,APK,src)
            def result(ns):
                warning = off_warning(ns)
                if warning is not None:
                    return "blocked",warning
                button = find(ns,INSTALLERS,{"install"})
                if button is not None:
                    return "prompt",button
                return None
            state, _ = wait(trial,result,"install-attempt",45)
            if state != "blocked":
                raise RuntimeError("Installer offered installation; this source may be privileged")
            evidence(trial,"2.2",off_warning,r)
            uid_info = adb("shell","pm","list","packages","-U",src)
            match = re.search(r"^package:"+re.escape(src)+r" uid:(\d+)$", uid_info, re.M)
            log = adb("logcat","-d","-v","threadtime")
            (trial/"attempt.log").write_text(log)
            starts = [line for line in log.splitlines() if "START u0" in line and "InstallStart" in line
                      and "package-archive" in line and match and re.search(r"from uid "+match[1]+r"\b",line)]
            if not starts:
                raise RuntimeError("Не подтверждён UID источника запуска Package Installer")
            # Follow the warning's own Settings button and verify where Android sent it.
            tap(wait(trial,lambda ns: find(ns,INSTALLERS,{"settings"}),"warning-settings"))
            wait(trial,off,"system-attributed-source")
            activity = adb("shell","dumpsys","activity","top")
            (trial/"attribution.txt").write_text(activity)
            if not re.search(r"dat=package:"+re.escape(src)+r"(?=[\s}])",activity):
                # Some dumps redact intent data. ActivityTaskManager logs it on other builds.
                launches = adb("logcat","-d","-v","brief")
                header_after = text_node(snapshot(trial),SETTINGS,lambda t:t == label) is not None
                if not (header_before and header_after) and not any("MANAGE_UNKNOWN_APP_SOURCES" in l and "dat=package:"+src in l for l in launches.splitlines()):
                    raise RuntimeError("Settings intent does not identify the warning's source")
            after = sample_state(folder,"after")
            if after["version"] is not None:
                raise Contrary("Пакет установлен при выключенном разрешении")
            r["technical"] = dict(source=src, caller=starts[-1], appop=appop, package_before=None, package_after=after)
            return
        except Contrary:
            raise
        except Exception as exc:
            diagnose(trial)
            (trial/"reason.txt").write_text(str(exc))
            if sample_state(folder,"after-attempt")["version"] is not None:
                raise Contrary("Проверяемый APK появился во время запрета") from exc
            if src == MANAGER:
                raise
            # Keep the incomplete primary attempt in diagnostics, exclude it from the result.
            r["images"] = []
            r["primary_source_limitation"] = str(exc)
            home(trial)


def swipe_up():
    size = adb("shell","wm","size")
    width,height = map(int,re.findall(r"(\d+)x(\d+)",size)[-1])
    adb("shell","input","swipe",str(width//2),str(height*4//5),str(width//2),str(height//3),"410")


def seek_setting(folder,title):
    wait(folder,lambda ns: next((n for n in ns if n.get("package") in SETTINGS),None),"settings-ready")
    for _ in range(12):
        nodes = snapshot(folder)
        row = switch_row(nodes,title)
        if row:
            return row
        swipe_up()
    raise TimeoutError("Не найден переключатель "+title)


def mic_notice(ns):
    owners = {"com.android.systemui", "com.android.permissioncontroller","com.google.android.permissioncontroller"}
    for package in owners:
        if find(ns,{package},{"unblock","turn on","allow access"}) is None:
            blocked = text_node(ns,{package},lambda s: s.rstrip(".!") == "microphone is blocked")
            if blocked is not None and find(ns,{package},{"ok","got it"}) is not None:
                return blocked
            continue
        label = text_node(ns,{package}, lambda s: "microphone" in s and
                          any(term in s for term in ("unblock","blocked","turn on","access","off")))
        if label is not None:
            return label
    return None


def recorder_screen(ns):
    permissions = {"com.android.permissioncontroller","com.google.android.permissioncontroller"}
    if text_node(ns,permissions,lambda s:"record audio" in s or "microphone" in s) is not None:
        choice = find(ns,permissions,{"while using the app","allow only while using the app"})
        if choice is not None:
            return "grant-mic",choice
    # The release uses its own storage explanation and directory picker, then Android SAF.
    explanation = text_node(ns,{RECORDER},lambda s:"confirm the folder" in s)
    picker = any(n.get("package") == RECORDER and "/filepicker_" in n.get("resource-id","") for n in ns)
    if explanation is not None or picker:
        ok = find(ns,{RECORDER},{"ok","select","choose"})
        if ok is not None:
            return "folder",ok
    confirm = text_node(ns,FILES,lambda s:"access" in s and ("files" in s or "folder" in s))
    allow = find(ns,FILES,{"allow"})
    if allow is not None and confirm is not None:
        return "authorize-folder",allow
    use = find(ns,FILES,{"use this folder"})
    if use is not None:
        return "use-folder",use
    if any(n.get("package") in FILES for n in ns):
        for name in ("music","recordings"):
            entry = find(ns,FILES,{name})
            if entry is not None:
                return "open-folder",entry
    button = visible_button(ns,RECORDER,"toggle_recording_button")
    return ("ready",button) if button is not None else None


def launch_recorder(folder):
    start("android.intent.action.MAIN","-c","android.intent.category.LAUNCHER","-p",RECORDER)
    for index in range(12):
        action,button = wait(folder,recorder_screen,f"recorder-{index}",38)
        if action == "ready":
            return button
        tap(button)
    raise RuntimeError("Не удалось завершить первоначальную настройку диктофона")


def recent_audio_op(value, elapsed):
    # cmd appops get prints access and rejection ages, for example 'rejectTime=+1s205ms ago'.
    for age in re.findall(r"\b(?:time|rejectTime)=\+?([0-9a-z]+) ago",value,re.I):
        parts = re.findall(r"(\d+)(ms|s|m|h|d)",age)
        if not parts or "".join(n+u for n,u in parts) != age:
            continue
        seconds = sum(int(n)*{"ms":.001,"s":1,"m":60,"h":3600,"d":86400}[u] for n,u in parts)
        if seconds <= elapsed + 0.15:
            return True
    return False


def microphone_case(folder,r):
    release = third_party(RECORDER,folder)
    permissions = adb("shell","dumpsys","package",RECORDER)
    for perm in ("POST_NOTIFICATIONS","READ_MEDIA_AUDIO"):
        if "android.permission."+perm in permissions:
            adb("shell","pm","grant",RECORDER,"android.permission."+perm)
    if "android.permission.MANAGE_EXTERNAL_STORAGE" in permissions:
        adb("shell","appops","set","--uid",RECORDER,"MANAGE_EXTERNAL_STORAGE","allow")
    adb("shell","mkdir","-p","/sdcard/Music/Recordings")
    launch_recorder(folder)
    grants = adb("shell","dumpsys","package",RECORDER)
    (folder/"permissions.txt").write_text(grants)
    if not re.search(r"android.permission.RECORD_AUDIO: granted=true",grants):
        raise RuntimeError("Runtime microphone permission was not granted")
    start("android.settings.PRIVACY_CONTROLS")
    _,control = seek_setting(folder,"microphone access")
    if control.get("checked") == "true":
        tap(control)
    off = switch_condition("microphone access",False)
    wait(folder,off,"global-off")
    (folder/"sensor-before.txt").write_text(adb("shell","dumpsys","sensor_privacy"))
    r["technical"] = dict(release=release, runtime_permission=True, microphone_access=False)
    # First return to the running app. If the reminder is missing, retry from a fresh foreground service.
    errors = []
    for attempt in (1,2):
        trial = folder/f"attempt-{attempt}"
        trial.mkdir()
        if attempt == 2:
            adb("shell","am","force-stop",RECORDER)
            home(trial)
        button = launch_recorder(trial)
        op_args = ("shell","cmd","appops","get","--user","0",RECORDER,"RECORD_AUDIO")
        before = adb(*op_args)
        (trial/"audio-before.txt").write_text(before)
        adb("logcat","-b","all","-c")
        started = time.monotonic()
        tap(button)
        try:
            evidence(trial,"3",mic_notice,r)
            audio = adb(*op_args)
            elapsed = time.monotonic()-started
            (trial/"audio-after.txt").write_text(audio)
            if not recent_audio_op(audio,elapsed):
                raise RuntimeError("Нет свежего AppOps-события RECORD_AUDIO диктофона")
            start("android.settings.PRIVACY_CONTROLS")
            seek_setting(folder,"microphone access")
            wait(folder,off,"global-still-off")
            r["technical"].update(audio_op=audio, seconds_since_tap=elapsed, microphone_still_disabled=True)
            return
        except Exception as exc:
            errors.append(str(exc))
            (trial/"audio-after.txt").write_text(adb(*op_args))
            diagnose(trial)
            if attempt == 1:
                # Captures with insufficient technical evidence remain diagnostic only.
                r["images"] = []
        finally:
            (trial/"audio-service.txt").write_text(adb("shell","dumpsys","audio"))
            (trial/"logcat.txt").write_text(adb("logcat","-d","-v","threadtime"))
    raise RuntimeError(" / ".join(errors))


def usb_case(folder,r):
    start("android.settings.APPLICATION_DEVELOPMENT_SETTINGS")
    _,control = seek_setting(folder,"usb debugging")
    value = adb("shell","settings","get","global","adb_enabled")
    if value not in {"0","1"} or (control.get("checked") == "true") != (value == "1"):
        raise RuntimeError("Положение переключателя расходится с adb_enabled")
    evidence(folder,"8.1",switch_condition("usb debugging",value=="1"),r)
    r["technical"] = dict(adb_enabled=int(value), rsa_tested=False,
                          optional_warning="Отладка включена. Канал управления оставлен активным.")
    if value == "0" and control.get("enabled") == "true":
        tap(control)
        try:
            evidence(folder,"8.2",lambda ns:text_node(ns,SETTINGS,lambda s:"allow usb debugging" in s),r)
            r["technical"]["optional_warning"] = "Системное предупреждение зафиксировано."
        except (RuntimeError,TimeoutError) as exc:
            r["technical"]["optional_warning"] = "Предупреждение не подтверждено: "+str(exc)
        finally:
            adb("shell","input","keyevent","BACK")
    elif value == "0":
        r["technical"]["optional_warning"] = "Переключатель выключен и недоступен."


def save_report(r):
    OUT.mkdir(exist_ok=True)
    (OUT/"report.json").write_text(json.dumps(r,ensure_ascii=False,indent=2)+"\n")
    lines = ["# "+TITLES[r["mechanism"]],"",f'**{r["status"]}**. '+r["reason"],""]
    for item in r["images"]:
        lines += [f'![{item["text"]}]({item["path"]})',"",item["text"],""]
    if r["technical"]:
        if r["mechanism"] == "usb":
            lines += [r["technical"]["optional_warning"], "", "RSA-авторизация компьютера не проверялась."]
        elif r["mechanism"] == "source":
            lines += ["Источник установки: `"+r["technical"]["source"]+"`."]
        else:
            lines += ["Версия диктофона: "+r["technical"]["release"]["version"]+"."]
    (OUT/"README.md").write_text("\n".join(lines)+"\n")


def execute(which):
    r = dict(mechanism=which,status="INCONCLUSIVE",images=[],technical={},reason="Опыт не завершён.",
             run_id=os.getenv("GITHUB_RUN_ID"),attempt=os.getenv("GITHUB_RUN_ATTEMPT"),commit=os.getenv("GITHUB_SHA"))
    save_report(r)
    folder = OUT/which
    folder.mkdir(exist_ok=True)
    try:
        ready_device()
        {"source":source_case,"microphone":microphone_case,"usb":usb_case}[which](folder,r)
        required = {"source":{"2.1","2.2"},"microphone":{"3"},"usb":{"8.1"}}[which]
        names = {Path(i["path"]).stem for i in r["images"] if (OUT/i["path"]).is_file()}
        if not required <= names or not r["technical"]:
            raise RuntimeError("Не выполнены обязательные проверки")
        r.update(status="PASS",reason={
            "source":"Android заблокировал установку из выбранного источника. Пакет остался неустановленным.",
            "microphone":"Android показал предупреждение при обращении диктофона к выключенному микрофону.",
            "usb":"Зафиксирован системный параметр USB debugging и проверено его состояние."
        }[which])
    except Exception as exc:
        r.update(status="FAIL" if isinstance(exc,Contrary) else "INCONCLUSIVE",reason=str(exc))
        diagnose(folder)
        for name,cmd in (("logcat",("logcat","-d","-v","threadtime")),
                         ("sensor",("shell","dumpsys","sensor_privacy"))):
            try:
                (folder/(name+".txt")).write_text(adb(*cmd))
            except Exception as error:
                (folder/(name+"-error.txt")).write_text(str(error))
    save_report(r)
    print(r["status"],r["reason"],flush=True)
    return int(r["status"]!="PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mechanism",choices=TITLES)
    args = parser.parse_args()
    raise SystemExit(execute(args.mechanism))
