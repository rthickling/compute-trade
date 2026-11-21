"""Independent packet oracle for the supported Optiq Cash OEG SBE messages.

Layouts use the public Euronext v354 OEG XML (schema 0, little endian) and
the v5.354.0 interface specification, sections 4.1, 5.3 and 5.4. Only wire
data used by the tests is named here. The production codec is never loaded.

https://connect.euronext.com/sites/default/files/it-documentation/oeg_binary_sbe_input_354.xml
XML SHA-256: 00764cc43fdd89efd7bceb145ee055489d2805ff174b4654683d2f820bf70c83
"""

import struct
from dataclasses import dataclass


SCHEMA_ID = 0
VERSION = 354
NEW_ORDER = 1
ACK = 3
FILL = 4
KILL = 5
CANCEL_REPLACE = 6
REJECT = 7
CANCEL_REQUEST = 12
LOGON = 100
LOGON_ACK = 101
LOGON_REJECT = 102

BUY = 1
SELL = 2
MARKET = 1
LIMIT = 2
NEW_ORDER_ACK = 0
REPLACE_ACK = 1
CLIENT_CANCEL = 1
DISCONNECT_CANCEL = 11
CONTINUOUS_TRADING = 1
NULL_CLIENT_ORDER_ID = -(1 << 63)
NULL_ORDER_ID = (1 << 64) - 1
NULL_UINT8 = (1 << 8) - 1
NULL_UINT16 = (1 << 16) - 1
NULL_INT32 = -(1 << 31)

# Frame length, block length, template ID, schema ID, schema version.
HEADER = struct.Struct("<HHHHH")
GROUP_HEADER = struct.Struct("<BB")


@dataclass(frozen=True)
class Message:
    template_id: int
    fields: dict[str, int | str | bytes]
    groups: tuple[tuple[bytes, ...], ...] = ()


@dataclass(frozen=True)
class Layout:
    fixed: struct.Struct
    fields: list[str]
    group_lengths: tuple[int, ...] = ()


_INBOUND = "client_sequence firm_id sending_time"
_OUTBOUND = "sequence firm_id"
_OUTBOUND_TIMES = (
    "sending_time gateway_in_from_member gateway_out_to_matcher book_in_time "
    "book_out_time gateway_in_from_matcher gateway_out_to_member"
)
_AMEND_PREFIX = (
    f"{_INBOUND} execution_within_firm_short_code client_identification_short_code "
    "client_order_id order_id original_client_order_id"
)


_LAYOUTS = {
    NEW_ORDER: Layout(
        struct.Struct("<I8sQqIBBBBqQiBBBBBBHHq"),
        (
            f"{_INBOUND} client_order_id symbol_index emm side order_type time_in_force "
            "price quantity execution_within_firm_short_code trading_capacity account_type "
            "liquidity_provider_role execution_instruction dark_execution_instruction "
            "mifid_indicators self_trade_prevention_id non_executing_client_id ioi_id"
        ).split(),
        (18, 12, 50, 35, 0, 0, 16, 4),
    ),
    ACK: Layout(
        struct.Struct("<I8s7QqqIBBBBQQqQBq"),
        (
            f"{_OUTBOUND} {_OUTBOUND_TIMES} client_order_id original_client_order_id "
            "symbol_index emm side ack_type ack_phase order_id priority price quantity "
            "ack_qualifiers order_tolerable_price"
        ).split(),
        (9,),
    ),
    FILL: Layout(
        struct.Struct("<I8s4QqIBBBBQqQQIBIB16s"),
        (
            f"{_OUTBOUND} trade_time book_out_time gateway_in_from_matcher gateway_out_to_member "
            "client_order_id symbol_index emm side trade_type trade_qualifier order_id "
            "execution_price execution_quantity leaves_quantity execution_id execution_phase "
            "lis_transaction_id escb_membership trade_unique_identifier"
        ).split(),
        (32, 41, 9, 17),
    ),
    KILL: Layout(
        struct.Struct("<I8s7QqqQIBHB"),
        (
            f"{_OUTBOUND} {_OUTBOUND_TIMES} client_order_id original_client_order_id "
            "order_id symbol_index emm kill_reason ack_qualifiers"
        ).split(),
        (9,),
    ),
    CANCEL_REPLACE: Layout(
        struct.Struct("<I8sQiiqQqqQIBBBBBBBBBH"),
        (
            f"{_AMEND_PREFIX} price quantity symbol_index emm side order_type time_in_force "
            "account_type liquidity_provider_role execution_instruction "
            "dark_execution_instruction mifid_indicators self_trade_prevention_id"
        ).split(),
        (18, 34, 33, 0, 0, 16),
    ),
    REJECT: Layout(
        struct.Struct("<I8s7QqQIBBHHB"),
        (
            f"{_OUTBOUND} {_OUTBOUND_TIMES} client_order_id order_id symbol_index emm "
            "deprecated_rejected_message error_code rejected_message_id ack_qualifiers"
        ).split(),
        (9, 9),
    ),
    CANCEL_REQUEST: Layout(
        struct.Struct("<I8sQiiqQqIBBBB"),
        f"{_AMEND_PREFIX} symbol_index emm side order_type order_category".split(),
        (0, 0),
    ),
    LOGON: Layout(
        struct.Struct("<IHI8sB"),
        "logical_access_id partition_id last_sequence software_provider queueing_indicator".split(),
    ),
    LOGON_ACK: Layout(struct.Struct("<8sI"), "exchange_id last_client_sequence".split()),
    LOGON_REJECT: Layout(
        struct.Struct("<8sBII"),
        "exchange_id logon_reject_code last_client_sequence last_sequence".split(),
    ),
}


