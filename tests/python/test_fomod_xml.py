"""The ModuleConfig.xml / info.xml parser."""

from __future__ import annotations

import codecs

import pytest

from fomod_logger_support.archive import fomod_xml
from fomod_logger_support.archive.fomod_xml import (
    Composite, FileCondition, FlagCondition, FomodParseError, ParseLimits, VersionCondition,
    collect_detected_plugins, display_options, parse_info_xml, parse_module_config,
    resolve_file_destination)

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
        xsi:noNamespaceSchemaLocation="http://qconsulting.ca/fo3/ModConfig5.0.xsd">
  <moduleName>Sample</moduleName>
  <moduleDependencies operator="And">
    <gameDependency version="1.5.97"/>
  </moduleDependencies>
  <requiredInstallFiles>
    <file source="core.esp" destination="core.esp"/>
    <folder source="meshes" destination="meshes" priority="2"/>
  </requiredInstallFiles>
  <installSteps order="Explicit">
    <installStep name="Main">
      <optionalFileGroups order="Explicit">
        <group name="Choice" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="Zed">
              <description>  The Z option.  </description>
              <image path="fomod/z.png"/>
              <conditionFlags><flag name="zed"> yes </flag><flag name=""> ignored </flag></conditionFlags>
              <files><file source="z\\z.esp" destination="z.esp" priority="5"/></files>
              <typeDescriptor><type name="Recommended"/></typeDescriptor>
            </plugin>
            <plugin name="Alpha">
              <typeDescriptor>
                <dependencyType>
                  <defaultType name="Optional"/>
                  <patterns>
                    <pattern>
                      <dependencies operator="Or">
                        <flagDependency flag="zed" value="yes"/>
                        <fileDependency file="Data\\Thing.esp" state="Active"/>
                      </dependencies>
                      <type name="NotUsable"/>
                    </pattern>
                  </patterns>
                </dependencyType>
              </typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
    <installStep name="Second">
      <visible operator="And">
        <flagDependency flag="zed" value="yes"/>
        <dependencies operator="Or">
          <fileDependency file="Other.esm" state="Active"/>
          <fileDependency file="Skipped.esp" state="Inactive"/>
          <foseDependency version="0.1.2"/>
        </dependencies>
      </visible>
      <optionalFileGroups>
        <group name="More" type="SelectAny">
          <plugins><plugin name="B"/><plugin name="A"/></plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
  <conditionalFileInstalls>
    <patterns>
      <pattern>
        <dependencies operator="And"><fileDependency file="Lanterns.esp" state="Active"/></dependencies>
        <files><file source="p\\GP - Lanterns.esp" destination="GP - Lanterns.esp"/></files>
      </pattern>
    </patterns>
  </conditionalFileInstalls>
