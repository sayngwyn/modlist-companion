"""Parse a FOMOD's ``ModuleConfig.xml`` (and ``info.xml``) into a plain option tree.

The reading rules follow the native installer (``src/fomodinstallerdialog.cpp``) so a
tree parsed here and a tree the installer built describe the same options with the same
ids, names, files and conditions:

* element and attribute names are case sensitive; unknown elements are skipped;
* a ``<file>`` or ``<folder>`` with an empty source is ignored; a missing destination
  means "same as the source"; ``priority`` is read like ``QString::toInt`` (0 on failure);
* every file entry gets an ``order``: its position among all entries of the document;
* the entries of one option are sorted by (priority, order), the way the installer sorts them;
* ids are by authored position: ``step:N``, ``step:N/group:M``, ``step:N/group:M/option:K``.

It never trusts the file: it reads UTF-8 and UTF-16 (with or without a BOM, and a wrong
declaration), refuses any DOCTYPE or ENTITY declaration (so there is nothing to expand),
and caps size, nesting and every count. Nothing here touches the disk.
"""

from __future__ import annotations

import codecs
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterator, Union

__all__ = [
    "Composite",
    "ConditionalInstall",
    "FileCondition",
    "FileEntry",
    "FlagCondition",
    "FomodInfo",
    "FomodParseError",
    "Group",
    "ModuleConfig",
    "Option",
    "ParseLimits",
    "PluginTypeInfo",
    "Step",
    "VersionCondition",
    "collect_detected_plugins",
    "display_options",
    "parse_info_xml",
    "parse_module_config",
    "resolve_file_destination",
]

GROUP_TYPES = ("SelectAtLeastOne", "SelectAtMostOne", "SelectExactlyOne", "SelectAny", "SelectAll")
PLUGIN_TYPES = ("Required", "Optional", "Recommended", "NotUsable", "CouldBeUsable")
ORDERS = ("Ascending", "Descending", "Explicit")
_PLUGIN_EXTENSIONS = (".esp", ".esm", ".esl")


class FomodParseError(ValueError):
    """The XML cannot be read as a FOMOD configuration."""


@dataclass(frozen=True)
class ParseLimits:
    """Caps on hostile or absurd input. Going over a count cap keeps the first items and notes it."""

    max_bytes: int = 8 * 1024 * 1024
    max_elements: int = 400_000
    max_depth: int = 64
    max_steps: int = 500
    max_groups_per_step: int = 500
    max_options_per_group: int = 1000
    max_options: int = 20_000
    max_files_per_item: int = 5000
    max_files: int = 200_000
    max_conditional_installs: int = 5000
    max_condition_depth: int = 32
    max_condition_nodes: int = 5000
    max_flags_per_option: int = 200
    max_patterns_per_option: int = 200
    max_text: int = 64 * 1024


# ----------------------------------------------------------------------------- model


@dataclass(frozen=True)
class FileEntry:
    kind: str                  # "file" | "folder"
    source: str                # as written
    destination: str           # as written (the source when the attribute is missing)
    priority: int
    order: int                 # position among all file entries of the document, from 1
    resolved: str              # where it installs: see resolve_file_destination
    escapes: bool
    always_install: bool = False
    install_if_usable: bool = False

    @property
    def is_folder(self) -> bool:
        return self.kind == "folder"


@dataclass(frozen=True)
class FlagCondition:
    flag: str
    value: str


@dataclass(frozen=True)
class FileCondition:
    file: str
    state: str                 # Active | Inactive | Missing (compared exactly)


@dataclass(frozen=True)
class VersionCondition:
    kind: str                  # game | fomm | fose
    version: str


@dataclass(frozen=True)
class Composite:
    operator: str              # And | Or
    children: tuple["Condition", ...]


Condition = Union[FlagCondition, FileCondition, VersionCondition, Composite]


@dataclass(frozen=True)
class PluginTypeInfo:
    default: str = "Optional"
    patterns: tuple[tuple[Composite, str], ...] = ()


@dataclass(frozen=True)
class Option:
    index: int                 # authored position within the group
    name: str
    description: str
    image: str
    files: tuple[FileEntry, ...]       # sorted by (priority, order)
    flags: tuple[tuple[str, str], ...]  # (name, value) set when the option is chosen
    type_info: PluginTypeInfo


