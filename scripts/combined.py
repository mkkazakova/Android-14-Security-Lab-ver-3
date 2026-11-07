"""Build the illustrated report for exactly the jobs requested in one dispatch."""
import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path

MECHANISMS = ("signature", "source", "microphone", "usb")


def assemble(collected, out, selected, run_id):
    out.mkdir(exist_ok=True, parents=True)
    reports = {}
    illustrated = {}
    seen = set()
    for name in selected:
        files = list(collected.glob(f"android14-{name}-{run_id}-*/report.json"))
        try:
            # GitHub may re-run only failed jobs. Select the latest available attempt per job.
            path = max(files,key=lambda f:int(f.parent.name.rsplit("-",1)[1]))
            r = json.loads(path.read_text())
            if str(r.get("run_id")) != run_id:
                raise ValueError("Чужой идентификатор запуска")
            reports[name] = r
            dest = out/name
            dest.mkdir(exist_ok=True)
            shutil.copy(path,dest)
            if name == "signature":
                items = [(v["screenshot"],v["png_sha256"],v["id"]) for v in r["experiments"] if v.get("screenshot")]
            else:
                items = [(v["path"],v["sha256"],v["text"]) for v in r["images"]]
            lines = []
            for rel,digest,label in items:
                source = (path.parent/rel).resolve()
                if not source.is_relative_to(path.parent.resolve()) or source.suffix != ".png":
                    raise ValueError("Некорректный путь снимка")
                data = source.read_bytes()
                if hashlib.sha256(data).hexdigest() != digest or digest in seen:
                    raise ValueError("Неверная контрольная сумма или повторный снимок")
                seen.add(digest)
                target = dest/rel
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(data)
                lines.append(f"![{label}]({name}/{rel})")
            illustrated[name] = lines
            if r["status"] == "PASS" and len(lines) < {"signature":3,"source":2,"microphone":1,"usb":1}[name]:
                raise ValueError("Не хватает обязательных изображений")
        except (OSError,ValueError,KeyError,TypeError) as exc:
            reports[name] = dict(status="INCONCLUSIVE",reason=f"Невозможно собрать доказательства: {exc}")
            illustrated[name] = []
    s = Path("README.md").read_text().replace("[Инструкция по запуску](docs/RUN.md)","")
    if "signature" in reports:
        sig = reports["signature"]
        start = s.index("### 1.1.")
        end = s.index("## 2.")
        body = [f'Текущий запуск `{run_id}`: **{sig["status"]}**.',sig.get("reason","")]
        for row in sig.get("experiments",[]):
            body += [f'### {row["id"]}. {row["expected"]}']
            if row.get("screenshot"):
                body += [f'![Системное окно {row["id"]}](signature/{row["screenshot"]})']
            body += [f'**{row["status"]}**. ' + ((row.get("installer") or {}).get("message","") + " " +
                     (row.get("technical") or "") + " " + (row.get("reason") or "")).strip()]
        s = s[:start]+"\n\n".join(body)+"\n\n"+s[end:]
    else:
        shutil.copytree("evidence",out/"evidence",dirs_exist_ok=True)
    for name in MECHANISMS[1:]:
        if name not in reports:
            lines = ["Этот механизм не выбран для данного запуска."]
        else:
            r = reports[name]
            lines = illustrated[name] + [f'**{r["status"]}**. {r["reason"]}']
            if r.get("technical",{}).get("source"):
                lines.append("Проверенный источник: `"+r["technical"]["source"]+"`.")
            if name == "usb":
                lines.append("Проверка ограничена настройкой эмулятора. RSA-авторизация не исследовалась.")
        s = re.sub(r"^\*\*Результат ожидает запуска\.\*\*[^\n]*",lambda _: "\n\n".join(lines),s,count=1,flags=re.M)
    s = s[:s.index("## Заключение")] + "## Заключение\n\n" + "\n\n".join(
        f'- {name} — **{r["status"]}**.' for name,r in reports.items()) + "\n"
    (out/"README.md").write_text(s)
    statuses = {k:v["status"] for k,v in reports.items()}
    (out/"summary.json").write_text(json.dumps(dict(run_id=run_id,results=statuses),indent=2))
    return all(v=="PASS" for v in statuses.values())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("selection",choices=("all",*MECHANISMS))
    p.add_argument("run_id")
    a = p.parse_args()
    ok = assemble(Path("collected"),Path("combined"),MECHANISMS if a.selection=="all" else [a.selection],a.run_id)
    raise SystemExit(int(not ok))
