from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from fomod_logger_support.core.storage import (
    CURRENT_DATABASE_VERSION,
    DatabaseVersionError,
    EventStore,
    EventValidationError,
    IngestionSummary,
    validate_event,
)


def _write(directory: Path, event: dict, name: str | None = None) -> Path:
    path = directory / f"{name or event['event_id']}.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda event: event.update(event_id="not-a-uuid"), "event_id"),
        (lambda event: event.update(started_at="2026-08-26 12:00:00"), "started_at"),
        (lambda event: event["instance"].update(id="short"), "instance.id"),
        (lambda event: event["mod"].update(nexus_id=True), "nexus_id"),
        (lambda event: event["installer"].update(upstream_commit="main"), "upstream_commit"),
        (lambda event: event["steps"][0]["groups"][0].update(type="Radio"), "type is invalid"),
        (lambda event: event["steps"][0]["groups"][1].update(id="step:0"), "duplicate identity"),
        (lambda event: event["mod"].pop("version"), "missing properties: version"),
        (lambda event: event.update(schema_version=2), "schema_version"),
    ],
)
def test_validator_rejects_broken_events(installation_event, mutate, message):
    mutate(installation_event)

    with pytest.raises(EventValidationError, match=message):
        validate_event(installation_event)


def test_unknown_fields_from_newer_builds_are_accepted_and_kept(tmp_path, installation_event):
    installation_event["future_field"] = {"anything": 1}
    installation_event["steps"][0]["groups"][0]["options"][0]["image"] = "x.png"

    with EventStore(tmp_path / "db") as store:
        assert store.ingest(installation_event) is True
        [stored] = store.latest()

    assert stored["future_field"] == {"anything": 1}
    assert stored["steps"][0]["groups"][0]["options"][0]["image"] == "x.png"


def test_ingestion_is_idempotent_and_ids_are_immutable(tmp_path, installation_event):
    with EventStore(tmp_path / "events.sqlite3") as store:
        assert store.ingest(installation_event) is True
        assert store.ingest(deepcopy(installation_event)) is False
        changed = deepcopy(installation_event)
        changed["mod"]["version"] = "9.9"
        with pytest.raises(EventValidationError, match="conflicts"):
            store.ingest(changed)
        assert store.event_count() == 1


def test_directory_scan_imports_once_and_never_rereads_known_files(
    tmp_path, clone_event, monkeypatch
):
    events = tmp_path / "events"
    events.mkdir()
    first = clone_event()
    second = clone_event()
    second["event_id"] = "22222222-2222-4222-8222-222222222222"
    _write(events, first)
    _write(events, second)

    with EventStore(tmp_path / "db") as store:
        assert store.ingest_directory(events) == IngestionSummary(2, 2, 0, 0)
        reads = []
        original = Path.read_bytes
        monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self) or original(self))
        assert store.ingest_directory(events) == IngestionSummary(2, 0, 2, 0)
        assert reads == []
        assert store.event_count() == 2


def test_directory_errors_are_reported_and_clear_when_fixed_or_deleted(
    tmp_path, clone_event
):
    events = tmp_path / "events"
    events.mkdir()
    good = clone_event()
    broken = clone_event()
    broken["event_id"] = "22222222-2222-4222-8222-222222222222"
    broken["mod"]["nexus_id"] = 2**63
    broken_path = _write(events, broken)
    junk = events / "33333333-3333-4333-8333-333333333333.json"
    junk.write_text("not json", encoding="utf-8")
    misnamed = clone_event()
    misnamed["event_id"] = "44444444-4444-4444-8444-444444444444"
    _write(events, misnamed, name="renamed-by-hand")
    _write(events, good)

    with EventStore(tmp_path / "db") as store:
        assert store.ingest_directory(events) == IngestionSummary(4, 1, 0, 3)
        problems = {Path(item["path"]).name: item["error"] for item in store.import_errors()}
        assert set(problems) == {broken_path.name, junk.name, "renamed-by-hand.json"}
        assert "signed 64-bit" in problems[broken_path.name]
        assert "file name" in problems["renamed-by-hand.json"]

        broken["mod"]["nexus_id"] = 1
        _write(events, broken)
        junk.unlink()
        (events / "renamed-by-hand.json").unlink()
        assert store.ingest_directory(events) == IngestionSummary(2, 1, 1, 0)
        assert store.import_errors() == []