def packet(template_id, block, groups=b"", *, schema_id=SCHEMA_ID, version=VERSION):
    """Build a frame without interpreting the supplied payload bytes."""
    length = HEADER.size + len(block) + len(groups)
    return HEADER.pack(length, len(block), template_id, schema_id, version) + block + groups


def _read_groups(frame, offset, group_lengths):
    groups = []
    for index, expected_length in enumerate(group_lengths):
        if len(frame) - offset < GROUP_HEADER.size:
            raise ValueError(f"missing group {index} header")
        length, count = GROUP_HEADER.unpack_from(frame, offset)
        offset += GROUP_HEADER.size
        if length != expected_length or (length == 0 and count != 0):
            raise ValueError(f"wrong group {index} dimensions: block {length}, count {count}")
        end = offset + length * count
        if end > len(frame):
            raise ValueError(f"truncated group {index} data")
        records = tuple(frame[start : start + length] for start in range(offset, end, length or 1))
        groups.append(records)
        offset = end
    if len(frame) != offset:
        raise ValueError(f"{len(frame) - offset} trailing frame bytes")
    return tuple(groups)


def decode_frame(data):
    """Decode exactly one v354 frame, checking all block and group boundaries."""
    if len(data) < HEADER.size:
        raise ValueError("incomplete OEG frame header")
    length, block_length, template_id, schema_id, version = HEADER.unpack_from(data)
    if length != len(data):
        raise ValueError(f"frame length {length} != actual length {len(data)}")
    if (schema_id, version) != (SCHEMA_ID, VERSION):
        raise ValueError(f"unexpected OEG schema {schema_id}/{version}")
    try:
        layout = _LAYOUTS[template_id]
    except KeyError as error:
        raise ValueError(f"unsupported v354 template {template_id}") from error
    if block_length != layout.fixed.size:
        raise ValueError(f"template {template_id} has unexpected block length {block_length}")
    block_end = HEADER.size + block_length
    if block_end > length:
        raise ValueError(f"template {template_id} has truncated block")
    values = layout.fixed.unpack_from(data, HEADER.size)
    fields = dict(zip(layout.fields, values, strict=True))
    for name in ("firm_id", "exchange_id", "software_provider", "trade_unique_identifier"):
        if name in fields:
            fields[name] = fields[name].rstrip(b"\0").decode("ascii")
    groups = _read_groups(data, block_end, layout.group_lengths)
    return Message(template_id, fields, groups)


def _encode_request(template, *values):
    layout = _LAYOUTS[template]
    groups = b"".join(GROUP_HEADER.pack(length, 0) for length in layout.group_lengths)
    return packet(template, layout.fixed.pack(*values), groups)


def _firm(firm):
    value = firm.encode("ascii")
    if not 1 <= len(value) <= 8 or b"\0" in value:
        raise ValueError("Firm ID must contain one to eight non-NUL ASCII bytes")
    return value


def logon(logical_access_id, partition_id, last_sequence=0):
    # No fictional software vendor. Set the queueing indicator to queue.
    return _encode_request(LOGON, logical_access_id, partition_id, last_sequence, b"", 1)


def new_order(client_order_id, firm, instrument, side, price, quantity, *, sequence=1,
              order_type=LIMIT, time_in_force=0, execution_instruction=0):
    return _encode_request(
        NEW_ORDER, sequence, _firm(firm), 1, client_order_id, instrument,
        CONTINUOUS_TRADING, side, order_type, time_in_force, price, quantity,
        1, 1, 2, NULL_UINT8, execution_instruction, 0, 0, NULL_UINT16,
        NULL_UINT16, NULL_CLIENT_ORDER_ID,
    )


def cancel(client_order_id, firm, instrument, side, order_id, *,
           original_client_order_id=None, sequence=1, order_type=LIMIT):
    return _encode_request(
        CANCEL_REQUEST, sequence, _firm(firm), 1, 1, NULL_INT32, client_order_id,
        NULL_ORDER_ID if order_id is None else order_id,
        NULL_CLIENT_ORDER_ID if original_client_order_id is None else original_client_order_id,
        instrument, CONTINUOUS_TRADING, side, order_type, 1,
    )


def replace(client_order_id, firm, instrument, side, order_id, price, quantity, *,
            original_client_order_id=None, sequence=1, order_type=LIMIT, time_in_force=0):
    return _encode_request(
        CANCEL_REPLACE, sequence, _firm(firm), 1, 1, NULL_INT32, client_order_id,
        NULL_ORDER_ID if order_id is None else order_id,
        NULL_CLIENT_ORDER_ID if original_client_order_id is None else original_client_order_id,
        price, quantity, instrument, CONTINUOUS_TRADING, side, order_type,
        time_in_force, NULL_UINT8, NULL_UINT8, 0, 0, 0, NULL_UINT16,
    )
