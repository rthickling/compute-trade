"""Check the test client against fixed vectors derived from the v354 OEG XML.

These examples were serialized field by field using the published schema
types and offsets. Each vector has its wire header on the first line and
any group dimensions on the last line.
"""

import unittest

from . import wire


LOGON_FRAME = bytes.fromhex("""
    1d 00 13 00 64 00 00 00 62 01
    04 03 02 01 06 05 0a 09 08 07 00 00 00 00 00 00 00 00 01
""")
LOGON_ACK_FRAME = bytes.fromhex("""
    16 00 0c 00 65 00 00 00 62 01
    43 54 00 00 00 00 00 00 04 03 02 01
""")
LOGON_REJECT_FRAME = bytes.fromhex("""
    1b 00 11 00 66 00 00 00 62 01
    43 54 00 00 00 00 00 00 02 04 03 02 01 0a 09 08 07
""")
NEW_FRAME = bytes.fromhex("""
    64 00 4a 00 01 00 00 00 62 01
    04 03 02 01 46 49 52 4d 31 00 00 00 01 00 00 00
    00 00 00 00 79 29 ed ff ff ff ff ff 0a 09 08 07
    01 02 02 00 45 23 01 00 00 00 00 00 89 67 00 00
    00 00 00 00 01 00 00 00 01 02 ff 00 00 00 ff ff
    ff ff 00 00 00 00 00 00 00 80
    12 00 0c 00 32 00 23 00 00 00 00 00 10 00 04 00
""")
ACK_FRAME = bytes.fromhex("""
    91 00 85 00 03 00 00 00 62 01
    0d 0c 0b 0a 46 49 52 4d 31 00 00 00 11 00 00 00
    00 00 00 00 ff ff ff ff ff ff ff ff ff ff ff ff
    ff ff ff ff 13 00 00 00 00 00 00 00 14 00 00 00
    00 00 00 00 ff ff ff ff ff ff ff ff ff ff ff ff
    ff ff ff ff 79 29 ed ff ff ff ff ff c0 1d fe ff
    ff ff ff ff 0a 09 08 07 01 02 01 01 88 77 66 55
    44 33 22 11 99 88 77 66 55 44 33 22 45 23 01 00
    00 00 00 00 89 67 00 00 00 00 00 00 c0 00 00 00
    00 00 00 00 80
    09 00
""")
FILL_FRAME = bytes.fromhex("""
    88 00 76 00 04 00 00 00 62 01
    0e 0c 0b 0a 46 49 52 4d 31 00 00 00 10 00 00 00
    00 00 00 00 14 00 00 00 00 00 00 00 ff ff ff ff
    ff ff ff ff ff ff ff ff ff ff ff ff 00 00 00 00
    00 00 00 80 0a 09 08 07 01 02 01 00 88 77 66 55
    44 33 22 11 01 20 01 00 00 00 00 00 02 10 00 00
    00 00 00 00 04 30 00 00 00 00 00 00 03 02 01 05
    01 ff ff ff ff ff 54 30 30 30 30 30 30 30 30 30
    30 30 30 30 30 31
    20 00 29 00 09 00 11 00
""")
KILL_FRAME = bytes.fromhex("""
    70 00 64 00 05 00 00 00 62 01
    0d 0c 0b 0a 46 49 52 4d 31 00 00 00 11 00 00 00
    00 00 00 00 ff ff ff ff ff ff ff ff ff ff ff ff
    ff ff ff ff 13 00 00 00 00 00 00 00 14 00 00 00
    00 00 00 00 ff ff ff ff ff ff ff ff ff ff ff ff
    ff ff ff ff 00 00 00 00 00 00 00 80 00 00 00 00
    00 00 00 80 88 77 66 55 44 33 22 11 0a 09 08 07
    01 0b 00 00
    09 00
""")
REPLACE_FRAME = bytes.fromhex("""
    69 00 53 00 06 00 00 00 62 01
    04 03 02 01 46 49 52 4d 31 00 00 00 01 00 00 00
    00 00 00 00 01 00 00 00 00 00 00 80 79 29 ed ff
    ff ff ff ff 88 77 66 55 44 33 22 11 c0 1d fe ff
    ff ff ff ff 45 23 01 00 00 00 00 00 89 67 00 00
    00 00 00 00 0a 09 08 07 01 02 02 00 ff ff 00 00
    00 ff ff
    12 00 22 00 21 00 00 00 00 00 10 00
""")
REJECT_FRAME = bytes.fromhex("""
    6d 00 5f 00 07 00 00 00 62 01
    0d 0c 0b 0a 46 49 52 4d 31 00 00 00 11 00 00 00
    00 00 00 00 ff ff ff ff ff ff ff ff ff ff ff ff
    ff ff ff ff 13 00 00 00 00 00 00 00 14 00 00 00
    00 00 00 00 ff ff ff ff ff ff ff ff ff ff ff ff
    ff ff ff ff 79 29 ed ff ff ff ff ff 88 77 66 55
    44 33 22 11 0a 09 08 07 01 ff 35 08 06 00 00
    09 00 09 00
""")
CANCEL_FRAME = bytes.fromhex("""
    4a 00 3c 00 0c 00 00 00 62 01
    04 03 02 01 46 49 52 4d 31 00 00 00 01 00 00 00
    00 00 00 00 01 00 00 00 00 00 00 80 79 29 ed ff
    ff ff ff ff 88 77 66 55 44 33 22 11 c0 1d fe ff
    ff ff ff ff 0a 09 08 07 01 02 02 01
    00 00 00 00
""")

