"""`statelock keygen --append`: the entry goes at the end of its section, the rest of the file is kept."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from statelock.__main__ import append_key_entry
from statelock.auth import hash_key

ENTRY = {"agent_id": "new_agent", "tenant": "acme", "key_sha256": hash_key("slk_new")}


def _entry(agent_id: str, key: str) -> str:
    return f"agent_id: {agent_id}, tenant: acme, key_sha256: {hash_key(key)}"


def test_entries_written_at_column_zero_keep_that_indentation(tmp_path: Path) -> None:
    path = tmp_path / "keys.yaml"
    path.write_text(f"agents:\n- {{{_entry('a', 'slk_a')}}}\nreviewers: []\n", encoding="utf-8")
    append_key_entry(path, "agents", ENTRY)
    text = path.read_text(encoding="utf-8")
    assert text.endswith(
        "- agent_id: new_agent\n  tenant: acme\n  key_sha256: " + ENTRY["key_sha256"] + "\nreviewers: []\n"
    )
    assert [entry["agent_id"] for entry in yaml.safe_load(text)["agents"]] == ["a", "new_agent"]


def test_the_entry_goes_before_the_next_section_and_its_comment(tmp_path: Path) -> None:
    path = tmp_path / "keys.yaml"
    path.write_text(
        f"agents:  # production\n  - {{{_entry('a', 'slk_a')}}}\n\n  # the next one\n"
        f"  - {{{_entry('b', 'slk_b')}}}\n\n# reviewers\nreviewers:\n  - reviewer_id: r\n"
        f"    tenant: acme\n    key_sha256: {hash_key('slk_r')}",  # no final newline
        encoding="utf-8",
    )
    append_key_entry(path, "agents", ENTRY)
    text = path.read_text(encoding="utf-8")
    assert text.index("new_agent") < text.index("# reviewers")
    loaded = yaml.safe_load(text)
    assert [entry["agent_id"] for entry in loaded["agents"]] == ["a", "b", "new_agent"]
    assert [entry["reviewer_id"] for entry in loaded["reviewers"]] == ["r"]


def test_a_missing_section_is_added_at_the_end(tmp_path: Path) -> None:
    path = tmp_path / "keys.yaml"
    path.write_text("# keys", encoding="utf-8")
    append_key_entry(path, "agents", ENTRY)
    assert path.read_text(encoding="utf-8").startswith("# keys\nagents:\n  - agent_id: new_agent\n")
    reviewer = {"reviewer_id": "alice", "tenant": "acme", "key_sha256": hash_key("slk_r")}
    append_key_entry(path, "reviewers", reviewer)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded == {"agents": [ENTRY], "reviewers": [reviewer]}


def test_a_file_the_text_insertion_cannot_extend_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "keys.yaml"
    original = f"{{agents: [{{{_entry('a', 'slk_a')}}}]}}\n"  # a flow mapping: no "agents:" line
    path.write_text(original, encoding="utf-8")
    with pytest.raises((ValueError, yaml.YAMLError)):
        append_key_entry(path, "agents", ENTRY)
    assert path.read_text(encoding="utf-8") == original
