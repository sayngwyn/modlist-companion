"""Export every FOMOD choice in a list: a readable install guide and a choices file.

Both come from the same data, the newest install of each mod that is still installed:

* ``build_choices_file`` makes the choices file (see ``choices_file`` for its shape), which another PC or
  MO2 instance can import so the installer offers those picks again;
* ``render_markdown`` and ``render_html`` turn that same data into a guide of the kind list authors write by
  hand: per mod, the pages and the options picked, with the unpicked ones optional.

Plain data in, text out; no Qt and no MO2.
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .choices_file import FORMAT_ID, FORMAT_VERSION

_DOMAIN = re.compile(r"[a-z0-9]{1,64}")
_GROUP_WORDS = {
    "SelectExactlyOne": "choose one",
    "SelectAtMostOne": "choose none or one",
    "SelectAtLeastOne": "choose at least one",
    "SelectAny": "choose any",
    "SelectAll": "all of these are required",
}


def now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _file_name(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def current_events(store: Any, *, instance_id: str = "", include_hidden: bool = False) -> list[dict[str, Any]]:
    """The newest install of each mod that is not removed (hidden mods only when asked for), for one MO2
    instance, or for all of them with a mod seen in several kept once, by its newest install."""

    # Mods that were not installed through an installer made no choices to export.
    events = [event for event in store.latest("", include_hidden=include_hidden) if event.get("kind") != "plain"]
    if instance_id:
        return [event for event in events if event["instance"]["id"] == instance_id]
    seen: set[str] = set()
    kept = []
    for event in events:  # newest first
        key = event["mod"]["name"].lower()
        if key not in seen:
            seen.add(key)
            kept.append(event)
    return kept


def _mod_entry(event: dict[str, Any]) -> dict[str, Any]:
    mod = event["mod"]
    nexus_id = mod.get("nexus_id")
    steps = []
    for step in event["steps"]:
        steps.append({
            "name": step["name"],
            "skipped": step.get("visible") is False,
            "groups": [{
                "name": group["name"],
                "type": group["type"],
                "options": [{"name": option["name"], "picked": option.get("selected") is True,
                             "enabled": option.get("enabled") is not False}
                            for option in group["options"]],
            } for group in step["groups"]],
        })
    return {
        "name": mod["name"],
        "nexus_id": nexus_id if isinstance(nexus_id, int) and not isinstance(nexus_id, bool) and nexus_id > 0 else 0,
        "version": mod.get("version", "") or "",
        "archive": _file_name(event["source"].get("archive", "") or ""),
        "installed_at": event["completed_at"],
        "steps": steps,
    }


def build_choices_file(events: Iterable[dict[str, Any]], *, game: str = "", nexus_domain: str = "",
                       instance_label: str = "", exported_at: str | None = None) -> dict[str, Any]:
    """The choices file for these events (as ``current_events`` returns them), mods in name order."""

    mods = sorted((_mod_entry(event) for event in events), key=lambda mod: (mod["name"].casefold(), mod["name"]))
    return {
        "format": FORMAT_ID,
        "format_version": FORMAT_VERSION,
        "exported_at": exported_at or now_stamp(),
        "source": {"game": game, "nexus_domain": nexus_domain, "instance_label": instance_label},
        "mods": mods,
    }


def choices_json(choices: dict[str, Any]) -> str:
    return json.dumps(choices, ensure_ascii=False, indent=2) + "\n"


def write_text(path: Path | str, text: str) -> None:
    """Write ``text`` as UTF-8 with LF line ends, replacing the file only once it is complete."""

    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


# --- the guide ---------------------------------------------------------------------------------------------

def _nexus_url(choices: dict[str, Any], mod: dict[str, Any]) -> str:
    domain = choices.get("source", {}).get("nexus_domain", "")
    if mod.get("nexus_id", 0) > 0 and _DOMAIN.fullmatch(domain or ""):
        return f"https://www.nexusmods.com/{domain}/mods/{mod['nexus_id']}"
    return ""


def _shown_steps(mod: dict[str, Any]) -> list[dict[str, Any]]:
    """The pages the installer showed (a skipped page chose nothing, so a guide has nothing to say about it)."""

    return [step for step in mod["steps"] if not step.get("skipped") and step["groups"]]


def _option_state(option: dict[str, Any]) -> str:
    """"picked", "required" (picked and not the user's to change), "optional" or "unavailable"."""

    if option["picked"]:
        return "picked" if option.get("enabled", True) else "required"
    return "optional" if option.get("enabled", True) else "unavailable"


def _day(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return stamp


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#|~])")


def _md(text: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", " ".join(text.split()))


def render_markdown(choices: dict[str, Any]) -> str:
    """The install guide as Markdown."""

    source = choices.get("source", {})
    mods = choices["mods"]
    lines = ["# FOMOD install guide", ""]
    facts = [part for part in (source.get("game"), f"made {_day(choices.get('exported_at', ''))}",
                               _plural(len(mods), "mod")) if part]
    lines += [" · ".join(_md(part) for part in facts), "",
              "Every FOMOD installer choice made for these mods. Install each mod and pick the ticked options "
              "on each page. The unticked ones are optional: leave them out, or take them if you want them.", ""]
    for mod in mods:
        lines += [f"## {_md(mod['name'])}", ""]
        details = []
        if mod.get("version"):
            details.append(f"version {_md(mod['version'])}")
        url = _nexus_url(choices, mod)
        if url:
            details.append(f"[Nexus page]({url})")
        elif mod.get("nexus_id", 0) > 0:
            details.append(f"Nexus ID {mod['nexus_id']}")
        if mod.get("archive"):
            details.append(f"file {_md(mod['archive'])}")
        if details:
            lines += [" · ".join(details), ""]
        steps = _shown_steps(mod)
        if not steps:
            lines += ["The installer had nothing to choose.", ""]
        for number, step in enumerate(steps, 1):
            lines += [f"### Page {number}: {_md(step['name'])}" if step["name"] else f"### Page {number}", ""]
            for group in step["groups"]:
                rule = _GROUP_WORDS.get(group.get("type", ""), "")
                lines += [f"**{_md(group['name'])}**" + (f" ({rule})" if rule else ""), ""]
                for option in group["options"]:
                    state = _option_state(option)
                    name = _md(option["name"]) or "(unnamed option)"
                    if state == "picked":
                        lines.append(f"- [x] **{name}**")
                    elif state == "required":
                        lines.append(f"- [x] **{name}** (required)")
                    elif state == "unavailable":
                        lines.append(f"- [ ] {name} (not available)")
                    else:
                        lines.append(f"- [ ] {name} (optional)")
                lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


_CSS = """
:root { color-scheme: light dark; --text: #1f2328; --muted: #59636e; --line: #d1d9e0; --page: #ffffff;
        --card: #f6f8fa; --pick: #1a7f37; --link: #0969da; }
@media (prefers-color-scheme: dark) {
  :root { --text: #e6edf3; --muted: #9198a1; --line: #3d444d; --page: #0d1117; --card: #151b23;
          --pick: #3fb950; --link: #4493f8; } }
body { margin: 0; background: var(--page); color: var(--text);
       font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 52rem; margin: 0 auto; padding: 1.5rem 1rem 3rem; }
h1 { font-size: 1.75rem; margin: 0 0 .25rem; }
h2 { font-size: 1.3rem; margin: 0 0 .25rem; }
h3 { font-size: 1rem; margin: 1.1rem 0 .4rem; color: var(--muted); font-weight: 600; }
a { color: var(--link); }
.meta, .intro { color: var(--muted); margin: .25rem 0 1rem; }
nav { border: 1px solid var(--line); border-radius: 8px; background: var(--card); padding: .6rem 1rem; margin-bottom: 1.5rem; }
nav ul { margin: .25rem 0; padding-left: 1.2rem; columns: 2 16rem; }
section.mod { border: 1px solid var(--line); border-radius: 8px; padding: 1rem 1.25rem; margin: 0 0 1.25rem; }
.group { margin: .5rem 0 .75rem; }
.group-name { font-weight: 600; }
.rule { color: var(--muted); font-weight: 400; }
ul.options { list-style: none; margin: .25rem 0 0; padding: 0; }
ul.options li { padding: .15rem 0 .15rem 1.6rem; position: relative; }
ul.options li::before { content: "\\2610"; position: absolute; left: 0; color: var(--muted); }
ul.options li.picked::before, ul.options li.required::before { content: "\\2611"; color: var(--pick); }
ul.options li.picked .name, ul.options li.required .name { font-weight: 700; }
ul.options li.optional, ul.options li.unavailable { color: var(--muted); }
.note { color: var(--muted); font-size: .9em; }
"""


def render_html(choices: dict[str, Any]) -> str:
    """The install guide as one self-contained HTML page (no scripts, no external files)."""

    esc = html.escape
    source = choices.get("source", {})
    mods = choices["mods"]
    facts = [part for part in (source.get("game"), f"made {_day(choices.get('exported_at', ''))}",
                               _plural(len(mods), "mod")) if part]
    out = ["<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           "<title>FOMOD install guide</title>", f"<style>{_CSS}</style>", "</head>", "<body>", "<main>",
           "<h1>FOMOD install guide</h1>", f'<p class="meta">{esc(" · ".join(facts))}</p>',
           '<p class="intro">Every FOMOD installer choice made for these mods. Install each mod and pick the '
           "ticked options on each page. The unticked ones are optional: leave them out, or take them if you "
           "want them.</p>"]
    if len(mods) > 1:
        out += ["<nav><strong>Mods</strong><ul>"]
        out += [f'<li><a href="#mod-{index}">{esc(mod["name"])}</a></li>' for index, mod in enumerate(mods, 1)]
        out += ["</ul></nav>"]
    for index, mod in enumerate(mods, 1):
        out += [f'<section class="mod" id="mod-{index}">', f"<h2>{esc(mod['name'])}</h2>"]
        details = []
        if mod.get("version"):
            details.append(f"version {esc(mod['version'])}")
        url = _nexus_url(choices, mod)
        if url:
            details.append(f'<a href="{esc(url, quote=True)}" rel="noopener">Nexus page</a>')
        elif mod.get("nexus_id", 0) > 0:
            details.append(f"Nexus ID {mod['nexus_id']}")
        if mod.get("archive"):
            details.append(f"file {esc(mod['archive'])}")
        if details:
            out.append(f'<p class="meta">{" · ".join(details)}</p>')
        steps = _shown_steps(mod)
        if not steps:
            out.append('<p class="note">The installer had nothing to choose.</p>')
        for number, step in enumerate(steps, 1):
            out.append(f"<h3>Page {number}{': ' + esc(step['name']) if step['name'] else ''}</h3>")
            for group in step["groups"]:
                rule = _GROUP_WORDS.get(group.get("type", ""), "")
                out.append(f'<div class="group"><span class="group-name">{esc(group["name"])}</span>'
                           + (f' <span class="rule">({esc(rule)})</span>' if rule else "") + '<ul class="options">')
                for option in group["options"]:
                    state = _option_state(option)
                    note = {"required": " (required)", "optional": " (optional)",
                            "unavailable": " (not available)"}.get(state, "")
                    name = esc(option["name"]) or "(unnamed option)"
                    out.append(f'<li class="{state}"><span class="name">{name}</span>'
                               + (f'<span class="note">{note}</span>' if note else "") + "</li>")
                out.append("</ul></div>")
        out.append("</section>")
    out += ["</main>", "</body>", "</html>"]
    return "\n".join(out) + "\n"
