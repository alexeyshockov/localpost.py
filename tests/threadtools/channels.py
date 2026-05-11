import threading
import time
from typing import cast

import pytest
from anyio import ClosedResourceError, EndOfStream, WouldBlock

from localpost.threadtools import Channel, SendChannel
from localpost.threadtools._channel import ChannelState, _SendChannel


def _state_of[T](s: SendChannel[T]) -> ChannelState[T]:
    """Reach into a sender's :class:`ChannelState` for white-box assertions.

    ``Channel.create`` returns the public ``SendChannel`` Protocol; tests
    that need to observe internal counters (``waiting_receivers``,
    ``pending_handoffs``) cast to the concrete impl through this helper so
    the access is explicit in one place.
    """
    return cast("_SendChannel[T]", s)._state


def test_basic_send_receive():
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


def test_multiple_senders_single_receiver():
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


def test_single_sender_multiple_receivers():
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


def test_no_receivers_error():
    """Test that sending without receivers raises ClosedResourceError."""
    sender, receiver = Channel.create()
    receiver.close()

    with pytest.raises(ClosedResourceError):
        sender.put(42)

    sender.close()


def test_end_of_channel():
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


def test_closed_channel_errors():
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


def test_blocking_receive():
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


def test_negative_capacity_rejected():
    """Channel capacities are None, 0, or a positive integer."""
    with pytest.raises(ValueError, match="capacity"):
        Channel.create(capacity=-1)


def test_rendezvous_put_nowait_requires_waiting_receiver():
    """A capacity=0 channel has no spare buffer slot for put_nowait."""
    sender, receiver = Channel.create(capacity=0)
    try:
        with pytest.raises(WouldBlock):
            sender.put_nowait("not-yet")
    finally:
        sender.close()
        receiver.close()


def test_rendezvous_put_nowait_hands_off_to_waiting_receiver():
    sender, receiver = Channel.create(capacity=0)
    received: list[str] = []

    def receive() -> None:
        received.append(receiver.get())

    receiver_thread = threading.Thread(target=receive)
    receiver_thread.start()

    deadline = time.monotonic() + 1.0
    while _state_of(sender).waiting_receivers == 0 and time.monotonic() < deadline:
        time.sleep(0.001)

    try:
        assert _state_of(sender).waiting_receivers == 1
        sender.put_nowait("ready")
        receiver_thread.join(timeout=1.0)
        assert not receiver_thread.is_alive()
        assert received == ["ready"]
    finally:
        sender.close()
        receiver.close()


def test_rendezvous_put_blocks_until_receiver_consumes():
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


