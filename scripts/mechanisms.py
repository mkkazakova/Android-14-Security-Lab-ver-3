
def evidence(folder, name, match, report):
    errors = []

    for attempt in range(3):
        try:
            before = wait(
                folder, match, name + "-before", 12
            )

            data, origin = png_bytes(attempt)
            check_png(data, before)

            after = wait(
                folder, match, name + "-after", 12
            )

            if any(
                before.get(k) != after.get(k)
                for k in (
                    "package",
                    "text",
                    "bounds",
                    "resource-id",
                )
            ):
                raise ValueError(
                    "System element changed during capture"
                )

            digest = hashlib.sha256(data).hexdigest()

            if any(
                item["sha256"] == digest
                for item in report["images"]
            ):
                raise ValueError("Duplicate PNG")

            path = folder / (name + ".png")
            path.write_bytes(data)

            report["images"].append({
                "path": str(path.relative_to(OUT)),
                "sha256": digest,
                "package": before.get("package"),
                "text": before.get("text"),
                "source": origin,
            })

            return

        except (
            OSError,
            RuntimeError,
            ValueError,
            subprocess.SubprocessError,
        ) as exc:
            errors.append(str(exc))

    raise RuntimeError(
        f"{name}: " + " / ".join(errors)
    )