@dataclass(frozen=True)
class Group:
    index: int
    name: str
    type: str
    order: str                 # how the installer lists the options: Ascending | Descending | Explicit
    options: tuple[Option, ...]        # authored order


@dataclass(frozen=True)
class Step:
    index: int
    name: str
    visible: Composite | None
    groups: tuple[Group, ...]


@dataclass(frozen=True)
class ConditionalInstall:
    index: int
    condition: Composite
    files: tuple[FileEntry, ...]


@dataclass
class ModuleConfig:
    module_name: str = ""
    required_files: tuple[FileEntry, ...] = ()
    steps: tuple[Step, ...] = ()               # authored order
    step_order: str = "Ascending"
    conditional_installs: tuple[ConditionalInstall, ...] = ()
    module_dependencies: Composite | None = None
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False                    # a count cap cut something

    def display_steps(self) -> list[Step]:
        """The steps in the order the installer shows them."""

        return _sorted_by_name(self.steps, self.step_order, lambda step: step.name)


@dataclass(frozen=True)
class FomodInfo:
    name: str = ""
    author: str = ""
    version: str = ""
    nexus_id: int = -1         # -1 when info.xml has no <Id>
    website: str = ""
    description: str = ""


def _utf16_key(text: str) -> bytes:
    # QString compares UTF-16 code units.
    return text.encode("utf-16-be", "surrogatepass")


def _sorted_by_name(items, order: str, name_of):
    items = list(items)
    if order == "Ascending":
        return sorted(items, key=lambda item: _utf16_key(name_of(item)))
    if order == "Descending":
        return sorted(items, key=lambda item: _utf16_key(name_of(item)), reverse=True)
    return items


def display_options(group: Group) -> list[Option]:
    """The options of a group in the order the installer lists them."""

    return _sorted_by_name(group.options, group.order, lambda option: option.name)


# ------------------------------------------------------------ destinations (native mirror)


def _clean_path(path: str) -> str:
    """``QDir::cleanPath`` for a '/'-separated path that has no drive letter and no UNC prefix."""

    if not path:
        return ""
    absolute = path.startswith("/")
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            else:
                # Qt keeps a ".." that is right after the root; the caller then sees it escape.
                parts.append("..")
            continue
        parts.append(part)
    cleaned = "/".join(parts)
    if absolute:
        cleaned = "/" + cleaned
    return cleaned or ("/" if absolute else ".")


def resolve_file_destination(source: str, destination: str, is_folder: bool) -> tuple[str, bool]:
    """Where a ``<file>``/``<folder>`` entry installs: ``(resolved, escapes)``.

    Exactly ``FomodLogger::resolveFileDestination``: forward slashes, relative to the mod
    folder, a leading ``Data/`` dropped, case kept. A file with no destination name, one
    ending in a slash, or one that names the mod folder keeps its own name. A destination
    outside the mod folder (``..``, a drive letter, a UNC path) resolves to ``""`` with
    ``escapes`` true.
    """

    path = destination.replace("\\", "/")
    name = source.replace("\\", "/")
    name = name[name.rfind("/") + 1:]

    drive = len(path) >= 2 and path[0].isalpha() and path[1] == ":"
    outside = drive or path.startswith("//")
    wants_name = (not is_folder) and (path == "" or path.endswith("/"))

    path = _clean_path(path).lstrip("/")      # a leading slash goes first, then ".." is looked for
    if path == ".." or path.startswith("../"):
        outside = True
    if outside:
        return "", True
    lowered = path.lower()
    if lowered == "data":
        path = ""
    elif lowered.startswith("data/"):
        path = path[5:]
    if path == ".":
        path = ""
    if (not is_folder) and (wants_name or path == ""):
        path = name if path == "" else path + "/" + name
    return path, False


# ------------------------------------------------------------------- decoding the bytes

