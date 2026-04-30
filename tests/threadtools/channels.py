import threading
import time
import pytest
from anyio import ClosedResourceError, EndOfStream, WouldBlock

from localpost import threadtools
from localpost.threadtools import Channel, SendChannel

@pytest.fixture
def no_anyio():
    original_check_cancelled = threadtools.check_cancelled
    threadtools.check_cancelled = lambda: None
    try:
        yield
    finally:
        threadtools.check_cancelled = original_check_cancelled

def test_basic_send_receive(no_anyio):
    """Test basic send and receive operations."""
    sender, receiver = Channel.create()

    # Send and receive a single item
    sender.put(42)
    assert receiver.get() == 42

    # Send and receive multiple items
    for i in range(5):
        sender.put(i)

    for i in range(5):
        assert receiver.get() == i

    sender.close()
    receiver.close()


def test_multiple_senders_single_receiver(no_anyio):
    """Test multiple senders with a single receiver."""
    sender, receiver = Channel.create()
    results = []
    num_senders = 3
    messages_per_sender = 3
    senders = [sender] + [sender.clone() for _ in range(num_senders - 1)]

    def send_messages(thread_sender: SendChannel[str], sender_id: int):
        for i in range(messages_per_sender):
            thread_sender.put(f"sender{sender_id}-msg{i}")
            time.sleep(0.05)  # Small delay to mix messages
        thread_sender.close()

    # Start multiple sender threads
    threads = []
    for i, ts in enumerate(senders):
        t = threading.Thread(target=send_messages, args=(ts, i))
        t.start()
        threads.append(t)

    # Receive all messages
    try:
        while True:
            results.append(receiver.get())
    except EndOfStream:
        pass

    # Wait for all threads to complete
    for t in threads:
        t.join()

    receiver.close()

    # Should have received all messages
    assert len(results) == num_senders * messages_per_sender
    # Check that we got messages from all senders
    for i in range(num_senders):
        sender_msgs = [msg for msg in results if msg.startswith(f"sender{i}")]
        assert len(sender_msgs) == messages_per_sender


def test_single_sender_multiple_receivers(no_anyio):
    """Test single sender with multiple receivers."""
    sender, receiver = Channel.create()
    results = {i: [] for i in range(3)}
    receivers = [receiver] + [receiver.clone() for _ in range(2)]

    def receive_messages(recv, receiver_id: int):
        try:
            while True:
                value = recv.get()
                results[receiver_id].append(value)
        except EndOfStream:
            pass
        recv.close()

    # Start multiple receiver threads
    threads = []
    for i, recv in enumerate(receivers):
        t = threading.Thread(target=receive_messages, args=(recv, i))
        t.start()
        threads.append(t)

    # Send messages
    for i in range(30):
        sender.put(i)
        time.sleep(0.001)  # Small delay to allow receivers to compete

    sender.close()

    # Wait for all threads to complete
    for t in threads:
        t.join()

    # Check that all messages were received (distributed among receivers)
    all_received = []
    for receiver_results in results.values():
        all_received.extend(receiver_results)

    assert sorted(all_received) == list(range(30))
    # Each receiver should have gotten some messages
    for receiver_results in results.values():
        assert len(receiver_results) > 0


def test_no_receivers_error(no_anyio):
    """Test that sending without receivers raises ClosedResourceError."""
    sender, receiver = Channel.create()
    receiver.close()

    with pytest.raises(ClosedResourceError):
        sender.put(42)

    sender.close()


def test_end_of_channel(no_anyio):
    """Test that receivers get EndOfStream when all senders close."""
    sender1, receiver = Channel.create()
    sender2 = sender1.clone()

    sender1.put(1)
    sender2.put(2)

    assert receiver.get() == 1
    assert receiver.get() == 2

    # Close one sender - receiver should still work
    sender1.close()
    sender2.put(3)
    assert receiver.get() == 3

    # Close last sender - receiver should get EndOfStream
    sender2.close()
    with pytest.raises(EndOfStream):
        receiver.get()

    receiver.close()


def test_closed_channel_errors(no_anyio):
    """Test operations on closed channels raise errors."""
    sender, receiver = Channel.create()

    # Close sender and try to use it
    sender.close()
    with pytest.raises(ClosedResourceError):
        sender.put(42)

    # Close receiver and try to use it
    receiver.close()
    with pytest.raises(ClosedResourceError):
        receiver.get()


