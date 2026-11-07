"""Write JSON plus a portable Russian report containing only observed images."""
import json
import os
import sys
from pathlib import Path

OUT = Path("results")
CASES = (
    ("1.1", "sample-v1-a.apk", "Установка версии 1 (A)", "Пакет отсутствует. Открыть первую версию и подтвердить установку.", None, 1),
    ("1.2", "sample-v2-b.apk", "Отклонение версии 2 (B)", "Поверх версии 1 предложить версию 2 с независимой подписью. Ожидается отказ с сохранением версии 1.", 1, 1),
    ("1.3", "sample-v2-a.apk", "Обновление до версии 2 (A)", "Поверх версии 1 предложить версию 2 с исходной подписью. Ожидается успешное обновление.", 1, 2),
)


def initial(reason):
    return [dict(id=id_, apk=apk, expected=expected, status="INCONCLUSIVE", reason=reason,
                 technical=None, installer=None, screenshot=None, png_valid=False)
            for id_, apk, _, expected, _, _ in CASES]


def save(rows):
    OUT.mkdir(exist_ok=True)
    status = ("FAIL" if any(r["status"] == "FAIL" for r in rows) else
              "PASS" if all(r["status"] == "PASS" for r in rows) else "INCONCLUSIVE")
    report = dict(status=status, run_id=os.getenv("GITHUB_RUN_ID"), attempt=os.getenv("GITHUB_RUN_ATTEMPT"),
                  commit=os.getenv("GITHUB_SHA"), experiments=rows)
    tmp = OUT / "report.tmp"
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    tmp.replace(OUT / "report.json")
    text = ["# Проверка цифровой подписи APK", ""]
    for row, case in zip(rows, CASES):
        text += ["## " + row["id"] + " — " + case[2], "", row["expected"], ""]
        if row["screenshot"]:
            text += [f'![Окно Android {row["id"]}]({row["screenshot"]})', ""]
        else:
            text += ["Проверенный снимок отсутствует.", ""]
        text += [f'**{row["status"]}**. ' + (row["reason"] or "Все проверки выполнены."), "",
                 "Сообщение Android: " + str((row["installer"] or {}).get("message", "не зафиксировано")), "",
                 "Проверка пакета: " + str(row["technical"] or "нет данных"), ""]
        if row.get("input"):
            text += ["Сертификат предложенного APK (SHA-256): `" + row["input"]["certificate_sha256"] + "`.", ""]
        if row.get("signature_error"):
            text += ["Запись системы, относящаяся к этому пакету:", "", "```text", row["signature_error"], "```", ""]
    text += ["## Общий вывод", "", f"Итог: **{status}**.", ""]
    if status == "PASS":
        text.append("Android принял исходный APK и обновление с сертификатом A. Обновление с сертификатом B отклонено; установленная версия 1 сохранилась до установки версии 2 с A.")
    else:
        text.append("Полная последовательность опытов не подтверждена. Причины указаны выше.")
    (OUT / "README.md").write_text("\n".join(text) + "\n")


if __name__ == "__main__":
    if not (OUT / "report.json").exists():
        save(initial("Сценарий не начат. " + " ".join(sys.argv[1:])))
