# SPDX-License-Identifier: Apache-2.0
"""File helpers shared by the configuration loaders and the stores that write files."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with the same key twice.

    PyYAML keeps the last value of a repeated key, so a second ``agents:`` block
    appended to a keys file would silently replace the first.
    """

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:  # noqa: FBT001, FBT002 - PyYAML's signature
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            try:
                duplicate = key in seen
            except TypeError:  # an unhashable key: the base constructor reports it
                break
            if duplicate:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_yaml(text: str) -> Any:
    """yaml.safe_load, but a repeated mapping key is an error (yaml.YAMLError)."""
    return yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - a SafeLoader subclass


def load_yaml_file(path: Path) -> Any:
    """A YAML configuration file (see load_yaml). The error names the file."""
    try:
        return load_yaml(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"{path}: {error}") from error


def write_atomic(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Write a file so that readers see either the old file or all of the new one.

    The data goes to a hidden temporary file beside ``path``, which then replaces
    ``path``. With ``mode`` (0o600 for a secret), the file is created owner-only and
    set to ``mode`` before any data is written, so a secret is never readable by
    others, even briefly; without it the umask decides. The temporary file is removed
    when anything fails.
    """
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666 if mode is None else 0o600)
        with os.fdopen(fd, "wb") as handle:
            if mode is not None:
                os.chmod(handle.fileno(), mode)
            handle.write(data)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
