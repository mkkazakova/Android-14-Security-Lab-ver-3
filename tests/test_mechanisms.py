
"""Replay the baseline and exercise the pass/fail boundary without an emulator."""
import hashlib
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import gui
import mechanisms as m
import run


def node(owner,text="",**kw):
    return ET.Element("node",package=owner,text=text,enabled="true",bounds="[0,0][50,30]",**kw)


class MechanismTests(unittest.TestCase):
    def test_storage_retry_checks_hash(self):
        digest = hashlib.sha256(b"fixture-apk").hexdigest()
        counter = {"push": 0}

        def fake_adb(*args, **kwargs):
            if args[0] == "push":
                counter["push"] += 1
                if counter["push"] == 1:
                    raise RuntimeError("remote couldn't create file: Operation not permitted")
            if args[:2] == ("shell", "sha256sum"):
                return digest + "  /sdcard/Download/sample.apk"
            return ""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apk = root / "sample.apk"
            apk.write_bytes(b"fixture-apk")
            with patch.object(run, "OUT", root), patch.object(run, "adb", side_effect=fake_adb):
                remote = run.push_download_apk(apk, attempts=2, pause=0)

        self.assertEqual(remote, "/sdcard/Download/sample.apk")
        self.assertEqual(counter["push"], 2)

    def test_recorder_uses_resolved_component(self):
        calls = []

        def fake_adb(*args, **kwargs):
            calls.append(args)
            if "resolve-activity" in args:
                return "priority=0\norg.fossify.voicerecorder/.activities.MainActivity"
            return "Starting: Intent"

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(m, "adb", side_effect=fake_adb), \
                patch.object(m, "wait", return_value=("ready", node(m.RECORDER))):
            m.launch_recorder(Path(tmp))

        self.assertTrue(any(args[:4] == ("shell", "am", "start", "-n") for args in calls))
        self.assertFalse(any("monkey" in args for args in calls))

    def test_usb_enables_developer_options_before_screen(self):
        calls = []

        def fake_adb(*args, **kwargs):
            calls.append(args)
            if args == ("shell", "settings", "get", "global", "development_settings_enabled"):
                return "1"
            if args == ("shell", "settings", "get", "global", "adb_enabled"):
                return "1"
            return ""

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(m, "adb", side_effect=fake_adb), \
                patch.object(m, "start", side_effect=lambda *a: calls.append(("start", *a))), \
                patch.object(m, "find_settings_label", return_value=node("com.android.settings", "Build number")), \
                patch.object(m, "tap", side_effect=lambda item: calls.append(("tap", item.get("text")))), \
                patch.object(m.time, "sleep"), \
                patch.object(m, "seek_setting", return_value=(node("com.android.settings"), node("com.android.settings", checked="true"))), \
                patch.object(m, "evidence"):
            m.usb_case(Path(tmp), {"technical": {}, "images": []})

        self.assertEqual(calls.count(("tap", "Build number")), 7)
        self.assertLess(
            calls.index(("start", "android.settings.DEVICE_INFO_SETTINGS")),
            calls.index(("start", "android.settings.APPLICATION_DEVELOPMENT_SETTINGS")),
        )
        self.assertFalse(any("pm enable" in " ".join(map(str, args)) for args in calls))

    def test_usb_refuses_unactivated_developer_options(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(m, "adb", return_value="0"), \
                patch.object(m, "start"), \
                patch.object(m, "find_settings_label", return_value=node("com.android.settings", "Build number")), \
                patch.object(m, "tap"), \
                patch.object(m.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "не активированы"):
                m.activate_developer_options(Path(tmp))

    def test_audio_policy_silencing_proves_current_package(self):
        line = ("11-07 20:23:34.368 I AudioPolicyService: App op 27 missing, "
                "silencing record AttributionSourceState{pid: 5553, uid: 10192, "
                "packageName: org.fossify.voicerecorder, attributionTag: (null)}")
        self.assertEqual(m.audio_policy_silencing(line), line)
        self.assertIsNone(m.audio_policy_silencing(line.replace("org.fossify.voicerecorder", "other.app")))
        self.assertIsNone(m.audio_policy_silencing(line.replace("silencing record", "starting record")))

    def test_settings_label_retries_after_transient_xml_failure(self):
        label = node("com.android.settings", "Build number")
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(m, "wait", side_effect=[TimeoutError("malformed XML"), label]) as checking, \
                patch.object(m, "swipe_up") as scrolling, \
                patch.object(m.time, "sleep"):
            result = m.find_settings_label(Path(tmp), "Build number", max_swipes=3)
        self.assertIs(result, label)
        self.assertEqual(checking.call_count, 2)
        scrolling.assert_called_once_with()

    def test_microphone_cancels_system_dialog_before_rechecking_privacy(self):
        calls = []
        policy = (
            "I AudioPolicyService: silencing record "
            "AttributionSourceState{packageName: org.fossify.voicerecorder, uid: 10050}"
        )

        def fake_adb(*args, **kwargs):
            if args[:3] == ("shell", "dumpsys", "package"):
                return "android.permission.RECORD_AUDIO: granted=true"
            if args[:4] == ("logcat", "-b", "all", "-d"):
                return policy
            if "appops" in args:
                return "RECORD_AUDIO: allow"
            return ""

        def fake_wait(folder, cond, name, *args, **kwargs):
            if name == "microphone-warning-cancel":
                return node("com.android.systemui", "Cancel")
            return True

        def fake_tap(item):
            calls.append(("tap", item.get("text")))

        def fake_start(action, *args):
            calls.append(("start", action))

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(m, "adb", side_effect=fake_adb), \
                patch.object(m, "start", side_effect=fake_start), \
                patch.object(m, "third_party", return_value={"version": "fixture"}), \
                patch.object(m, "launch_recorder", return_value=node(m.RECORDER, "Record")), \
                patch.object(m, "seek_setting", return_value=(node("com.android.settings"), node("com.android.settings", checked="false"))), \
                patch.object(m, "wait", side_effect=fake_wait), \
                patch.object(m, "evidence"), \
                patch.object(m, "tap", side_effect=fake_tap), \
                patch.object(m, "diagnose"):
            folder = Path(tmp) / "microphone"
            folder.mkdir()
            report = {"technical": {}, "images": []}
            m.microphone_case(folder, report)

        self.assertTrue(report["technical"]["microphone_still_disabled"])
        self.assertIn(("tap", "Cancel"), calls)
        self.assertNotIn(("tap", "Unblock"), calls)
        self.assertLess(
            calls.index(("tap", "Cancel")),
            len(calls) - 1 - calls[::-1].index(("start", "android.settings.PRIVACY_CONTROLS")),
        )

    def test_real_baseline(self):
        root = ROOT/"evidence/42400869470-1"
        report = json.loads((root/"report.json").read_text())
        digests = set()
        for r in report["experiments"]:
            nodes = list(ET.parse(root/r["id"]/"terminal.xml").iter("node"))
            state,item = gui.outcome(nodes)
            self.assertEqual((state,item.get("text")),(r["installer"]["state"],r["installer"]["message"]))
            data = (root/r["screenshot"]).read_bytes()
            gui.check_png(data,item)
            digest = hashlib.sha256(data).hexdigest()
            self.assertEqual(digest,r["png_sha256"])
            digests.add(digest)
        self.assertEqual(len(digests),3)

    def test_switch_belongs_to_preference(self):
        owner = "com.android.settings"
        root = node(owner)
        for title,value in (("camera access","true"),("microphone access","false")):
            row = node(owner)
            row.extend([node(owner,title),node(owner,checkable="true",checked=value)])
            root.append(row)
        ns = list(root.iter())
        self.assertIsNotNone(m.switch_condition("microphone access",False)(ns))
        self.assertIsNone(m.switch_condition("microphone access",True)(ns))

    def test_google_privacy_controls_switch(self):
        owner = "com.google.android.permissioncontroller"
        root = node(owner)
        camera = node(owner)
        camera.extend([node(owner,"Camera access"), node(owner,checkable="true",checked="true")])
        microphone = node(owner)
        microphone.extend([node(owner,"Microphone access"), node(owner,checkable="true",checked="true")])
        root.extend([camera,microphone])
        ns = list(root.iter())
        row = m.switch_row(ns,"microphone access",m.PRIVACY_SETTINGS)
        self.assertIsNotNone(row)
        self.assertEqual(row[1].get("checked"),"true")
        self.assertIsNone(m.switch_row(ns,"microphone access"))

    def test_unknown_sources_not_signature_failure(self):
        owner = "com.google.android.packageinstaller"
        self.assertIsNone(m.off_warning([node(owner,"App not installed as package conflicts with an existing package.")]))
        warning = [node(owner,"For your security, this source isn't allowed to install unknown apps."),node(owner,"Settings")]
        self.assertIsNotNone(m.off_warning(warning))
        self.assertIsNone(m.off_warning([node("org.fossify.filemanager",n.get("text")) for n in warning]))

    def test_mic_requires_system_owner_and_unblock_action(self):
        owner = "com.android.systemui"
        self.assertIsNotNone(m.mic_notice([node(owner,"Unblock device microphone?"),node(owner,"Unblock")]))
        self.assertIsNone(m.mic_notice([node(m.RECORDER,"Unblock device microphone?"),node(m.RECORDER,"Unblock")]))
        self.assertIsNone(m.mic_notice([node(owner,"Microphone access")]))

    def test_runtime_permission_and_storage_picker(self):
        owner = "com.google.android.permissioncontroller"
        grant = m.recorder_screen([node(owner,"Allow Voice Recorder to record audio?"),node(owner,"While using the app")])
        self.assertEqual(grant[0],"grant-mic")
        choice = m.recorder_screen([node(m.RECORDER,"You must confirm the folder for recordings."),node(m.RECORDER,"OK")])
        self.assertEqual(choice[0],"folder")
        self.assertIsNone(m.recorder_screen([node(m.RECORDER,"Allow")]))

    def test_fresh_appop(self):
        self.assertTrue(m.recent_audio_op("RECORD_AUDIO: allow; rejectTime=+2s102ms ago",3))
        self.assertFalse(m.recent_audio_op("RECORD_AUDIO: allow; time=+1m2s ago",3))
        self.assertFalse(m.recent_audio_op("RECORD_AUDIO: allow",3))

    def test_gui_timeout_cannot_pass(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m,"OUT",Path(tmp)), patch.object(m,"ready_device"), \
                patch.object(m,"source_case",side_effect=TimeoutError("no warning")), patch.object(m,"diagnose"), patch.object(m,"adb",return_value=""):
            self.assertEqual(m.execute("source"),1)
            data = json.loads((Path(tmp)/"report.json").read_text())
            self.assertEqual((data["status"],data["images"]),("INCONCLUSIVE",[]))

    def test_missing_second_image_cannot_pass(self):
        def partial(folder,report):
            (folder/"2.1.png").touch()
            report.update(images=[dict(path="source/2.1.png",text="test")],technical={"source":"example"})
        with tempfile.TemporaryDirectory() as tmp, patch.object(m,"OUT",Path(tmp)), patch.object(m,"ready_device"), \
                patch.object(m,"source_case",side_effect=partial), patch.object(m,"diagnose"), patch.object(m,"adb",return_value=""):
            self.assertEqual(m.execute("source"),1)
            self.assertEqual(json.loads((Path(tmp)/"report.json").read_text())["status"],"INCONCLUSIVE")

    def test_screen_change_rejected(self):
        states = [node("com.android.settings",s) for s in ("Before","After")]*3
        with tempfile.TemporaryDirectory() as tmp, patch.object(m,"wait",side_effect=states), \
                patch.object(m,"png_bytes",return_value=(b"x","test")), patch.object(m,"check_png"):
            with self.assertRaisesRegex(RuntimeError,"changed"):
                m.evidence(Path(tmp),"8.1",None,{"images":[]})
            self.assertFalse((Path(tmp)/"8.1.png").exists())


class ReportTests(unittest.TestCase):
    def test_incomplete_or_foreign_attempt_does_not_pass(self):
        from combined import assemble
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            collected = root/"collected"
            path = collected/"android14-usb-123-1"
            path.mkdir(parents=True)
            (path/"report.json").write_text(json.dumps(dict(run_id="123",status="PASS",images=[],reason="",technical={})))
            self.assertFalse(assemble(collected,root/"out",["usb"],"123"))
            out = json.loads((root/"out/summary.json").read_text())
            self.assertEqual(out["results"]["usb"],"INCONCLUSIVE")

    def test_baseline_report_can_be_rebuilt(self):
        import shutil
        from combined import assemble
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = "42400869470"
            dst = root/"collected"/f"android14-signature-{run_id}-1"
            shutil.copytree(ROOT/"evidence/42400869470-1",dst)
            self.assertTrue(assemble(root/"collected",root/"out",["signature"],run_id))
            self.assertEqual(len(list((root/"out/signature").rglob("*.png"))),3)

    def test_invalid_package_query_is_not_absence(self):
        with patch.object(m,"adb",return_value="Error: package service unavailable"):
            with self.assertRaises(RuntimeError):
                m.sample_state(Path("."),"after")