_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)
_ENCODING = re.compile(rb"""encoding\s*=\s*["']([A-Za-z0-9._:-]+)["']""", re.IGNORECASE)
_STILL_A_DECLARATION = re.compile(r"\s*<\?xml[\s?]", re.IGNORECASE)
_DECLARATION_HEAD = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _decode_candidates(data: bytes) -> Iterator[tuple[str, str]]:
    """(label, text) for the readings worth trying, best first: BOM, then declaration, then fallbacks."""

    tried: set[str] = set()

    def attempt(label: str, codec: str, payload: bytes):
        if codec in tried:
            return None
        tried.add(codec)
        try:
            return label, payload.decode(codec)
        except (UnicodeDecodeError, LookupError):
            return None

    if data.startswith(codecs.BOM_UTF8):
        found = attempt("utf-8 (BOM)", "utf-8", data[3:])
    elif data.startswith(codecs.BOM_UTF16_LE):
        found = attempt("utf-16le (BOM)", "utf-16-le", data[2:])
    elif data.startswith(codecs.BOM_UTF16_BE):
        found = attempt("utf-16be (BOM)", "utf-16-be", data[2:])
    elif data[:4] == b"<\x00?\x00":
        found = attempt("utf-16le", "utf-16-le", data)
    elif data[:4] == b"\x00<\x00?":
        found = attempt("utf-16be", "utf-16-be", data)
    else:
        found = None
        declared = _ENCODING.search(data[:512])
        if declared:
            try:
                codec = codecs.lookup(declared.group(1).decode("ascii")).name
                found = attempt(f"declared {codec}", codec, data)
            except LookupError:
                found = None
    if found:
        yield found
    for label, codec in (("utf-8", "utf-8"), ("latin-1", "latin-1")):
        found = attempt(label, codec, data)
        if found:
            yield found


def _check_no_declarations(text: str) -> None:
    """Refuse a DOCTYPE or ENTITY declaration in exactly the text the XML parser will read.

    Markup that can hide one is skipped the way the parser skips it: processing
    instructions (``<?`` to the first ``?>``), comments and CDATA sections. One linear pass.
    """

    position = 0
    while True:
        found = text.find("<", position)
        if found < 0:
            return
        following = text[found + 1:found + 2]
        if following == "?":
            end = text.find("?>", found + 2)
            if end < 0:
                return
            position = end + 2
        elif text.startswith("<!--", found):
            end = text.find("-->", found + 4)
            if end < 0:
                return
            position = end + 3
        elif text.startswith("<![CDATA[", found):
            end = text.find("]]>", found + 9)
            if end < 0:
                return
            position = end + 3
        elif following == "!":
            if _DECLARATION_HEAD.match(text, found):
                raise FomodParseError("the XML declares a DOCTYPE or ENTITY, which is not accepted")
            position = found + 2
        else:
            position = found + 1


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _parse_tree(data: bytes, limits: ParseLimits) -> ET.Element:
    if len(data) > limits.max_bytes:
        raise FomodParseError(f"the XML is {len(data)} bytes, over the {limits.max_bytes} byte limit")
    last_error = "empty file"
    for label, text in _decode_candidates(data):
        text = text.lstrip(chr(0xFEFF))
        # What the parser reads is the text without its XML declaration; that is what is checked.
        text = _DECLARATION.sub("", text, count=1)
        if _STILL_A_DECLARATION.match(text):
            raise FomodParseError("the XML declaration is not in a form that can be read safely")
        _check_no_declarations(text)
        payload = text.encode("utf-8", "surrogatepass")
        parser = ET.XMLPullParser(events=("start", "end"))
        depth = 0
        count = 0
        root = None
        try:
            for start in range(0, len(payload), 1 << 16):
                parser.feed(payload[start:start + (1 << 16)])
                for event, element in parser.read_events():  # pyright: ignore[reportAssignmentType]  # typeshed types the events loosely
                    if event == "start":
                        depth += 1
                        count += 1
                        if depth > limits.max_depth:
                            raise FomodParseError(f"elements are nested deeper than {limits.max_depth}")
                        if count > limits.max_elements:
                            raise FomodParseError(f"more than {limits.max_elements} elements")
                    else:
                        depth -= 1
                        if depth == 0:
                            root = element
            parser.close()
            for event, element in parser.read_events():  # pyright: ignore[reportAssignmentType]  # typeshed types the events loosely
                if event == "end" and root is None:
                    root = element
        except ET.ParseError as exc:
            last_error = f"{label}: {exc}"
            continue
        if root is None:
            last_error = f"{label}: no root element"
            continue
        return root  # pyright: ignore[reportReturnType]  # typeshed types the events loosely
    raise FomodParseError(f"cannot read the XML ({last_error})")


