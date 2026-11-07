
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
                patch.object(m, "seek_setting", return_value=(node("com.android.settings"), node("com.android.settings", checked="true"))), \
                patch.object(m, "evidence"):
            m.usb_case(Path(tmp), {"technical": {}, "images": []})

        self.assertLess(
            calls.index(("shell", "settings", "put", "global", "development_settings_enabled", "1")),
            calls.index(("start", "android.settings.APPLICATION_DEVELOPMENT_SETTINGS")),
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