def test_new_database_is_stamped_and_reopens(tmp_path, installation_event):
    database = tmp_path / "db"
    with EventStore(database) as store:
        store.ingest(installation_event)
    with EventStore(database) as store:
        assert store.event_count() == 1
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == (
            CURRENT_DATABASE_VERSION)


@pytest.mark.parametrize("version", [0, 1, 4])
def test_unsupported_test_database_fails_clearly_and_is_left_unchanged(tmp_path, version):
    database = tmp_path / "fomod_logger.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO events VALUES ('kept')")
        connection.execute(f"PRAGMA user_version = {version}")
    connection.close()
    before = database.read_bytes()

    with pytest.raises(DatabaseVersionError, match="test build.*rename or move"):
        EventStore(database)

    assert database.read_bytes() == before


def test_newer_database_version_fails_closed(tmp_path):
    database = tmp_path / "db"
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version = {CURRENT_DATABASE_VERSION + 1}")
    connection.close()

    with pytest.raises(DatabaseVersionError, match="newer Modlist Companion"):
        EventStore(database)


def test_hidden_events_are_filtered_but_exportable(tmp_path, installation_event):
    with EventStore(tmp_path / "events.sqlite3") as store:
        store.ingest(installation_event)
        assert store.set_hidden(installation_event["event_id"], True) is True
        assert store.set_hidden("missing", True) is False
        assert store.latest() == []
        assert store.history() == []
        assert store.latest(include_hidden=True)[0]["hidden"] is True
        assert store.full_history_export()["events"][0]["hidden"] is True


def test_rename_moves_the_current_record_without_rewriting_evidence(tmp_path, clone_event):
    older = clone_event()
    newer = clone_event()
    newer["event_id"] = "22222222-2222-4222-8222-222222222222"
    newer["completed_at"] = "2026-08-26T13:00:00+00:00"
    instance = "a" * 64

    with EventStore(tmp_path / "db") as store:
        store.ingest(older)
        store.ingest(newer)
        assert store.rename_mod(instance, "fixture mod", "Fixture Mod (renamed)") is True
        assert store.current_mod_names(instance) == ["Fixture Mod (renamed)"]
        [current] = store.latest("renamed")
        assert current["event_id"] == newer["event_id"]
        assert current["mod"]["recorded_name"] == "Fixture Mod"
        # Every older record moved too, so none resurfaces under the old name.
        assert all(e["mod"]["name"] == "Fixture Mod (renamed)" for e in store.history())
        raw = store._connection.execute(
            "SELECT raw_json FROM events WHERE event_id = ?", (newer["event_id"],)).fetchone()[0]
        assert json.loads(raw)["mod"]["name"] == "Fixture Mod"

        # Renaming back clears the override; removal follows the new name.
        assert store.rename_mod(instance, "Fixture Mod (renamed)", "Fixture Mod") is True
        assert "recorded_name" not in store.latest()[0]["mod"]
        assert store.rename_mod(instance, "Not logged", "Anything") is False
        assert store.mark_removed(instance, "FIXTURE MOD", "2026-08-27T00:00:00Z") is True
        assert store.rename_mod(instance, "Fixture Mod", "Too late") is False


def test_removed_latest_event_stays_in_history_and_does_not_reveal_older_current(
    tmp_path, installation_event
):
    older = deepcopy(installation_event)
    newer = deepcopy(installation_event)
    newer["event_id"] = "22222222-2222-4222-8222-222222222222"
    newer["completed_at"] = "2026-08-26T13:00:00+00:00"
    reinstalled = deepcopy(installation_event)
    reinstalled["event_id"] = "33333333-3333-4333-8333-333333333333"
    reinstalled["completed_at"] = "2026-08-26T14:00:00+00:00"
    removed_at = "2026-08-26T13:30:00+00:00"

    with EventStore(tmp_path / "events.sqlite3") as store:
        store.ingest(older)
        store.ingest(newer)
        assert store.current_mod_names("a" * 64) == ["Fixture Mod"]
        assert store.mark_removed("a" * 64, "fixture mod", removed_at) is True
        assert store.mark_removed("a" * 64, "Fixture Mod", removed_at) is False
        assert store.latest() == []
        assert store.current_mod_names("a" * 64) == []

        history = store.history()
        assert [(e["event_id"], e["removed_at"]) for e in history] == [
            (newer["event_id"], removed_at), (older["event_id"], None)]

        store.ingest(reinstalled)
        assert store.latest()[0]["event_id"] == reinstalled["event_id"]
        assert store.current_mod_names("a" * 64) == ["Fixture Mod"]


