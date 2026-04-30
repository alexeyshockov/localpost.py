"""Property-based tests for ``Channel`` (single-threaded).

These complement the example-based / threaded tests in ``channels.py``.

Scope:
    Single-threaded ``RuleBasedStateMachine`` driving sequences of
    ``put_nowait`` / ``get`` / ``close`` / ``clone`` against a small reference
    model (deque + counters). Catches state-tracking regressions —
    ``open_send_channels``, ``open_receive_channels``, buffer length, capacity
    bound — under arbitrary op orderings.

Out of scope:
    Rendezvous (``capacity=0``) and any property that depends on thread
    interleavings. Those live in ``channels.py``.
"""

from __future__ import annotations

from collections import deque
from typing import ClassVar

import pytest
from anyio import ClosedResourceError, EndOfStream, WouldBlock
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    precondition,
    rule,
)

from localpost import threadtools
from localpost.threadtools import Channel


def _noop_check_cancelled() -> None:
    return None


@pytest.fixture
def no_anyio(monkeypatch: pytest.MonkeyPatch):
    # The channel calls ``threadtools.check_cancelled()`` (an alias of
    # ``anyio.from_thread.check_cancelled``); outside an anyio thread context
    # that raises. Stub it for the duration of the test.
    monkeypatch.setattr(threadtools, "check_cancelled", _noop_check_cancelled)


class _ChannelMachine(RuleBasedStateMachine):
    """Drives a ``Channel`` with one capacity, one producer-mode flag.

    Subclasses set ``capacity`` and ``single_producer`` as class vars; the
    machine is then exposed to pytest via ``Subclass.TestCase``.
    """

    capacity: ClassVar[int | None] = None
    single_producer: ClassVar[bool] = False

    senders = Bundle("senders")
    receivers = Bundle("receivers")
    closed_senders = Bundle("closed_senders")
    closed_receivers = Bundle("closed_receivers")

    def __init__(self) -> None:
        super().__init__()
        s, r = Channel.create(capacity=self.capacity)
        self._first_sender = s
        self._first_receiver = r
        self._state = s._state
        self.model_buffer: deque = deque()
        self.model_open_senders = 1
        self.model_open_receivers = 1
        self.model_sent: list = []
        self.model_received: list = []

    # --- seed bundles -------------------------------------------------------

    @initialize(target=senders)
    def _seed_sender(self):
        return self._first_sender

    @initialize(target=receivers)
    def _seed_receiver(self):
        return self._first_receiver

    # --- clone --------------------------------------------------------------

    @precondition(lambda self: not self.single_producer)
    @rule(target=senders, s=senders)
    def clone_sender(self, s):
        new_s = s.clone()
        self.model_open_senders += 1
        return new_s

    @rule(target=receivers, r=receivers)
    def clone_receiver(self, r):
        new_r = r.clone()
        self.model_open_receivers += 1
        return new_r

    # --- put_nowait variants ------------------------------------------------

    @precondition(
        lambda self: self.model_open_receivers > 0
        and (self.capacity is None or len(self.model_buffer) < self.capacity)
    )
    @rule(s=senders, item=st.integers())
    def put_nowait_ok(self, s, item):
        s.put_nowait(item)
        self.model_buffer.append(item)
        if self.single_producer:
            self.model_sent.append(item)

    @precondition(
        lambda self: self.capacity is not None
        and self.capacity > 0
        and len(self.model_buffer) >= self.capacity
        and self.model_open_receivers > 0
    )
    @rule(s=senders, item=st.integers())
    def put_nowait_full_blocks(self, s, item):
        with pytest.raises(WouldBlock):
            s.put_nowait(item)

    @precondition(lambda self: self.model_open_receivers == 0)
    @rule(s=senders, item=st.integers())
    def put_nowait_no_receivers(self, s, item):
        with pytest.raises(ClosedResourceError):
            s.put_nowait(item)

    # --- get variants -------------------------------------------------------

    @precondition(lambda self: len(self.model_buffer) > 0)
    @rule(r=receivers)
    def get_nonempty(self, r):
        expected = self.model_buffer.popleft()
        actual = r.get()
        assert actual == expected
        if self.single_producer:
            self.model_received.append(actual)

    @precondition(lambda self: len(self.model_buffer) == 0 and self.model_open_senders == 0)
    @rule(r=receivers)
    def get_drained_raises_eos(self, r):
        with pytest.raises(EndOfStream):
            r.get()

    # --- close --------------------------------------------------------------

    @rule(target=closed_senders, s=consumes(senders))
    def close_sender(self, s):
        s.close()
        self.model_open_senders -= 1
        return s

    @rule(target=closed_receivers, r=consumes(receivers))
    def close_receiver(self, r):
        r.close()
        self.model_open_receivers -= 1
        return r

    @rule(s=closed_senders, item=st.integers())
    def put_on_closed_sender_raises(self, s, item):
        with pytest.raises(ClosedResourceError):
            s.put_nowait(item)

    @rule(r=closed_receivers)
    def get_on_closed_receiver_raises(self, r):
        with pytest.raises(ClosedResourceError):
            r.get()

    @rule(s=closed_senders)
    def clone_closed_sender_raises(self, s):
        with pytest.raises(ClosedResourceError):
            s.clone()

    @rule(r=closed_receivers)
    def clone_closed_receiver_raises(self, r):
        with pytest.raises(ClosedResourceError):
            r.clone()

    # --- invariants ---------------------------------------------------------

    @invariant()
    def open_handle_counters_match_model(self):
        assert self._state.open_send_channels == self.model_open_senders
        assert self._state.open_receive_channels == self.model_open_receivers

    @invariant()
    def buffer_length_matches_model(self):
        assert len(self._state.buffer) == len(self.model_buffer)

    @invariant()
    def buffer_within_capacity(self):
        if self.capacity is not None:
            assert len(self._state.buffer) <= self.capacity

    @invariant()
    def waiting_receivers_zero_in_single_thread(self):
        # No thread ever blocks in get() here, so this should never grow.
        assert self._state.waiting_receivers == 0

    @invariant()
    def fifo_under_single_producer(self):
        if not self.single_producer:
            return
        # Items received so far must be a prefix of items sent so far.
        n = len(self.model_received)
        assert self.model_sent[:n] == self.model_received


