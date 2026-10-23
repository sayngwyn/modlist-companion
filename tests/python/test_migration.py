from __future__ import annotations

import json
from pathlib import Path

import pytest

from fomod_logger_support.core.identity import stable_instance_id
from fomod_logger_support.core.migration import legacy_entry_to_event, load_legacy_file
from fomod_logger_support.core.storage import EventStore, validate_event


def test_legacy_conversion_is_deterministic_and_keeps_duplicate_labels():
    entry = {
        "mod_name": "Old Logger Record",
        "version": "1.2",
        "timestamp": "2025-01-02T03:04:05+00:00",
        "options": [
            {"step": "Setup", "group": "A", "option": "Same", "selected": True},
            {"step": "Setup", "group": "B", "option": "Same", "selected": False},
        ],
    }
    first = legacy_entry_to_event(entry, instance_id="c" * 64, instance_label="Old")
    second = legacy_entry_to_event(entry, instance_id="c" * 64, instance_label="Old")

    assert first == second
    assert validate_event(first) is first
    option_ids = [
        option["id"]
        for group in first["steps"][0]["groups"]
        for option in group["options"]
    ]
    assert option_ids == [
        "step:0/group:0/option:0",
        "step:0/group:1/option:0",
    ]


def test_legacy_file_loads_and_ingests(tmp_path):
    legacy_path = tmp_path / "fomod_data.json"
    legacy_path.write_text(
        json.dumps(
            {
                "mods": [
                    {
                        "mod_name": "Migrated Mod",
                        "version": "4",
                        "timestamp": "2025-06-07 08:09:10",
                        "options": [],
                    }
                ],
                "cross_patches": [],
            }
        ),
        encoding="utf-8",
    )
    instance_path = tmp_path / "instance"
    instance_path.mkdir()
    events = load_legacy_file(
        legacy_path, instance_path=instance_path, instance_label="Migrated"
    )
    expected_id = stable_instance_id(instance_path)

    assert events[0]["instance"]["id"] == expected_id
    with EventStore(tmp_path / "events.sqlite3") as store:
        assert store.ingest(events[0]) is True
        assert store.latest()[0]["mod"]["name"] == "Migrated Mod"


def test_legacy_migration_uses_native_derived_windows_instance_identity(tmp_path):
    legacy_path = tmp_path / "fomod_data.json"
    legacy_path.write_text('{"mods": []}', encoding="utf-8")

    events = load_legacy_file(
        legacy_path,
        instance_path=Path(r"C:\Temp\Straße"),
        instance_label="Unicode fixture",
    )
    assert events == []

    legacy_path.write_text(
        '{"mods": [{"mod_name": "Native identity fixture"}]}',
        encoding="utf-8",
    )
    events = load_legacy_file(
        legacy_path,
        instance_path=Path(r"C:\Temp\Straße"),
        instance_label="Unicode fixture",
    )

    assert events[0]["instance"]["id"] == (
        "308e58a2f2e8b009e6e5ee06a49b8d20f245a5899ea83aa7c53d495ae64d765b"
    )


@pytest.mark.parametrize(
    "payload,bad_index",
    [
        ({"mods": [42]}, 0),
        ({"mods": [{"mod_name": "valid"}, "not an object"]}, 1),
        ([{"mod_name": "valid"}, None], 1),
    ],
)
def test_legacy_file_rejects_non_object_entries(tmp_path, payload, bad_index):
    legacy_path = tmp_path / "fomod_data.json"
    legacy_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ValueError, match=rf"legacy mods\[{bad_index}\] must be an object"
    ):
        load_legacy_file(
            legacy_path,
            instance_path=tmp_path / "instance",
            instance_label="Migrated",
        )


def test_legacy_object_requires_explicit_mods_array(tmp_path):
    legacy_path = tmp_path / "fomod_data.json"
    legacy_path.write_text(
        json.dumps({"cross_patches": [{"option": "orphan"}]}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="must contain a mods array"):
        load_legacy_file(
            legacy_path,
            instance_path=tmp_path / "instance",
            instance_label="Migrated",
        )


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"not": "an array"}, r"mods\[0\]\.options must be an array"),
        ([42], r"mods\[0\]\.options\[0\] must be an object"),
    ],
)
def test_legacy_file_rejects_malformed_options(tmp_path, options, message):
    legacy_path = tmp_path / "fomod_data.json"
    legacy_path.write_text(
        json.dumps({"mods": [{"mod_name": "Fixture", "options": options}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_legacy_file(
            legacy_path,
            instance_path=tmp_path / "instance",
            instance_label="Migrated",
        )


@pytest.mark.parametrize("selected", ["false", "true", "", 0, 1, None, [], {}])
def test_legacy_selection_rejects_non_boolean_values(tmp_path, selected):
    entry = {
        "mod_name": "Malformed selection",
        "options": [{"step": "S", "group": "G", "option": "O", "selected": selected}],
    }
    with pytest.raises(ValueError, match="selected must be a boolean"):
        legacy_entry_to_event(entry, instance_id="c" * 64, instance_label="Old")

    path = tmp_path / "fomod_data.json"
    path.write_text(json.dumps({"mods": [entry]}), encoding="utf-8")
    with pytest.raises(ValueError, match="selected must be a boolean"):
        load_legacy_file(path, instance_path=tmp_path, instance_label="Old")


@pytest.mark.parametrize("selection", [{"selected": False}, {"selected": True}, {}])
def test_legacy_selection_preserves_booleans_and_missing_default(tmp_path, selection):
    entry = {"mod_name": "Valid selection", "options": [{"option": "O", **selection}]}
    path = tmp_path / "fomod_data.json"
    path.write_text(json.dumps({"mods": [entry]}), encoding="utf-8")
    event = load_legacy_file(path, instance_path=tmp_path, instance_label="Old")[0]
    assert event["steps"][0]["groups"][0]["options"][0]["selected"] is selection.get(
        "selected", False
    )
