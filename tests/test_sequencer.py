from helpers import cancel

from matching_engine.sequencer import Sequencer


def test_sequence_numbers_are_consecutive_from_the_start_value() -> None:
    seq = Sequencer(next_seq=41, clock=lambda: 5)

    stamped = [seq.stamp(cancel(1)) for _ in range(3)]

    assert [s.seq for s in stamped] == [41, 42, 43]
    assert seq.next_seq == 44


def test_timestamps_never_run_backwards_when_the_clock_does() -> None:
    readings = iter([100, 90, 120, 110])
    seq = Sequencer(clock=lambda: next(readings))

    assert [seq.stamp(cancel(1)).ts for _ in range(4)] == [100, 100, 120, 120]


def test_a_resumed_sequencer_respects_the_last_logged_timestamp() -> None:
    seq = Sequencer(next_seq=10, last_ts=500, clock=lambda: 400)

    assert seq.stamp(cancel(1)).ts == 500
