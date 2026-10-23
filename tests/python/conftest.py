from __future__ import annotations

from copy import deepcopy

import pytest


@pytest.fixture
def installation_event() -> dict:
    return {
        "schema_version": 1,
        "event_id": "11111111-1111-4111-8111-111111111111",
        "started_at": "2026-08-26T12:00:00+00:00",
        "completed_at": "2026-08-26T12:01:00+00:00",
        "instance": {"id": "a" * 64, "label": "MO2 Test"},
        "source": {"archive": "fixture.zip", "reinstallation": False},
        "mod": {"name": "Fixture Mod", "version": "1.0", "nexus_id": 170511},
        "installer": {
            "name": "FOMOD Logger Native Installer",
            "version": "1.0.0",
            "upstream_commit": "fc263f2d923c704b4853c11ed4f8b8cf3920f30d",
        },
        "steps": [
            {
                "id": "step:0",
                "name": "Main",
                "visible": True,
                "groups": [
                    {
                        "id": "step:0/group:0",
                        "name": "First group",
                        "type": "SelectExactlyOne",
                        "options": [
                            {
                                "id": "step:0/group:0/option:0",
                                "name": "Shared label",
                                "selected": True,
                                "enabled": True,
                            }
                        ],
                    },
                    {
                        "id": "step:0/group:1",
                        "name": "Second group",
                        "type": "SelectAny",
                        "options": [
                            {
                                "id": "step:0/group:1/option:0",
                                "name": "Shared label",
                                "selected": False,
                                "enabled": True,
                            }
                        ],
                    },
                ],
            }
        ],
    }


@pytest.fixture
def clone_event(installation_event):
    def clone() -> dict:
        return deepcopy(installation_event)

    return clone