</config>
"""


def _config(text: str):
    return parse_module_config(text.encode("utf-8"))


# ---------------------------------------------------------------------------- the tree


def test_the_sample_parses_into_the_full_option_tree():
    config = _config(SAMPLE)
    assert config.module_name == "Sample" and not config.warnings and not config.truncated
    assert [(f.kind, f.source, f.resolved, f.priority, f.order) for f in config.required_files] == [
        ("file", "core.esp", "core.esp", 0, 1), ("folder", "meshes", "meshes", 2, 2)]
    assert config.module_dependencies == Composite("And", (VersionCondition("game", "1.5.97"),))

    main, second = config.steps
    assert (main.index, main.name, main.visible) == (0, "Main", None)
    group = main.groups[0]
    assert (group.index, group.name, group.type, group.order) == (0, "Choice", "SelectExactlyOne", "Explicit")
    zed, alpha = group.options
    assert (zed.index, zed.name, zed.description, zed.image) == (0, "Zed", "The Z option.", "fomod/z.png")
    assert zed.flags == (("zed", "yes"), ("", "ignored"))          # the parser keeps them; the replay skips ""
    assert zed.type_info.default == "Recommended" and not zed.type_info.patterns
    assert [(f.source, f.destination, f.priority, f.order) for f in zed.files] == [
        ("z\\z.esp", "z.esp", 5, 3)]
    pattern, kind = alpha.type_info.patterns[0]
    assert kind == "NotUsable"
    assert pattern == Composite("Or", (FlagCondition("zed", "yes"), FileCondition("Data\\Thing.esp", "Active")))

    assert second.visible == Composite("And", (
        FlagCondition("zed", "yes"),
        Composite("Or", (FileCondition("Other.esm", "Active"), FileCondition("Skipped.esp", "Inactive"),
                         VersionCondition("fose", "0.1.2")))))
    install = config.conditional_installs[0]
    assert install.index == 0 and install.condition == Composite("And", (FileCondition("Lanterns.esp", "Active"),))
    assert install.files[0].order == 4


def test_detected_plugins_follow_the_native_rule():
    config = _config(SAMPLE)
    assert collect_detected_plugins(config.steps[1].visible) == ["Other.esm"]    # Active + plugin only
    pattern = config.steps[0].groups[0].options[1].type_info.patterns[0][0]
    assert collect_detected_plugins(pattern) == ["Thing.esp"]                    # file name only
    names: list[str] = []
    collect_detected_plugins(Composite("And", (FileCondition("a.esp", "Active"), FileCondition("A.ESP", "active"),
                                               FileCondition("x.txt", "Active"))), names)
    assert names == ["a.esp"]                                                    # no case-insensitive repeats


def test_display_order_follows_the_order_attributes():
    config = _config(SAMPLE)
    assert [s.name for s in config.display_steps()] == ["Main", "Second"]        # Explicit
    group = config.steps[1].groups[0]
    assert group.order == "Ascending"                                            # the default
    assert [o.name for o in display_options(group)] == ["A", "B"]
    descending = _config(SAMPLE.replace('<plugins><plugin name="B"/>', '<plugins order="Descending"><plugin name="B"/>'))
    assert [o.name for o in display_options(descending.steps[1].groups[0])] == ["B", "A"]
    ordered = _config(SAMPLE.replace('<installSteps order="Explicit">', "<installSteps>"))
    assert [s.name for s in ordered.display_steps()] == ["Main", "Second"]       # Ascending by name
    reverse = _config(SAMPLE.replace('<installSteps order="Explicit">', '<installSteps order="Descending">'))
    assert [s.name for s in reverse.display_steps()] == ["Second", "Main"]
    assert [s.index for s in reverse.steps] == [0, 1]                            # ids stay authored


def test_file_entries_sort_by_priority_then_position():
    config = _config("""<config><installSteps><installStep name="S"><optionalFileGroups>
      <group name="G" type="SelectAny"><plugins><plugin name="P"><files>
        <file source="c" destination="c" priority="3"/>
        <file source="a" destination="a"/>
        <file source="" destination="ignored"/>
        <file source="b" destination="b" priority="3"/>
        <folder source="d"/>
      </files></plugin></plugins></group></optionalFileGroups></installStep></installSteps></config>""")
    files = config.steps[0].groups[0].options[0].files
    assert [(f.source, f.priority, f.order) for f in files] == [("a", 0, 2), ("d", 0, 4), ("c", 3, 1), ("b", 3, 3)]
    assert files[1].destination == "d"          # a missing destination is the source


@pytest.mark.parametrize("raw, expected", [("5", 5), (" 7 ", 7), ("+3", 3), ("-2", -2), ("x", 0), ("", 0),
                                           ("3.5", 0), ("99999999999", 0), ("١٢", 0)])
def test_priority_is_read_like_qstring_toint(raw, expected):
    config = _config(f'<config><requiredInstallFiles><file source="a" priority="{raw}"/></requiredInstallFiles></config>')
    assert config.required_files[0].priority == expected


def test_missing_and_odd_attributes_are_tolerated():
    config = _config("""<config>
      <installSteps order="Sideways"><installStep><optionalFileGroups>
        <group><plugins><plugin/><plugin name="N"><typeDescriptor><type name="Bogus"/></typeDescriptor></plugin></plugins></group>
        <group name="T" type="Nonsense"><plugins order="Weird"><plugin name="x"/></plugins></group>
      </optionalFileGroups></installStep></installSteps></config>""")
    step = config.steps[0]
    assert step.name == "" and step.groups[0].name == "" and step.groups[0].type == "SelectAny"
    assert [o.name for o in step.groups[0].options] == ["", "N"]
    assert step.groups[0].options[1].type_info.default == "Optional"
    assert step.groups[1].type == "SelectAny"
    assert any("unknown" in w for w in config.warnings)
    assert config.step_order == "Ascending"


def test_unknown_elements_and_foreign_namespaces_are_skipped():
    config = _config("""<config xmlns="urn:x"><banner/><installSteps><installStep name="S"><weird>
        <installStep name="nested"/></weird><optionalFileGroups><group name="G" type="SelectAny"><plugins>
        <plugin name="P"><surprise/></plugin></plugins></group></optionalFileGroups></installStep></installSteps></config>""")
    assert [s.name for s in config.steps] == ["S"] and config.steps[0].groups[0].options[0].name == "P"


def test_a_wrong_root_is_an_error():
    with pytest.raises(FomodParseError):
        _config("<Config/>")
    with pytest.raises(FomodParseError):
        _config("<other/>")


# ---------------------------------------------------------------------------- encodings


XML = '<?xml version="1.0" encoding="{enc}"?><config><moduleName>Caf\u00e9 \u00fcber</moduleName></config>'


@pytest.mark.parametrize("label, data", [
    ("utf-8", XML.format(enc="UTF-8").encode("utf-8")),
    ("utf-8 bom", codecs.BOM_UTF8 + XML.format(enc="UTF-8").encode("utf-8")),
    ("utf-16le bom", codecs.BOM_UTF16_LE + XML.format(enc="utf-16").encode("utf-16-le")),
    ("utf-16be bom", codecs.BOM_UTF16_BE + XML.format(enc="utf-16").encode("utf-16-be")),
    ("utf-16le no bom", XML.format(enc="UTF-16").encode("utf-16-le")),
    ("utf-16be no bom", XML.format(enc="UTF-16").encode("utf-16-be")),
    ("declared utf-16, written utf-8", XML.format(enc="utf-16").encode("utf-8")),
    ("declared utf-8, utf-16 with bom", codecs.BOM_UTF16_LE + XML.format(enc="UTF-8").encode("utf-16-le")),
    ("iso-8859-1", XML.format(enc="iso-8859-1").encode("latin-1")),
    ("windows-1252", XML.format(enc="windows-1252").encode("cp1252")),
    ("no declaration, utf-8", b"<config><moduleName>Caf\xc3\xa9 \xc3\xbcber</moduleName></config>"),
    ("no declaration, latin-1", b"<config><moduleName>Caf\xe9 \xfcber</moduleName></config>"),
    ("unknown declared encoding", XML.format(enc="klingon").encode("utf-8")),
])
def test_input_encodings(label, data):
    assert parse_module_config(data).module_name == "Caf\u00e9 \u00fcber", label


def test_garbage_is_an_error_not_a_crash():
    for data in (b"", b"\x00\x01\x02", b"<config>", b"<config></Config>", b"\xff\xfe<\x00", b"not xml at all"):
        with pytest.raises(FomodParseError):
            parse_module_config(data)


# ---------------------------------------------------------------------------- hostile input


BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<config><moduleName>&lol3;</moduleName></config>"""


