"""Unit tests for the BLE transport chunking layer."""

import pytest

from pymadoka.transport import MAX_CHUNK_SIZE, Transport, TransportDelegate


class RecordingDelegate(TransportDelegate):
    """Delegate that records rebuilt and failed messages."""

    def __init__(self):
        self.rebuilt = []
        self.failed = []

    def response_rebuilt(self, data: bytearray):
        self.rebuilt.append(bytes(data))

    def response_failed(self, data: bytearray):
        self.failed.append(bytes(data))


def make_message(payload_size: int) -> bytearray:
    """Build a protocol message: first byte is the total message length."""
    body = bytes(range(1, payload_size))[: payload_size - 1]
    message = bytearray([payload_size]) + bytearray(body)
    assert len(message) == payload_size
    return message


@pytest.fixture
def delegate():
    return RecordingDelegate()


@pytest.fixture
def transport(delegate):
    return Transport(delegate)


@pytest.mark.parametrize("size", [5, 19, 30, 45])
def test_split_and_rebuild_round_trip(transport, delegate, size):
    """Chunks produced by split_in_chunks rebuild into the original message."""
    message = make_message(size)

    chunks = transport.split_in_chunks(message)

    # Every chunk fits in one BLE write and is sequence-numbered.
    for i, chunk in enumerate(chunks):
        assert len(chunk) <= MAX_CHUNK_SIZE
        assert chunk[0] == i

    for chunk in chunks:
        transport.rebuild_chunk(chunk)

    assert delegate.rebuilt == [bytes(message)]
    assert delegate.failed == []


def test_single_chunk_message(transport, delegate):
    message = make_message(7)
    transport.rebuild_chunk(bytearray([0]) + message)
    assert delegate.rebuilt == [bytes(message)]


def test_short_chunk_is_discarded(transport, delegate):
    transport.rebuild_chunk(bytearray([0]))
    assert delegate.rebuilt == []
    assert delegate.failed == []
    assert transport.chunks == []


def test_restarted_sequence_discards_previous_chunks(transport, delegate):
    """A chunk id that does not advance the sequence flushes the partial message."""
    # First chunk of a 30-byte message (needs 2 chunks, so incomplete).
    message = make_message(30)
    first_chunk = transport.split_in_chunks(message)[0]
    transport.rebuild_chunk(first_chunk)
    assert delegate.rebuilt == []

    # A new message starting over at id 0 discards the partial one.
    transport.rebuild_chunk(bytearray([0]) + make_message(7))

    assert len(delegate.failed) == 1
    assert delegate.rebuilt == [bytes(make_message(7))]