def test_blocking_receive(no_anyio):
    """Test that receive blocks until item is available."""
    sender, receiver = Channel.create()
    result = []

    def delayed_send():
        time.sleep(0.1)
        sender.put("delayed message")
        sender.close()

    def receive():
        try:
            result.append(receiver.get())
        except EndOfStream:
            pass
        receiver.close()

    # Start receiver first (will block)
    receiver_thread = threading.Thread(target=receive)
    receiver_thread.start()

    # Start sender after delay
    sender_thread = threading.Thread(target=delayed_send)
    sender_thread.start()

    # Wait for both threads
    receiver_thread.join(timeout=1.0)
    sender_thread.join(timeout=1.0)

    assert result == ["delayed message"]


def test_negative_capacity_rejected(no_anyio):
    """Channel capacities are None, 0, or a positive integer."""
    with pytest.raises(ValueError, match="capacity"):
        Channel.create(capacity=-1)


def test_rendezvous_put_nowait_requires_waiting_receiver(no_anyio):
    """A capacity=0 channel has no spare buffer slot for put_nowait."""
    sender, receiver = Channel.create(capacity=0)
    try:
        with pytest.raises(WouldBlock):
            sender.put_nowait("not-yet")
    finally:
        sender.close()
        receiver.close()


def test_rendezvous_put_nowait_hands_off_to_waiting_receiver(no_anyio):
    sender, receiver = Channel.create(capacity=0)
    received: list[str] = []

    def receive() -> None:
        received.append(receiver.get())

    receiver_thread = threading.Thread(target=receive)
    receiver_thread.start()

    deadline = time.monotonic() + 1.0
    while sender._state.waiting_receivers == 0 and time.monotonic() < deadline:
        time.sleep(0.001)

    try:
        assert sender._state.waiting_receivers == 1
        sender.put_nowait("ready")
        receiver_thread.join(timeout=1.0)
        assert not receiver_thread.is_alive()
        assert received == ["ready"]
    finally:
        sender.close()
        receiver.close()


def test_rendezvous_put_blocks_until_receiver_consumes(no_anyio):
    sender, receiver = Channel.create(capacity=0)
    sent = threading.Event()

    def send() -> None:
        sender.put("value")
        sent.set()

    sender_thread = threading.Thread(target=send)
    sender_thread.start()

    try:
        time.sleep(0.05)
        assert not sent.is_set()
        assert receiver.get() == "value"
        sender_thread.join(timeout=1.0)
        assert not sender_thread.is_alive()
        assert sent.is_set()
    finally:
        sender.close()
        receiver.close()


def test_concurrent_stress(no_anyio):
    """Stress test with many concurrent senders and receivers."""
    num_senders = 5
    num_receivers = 3
    messages_per_sender = 100

    sender, receiver = Channel.create()
    senders = [sender] + [sender.clone() for _ in range(num_senders - 1)]
    receivers = [receiver] + [receiver.clone() for _ in range(num_receivers - 1)]

    received = []
    received_lock = threading.Lock()

    def sender_work(s, sender_id: int):
        try:
            for i in range(messages_per_sender):
                s.put(sender_id * 1000 + i)
        finally:
            s.close()

    def receiver_work(r):
        local_received = []
        try:
            while True:
                local_received.append(r.get())
        except EndOfStream:
            pass
        finally:
            r.close()

        with received_lock:
            received.extend(local_received)

    # Start all threads
    threads = []

    # Start senders first to ensure at least one is open when receivers start
    for i, s in enumerate(senders):
        t = threading.Thread(target=sender_work, args=(s, i))
        t.start()
        threads.append(t)

    # Give senders time to start
    time.sleep(0.05)

    # Start receivers
    for r in receivers:
        t = threading.Thread(target=receiver_work, args=(r,))
        t.start()
        threads.append(t)

    # Wait for all threads
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive(), "Thread did not complete in time"

    # Verify all messages were received
    assert len(received) == num_senders * messages_per_sender

    # Verify all messages are unique and accounted for
    expected = []
    for sender_id in range(num_senders):
        for i in range(messages_per_sender):
            expected.append(sender_id * 1000 + i)

    assert sorted(received) == sorted(expected)


def test_channel_cleanup(no_anyio):
    """Test that channels clean up properly when references are dropped."""
    for _ in range(10):
        sender, receiver = Channel.create()
        sender.put(42)
        assert receiver.get() == 42
        state = sender._state
        sender.close()
        receiver.close()

        # Verify state is clean
        assert state.open_send_channels == 0
        assert state.open_receive_channels == 0
        assert len(state.buffer) == 0