@pytest.mark.parametrize("text", [
    BILLION_LAUGHS,
    '<!DOCTYPE config><config/>',
    '<!doctype config><config/>',
    '<?xml version="1.0"?><!DOCTYPE config [<!ENTITY x SYSTEM "file:///c:/windows/win.ini">]><config>&x;</config>',
    '<config><!ENTITY x "y"></config>',
])
def test_doctype_and_entity_declarations_are_refused(text):
    with pytest.raises(FomodParseError, match="DOCTYPE or ENTITY"):
        parse_module_config(text.encode("utf-8"))
    with pytest.raises(FomodParseError):
        parse_module_config(codecs.BOM_UTF16_LE + text.encode("utf-16-le"))


def test_doctype_text_inside_comments_and_cdata_is_harmless():
    config = _config("<config><!-- <!DOCTYPE x> <!ENTITY y 'z'> --><moduleName><![CDATA[<!DOCTYPE x>]]></moduleName></config>")
    assert config.module_name == "<!DOCTYPE x>"


def test_scanning_for_declarations_is_linear():
    import time
    hostile = ("<!--" * 400_000).encode("ascii")
    started = time.perf_counter()
    with pytest.raises(FomodParseError):
        parse_module_config(hostile)
    assert time.perf_counter() - started < 5