def test_full_history_export_preserves_overlays_and_holds_one_snapshot(
    tmp_path, installation_event, monkeypatch
):
    database = tmp_path / "events.sqlite3"
    second = deepcopy(installation_event)
    second["event_id"] = "22222222-2222-4222-8222-222222222222"
    second["mod"]["name"] = "Second Mod"
    late = deepcopy(installation_event)
    late["event_id"] = "33333333-3333-4333-8333-333333333333"
    late["mod"]["name"] = "Late Mod"
    removed_at = "2026-08-26T13:30:00+00:00"

    with EventStore(database) as store:
        store.ingest(installation_event)
        store.ingest(second)
        store.set_hidden(installation_event["event_id"], True)
        store.mark_removed("a" * 64, second["mod"]["name"], removed_at)
        original_history = store.history

        def history_then_mutate(*args, **kwargs):
            result = original_history(*args, **kwargs)
            with EventStore(database) as writer:
                writer.ingest(late)
            return result

        monkeypatch.setattr(store, "history", history_then_mutate)
        exported = store.full_history_export()

    assert exported["format"] == "fomod-logger-full-history"
    assert exported["format_version"] == 2
    by_id = {event["event_id"]: event for event in exported["events"]}
    assert set(by_id) == {installation_event["event_id"], second["event_id"]}
    assert by_id[installation_event["event_id"]]["hidden"] is True
    assert by_id[second["event_id"]]["removed_at"] == removed_at
    assert exported["current_cross_patches"] == []
    with EventStore(database) as store:
        assert store.event_count() == 3


def test_cross_patch_search_uses_unicode_casefold(tmp_path, installation_event):
    first = deepcopy(installation_event)
    second = deepcopy(installation_event)
    second["event_id"] = "22222222-2222-4222-8222-222222222222"
    second["mod"]["name"] = "Second Mod"
    for event in (first, second):
        for group in event["steps"][0]["groups"]:
            for option in group["options"]:
                option["name"] = "Straße Compatibility"

    with EventStore(tmp_path / "events.sqlite3") as store:
        store.ingest(first)
        store.ingest(second)
        assert [p["option"] for p in store.cross_patches("STRASSE")] == ["Straße Compatibility"]
        assert [p["option"] for p in store.cross_patches("second mod")] == ["Straße Compatibility"]
        assert store.cross_patches("not present") == []


def test_instances_use_the_newest_label(tmp_path, clone_event):
    first = clone_event()
    renamed = clone_event()
    renamed["event_id"] = "22222222-2222-4222-8222-222222222222"
    renamed["completed_at"] = "2026-08-26T13:00:00+00:00"
    renamed["instance"]["label"] = "Renamed MO2"
    with EventStore(tmp_path / "db") as store:
        store.ingest(first)
        store.ingest(renamed)
        assert store.instances() == [{"id": "a" * 64, "label": "Renamed MO2"}]


def test_current_mods_reports_archive_and_first_install_since_last_removal(tmp_path, clone_event):
    first = clone_event()
    first["started_at"] = "2026-01-01T00:00:00+00:00"
    first["completed_at"] = "2026-01-01T00:01:00+00:00"
    reinstall = clone_event()
    reinstall["event_id"] = "22222222-2222-4222-8222-222222222222"
    reinstall["started_at"] = "2026-02-01T00:00:00+00:00"
    reinstall["completed_at"] = "2026-02-01T00:01:00+00:00"
    update = clone_event()
    update["event_id"] = "33333333-3333-4333-8333-333333333333"
    update["started_at"] = "2026-03-01T00:00:00+00:00"
    update["completed_at"] = "2026-03-01T00:01:00+00:00"
    update["source"]["archive"] = "fixture-v2.zip"
    with EventStore(tmp_path / "db") as store:
        store.ingest(first)
        store.mark_removed("a" * 64, "Fixture Mod", "2026-01-15T00:00:00Z")
        store.ingest(reinstall)
        store.ingest(update)
        assert store.current_mods("a" * 64) == [{
            "name": "Fixture Mod", "archive": "fixture-v2.zip",
            "first_started_at": "2026-02-01T00:00:00+00:00", "kind": "fomod"}]


