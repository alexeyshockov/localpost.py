import threading
import time
import pytest
from anyio import EndOfStream

from localpost.threadtools import Channel, SendChannel

def no_anyio

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
    num_messages_per_sender = 4
    senders = [sender] + [sender.clone() for _ in range(num_senders - 1)]

    def send_messages(thread_sender: SendChannel[str], sender_id: int):
        for i in range(3):
            thread_sender.put(f"sender{sender_id}-msg{i}")
            time.sleep(0.01)  # Small delay to mix messages
        sender.close()

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
    assert len(results) == num_senders * num_messages_per_sender
    # Check that we got messages from all senders
    for i in range(num_senders):
        sender_msgs = [msg for msg in results if msg.startswith(f"sender{i}")]
        assert len(sender_msgs) == num_messages_per_sender


def test_single_sender_multiple_receivers():
    """Test single sender with multiple receivers."""
    channel = Channel[int]()
    sender = channel.open_sender()
    results = {i: [] for i in range(3)}

    def receive_messages(receiver_id: int):
        receiver = channel.open_receiver()
        try:
            while True:
                value = receiver.get()
                results[receiver_id].append(value)
        except EndOfStream:
            pass
        receiver.close()

    # Start multiple receiver threads
    threads = []
    for i in range(3):
        t = threading.Thread(target=receive_messages, args=(i,))
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
    """Test that sending without receivers does not raise an error."""
    channel = Channel[int]()
    sender = channel.open_sender()

    # Should not raise an error anymore - items go to buffer
    sender.put(42)
    sender.put(43)

    # Now open a receiver and verify it gets the buffered items
    receiver = channel.open_receiver()
    assert receiver.get() == 42
    assert receiver.get() == 43

    sender.close()
    receiver.close()


def test_end_of_channel():
    """Test that receivers get EndOfStream when all senders close."""
    channel = Channel[int]()
    sender1 = channel.open_sender()
    sender2 = channel.open_sender()
    receiver = channel.open_receiver()

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
    channel = Channel[int]()
    sender = channel.open_sender()
    receiver = channel.open_receiver()

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
    channel = Channel[str]()
    sender = channel.open_sender()
    receiver = channel.open_receiver()
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


def test_concurrent_stress():
    """Stress test with many concurrent senders and receivers."""
    channel = Channel[int]()
    num_senders = 5
    num_receivers = 3
    messages_per_sender = 100

    received = []
    received_lock = threading.Lock()

    def sender_work(sender_id: int):
        sender = channel.open_sender()
        try:
            for i in range(messages_per_sender):
                sender.put(sender_id * 1000 + i)
        finally:
            sender.close()

    def receiver_work():
        receiver = channel.open_receiver()
        local_received = []
        try:
            while True:
                local_received.append(receiver.get())
        except EndOfStream:
            pass
        finally:
            receiver.close()

        with received_lock:
            received.extend(local_received)

    # Start all threads
    threads = []

    # Start senders first to ensure at least one is open when receivers start
    sender_threads = []
    for i in range(num_senders):
        t = threading.Thread(target=sender_work, args=(i,))
        t.start()
        sender_threads.append(t)
        threads.append(t)

    # Give senders time to start
    time.sleep(0.01)

    # Start receivers
    for _ in range(num_receivers):
        t = threading.Thread(target=receiver_work)
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


def test_channel_cleanup():
    """Test that channels clean up properly when references are dropped."""
    channel = Channel[int]()

    # Open and immediately close channels
    for _ in range(10):
        sender = channel.open_sender()
        receiver = channel.open_receiver()
        sender.put(42)
        assert receiver.get() == 42
        sender.close()
        receiver.close()

    # Verify state is clean
    assert channel._state.open_send_channels == 0
    assert channel._state.open_receive_channels == 0
    assert len(channel._state.buffer) == 0