def test_oversized_nested_and_crowded_documents_are_refused():
    with pytest.raises(FomodParseError, match="byte limit"):
        parse_module_config(b"<config>" + b" " * 200 + b"</config>", ParseLimits(max_bytes=100))
    with pytest.raises(FomodParseError, match="nested"):
        parse_module_config(b"<config>" + b"<a>" * 5000 + b"</a>" * 5000 + b"</config>")
    with pytest.raises(FomodParseError, match="elements"):
        parse_module_config(b"<config>" + b"<a/>" * 5000 + b"</config>", ParseLimits(max_elements=1000))


def test_huge_counts_are_capped_and_noted():
    limits = ParseLimits(max_steps=3, max_groups_per_step=2, max_options_per_group=4, max_files_per_item=2,
                         max_options=100)
    steps = "".join(
        f'<installStep name="s{i}"><optionalFileGroups>'
        + "".join(f'<group name="g{j}" type="SelectAny"><plugins>'
                  + "".join(f'<plugin name="o{k}"><files><file source="a{k}"/><file source="b{k}"/><file source="c{k}"/></files></plugin>'
                            for k in range(10))
                  + "</plugins></group>" for j in range(5))
        + "</optionalFileGroups></installStep>" for i in range(10))
    config = parse_module_config(f"<config><installSteps>{steps}</installSteps></config>".encode(), limits)
    assert config.truncated and config.warnings
    assert len(config.steps) == 3
    assert all(len(step.groups) == 2 for step in config.steps)
    group = config.steps[0].groups[0]
    assert [o.index for o in group.options] == [0, 1, 2, 3]
    assert all(len(o.files) == 2 for o in group.options)
    # `order` still counts every real entry, so it matches the installer's numbering.
    assert [f.order for f in group.options[1].files] == [4, 5]


def test_conditions_are_depth_and_size_limited():
    deep = "<dependencies>" * 40 + "<flagDependency flag='a' value='b'/>" + "</dependencies>" * 40
    config = _config(f"<config><installSteps><installStep name='s'><visible>{deep}</visible></installStep></installSteps></config>")
    assert config.truncated
    wide = "<flagDependency flag='a' value='b'/>" * 500
    config = parse_module_config(
        f"<config><installSteps><installStep name='s'><visible>{wide}</visible></installStep></installSteps></config>".encode(),
        ParseLimits(max_condition_nodes=100))
    assert config.truncated and len(config.steps[0].visible.children) <= 100


# ----------------------------------------------------------------------------- info.xml


def test_info_xml():
    info = parse_info_xml("""<?xml version="1.0"?><fomod><Name>Lux</Name><Author>me</Author>
        <Version MachineVersion="1">2.2</Version><Id>116722</Id><Website>https://x/y</Website>
        <Description>desc</Description></fomod>""".encode())
    assert (info.name, info.author, info.version, info.nexus_id, info.website, info.description) == (
        "Lux", "me", "2.2", 116722, "https://x/y", "desc")
    assert parse_info_xml(b"<fomod/>").nexus_id == -1
    assert parse_info_xml(b"<fomod><Id>abc</Id></fomod>").nexus_id == 0
    assert parse_info_xml(BILLION_LAUGHS.encode()) == fomod_xml.FomodInfo()
    assert parse_info_xml(b"garbage") == fomod_xml.FomodInfo()


# ----------------------------------------------------- destinations: the native test cases


def resolved(source, destination, folder, expect_escape=False):
    path, escapes = resolve_file_destination(source, destination, folder)
    assert escapes == expect_escape
    return path


