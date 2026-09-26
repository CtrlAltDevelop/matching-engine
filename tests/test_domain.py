import msgspec

from matching_engine import CancelOrder, NewOrder, SequencedCommand, Side


def test_commands_round_trip_through_json_with_a_type_tag() -> None:
    original = SequencedCommand(
        seq=7,
        ts=1_000,
        command=NewOrder(order_id=7, account=1, side=Side.BUY, price=100, qty=5),
    )
    raw = msgspec.json.encode(original)

    assert b'"type":"new"' in raw
    assert msgspec.json.decode(raw, type=SequencedCommand) == original


def test_cancel_is_distinguished_from_new_by_its_tag() -> None:
    raw = msgspec.json.encode(SequencedCommand(seq=1, ts=0, command=CancelOrder(3, 1)))
    decoded = msgspec.json.decode(raw, type=SequencedCommand)

    assert isinstance(decoded.command, CancelOrder)
