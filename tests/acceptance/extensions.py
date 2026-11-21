"""Independent encoding for Compute Trade session-control messages."""

import struct

from . import wire

SCHEMA = 1
VERSION = 1
CONFIRM = 200
REPLAY_GAP = 201
REPLAY_COMPLETE = 202

_LAYOUTS = {
    CONFIRM: (struct.Struct("<I"), ("sequence",)),
    REPLAY_GAP: (struct.Struct("<II"), ("first_sequence", "last_sequence")),
    REPLAY_COMPLETE: (struct.Struct("<I"), ("sequence",)),
}


def encode(template_id, **fields):
    layout, names = _LAYOUTS[template_id]
    if set(fields) != set(names):
        raise ValueError(f"template {template_id} needs fields {names}")
    values = [fields[name] for name in names]
    if any(not 0 <= value < 0xFFFFFFFF for value in values):
        raise ValueError("sequence outside the application range")
    if template_id == REPLAY_GAP and not 0 < values[0] <= values[1]:
        raise ValueError("invalid replay gap")
    return wire.packet(template_id, layout.pack(*values), schema_id=SCHEMA, version=VERSION)


def decode(frame):
    length, block_size, template_id, schema, version = wire.HEADER.unpack_from(frame)
    if schema != SCHEMA:
        return wire.decode_frame(frame)
    if version != VERSION or template_id not in _LAYOUTS:
        raise ValueError("unknown Compute Trade session-control message")
    layout, names = _LAYOUTS[template_id]
    if block_size != layout.size or length != wire.HEADER.size + layout.size or len(frame) != length:
        raise ValueError("invalid Compute Trade session-control length")
    fields = dict(zip(names, layout.unpack_from(frame, wire.HEADER.size)))
    if encode(template_id, **fields) != frame:
        raise ValueError("invalid Compute Trade session-control fields")
    return wire.Message(template_id, fields)