def test_invisible_step_cannot_contain_selected_option(installation_event):
    installation_event["steps"][0]["visible"] = False

    with pytest.raises(EventValidationError, match="invisible step"):
        validate_event(installation_event)


def test_search_matches_step_group_and_option_headers(tmp_path, clone_event):
    event = clone_event()
    with EventStore(tmp_path / "events.sqlite3") as store:
        store.ingest(event)
        assert store.latest("main")[0]["event_id"] == event["event_id"]
        assert store.latest("second group")[0]["event_id"] == event["event_id"]
        assert store.latest("shared label")[0]["event_id"] == event["event_id"]
        assert store.latest("not present") == []


def test_latest_is_scoped_by_instance_and_preserves_history(tmp_path, clone_event):
    first = clone_event()
    newer = clone_event()
    newer["event_id"] = "22222222-2222-4222-8222-222222222222"
    newer["completed_at"] = "2026-08-26T13:00:00+00:00"
    newer["mod"]["version"] = "2.0"
    other_instance = clone_event()
    other_instance["event_id"] = "33333333-3333-4333-8333-333333333333"
    other_instance["instance"] = {"id": "b" * 64, "label": "Second Instance"}

    with EventStore(tmp_path / "events.sqlite3") as store:
        for event in (first, newer, other_instance):
            store.ingest(event)
        latest = store.latest()
        history = store.history()
        assert store.event_count() == 3

    assert {event["event_id"] for event in latest} == {
        newer["event_id"],
        other_instance["event_id"],
    }
    assert [event["event_id"] for event in history] == [
        newer["event_id"],
        other_instance["event_id"],
        first["event_id"],
    ]


def test_history_search_includes_matching_superseded_event(tmp_path, clone_event):
    older = clone_event()
    older["steps"][0]["name"] = "Legacy Header"
    newer = clone_event()
    newer["event_id"] = "22222222-2222-4222-8222-222222222222"
    newer["completed_at"] = "2026-08-26T13:00:00+00:00"
    newer["steps"][0]["name"] = "Current Header"

    with EventStore(tmp_path / "events.sqlite3") as store:
        store.ingest(older)
        store.ingest(newer)

        assert store.latest("legacy header") == []
        assert [event["event_id"] for event in store.history("legacy header")] == [
            older["event_id"]
        ]


def test_current_and_history_order_timestamps_by_instant(tmp_path, clone_event):
    earlier = clone_event()
    earlier["started_at"] = "2026-01-01T00:59:00+02:00"
    earlier["completed_at"] = "2026-01-01T01:00:00+02:00"
    later = clone_event()
    later["event_id"] = "22222222-2222-4222-8222-222222222222"
    later["started_at"] = "2026-01-01T00:29:00+00:00"
    later["completed_at"] = "2026-01-01T00:30:00+00:00"

    with EventStore(tmp_path / "events.sqlite3") as store:
        store.ingest(earlier)
        store.ingest(later)

        assert [event["event_id"] for event in store.latest()] == [
            later["event_id"]
        ]
        assert [event["event_id"] for event in store.history()] == [
            later["event_id"],
            earlier["event_id"],
        ]


@pytest.mark.parametrize("state", ["selected", "unselected", "unavailable", "step_not_shown"])
def test_literal_search_finds_unique_choices_in_all_recorded_states(tmp_path, installation_event, state):
    option = installation_event["steps"][0]["groups"][0]["options"][0]
    option["name"] = "Unique <patch> 100%_literal [a-z].*"
    option["selected"] = state == "selected"
    option["enabled"] = state != "unavailable"
    if state == "step_not_shown":
        installation_event["steps"][0]["visible"] = False
    with EventStore(tmp_path / "db") as store:
        store.ingest(installation_event)
        for method in (store.latest, store.history):
            matches = method("100%_literal [a-z].*")
            assert len(matches) == 1
            assert matches[0]["steps"][0]["groups"][0]["options"][0] == option
            assert method("100%_literal [a-z].*extra") == []



