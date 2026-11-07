"""Verify the actual APKs with SDK Build Tools before any UI interaction."""
import hashlib
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path

PACKAGE = "ru.securitylab.sample"
CERT = re.compile(r"^(?:Signer(?:\s+#\d+|\s+\([^)]*\))|V\d+(?:\.\d+)?\s+Signer:?)\s+certificate SHA-256 digest:\s*([0-9a-f]{64})\s*$", re.I | re.M)
INPUTS = Path("apks")
OUT = Path("results")


def verify():
    pinned = json.loads((INPUTS / "inputs.json").read_text())
    sdk = Path(os.environ["ANDROID_HOME"]) / "build-tools/35.0.0"
    records = {}
    dest = OUT / "inputs"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("sample-v1-a.apk", "sample-v2-b.apk", "sample-v2-a.apk"):
        apk = INPUTS / name
        def run(tool, *args):
            p = subprocess.run([sdk / tool, *args, apk], capture_output=True, text=True)
            (dest / f"{name}.{tool}.txt").write_text(p.stdout + p.stderr)
            p.check_returncode()
            return p.stdout
        certs = set(s.lower() for s in CERT.findall(run("apksigner", "verify", "--verbose", "--print-certs")))
        info = run("aapt", "dump", "badging")
        match = re.search(r"^package: name='([^']+)' versionCode='(\d+)'", info, re.M)
        if len(certs) != 1 or not match or any(f"{k}:'34'" not in info for k in ("sdkVersion", "targetSdkVersion")):
            raise ValueError(f"Unexpected manifest or signing output: {name}")
        records[name] = dict(package=match[1], version=int(match[2]), certificate_sha256=certs.pop(),
                             apk_sha256=hashlib.sha256(apk.read_bytes()).hexdigest())
    if records != pinned:
        raise ValueError("APK identity differs from inputs.json")
    original, conflict, update = records.values()
    if ([r["version"] for r in records.values()] != [1, 2, 2]
            or any(r["package"] != PACKAGE for r in records.values())
            or original["certificate_sha256"] != update["certificate_sha256"]
            or original["certificate_sha256"] == conflict["certificate_sha256"]):
        raise ValueError("Invalid experimental controls")
    with zipfile.ZipFile(INPUTS / "sample-v2-a.apk") as a, zipfile.ZipFile(INPUTS / "sample-v2-b.apk") as b:
        if set(a.namelist()) != set(b.namelist()) or any(a.read(n) != b.read(n) for n in a.namelist()):
            raise ValueError("Version 2 controls have different application contents")
    (OUT / "verified.json").write_text(json.dumps(records, indent=2))


if __name__ == "__main__":
    verify()
