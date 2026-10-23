"""Draw every scenario of the Modlist Companion window at every theme and size, then check them.

    python tests/gallery/render.py --out <folder> [--scenarios typical,huge] [--themes light,dark]
    python tests/gallery/render.py --out <folder> --sheets-only     # rebuild the sheets and checks.txt from results.json

Output in <folder>:
    png/<scenario>__<theme>__<size>.png   every picture
    contact_sheet.png                     all scenarios x themes at the wide size, labelled
    contact_<scenario>.png                one scenario: themes across, sizes down, larger
    checks.txt                            the same checks the tests run, timings, and the defects found
    results.json                          the raw findings behind checks.txt

Needs MO2's bundled PyQt6 (set FOMOD_LOGGER_MO2_ROOT, or keep the project's test MO2 beside
the source). No window is shown; pictures come from QWidget.grab().

Scales 1.25 and 1.5 are drawn by a separate process started with QT_SCALE_FACTOR set, because
Qt reads it only at start-up; each process draws its own sizes and the results are merged here.
The databases are built in <folder>/_db and deleted at the end.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/, so `gallery` imports

from gallery import checks, qtenv, scenarios  # noqa: E402


# ---------- worker: one process, one scale ----------

def _worker(args: argparse.Namespace) -> int:
    window_module = qtenv.load()
    if window_module is None:
        print("PyQt6 from MO2 isn't available (set FOMOD_LOGGER_MO2_ROOT)", file=sys.stderr)
        return 2
    env = checks.Env(window_module)
    out = Path(args.out)
    (out / "png").mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    timings: dict[str, list[float]] = {}
    sizes = [size for size in checks.SIZES if size[3] == args.scale]
    for name in args.scenarios:
        scenario = scenarios.build(name)
        database = Path(args.db) / name / "logger.sqlite3"
        for theme in args.themes:
            for size_name, width, height, _scale in sizes:
                window = env.open(database, scenario, theme, width, height)
                target = out / "png" / f"{name}__{theme}__{size_name}.png"
                pixmap = window.grab()
                pixmap.save(str(target))
                results = checks.run_all(env, window, scenario)
                findings = [f for kind in results.values() for f in kind] + checks.size_note(window, width, height)
                records.append({
                    "scenario": name, "theme": theme, "size": size_name, "scale": args.scale,
                    "requested": [width, height], "actual": [window.width(), window.height()],
                    "png": f"png/{target.name}", "png_size": [pixmap.width(), pixmap.height()],
                    "summary": checks.summarize(results), "findings": [asdict(f) for f in findings],
                })
                window.deleteLater()
                env.app.processEvents()
            if scenario.compare:
                # The Compare choices dialog in each of its modes, at the first size of this scale.
                window = env.open(database, scenario, theme, sizes[0][1], sizes[0][2])
                for spec in scenario.compare:
                    dialog = checks.open_compare(env, window, scenario, spec)
                    label = f"{spec['kind']}-dialog"
                    target = out / "png" / f"{name}__{theme}__{label}.png"
                    pixmap = dialog.grab()
                    pixmap.save(str(target))
                    results = checks.compare_all(env, dialog, spec)
                    records.append({
                        "scenario": name, "theme": theme, "size": label, "scale": args.scale,
                        "requested": list(checks.COMPARE_SIZE), "actual": [dialog.width(), dialog.height()],
                        "png": f"png/{target.name}", "png_size": [pixmap.width(), pixmap.height()],
                        "summary": checks.summarize(results),
                        "findings": [asdict(f) for kind in results.values() for f in kind],
                    })
                    dialog.close()
                window.deleteLater()
                env.app.processEvents()
        if args.scale == 1.0 and name in ("huge", "plain_many", "history_huge"):
            first, repeat = checks.timings(env, database, scenario)
            timings[name] = [first, repeat]
    (out / f"_results_{args.scale}.json").write_text(json.dumps({"records": records, "timings": timings}), encoding="utf-8")
    sys.stdout.flush()
    sys.stderr.flush()
    # Qt sometimes crashes while a process with many deleted windows shuts down; the results are already written.
    os._exit(0)


# ---------- contact sheets ----------

def _sheet(env, cells: list[dict], columns: int, thumb: int, target: Path, title: str) -> None:
    """cells: {"label", "png", "fails"} laid out row by row, each scaled to ``thumb`` px wide."""

    Gui, Qt = env.Gui, env.Qt
    margin, label_h, title_h = 12, 24, 36
    pictures = []
    for cell in cells:
        image = Gui.QImage(cell["png"])
        pictures.append(image.scaledToWidth(thumb, Qt.Qt.TransformationMode.SmoothTransformation)
                        if not image.isNull() else Gui.QImage(thumb, 60, Gui.QImage.Format.Format_RGB32))
    rows = [pictures[i:i + columns] for i in range(0, len(pictures), columns)]
    heights = [max(p.height() for p in row) + label_h + margin for row in rows]
    canvas = Gui.QImage(columns * (thumb + margin) + margin, title_h + sum(heights) + margin, Gui.QImage.Format.Format_RGB32)
    canvas.fill(Gui.QColor("#16181c"))
    painter = Gui.QPainter(canvas)
    painter.setRenderHint(Gui.QPainter.RenderHint.TextAntialiasing)
    font = Gui.QFont("Segoe UI", 11)
    painter.setFont(font)
    painter.setPen(Gui.QColor("#ffffff"))
    painter.drawText(margin, 24, title)
    y = title_h
    for r, row in enumerate(rows):
        for c, picture in enumerate(row):
            cell = cells[r * columns + c]
            x = margin + c * (thumb + margin)
            painter.setPen(Gui.QColor("#ff8a80" if cell["fails"] else "#9be29b"))
            painter.drawText(x, y + 17, cell["label"] + (f"  [{cell['fails']} defects]" if cell["fails"] else "  [ok]"))
            painter.drawImage(x, y + label_h, picture)
        y += heights[r]
    painter.end()
    canvas.save(str(target))


def _distinct(record: dict) -> int:
    """How many different defects a picture has (the same defect on many widgets counts once)."""

    return len({checks.Finding(**f).group_key() for f in record["findings"] if f["severity"] == "FAIL"})


def _contact_sheets(env, records: list[dict], out: Path) -> None:
    by = {(r["scenario"], r["theme"], r["size"]): r for r in records}
    names = list(dict.fromkeys(r["scenario"] for r in records))
    themes = list(dict.fromkeys(r["theme"] for r in records))
    def cell(record: dict, label: str) -> dict:
        return {"label": label, "png": str(out / record["png"]), "fails": _distinct(record)}

    overview = [cell(by[(n, t, "wide")], f"{n} / {t}") for n in names for t in themes if (n, t, "wide") in by]
    _sheet(env, overview, len(themes), 640, out / "contact_sheet.png",
           "Modlist Companion scenario gallery: wide window (1500x760), every scenario and theme. "
           "Per-scenario sheets show every size.")
    for n in names:
        cells = []
        for size in (s[0] for s in checks.SIZES):
            for t in themes:
                if (n, t, size) in by:
                    r = by[(n, t, size)]
                    cells.append(cell(r, f"{t} / {size} {r['actual'][0]}x{r['actual'][1]}"))
        _sheet(env, cells, len(themes), 560, out / f"contact_{n}.png", f"{n}: themes across, sizes down")


# ---------- checks.txt ----------

def _checks_text(records: list[dict], timings: dict[str, list[float]], qt_versions: str) -> str:
    lines = ["Modlist Companion scenario gallery: checks",
             f"generated {time.strftime('%Y-%m-%d %H:%M:%S')}  |  {qt_versions}  |  style Fusion",
             "sizes: " + ", ".join(f"{n} {w}x{h} @{s}" for n, w, h, s in checks.SIZES),
             "scaling: one process per scale with QT_SCALE_FACTOR (1.25 and 1.5); pictures are in physical pixels.",
             "themes: auto = the app's light Fusion palette, light, dark, and auto-dark = a dark Fusion palette "
             "(what a dark MO2 theme gives the window).",
             "contrast: text colour (the stylesheet's, or the rendered one for buttons) against the colour actually "
             "drawn behind it; 4.5:1, or 3:1 for 18pt+ or bold 14pt+. Scenarios with hundreds of look-alike widgets "
             f"are sampled ({checks.SAMPLE_PER_STYLE} per style).", ""]
    lines.append("== Speed (huge, plain_many, history_huge) ==")
    for name, (first, repeat) in timings.items():
        lines.append(f"{name}: first refresh {first:.2f}s (limit 1.5s) {'OK' if first < 1.5 else 'MISS'}; "
                     f"repeat refresh {repeat:.2f}s (limit 0.5s) {'OK' if repeat < 0.5 else 'MISS'}")
    lines += ["", "== Every picture =="]
    for r in records:
        fails = _distinct(r)
        lines.append(f"{r['scenario']:15} {r['theme']:9} {r['size']:10} x{r['scale']:<4} "
                     f"{r['actual'][0]}x{r['actual'][1]} (asked {r['requested'][0]}x{r['requested'][1]})  "
                     f"{'ok' if not fails else str(fails) + ' distinct defects'}  [{r['summary']}]")
    # defects, merged across pictures
    merged: dict[tuple, dict] = {}
    for r in records:
        for raw in r["findings"]:
            finding = checks.Finding(**raw)
            entry = merged.setdefault(finding.group_key(), {"example": finding, "runs": [], "ratios": []})
            entry["runs"].append((r["scenario"], r["theme"], r["size"]))
            entry["ratios"].append(finding.ratio())
            if finding.kind == "contrast" and finding.ratio() < entry["example"].ratio():
                entry["example"] = finding  # show the worst measurement
    for severity, title in (("FAIL", "Defects"), ("NOTE", "Notes")):
        lines += ["", f"== {title} (merged across pictures) =="]
        entries = [e for e in merged.values() if e["example"].severity == severity]
        entries.sort(key=lambda e: (e["example"].kind, e["example"].widget.split(" '")[0], e["example"].detail))
        if not entries:
            lines.append("none")
        for entry in entries:
            runs = entry["runs"]
            where: dict[str, set] = {}
            for scenario, theme, size in runs:
                where.setdefault(scenario, set()).add(f"{theme}/{size}")
            lines.append(entry["example"].line() + (f"  (worst; seen from {min(entry['ratios']):.2f}:1 to {max(entry['ratios']):.2f}:1)"
                                                   if entry["example"].kind == "contrast" else ""))
            lines.append("    seen in " + "; ".join(f"{s} ({len(v)})" for s, v in sorted(where.items()))
                         + f" -- {len(runs)} occurrences")
    return "\n".join(lines) + "\n"


# ---------- orchestrator ----------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="folder for the pictures, contact sheets and checks.txt")
    parser.add_argument("--scenarios", default=",".join(scenarios.SCENARIOS))
    parser.add_argument("--themes", default=",".join(checks.THEMES))
    parser.add_argument("--sheets-only", action="store_true", help="redraw the contact sheets and checks.txt from results.json")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--scale", type=float, default=1.0, help=argparse.SUPPRESS)
    parser.add_argument("--db", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.scenarios = [s for s in args.scenarios.split(",") if s]
    args.themes = [t for t in args.themes.split(",") if t]
    if args.worker:
        return _worker(args)

    window_module = qtenv.load()
    if window_module is None:
        print("PyQt6 from MO2 isn't available (set FOMOD_LOGGER_MO2_ROOT)", file=sys.stderr)
        return 2
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    database_dir = out / "_db"
    started = time.perf_counter()
    try:
        if args.sheets_only:
            saved = json.loads((out / "results.json").read_text(encoding="utf-8"))
            return _finish(window_module, out, saved["records"], saved["timings"])
        for name in args.scenarios:
            scenarios.materialize(scenarios.build(name), database_dir / name)
        records: list[dict] = []
        timings: dict[str, list[float]] = {}
        for scale in sorted({size[3] for size in checks.SIZES}):
            environment = dict(os.environ)
            if scale == 1.0:
                environment.pop("QT_SCALE_FACTOR", None)
            else:
                environment["QT_SCALE_FACTOR"] = str(scale)
            command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--scale", str(scale), "--out", str(out),
                       "--db", str(database_dir), "--scenarios", ",".join(args.scenarios), "--themes", ",".join(args.themes)]
            print(f"drawing scale {scale} ...", flush=True)
            done = subprocess.run(command, env=environment)
            result_file = out / f"_results_{scale}.json"
            if done.returncode or not result_file.is_file():
                print(f"the scale {scale} process failed ({done.returncode})", file=sys.stderr)
                return done.returncode or 1
            data = json.loads(result_file.read_text(encoding="utf-8"))
            result_file.unlink()
            records += data["records"]
            timings.update(data["timings"])
        (out / "results.json").write_text(json.dumps({"records": records, "timings": timings}), encoding="utf-8")
        _finish(window_module, out, records, timings)
    finally:
        if database_dir.is_dir():
            shutil.rmtree(database_dir)
    print(f"done in {time.perf_counter() - started:.0f}s: {out}")
    return 0


def _finish(window_module, out: Path, records: list[dict], timings: dict) -> int:
    env = checks.Env(window_module)
    from PyQt6.QtCore import PYQT_VERSION_STR, qVersion

    _contact_sheets(env, records, out)
    env.restore()
    (out / "checks.txt").write_text(
        _checks_text(records, timings, f"PyQt6 {PYQT_VERSION_STR}, Qt {qVersion()}"), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