def test_resolve_file_destination_matches_the_native_cases():
    # the same expectations as testResolveFileDestination in tests/native/previous_choices_test.cpp
    assert resolved("a\\b.dds", "Textures\\Foo\\Bar.dds", False) == "Textures/Foo/Bar.dds"
    assert resolved("2K\\bar.dds", "", False) == "bar.dds"
    assert resolved("2K/bar.dds", "textures/foo/", False) == "textures/foo/bar.dds"
    assert resolved("bar.dds", "textures\\foo\\", False) == "textures/foo/bar.dds"
    assert resolved("2K\\Textures", "Textures\\", True) == "Textures"
    assert resolved("Data", "", True) == ""
    assert resolved("Data", ".", True) == ""
    assert resolved("Data", "/", True) == ""
    assert resolved("x", "/textures//./foo/x.dds", False) == "textures/foo/x.dds"
    assert resolved("core\\x.dds", ".", False) == "x.dds"
    assert resolved("core\\x.dds", "/", False) == "x.dds"
    assert resolved("core\\x.dds", "\\", False) == "x.dds"
    assert resolved("core\\x.dds", "Data", False) == "x.dds"
    assert resolved("core\\x.dds", "Data/", False) == "x.dds"
    assert resolved("a", "Data\\Textures\\a.dds", False) == "Textures/a.dds"
    assert resolved("a", "DATA/textures/a.dds", False) == "textures/a.dds"
    assert resolved("Fix", "data/textures", True) == "textures"
    assert resolved("Fix", "Data", True) == ""
    assert resolved("a", "Database/a.dds", False) == "Database/a.dds"
    assert resolved("a", "textures/data/a.dds", False) == "textures/data/a.dds"
    assert resolved("a", "textures/../meshes/a.nif", False) == "meshes/a.nif"
    for folder in (False, True):
        for hostile in ("../x.dds", "..\\..\\x", "..", "textures/../../x.dds", "C:/Windows/x.dds",
                        "c:\\Windows\\x.dds", "C:x.dds", "\\\\server\\share\\x.dds", "//server/share/x",
                        "/../x.dds", "\\..\\x", "/..", "/a/../../x.dds"):
            assert resolved("a", hostile, folder, expect_escape=True) == ""
    assert resolved("a", "textures/a.dds", False) == "textures/a.dds"
    assert resolved("Data", "", True) == ""


def test_the_resolved_fields_land_on_the_file_entries():
    config = _config("""<config><requiredInstallFiles>
        <file source="p\\x.esp" destination="Data\\"/>
        <folder source="f" destination="..\\out"/></requiredInstallFiles></config>""")
    first, second = config.required_files
    assert (first.resolved, first.escapes) == ("x.esp", False)
    assert (second.resolved, second.escapes) == ("", True)


EXPANDING = ('<!DOCTYPE config [<!ENTITY a0 "aaaaaaaaaa">'
             + "".join(f'<!ENTITY a{i} "{{}}">'.format(f"&a{i - 1};" * 10) for i in range(1, 6))
             + "]>")


@pytest.mark.parametrize("text", [
    # a comment opener inside a processing instruction must not hide the DOCTYPE after it
    "<?x <!-- ?>" + EXPANDING + "<!-- --><config><moduleName>&a5;</moduleName></config>",
    # a comment opener inside the XML declaration (checked after the declaration is dropped)
    '<?xml version="1.0" foo="<!--"?>' + EXPANDING + "<!-- --><config><moduleName>&a5;</moduleName></config>",
    # the same two with a processing instruction in the middle of the document
    "<config><?x <!-- ?></config>" + "<!DOCTYPE x>",
    '<?xml version="1.0" foo=">"?>' + EXPANDING + "<config>&a5;</config>",
    "<?xml-stylesheet <!-- ?>" + EXPANDING + "<!-- --><config>&a5;</config>",
])
def test_doctype_cannot_hide_behind_a_processing_instruction_or_the_declaration(text):
    with pytest.raises(FomodParseError):
        parse_module_config(text.encode("utf-8"))
    with pytest.raises(FomodParseError):
        parse_module_config(codecs.BOM_UTF16_LE + text.encode("utf-16-le"))


def test_processing_instructions_and_declarations_are_otherwise_fine():
    config = _config('<?xml version="1.0" encoding="UTF-8"?><?note <!-- not a comment ?><config><moduleName>ok</moduleName></config>')
    assert config.module_name == "ok"
