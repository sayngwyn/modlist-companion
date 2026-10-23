from copy import deepcopy
import json
import sqlite3
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest

from fomod_logger_support.core.storage import EventStore


def reminder(event, option_id=""):
    return dict(event_id=event["event_id"], option_id=option_id,
                reason="Check this patch after building the list.", state="pending")


@pytest.mark.parametrize("changed", [False, True])
def test_review_lifecycle_keeps_original_across_reinstall(tmp_path, installation_event, changed):
    db = tmp_path / "logger.db"
    original = deepcopy(installation_event)
    with EventStore(db) as store:
        store.ingest(original)
        body = reminder(original, original["steps"][0]["groups"][0]["options"][0]["id"])
        saved = store.save_review(body)
        newer = deepcopy(original)
        newer["event_id"] = str(uuid4())
        newer["completed_at"] = "2026-09-01T12:00:00Z"
        newer["source"]["reinstallation"] = True
        if changed:
            newer["steps"][0]["groups"][0]["options"][0]["selected"] = False
        store.ingest(newer)
        store.set_hidden(original["event_id"], True)
        store.set_hidden(newer["event_id"], True)
        store.mark_removed(newer["instance"]["id"], newer["mod"]["name"], "2026-09-02T12:00:00Z")
    for state in ("pending", "resolved", "dismissed", "pending"):
        with EventStore(db) as store:
            review = store.reviews()[0]
            assert review["state"] == body["state"]
            assert review["review_id"] == saved["review_id"]
            assert review["created_at"] == saved["created_at"]
            assert review["original_event"]["steps"] == original["steps"]
            assert review["original_event"]["hidden"] is True
            assert review["latest_event"]["event_id"] == newer["event_id"]
            assert review["latest_event"]["removed_at"] is not None
            assert review["newer_record"] is True
            assert review["choices_changed"] is changed
            body.update(state=state, reason=f"Keep note: {state}")
            store.save_review(body)
            exported = store.full_history_export()
            assert exported["format_version"] == 2
            assert exported["reviews"][0]["reason"] == body["reason"]
            assert len(exported["events"]) == 2
            assert json.loads(store._connection.execute(
                "SELECT raw_json FROM events WHERE event_id = ?", (original["event_id"],)
            ).fetchone()[0]) == original


def test_review_instance_scope_and_original_option_validation(tmp_path, clone_event):
    a, b = clone_event(), clone_event()
    b["event_id"] = str(uuid4())
    b["instance"]["id"] = "b" * 64
    with EventStore(tmp_path / "db") as store:
        store.ingest(a)
        store.ingest(b)
        store.save_review(reminder(a))
        store.save_review(reminder(b))
        assert len(store.reviews()) == 2
        assert not any(r["newer_record"] for r in store.reviews())
        for bad in (dict(reminder(a), state="done"), dict(reminder(a), reason="x" * 2001),
                    dict(reminder(a), option_id="missing"), dict(reminder(a), event_id=str(uuid4()))):
            with pytest.raises(ValueError):
                store.save_review(bad)


def test_failed_migration_leaves_no_partial_schema(tmp_path, installation_event, monkeypatch):
    from fomod_logger_support.core import storage
    db = tmp_path / "new.db"
    broken = (storage._MIGRATIONS[0] + ("CREATE TABLE broken (",),)
    monkeypatch.setattr(storage, "_MIGRATIONS", broken)
    with pytest.raises(sqlite3.Error):
        EventStore(db)
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not c.execute("SELECT name FROM sqlite_master").fetchall()
    c.close()
    monkeypatch.undo()
    with EventStore(db) as store:
        store.ingest(installation_event)
        store.save_review(reminder(installation_event))
    with EventStore(db) as store:
        assert len(store.reviews()) == 1


def test_reviews_are_exported_with_history(tmp_path, installation_event):
    with EventStore(tmp_path / "db") as store:
        store.ingest(installation_event)
        store.save_review(reminder(installation_event))
        exported = store.full_history_export()
    assert len(exported["reviews"]) == 1
    assert exported["reviews"][0]["state"] == "pending"


def test_reminder_follows_its_choice_by_name_when_a_reinstall_renumbers_options(tmp_path, installation_event):
    # Older builds numbered options in display order, newer ones in authoring
    # order, so the same choice can have a different id after a reinstall.
    original = deepcopy(installation_event)
    options = original["steps"][0]["groups"][0]["options"]
    options[1:] = [{"id": "step:0/group:0/option:1", "name": "Second choice",
                    "selected": False, "enabled": True}]
    with EventStore(tmp_path / "logger.db") as store:
        store.ingest(original)
        store.save_review(reminder(original, options[1]["id"]))
        newer = deepcopy(original)
        newer["event_id"] = str(uuid4())
        newer["completed_at"] = "2026-09-01T12:00:00Z"
        newer_options = newer["steps"][0]["groups"][0]["options"]
        # Same choices, ids swapped.
        newer_options[0]["id"], newer_options[1]["id"] = newer_options[1]["id"], newer_options[0]["id"]
        store.ingest(newer)
        [review] = store.reviews()
    assert review["latest_option_id"] == newer_options[1]["id"] == options[0]["id"]
    assert review["choices_changed"] is False  # an identical reinstall, despite new ids


def test_notes_are_optional_and_a_reminder_can_be_removed(tmp_path, installation_event):
    with EventStore(tmp_path / "logger.db") as store:
        store.ingest(installation_event)
        body = dict(reminder(installation_event), reason="")
        assert store.save_review(body)["reason"] == ""
        assert store.delete_review({"event_id": installation_event["event_id"], "option_id": ""}) is True
        assert store.reviews() == []
        assert store.delete_review({"event_id": installation_event["event_id"], "option_id": ""}) is False