class _UnboundedMachine(_ChannelMachine):
    capacity: ClassVar[int | None] = None


class _Bounded1Machine(_ChannelMachine):
    capacity: ClassVar[int | None] = 1


class _Bounded4Machine(_ChannelMachine):
    capacity: ClassVar[int | None] = 4


class _SingleProducerUnboundedMachine(_ChannelMachine):
    capacity: ClassVar[int | None] = None
    single_producer: ClassVar[bool] = True


class _SingleProducerBounded4Machine(_ChannelMachine):
    capacity: ClassVar[int | None] = 4
    single_producer: ClassVar[bool] = True


# Expose the auto-generated unittest TestCases to pytest. ``usefixtures``
# applies ``no_anyio`` for the lifetime of each Hypothesis test method.


@pytest.mark.usefixtures("no_anyio")
class TestChannelUnbounded(_UnboundedMachine.TestCase):
    pass


@pytest.mark.usefixtures("no_anyio")
class TestChannelBounded1(_Bounded1Machine.TestCase):
    pass


@pytest.mark.usefixtures("no_anyio")
class TestChannelBounded4(_Bounded4Machine.TestCase):
    pass


@pytest.mark.usefixtures("no_anyio")
class TestChannelSingleProducerUnbounded(_SingleProducerUnboundedMachine.TestCase):
    pass


@pytest.mark.usefixtures("no_anyio")
class TestChannelSingleProducerBounded4(_SingleProducerBounded4Machine.TestCase):
    pass
