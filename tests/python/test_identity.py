from __future__ import annotations

from fomod_logger_support.core.identity import (
    normalized_instance_path,
    stable_instance_id,
)


def test_windows_instance_identity_matches_native_qdir_normalization():
    backslash_path = r"C:\Example\MO2"
    forward_path = "C:/Example/MO2/"

    assert normalized_instance_path(backslash_path) == (
        "c:/example/mo2"
    )
    assert stable_instance_id(backslash_path) == (
        "cb0120793a3452e336490e2b3b0bca462bd76b8e7c674e603e63aa3b6de9d87b"
    )
    assert stable_instance_id(forward_path) == stable_instance_id(backslash_path)


def test_windows_normalization_matches_verified_qdir_edge_cases():
    cases = {
        "C:/": "c:/",
        "C:/Example/./instance/": "c:/example/instance",
        "//server/share/a/../b": "//server/share/b",
        r"\\server\share\a\..\b": "//server/share/b",
        "//server/share/../../x": "//server/../x",
        r"C:\Temp\name.": "c:/temp/name.",
        "C:/Temp/name. ": "c:/temp/name. ",
        r"C:\Temp\Straße": "c:/temp/strasse",
    }

    assert {
        path: normalized_instance_path(path) for path in cases
    } == cases
