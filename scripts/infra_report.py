"""Generate a missing CI report even if downloading Python dependencies failed."""
import json
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    folder = Path("results")
    folder.mkdir(exist_ok=True)
    file = folder/"report.json"
    if not file.exists():
        mechanism, reason = sys.argv[1:]
        file.write_text(json.dumps(dict(mechanism=mechanism,status="INCONCLUSIVE",reason=reason,images=[],
                                        technical={},run_id=os.getenv("GITHUB_RUN_ID")),ensure_ascii=False,indent=2))
        (folder/"README.md").write_text(f"# {mechanism}\n\n**INCONCLUSIVE**. {reason}\n")