CLIENT_ID = -1234567
ORIGINAL_ID = -123456
ORDER_ID = 0x1122334455667788
INSTRUMENT = 0x0708090A
CLIENT_SEQUENCE = 0x01020304
PRIVATE_SEQUENCE = 0x0A0B0C0D
PRICE = 0x12345
QUANTITY = 0x6789


class WireTest(unittest.TestCase):
    def test_request_encoders_match_reference_bytes(self):
        target = CLIENT_ID, "FIRM1", INSTRUMENT, wire.SELL, ORDER_ID
        encoders = (
            (LOGON_FRAME, wire.logon(0x01020304, 0x0506, 0x0708090A)),
            (NEW_FRAME, wire.new_order(
                CLIENT_ID, "FIRM1", INSTRUMENT, wire.SELL, PRICE, QUANTITY,
                sequence=CLIENT_SEQUENCE,
            )),
            (REPLACE_FRAME, wire.replace(
                *target, PRICE, QUANTITY, original_client_order_id=ORIGINAL_ID,
                sequence=CLIENT_SEQUENCE,
            )),
            (CANCEL_FRAME, wire.cancel(
                *target, original_client_order_id=ORIGINAL_ID, sequence=CLIENT_SEQUENCE,
            )),
        )
        for expected, encoded in encoders:
            with self.subTest(template=expected[4]):
                self.assertEqual(encoded, expected)

    def test_absent_target_uses_numeric_null(self):
        cancel = wire.cancel(CLIENT_ID, "FIRM1", INSTRUMENT, wire.SELL, None)
        replace = wire.replace(CLIENT_ID, "FIRM1", INSTRUMENT, wire.SELL, None, PRICE, QUANTITY)
        for frame in (cancel, replace):
            with self.subTest(template=frame[4]):
                self.assertEqual(frame[46:62], bytes.fromhex("ff ff ff ff ff ff ff ff 00 00 00 00 00 00 00 80"))

    def test_logon_replies(self):
        ack = wire.decode_frame(LOGON_ACK_FRAME)
        self.assertEqual(ack.template_id, wire.LOGON_ACK)
        self.assertEqual(ack.fields, {"exchange_id": "CT", "last_client_sequence": CLIENT_SEQUENCE})
        reject = wire.decode_frame(LOGON_REJECT_FRAME)
        self.assertEqual(reject.template_id, wire.LOGON_REJECT)
        self.assertEqual(reject.fields, {
            "exchange_id": "CT", "last_client_sequence": CLIENT_SEQUENCE,
            "last_sequence": 0x0708090A, "logon_reject_code": 2,
        })

    def test_application_replies(self):
        cases = (
            (ACK_FRAME, wire.ACK, PRIVATE_SEQUENCE, {
                "ack_type": wire.REPLACE_ACK, "ack_qualifiers": 0xC0,
                "client_order_id": CLIENT_ID, "original_client_order_id": ORIGINAL_ID,
                "priority": 0x2233445566778899, "price": PRICE, "quantity": QUANTITY,
                "side": wire.SELL, "order_tolerable_price": -(1 << 63),
            }),
            (FILL_FRAME, wire.FILL, PRIVATE_SEQUENCE + 1, {
                "client_order_id": -(1 << 63), "execution_id": 0x05010203,
                "execution_price": 0x12001, "execution_quantity": 0x1002,
                "leaves_quantity": 0x3004, "trade_unique_identifier": "T000000000000001",
            }),
            (KILL_FRAME, wire.KILL, PRIVATE_SEQUENCE, {
                "client_order_id": -(1 << 63), "original_client_order_id": -(1 << 63),
                "kill_reason": 11,
            }),
            (REJECT_FRAME, wire.REJECT, PRIVATE_SEQUENCE, {
                "client_order_id": CLIENT_ID, "error_code": 2101, "rejected_message_id": 6,
                "deprecated_rejected_message": 255,
            }),
        )
        for frame, template, sequence, fields in cases:
            with self.subTest(template=template):
                message = wire.decode_frame(frame)
                self.assertEqual(message.template_id, template)
                self.assertEqual(message.fields["firm_id"], "FIRM1")
                self.assertEqual(message.fields["sequence"], sequence)
                self.assertEqual(message.fields["order_id"], ORDER_ID)
                self.assertEqual(message.fields["symbol_index"], INSTRUMENT)
                self.assertEqual({key: message.fields[key] for key in fields}, fields)

    def test_optional_group_can_carry_one_entry(self):
        entry = bytes.fromhex("07 00 00 00 0d 00 00 00 01")
        frame = bytes.fromhex("9a 00") + ACK_FRAME[2:-1] + b"\x01" + entry
        self.assertEqual(wire.decode_frame(frame).groups, ((entry,),))

    def test_rejects_invalid_framing_blocks_and_groups(self):
        cases = (
            (ACK_FRAME[:9], "incomplete OEG frame header"),
            (ACK_FRAME[:-1], "frame length"),
            (ACK_FRAME + FILL_FRAME, "frame length"),
            (bytes.fromhex("91 00 85 00 03 00 00 00 61 01") + ACK_FRAME[10:], "schema"),
            (bytes.fromhex("91 00 85 00 03 00 01 00 62 01") + ACK_FRAME[10:], "schema"),
            (bytes.fromhex("91 00 85 00 ff ff 00 00 62 01") + ACK_FRAME[10:], "unsupported"),
            (bytes.fromhex("91 00 84 00 03 00 00 00 62 01") + ACK_FRAME[10:], "block length"),
            (bytes.fromhex("8e 00") + ACK_FRAME[2:-3], "truncated block"),
            (bytes.fromhex("90 00") + ACK_FRAME[2:-1], "missing group"),
            (ACK_FRAME[:-2] + b"\x00\x00", "dimensions"),
            (CANCEL_FRAME[:-1] + b"\x01", "dimensions"),
            (ACK_FRAME[:-1] + b"\x01", "truncated group"),
            (bytes.fromhex("92 00") + ACK_FRAME[2:] + b"\x00", "trailing frame bytes"),
        )
        for frame, error in cases:
            with self.subTest(error=error, frame=frame.hex()):
                with self.assertRaisesRegex(ValueError, error):
                    wire.decode_frame(frame)


if __name__ == "__main__":
    unittest.main()
