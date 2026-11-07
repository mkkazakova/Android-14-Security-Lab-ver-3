
"""Host-side regression checks. Fixtures never enter an experiment report."""

import io
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "scripts")
)

import gui
import run
from inspect_apks import CERT
from report import CASES, initial


def nodes(pkg, *labels):
    return [
        ET.Element(
            "node",
            package=pkg,
            text=s,
            enabled="true",
            bounds="[0,0][30,24]"
        )
        for s in labels
    ]


class EvidenceTests(unittest.TestCase):

    def test_system_success(self):
        ns = nodes(
            "com.android.packageinstaller",
            "Update Sample",
            "App installed.",
            "Done"
        )
        self.assertEqual(gui.outcome(ns)[0], "success")

    def test_system_failure(self):
        ns = nodes(
            "com.google.android.packageinstaller",
            "Update Sample",
            "App not installed as package conflicts with an existing package.",
            "Done"
        )
        self.assertEqual(gui.outcome(ns)[0], "failure")

    def test_result_without_title_or_done(self):
        self.assertEqual(
            gui.outcome(
                nodes(
                    "com.google.android.packageinstaller",
                    "App not installed."
                )
            )[0],
            "failure"
        )
        self.assertEqual(
            gui.outcome(
                nodes(
                    "com.android.packageinstaller",
                    "App installed."
                )
            )[0],
            "success"
        )

    def test_impostor_or_settings(self):
        for package in [
            "ru.securitylab.sample",
            "com.android.settings"
        ]:
            self.assertIsNone(
                gui.outcome(
                    nodes(
                        package,
                        "Update Sample",
                        "App not installed.",
                        "Done"
                    )
                )
            )

    def test_prompt_is_not_a_result(self):
        self.assertIsNone(
            gui.outcome(
                nodes(
                    "com.android.packageinstaller",
                    "Update Sample",
                    "Install",
                    "Cancel"
                )
            )
        )

    def test_correct_failure_attribution(self):
        self.assertTrue(
            run.mismatch(
                "W PackageManager: Existing package "
                "ru.securitylab.sample signatures do not match newer version"
            )
        )
        self.assertFalse(
            run.mismatch(
                "E PackageManager: INSTALL_FAILED_UPDATE_INCOMPATIBLE "
                "for another.package"
            )
        )
        self.assertFalse(
            run.mismatch(
                "E PackageManager: ru.securitylab.sample "
                "INSTALL_FAILED_VERSION_DOWNGRADE"
            )
        )

    def test_cert_formats(self):
        for prefix in [
            "Signer #1",
            "V2 Signer:",
            "V3.1 Signer:"
        ]:
            self.assertEqual(
                CERT.findall(
                    prefix
                    + " certificate SHA-256 digest: "
                    + "f" * 64
                ),
                ["f" * 64]
            )

    def test_hidden_or_corrupt_message(self):
        node, = nodes(
            "com.android.packageinstaller",
            "App installed."
        )

        stream = io.BytesIO()
        Image.new(
            "RGB",
            (120, 120),
            "black"
        ).save(stream, format="PNG")

        for data in [
            stream.getvalue(),
            stream.getvalue()[:16]
        ]:
            with self.assertRaises((ValueError, OSError)):
                gui.check_png(data, node)

    def test_no_image_no_pass_even_with_signature_proof(self):
        row = initial("")[1]

        inputs = {
            case[1]: {
                "apk_sha256": "test-" + case[1]
            }
            for case in CASES
        }

        state = {
            "version": 1,
            "sha256": inputs[CASES[0][1]]["apk_sha256"]
        }

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(run, "OUT", Path(tmp)), \
                patch.object(
                    run,
                    "installed",
                    return_value=state
                ), \
                patch.object(run, "diagnose"), \
                patch.object(
                    run,
                    "wait",
                    return_value=True
                ), \
                patch.object(
                    run,
                    "install",
                    side_effect=TimeoutError("no UI")
                ), \
                patch.object(
                    run,
                    "adb",
                    return_value=(
                        "W PackageManager: "
                        "ru.securitylab.sample signatures do not match"
                    )
                ):

            run.stage(
                row,
                CASES[1],
                inputs,
                "com.android.documentsui",
                set()
            )

        self.assertTrue(row["technical_verified"])
        self.assertIsNone(row["screenshot"])
        self.assertEqual(row["status"], "INCONCLUSIVE")


if __name__ == "__main__":
    unittest.main()