# ----------------------------------------------------------------------- the parser


def _qt_to_int(text: str | None) -> int:
    """``QString::toInt``: the number, or 0 when it is not a plain 32-bit integer."""

    if text is None:
        return 0
    match = re.fullmatch(r"\s*([+-]?[0-9]+)\s*", text)
    if not match:
        return 0
    value = int(match.group(1))
    return value if -(2**31) <= value < 2**31 else 0


def _text_of(element: ET.Element, limit: int) -> str:
    return "".join(element.itertext()).strip()[:limit]


class _Parser:
    def __init__(self, limits: ParseLimits):
        self.limits = limits
        self.config = ModuleConfig()
        self.sequence = 0
        self.total_options = 0
        self.total_files = 0

    def warn(self, message: str) -> None:
        if len(self.config.warnings) < 100 and message not in self.config.warnings:
            self.config.warnings.append(message)

    def cut(self, message: str) -> None:
        self.config.truncated = True
        self.warn(message)

    # --- files
    def files(self, element: ET.Element, limit: int | None = None) -> list[FileEntry]:
        entries: list[FileEntry] = []
        cap = self.limits.max_files_per_item if limit is None else limit
        for child in element:
            tag = _local(child.tag)
            if tag not in ("file", "folder"):
                continue
            source = child.get("source", "")
            if source == "":
                continue   # "an option that does nothing": ignored and not counted
            # The sequence counts every real entry, so `order` matches the native installer
            # even for entries cut below.
            self.sequence += 1
            if len(entries) >= cap or self.total_files >= self.limits.max_files:
                self.cut("some file entries were not read (too many)")
                continue
            destination = child.get("destination", source)
            is_folder = tag == "folder"
            resolved, escapes = resolve_file_destination(source, destination, is_folder)
            entries.append(FileEntry(
                kind=tag, source=source, destination=destination,
                priority=_qt_to_int(child.get("priority")) if child.get("priority") is not None else 0,
                order=self.sequence, resolved=resolved, escapes=escapes,
                always_install=child.get("alwaysInstall") == "true",
                install_if_usable=child.get("installIfUsable") == "true"))
            self.total_files += 1
        return entries

    # --- conditions
    def composite(self, element: ET.Element, depth: int = 0, budget: list[int] | None = None) -> Composite:
        budget = budget if budget is not None else [self.limits.max_condition_nodes]
        operator = element.get("operator", "And")
        if operator not in ("And", "Or"):
            self.warn(f"unknown dependency operator {operator!r}: treated as And")
            operator = "And"
        children: list[Condition] = []
        for child in element:
            tag = _local(child.tag)
            budget[0] -= 1
            if budget[0] < 0:
                self.cut("a condition is too large; the rest of it was ignored")
                break
            if tag == "fileDependency":
                children.append(FileCondition(child.get("file", ""), child.get("state", "")))
            elif tag == "flagDependency":
                children.append(FlagCondition(child.get("flag", ""), child.get("value", "")))
            elif tag == "gameDependency":
                children.append(VersionCondition("game", child.get("version", "")))
            elif tag == "fommDependency":
                children.append(VersionCondition("fomm", child.get("version", "")))
            elif tag == "foseDependency":
                children.append(VersionCondition("fose", child.get("version", "")))
            elif tag == "dependencies":
                if depth + 1 > self.limits.max_condition_depth:
                    self.cut("conditions are nested too deeply; the inner ones were ignored")
                    continue
                children.append(self.composite(child, depth + 1, budget))
        return Composite(operator, tuple(children))

    # --- options
    def plugin_type(self, element: ET.Element) -> PluginTypeInfo:
        default = "Optional"
        patterns: list[tuple[Composite, str]] = []
        for child in element:
            tag = _local(child.tag)
            if tag == "type":
                default = self.type_name(child.get("name", ""))
            elif tag == "dependencyType":
                for part in child:
                    part_tag = _local(part.tag)
                    if part_tag == "defaultType":
                        default = self.type_name(part.get("name", ""))
                    elif part_tag == "patterns":
                        for pattern in part:
                            if _local(pattern.tag) != "pattern":
                                continue
                            if len(patterns) >= self.limits.max_patterns_per_option:
                                self.cut("an option has too many type patterns; the rest were ignored")
                                break
                            condition = Composite("And", ())
                            kind = "Optional"
                            for piece in pattern:
                                piece_tag = _local(piece.tag)
                                if piece_tag == "dependencies":
                                    condition = self.composite(piece)
                                elif piece_tag == "type":
                                    kind = self.type_name(piece.get("name", ""))
                            patterns.append((condition, kind))
        return PluginTypeInfo(default, tuple(patterns))

    def type_name(self, name: str) -> str:
        if name in PLUGIN_TYPES:
            return name
        self.warn(f"unknown plugin type {name!r}: treated as Optional")
        return "Optional"

    def option(self, element: ET.Element, index: int) -> Option:
        description = ""
        image = ""
        files: list[FileEntry] = []
        flags: list[tuple[str, str]] = []
        type_info = PluginTypeInfo()
        for child in element:
            tag = _local(child.tag)
            if tag == "description":
                description = _text_of(child, self.limits.max_text)
            elif tag == "image":
                image = child.get("path", "")
            elif tag == "files":
                files += self.files(child, max(0, self.limits.max_files_per_item - len(files)))
            elif tag == "conditionFlags":
                for flag in child:
                    if _local(flag.tag) != "flag":
                        continue
                    if len(flags) >= self.limits.max_flags_per_option:
                        self.cut("an option sets too many flags; the rest were ignored")
                        break
                    flags.append((flag.get("name", ""), _text_of(flag, self.limits.max_text)))
            elif tag == "typeDescriptor":
                type_info = self.plugin_type(child)
        files.sort(key=lambda entry: (entry.priority, entry.order))
        return Option(index, element.get("name", ""), description, image, tuple(files), tuple(flags), type_info)

    def group(self, element: ET.Element, index: int) -> Group:
        name = element.get("name", "")
        kind = element.get("type", "")
        if kind not in GROUP_TYPES:
            self.warn(f"group {name!r} has unknown type {kind!r}: treated as SelectAny")
            kind = "SelectAny"
        order = "Ascending"
        options: list[Option] = []
        for plugins in element:
            if _local(plugins.tag) != "plugins":
                continue
            declared = plugins.get("order")
            if declared is not None:
                if declared in ORDERS:
                    order = declared
                else:
                    self.warn(f"unknown order {declared!r}: treated as Ascending")
            for plugin in plugins:
                if _local(plugin.tag) != "plugin":
                    continue
                if (len(options) >= self.limits.max_options_per_group
                        or self.total_options >= self.limits.max_options):
                    self.cut("a group has too many options; the rest were ignored")
                    # Keep the file sequence honest for what follows.
                    for files in plugin.iter():
                        if _local(files.tag) == "files":
                            self.files(files, 0)
                    continue
                options.append(self.option(plugin, len(options)))
                self.total_options += 1
        return Group(index, name, kind, order, tuple(options))

    def step(self, element: ET.Element, index: int) -> Step:
        visible: Composite | None = None
        groups: list[Group] = []
        for child in element:
            tag = _local(child.tag)
            if tag == "visible":
                composite = self.composite(child)
                visible = composite if composite.children else None
            elif tag == "optionalFileGroups":
                for group in child:
                    if _local(group.tag) != "group":
                        continue
                    if len(groups) >= self.limits.max_groups_per_step:
                        self.cut("a step has too many groups; the rest were ignored")
                        continue
                    groups.append(self.group(group, len(groups)))
        return Step(index, element.get("name", ""), visible, tuple(groups))

    # --- document
    def run(self, root: ET.Element) -> ModuleConfig:
        if _local(root.tag) != "config":
            raise FomodParseError(f"expected <config>, found <{_local(root.tag)}>")
        steps: list[Step] = []
        conditional: list[ConditionalInstall] = []
        required: list[FileEntry] = []
        config = self.config
        for child in root:
            tag = _local(child.tag)
            if tag == "moduleName":
                config.module_name = _text_of(child, self.limits.max_text)
            elif tag == "moduleDependencies":
                config.module_dependencies = self.composite(child)
            elif tag == "requiredInstallFiles":
                required += self.files(child)
            elif tag == "installSteps":
                declared = child.get("order")
                if declared is not None:
                    if declared in ORDERS:
                        config.step_order = declared
                    else:
                        self.warn(f"unknown step order {declared!r}: treated as Ascending")
                for step in child:
                    if _local(step.tag) != "installStep":
                        continue
                    if len(steps) >= self.limits.max_steps:
                        self.cut("too many install steps; the rest were ignored")
                        continue
                    steps.append(self.step(step, len(steps)))
            elif tag == "conditionalFileInstalls":
                for patterns in child:
                    if _local(patterns.tag) != "patterns":
                        continue
                    for pattern in patterns:
                        if _local(pattern.tag) != "pattern":
                            continue
                        condition = Composite("And", ())
                        files: list[FileEntry] = []
                        for piece in pattern:
                            piece_tag = _local(piece.tag)
                            if piece_tag == "dependencies":
                                condition = self.composite(piece)
                            elif piece_tag == "files":
                                files += self.files(piece)
                        if len(conditional) >= self.limits.max_conditional_installs:
                            self.cut("too many conditional installs; the rest were ignored")
                            continue
                        conditional.append(ConditionalInstall(len(conditional), condition, tuple(files)))
        config.required_files = tuple(required)
        config.steps = tuple(steps)
        config.conditional_installs = tuple(conditional)
        return config