def _wait_for(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return predicate()


def test_rendezvous_put_nowait_with_multiple_receivers():
    """N concurrent put_nowait calls succeed when N receivers are waiting."""
    n = 4
    sender, receiver = Channel.create(capacity=0)
    receivers = [receiver] + [receiver.clone() for _ in range(n - 1)]
    received: list[int] = []
    received_lock = threading.Lock()

    def receive(r):
        try:
            value = r.get()
        finally:
            r.close()
        with received_lock:
            received.append(value)

    threads = [threading.Thread(target=receive, args=(r,)) for r in receivers]
    for t in threads:
        t.start()

    try:
        assert _wait_for(lambda: _state_of(sender).waiting_receivers == n), (
            f"only {_state_of(sender).waiting_receivers} receivers waiting"
        )

        for i in range(n):
            sender.put_nowait(i)

        with pytest.raises(WouldBlock):
            sender.put_nowait(n)

        for t in threads:
            t.join(timeout=1.0)
            assert not t.is_alive()
        assert sorted(received) == list(range(n))
    finally:
        sender.close()


def test_rendezvous_put_nowait_decrements_after_consume():
    """pending_handoffs returns to 0 after each successful handoff."""
    sender, receiver = Channel.create(capacity=0)

    def receive_one(r, sink: list[str]) -> None:
        sink.append(r.get())

    try:
        for round_idx in range(2):
            received: list[str] = []
            t = threading.Thread(target=receive_one, args=(receiver, received))
            t.start()
            assert _wait_for(lambda: _state_of(sender).waiting_receivers == 1)

            sender.put_nowait(f"msg-{round_idx}")
            t.join(timeout=1.0)
            assert not t.is_alive()
            assert received == [f"msg-{round_idx}"]
            assert _state_of(sender).pending_handoffs == 0
    finally:
        sender.close()
        receiver.close()


def test_rendezvous_concurrent_blocking_puts_pair_with_receivers():
    """N concurrent put() calls all return when N receivers are waiting."""
    n = 4
    sender, receiver = Channel.create(capacity=0)
    senders = [sender] + [sender.clone() for _ in range(n - 1)]
    receivers = [receiver] + [receiver.clone() for _ in range(n - 1)]

    received: list[int] = []
    received_lock = threading.Lock()
    sends_done = threading.Event()
    sends_completed = 0
    sends_lock = threading.Lock()

    def receive(r):
        try:
            value = r.get()
            with received_lock:
                received.append(value)
        finally:
            r.close()

    def send(s, value: int):
        try:
            s.put(value)
        finally:
            s.close()
        nonlocal sends_completed
        with sends_lock:
            sends_completed += 1
            if sends_completed == n:
                sends_done.set()

    receiver_threads = [threading.Thread(target=receive, args=(r,)) for r in receivers]
    for t in receiver_threads:
        t.start()

    try:
        assert _wait_for(lambda: _state_of(sender).waiting_receivers == n)

        sender_threads = [threading.Thread(target=send, args=(s, i)) for i, s in enumerate(senders)]
        for t in sender_threads:
            t.start()

        assert sends_done.wait(timeout=2.0), "blocking puts did not all return"
        for t in sender_threads:
            t.join(timeout=1.0)
            assert not t.is_alive()
        for t in receiver_threads:
            t.join(timeout=1.0)
            assert not t.is_alive()

        assert sorted(received) == list(range(n))
    finally:
        for s in senders:
            try:
                s.close()
            except ClosedResourceError:
                pass


def test_rendezvous_put_returns_when_its_own_item_consumed():
    """Blocking put waits for *its* item, not just any buffer drain."""
    sender, receiver = Channel.create(capacity=0)
    sender_b = sender.clone()
    received: list[str] = []
    b_returned = threading.Event()

    def receive_one():
        received.append(receiver.get())

    receiver_thread = threading.Thread(target=receive_one)
    receiver_thread.start()

    try:
        assert _wait_for(lambda: _state_of(sender).waiting_receivers == 1)

        # "a" claims the only waiting receiver.
        sender.put_nowait("a")

        def send_b():
            sender_b.put("b")
            b_returned.set()

        sender_b_thread = threading.Thread(target=send_b)
        sender_b_thread.start()

        # Receiver pops "a" first (FIFO). Sender B should still be in Phase 2
        # because its own item ("b") has not been consumed yet.
        receiver_thread.join(timeout=1.0)
        assert not receiver_thread.is_alive()
        assert received == ["a"]

        time.sleep(0.05)
        assert not b_returned.is_set(), "put('b') returned before 'b' was consumed"

        # New receiver consumes "b" — now B can return.
        received_b: list[str] = []
        receiver_b_thread = threading.Thread(target=lambda: received_b.append(receiver.get()))
        receiver_b_thread.start()
        receiver_b_thread.join(timeout=1.0)
        assert not receiver_b_thread.is_alive()
        assert received_b == ["b"]

        assert b_returned.wait(timeout=1.0), "put('b') did not return after 'b' was consumed"
        sender_b_thread.join(timeout=1.0)
        assert not sender_b_thread.is_alive()
    finally:
        sender.close()
        sender_b.close()
        receiver.close()


def test_concurrent_stress():
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
    expected = [sender_id * 1000 + i for sender_id in range(num_senders) for i in range(messages_per_sender)]

    assert sorted(received) == sorted(expected)


def test_get_with_timeout_raises_timeout_error():
    sender, receiver = Channel.create()
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        receiver.get(timeout=0.05)
    elapsed = time.monotonic() - started
    assert 0.04 <= elapsed < 0.5
    sender.close()
    receiver.close()


def test_get_with_timeout_zero_is_nonblocking():
    sender, receiver = Channel.create()
    with pytest.raises(TimeoutError):
        receiver.get(timeout=0)
    sender.put(1)
    assert receiver.get(timeout=0) == 1
    sender.close()
    receiver.close()


def test_put_with_timeout_raises_when_buffer_full():
    sender, receiver = Channel.create(capacity=1)
    sender.put(1)  # fills the buffer
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        sender.put(2, timeout=0.05)
    elapsed = time.monotonic() - started
    assert 0.04 <= elapsed < 0.5
    sender.close()
    receiver.close()


def test_get_nowait_empty_raises_would_block():
    sender, receiver = Channel.create()
    with pytest.raises(WouldBlock):
        receiver.get_nowait()
    sender.put(1)
    assert receiver.get_nowait() == 1
    sender.close()
    receiver.close()


def test_get_nowait_after_senders_closed_raises_eos():
    sender, receiver = Channel.create()
    sender.close()
    with pytest.raises(EndOfStream):
        receiver.get_nowait()
    receiver.close()


def test_get_unblocks_when_receiver_closed_from_other_thread():
    sender, receiver = Channel.create()
    raised: list[BaseException] = []

    def receive():
        try:
            receiver.get()
        except BaseException as exc:  # noqa: BLE001
            raised.append(exc)

    t = threading.Thread(target=receive)
    t.start()
    time.sleep(0.05)
    receiver.close()
    t.join(timeout=1.0)
    assert not t.is_alive()
    assert len(raised) == 1
    assert isinstance(raised[0], ClosedResourceError)
    sender.close()


def test_close_broadcasts_to_cloned_receivers():
    """When senders close, every cloned receiver waiting in get() must wake
    up and observe EndOfStream — not just one of them."""
    sender, receiver = Channel.create()
    receivers = [receiver] + [receiver.clone() for _ in range(3)]
    seen_eos = threading.Event()
    eos_count = 0
    eos_lock = threading.Lock()

    def receive(r):
        nonlocal eos_count
        try:
            r.get()
        except EndOfStream:
            with eos_lock:
                eos_count += 1
                if eos_count == len(receivers):
                    seen_eos.set()
        finally:
            r.close()

    threads = [threading.Thread(target=receive, args=(r,)) for r in receivers]
    for t in threads:
        t.start()
    time.sleep(0.05)
    sender.close()
    for t in threads:
        t.join(timeout=1.0)
        assert not t.is_alive()
    assert seen_eos.is_set()


def test_channel_cleanup():
    """Test that channels clean up properly when references are dropped."""
    for _ in range(10):
        sender, receiver = Channel.create()
        sender.put(42)
        assert receiver.get() == 42
        state = _state_of(sender)
        sender.close()
        receiver.close()

        # Verify state is clean
        assert state.open_send_channels == 0
        assert state.open_receive_channels == 0
        assert len(state.buffer) == 0