def test_search_uses_unicode_nfkc_casefold_and_literal_wildcards(
    tmp_path, installation_event
):
    unicode_event = deepcopy(installation_event)
    unicode_event["mod"]["name"] = "Straße Visuals"
    unicode_event["steps"][0]["name"] = "Ｍａｉｎ Header"
    unicode_event["steps"][0]["groups"][0]["name"] = "Grüße Group"
    unicode_event["steps"][0]["groups"][0]["options"][0]["name"] = "100%_Ready"

    plain_event = deepcopy(installation_event)
    plain_event["event_id"] = "22222222-2222-4222-8222-222222222222"
    plain_event["mod"]["name"] = "Plain Mod"

    with EventStore(tmp_path / "events.sqlite3") as store:
        store.ingest(unicode_event)
        store.ingest(plain_event)

        for query in (
            "Straße",
            "STRASSE",
            "  STRASSE  ",
            "main header",
            "GRÜSSE GROUP",
        ):
            assert [event["event_id"] for event in store.latest(query)] == [
                unicode_event["event_id"]
            ]
        assert [event["event_id"] for event in store.latest("%")] == [
            unicode_event["event_id"]
        ]
        assert [event["event_id"] for event in store.history("_")] == [
            unicode_event["event_id"]
        ]


def _public_schema() -> dict:
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "installation-event.schema.json"
    )
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _public_schema_errors(event: dict) -> list:
    schema = _public_schema()
    Draft202012Validator.check_schema(schema)
    return list(Draft202012Validator(schema).iter_errors(event))


def test_public_schema_requires_nonempty_nested_ids():
    schema = _public_schema()

    assert schema["$defs"]["step"]["properties"]["id"]["minLength"] == 1
    assert schema["$defs"]["group"]["properties"]["id"]["minLength"] == 1
    assert schema["$defs"]["option"]["properties"]["id"]["minLength"] == 1


@pytest.mark.parametrize("nexus_id", [-(2**63) - 1, 2**63])
def test_public_schema_rejects_nexus_ids_outside_sqlite_integer_range(
    installation_event, nexus_id
):
    installation_event["mod"]["nexus_id"] = nexus_id

    with pytest.raises(EventValidationError, match="signed 64-bit"):
        validate_event(installation_event)

    errors = _public_schema_errors(installation_event)
    assert len(errors) == 1
    assert errors[0].validator in {"minimum", "maximum"}
    assert list(errors[0].absolute_path) == ["mod", "nexus_id"]


@pytest.mark.parametrize("nexus_id", [-(2**63), 2**63 - 1])
def test_public_schema_accepts_sqlite_integer_boundaries(
    installation_event, nexus_id
):
    installation_event["mod"]["nexus_id"] = nexus_id

    assert validate_event(installation_event) is installation_event
    assert _public_schema_errors(installation_event) == []


@pytest.mark.parametrize("selected_group_index", [0, 1])
def test_public_schema_rejects_selected_option_in_any_invisible_step_group(
    installation_event, selected_group_index
):
    step = installation_event["steps"][0]
    step["visible"] = False
    for group in step["groups"]:
        for option in group["options"]:
            option["selected"] = False
    step["groups"][selected_group_index]["options"][0]["selected"] = True

    with pytest.raises(EventValidationError, match="invisible step"):
        validate_event(installation_event)

    errors = _public_schema_errors(installation_event)
    assert len(errors) == 1
    assert errors[0].validator == "const"
    assert list(errors[0].absolute_path) == [
        "steps",
        0,
        "groups",
        selected_group_index,
        "options",
        0,
        "selected",
    ]


def test_public_schema_accepts_unselected_options_in_invisible_step(
    installation_event,
):
    step = installation_event["steps"][0]
    step["visible"] = False
    for group in step["groups"]:
        for option in group["options"]:
            option["selected"] = False

    assert validate_event(installation_event) is installation_event
    assert _public_schema_errors(installation_event) == []


