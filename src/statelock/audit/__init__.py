# SPDX-License-Identifier: Apache-2.0
"""Artifact records, redaction and sinks."""

from statelock.audit.records import SCHEMA_VERSION, ActionRecord, build_action_record, build_network_record
from statelock.audit.redaction import mask_secret_input, redact_payload
from statelock.audit.sequencing import SequencedWriter
from statelock.audit.sink import ArtifactSink, LocalJsonSink

__all__ = [
    "SCHEMA_VERSION",
    "ActionRecord",
    "ArtifactSink",
    "LocalJsonSink",
    "SequencedWriter",
    "build_action_record",
    "build_network_record",
    "mask_secret_input",
    "redact_payload",
]