def parse_module_config(data: bytes, limits: ParseLimits | None = None) -> ModuleConfig:
    """Parse ``ModuleConfig.xml`` bytes. Raises :class:`FomodParseError` on unreadable input."""

    limits = limits or ParseLimits()
    root = _parse_tree(data, limits)
    try:
        return _Parser(limits).run(root)
    except RecursionError as exc:
        raise FomodParseError("the XML is nested too deeply") from exc


def parse_info_xml(data: bytes, limits: ParseLimits | None = None) -> FomodInfo:
    """Parse ``fomod/info.xml``. Anything unreadable gives an empty :class:`FomodInfo`."""

    limits = limits or ParseLimits()
    try:
        root = _parse_tree(data, limits)
    except FomodParseError:
        return FomodInfo()
    values: dict[str, str] = {}
    for element in root.iter():
        tag = _local(element.tag)
        if tag in ("Name", "Author", "Version", "Id", "Website", "Description") and tag not in values:
            values[tag] = _text_of(element, limits.max_text)
    return FomodInfo(
        name=values.get("Name", ""), author=values.get("Author", ""),
        version=values.get("Version", ""),
        nexus_id=_qt_to_int(values["Id"]) if "Id" in values else -1,
        website=values.get("Website", ""), description=values.get("Description", ""))


# --------------------------------------------------------------------------- helpers


def _file_leaf(path: str) -> str:
    text = path.replace("\\", "/")
    return text[text.rfind("/") + 1:]


def collect_detected_plugins(condition: Composite | None, out: list[str] | None = None) -> list[str]:
    """Plugins a condition looks for being Active (the ``detects`` of an option), nested ones too.

    Mirrors ``collectDetectedPlugins``: only ``state="Active"`` (any case), only
    ``.esp``/``.esm``/``.esl``, file name only, no case-insensitive duplicates.
    """

    names = out if out is not None else []
    if condition is None:
        return names
    for child in condition.children:
        if isinstance(child, FileCondition):
            name = _file_leaf(child.file)
            if (child.state.casefold() == "active" and name.lower().endswith(_PLUGIN_EXTENSIONS)
                    and name.casefold() not in {existing.casefold() for existing in names}):
                names.append(name)
        elif isinstance(child, Composite):
            collect_detected_plugins(child, names)
    return names