def test_concurrent_opens_of_a_new_database_all_succeed(tmp_path):
    import threading

    for attempt in range(10):
        database = tmp_path / f"race-{attempt}.sqlite3"
        start = threading.Barrier(4)
        failures = []

        def open_store():
            start.wait()
            try:
                with EventStore(database) as store:
                    store.event_count()
            except Exception as exc:  # noqa: BLE001 - collected for the assertion
                failures.append(exc)

        threads = [threading.Thread(target=open_store) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert failures == []


def _back_to_version_7(connection) -> None:
    """Take the plain-record additions (version 8) off a database, as the last release left it."""

    connection.execute("DROP TABLE plain_files")
    connection.execute("DROP INDEX events_kind")
    connection.execute("ALTER TABLE events DROP COLUMN kind")
    _back_to_version_9(connection)


def _back_to_version_9(connection) -> None:
    """Take the change history's additions (version 10) off a database, as the last release left it."""

    for name in ("history_when", "history_kind", "history_event", "events_mod_name", "events_override_name"):
        connection.execute(f"DROP INDEX IF EXISTS {name}")
    connection.execute("DROP TABLE IF EXISTS history")
    connection.execute("DROP TABLE IF EXISTS history_snapshots")


def test_a_version_5_database_gains_the_seen_table_and_keeps_its_data(tmp_path, installation_event):
    from fomod_logger_support.core import storage

    database = tmp_path / "v5.db"
    with sqlite3.connect(database) as connection:  # a database as the first release wrote it
        for statement in storage._MIGRATIONS[0]:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 5")
    connection.close()
    with EventStore(database) as store:
        assert store.ingest(installation_event)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE changes_seen")
        connection.execute("DROP TABLE patch_alerts")
        _back_to_version_7(connection)
        connection.execute("PRAGMA user_version = 5")
    connection.close()

    with EventStore(database) as store:  # migrated in place
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_DATABASE_VERSION
        assert store.event_count() == 1 and store.changes_seen() == set()
        assert store.set_changes_seen(installation_event["event_id"], True) is True
    with EventStore(database) as store:
        assert store.changes_seen() == {installation_event["event_id"]}
        assert store.set_changes_seen(installation_event["event_id"], False) is True
        assert store.changes_seen() == set()
        assert store.set_changes_seen("00000000-0000-4000-8000-000000000000", True) is False


def test_a_version_6_database_gains_the_alerts_table_additively(tmp_path, installation_event):
    from fomod_logger_support.core import storage

    database = tmp_path / "v6.db"
    with EventStore(database) as store:
        assert store.ingest(installation_event)
        store.set_changes_seen(installation_event["event_id"], True)
    with sqlite3.connect(database) as connection:  # a database as the last release wrote it
        connection.execute("DROP TABLE patch_alerts")
        _back_to_version_7(connection)
        connection.execute("PRAGMA user_version = 6")
    connection.close()
    assert storage.CURRENT_DATABASE_VERSION == 10

    with EventStore(database) as store:  # migrated in place, nothing lost
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_DATABASE_VERSION
        assert store.event_count() == 1 and store.changes_seen() == {installation_event["event_id"]}
        assert store.patch_alerts() == []


def test_a_version_7_database_gains_plain_records_additively(tmp_path, installation_event):
    database = tmp_path / "v7.db"
    with EventStore(database) as store:
        assert store.ingest(installation_event)
        store.mark_removed(installation_event["instance"]["id"], "Fixture Mod", "2026-10-21T10:00:00+00:00")
    with sqlite3.connect(database) as connection:  # a database as the last release wrote it
        _back_to_version_7(connection)
        connection.execute("PRAGMA user_version = 7")
    connection.close()

    with EventStore(database) as store:  # migrated in place: the event is still there, an installer's, removed
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_DATABASE_VERSION
        row = store._connection.execute("SELECT kind, removed_at FROM events").fetchone()
        assert (row["kind"], row["removed_at"]) == ("fomod", "2026-10-21T10:00:00+00:00")
        assert store.plain_files(installation_event["event_id"]) == []
        assert store.current_mods(installation_event["instance"]["id"]) == []


def test_a_version_8_database_moves_its_plain_file_lists_over_and_keeps_every_record(tmp_path):
    """Version 8 (a test build) kept the file lists with a text event id, the path and two copies of it, and three
    indexes; version 9 keeps (the event's rowid, the path) in one index."""

    from test_plain_records import make_mod, plain_event

    database = tmp_path / "v8.db"
    first = plain_event(tmp_path, files={"A.esp": b"x", "Textures/B/c.dds": "x", "meshes/d.nif": "x"})
    second = plain_event(tmp_path, name="Other", files={"o.txt": "x"})
    with EventStore(database) as store:
        store.ingest(first)
        store.ingest(second)
        wanted = {event["event_id"]: store.plain_files(event["event_id"]) for event in (first, second)}
    with sqlite3.connect(database) as connection:      # the shape version 8 wrote
        rows = [(event_id, path) for event_id, paths in wanted.items() for path in paths]
        _back_to_version_9(connection)
        connection.execute("DROP TABLE plain_files")
        connection.execute("CREATE TABLE plain_files (event_id TEXT NOT NULL, path TEXT NOT NULL, key TEXT NOT NULL, name TEXT NOT NULL)")
        connection.executemany("INSERT INTO plain_files VALUES (?, ?, ?, ?)",
                               [(e, p, p.lower(), p.lower().rpartition("/")[2]) for e, p in rows])
        for name, column in (("plain_files_event", "event_id"), ("plain_files_key", "key"), ("plain_files_name", "name")):
            connection.execute(f"CREATE INDEX {name} ON plain_files ({column})")
        connection.execute("PRAGMA user_version = 8")
    connection.close()

    with EventStore(database) as store:
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_DATABASE_VERSION
        assert store.event_count() == 2
        for event_id, paths in wanted.items():
            assert sorted(store.plain_files(event_id)) == sorted(paths) and paths
        assert not store._connection.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'plain_files_%'").fetchall()
        assert [m["mod_name"] for m in store.file_origins("textures/b/C.dds")["matches"]] == ["Lantern"]
        assert store.ingest(plain_event(tmp_path, name="After", files={"z.txt": "x"})) is True       # and it takes new ones


def test_patch_alerts_are_kept_cleared_and_forgotten_per_mod(tmp_path, installation_event):
    instance = installation_event["instance"]["id"]
    event_id = installation_event["event_id"]
    keys = [(event_id, "step:0/group:1/option:0"), (event_id, "step:0/group:0/option:0")]
    with EventStore(tmp_path / "db") as store:
        store.ingest(installation_event)
        assert store.add_patch_alerts(instance, "Village", "", keys) == keys
        # The same mod again (any spelling) doesn't announce the same patches twice; another mod does.
        assert store.add_patch_alerts(instance, "village", "", keys) == []
        assert store.add_patch_alerts(instance, "Other", "", keys[:1]) == keys[:1]
        rows = store.patch_alerts(instance)
        assert sorted((row["mod_name"], row["option_id"]) for row in rows) == sorted([
            ("Village", keys[0][1]), ("Village", keys[1][1]), ("Other", keys[0][1])])
        assert store.patch_alerts("b" * 64) == []

        assert store.clear_patch_alerts([row for row in rows if row["mod_name"] == "Village"]) == 2
        assert store.clear_patch_alerts([row for row in rows if row["mod_name"] == "Village"][:1]) == 0  # already cleared
        assert [row["mod_name"] for row in store.patch_alerts(instance)] == ["Other"]
        assert len(store.patch_alerts(instance, pending=False)) == 3
        # A cleared row still stops a repeat announcement, until the mod is removed.
        assert store.add_patch_alerts(instance, "Village", "", keys) == []
        assert store.forget_patch_alerts(instance, "VILLAGE") == 2
        assert store.add_patch_alerts(instance, "Village", "", keys) == keys

        with pytest.raises(EventValidationError):
            store.add_patch_alerts("not an id", "Village", "", keys)


def test_removing_and_renaming_in_one_connection_never_reads_stale_rows(tmp_path, installation_event):
    instance = installation_event["instance"]["id"]
    with EventStore(tmp_path / "db") as store:
        store.ingest(installation_event)
        assert store.newest_for_mod(instance, "Fixture Mod")["removed_at"] is None
        assert store.rename_mod(instance, "Fixture Mod", "Renamed") is True
        assert store.newest_for_mod(instance, "Renamed")["mod"]["name"] == "Renamed"
        assert store.newest_for_mod(instance, "Fixture Mod") is None
        assert store.mark_removed(instance, "Renamed", "2026-10-21T10:00:00+00:00") is True
        assert store.mark_removed(instance, "Renamed", "2026-10-21T10:00:01+00:00") is False
        assert store.newest_for_mod(instance, "Renamed")["removed_at"] == "2026-10-21T10:00:00+00:00"
