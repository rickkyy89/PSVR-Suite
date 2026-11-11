"""One-way clock mapping with a bounded transport-floor observation window.

The clocks are in seconds. A two-second sliding minimum follows slow relative
drift in either direction without treating individual transport spikes as clock
changes. Upward corrections have a rate limit even when the window expires;
otherwise queued old samples would look fresh after a transport stall.
This is not clock synchronisation: constant latency is unobservable, and drift
and latency changes within the rate budget cannot be distinguished.
Published timestamps are immutable; equal source sample times stay equal even
when sends continue during sensor loss. Callers own session/order validation.
"""
from collections import deque


class ImuClockMapper:
    WINDOW_S = 2.0
    # Supported relative clock-rate error (1000 ppm). A transport stall is not
    # evidence of a clock jump: never raise the floor faster than this rate.
    MAX_DRIFT = 1e-3

    def __init__(self):
        self.observations = deque()
        self.offset = None
        self.last_sent = None
        self.last_sample = None
        self.last_mapped = None
        self.last_received = None

    def reset(self):
        # A new clock origin (session/epoch/elapsed reset) invalidates the old
        # mapping anchors. Monotonicity is only guaranteed within an origin:
        # a new session's first sample can itself be old.
        self.observations.clear()
        self.offset = self.last_sent = self.last_sample = self.last_mapped = None
        self.last_received = None

    def map(self, sample, sent, received):
        if self.last_sent is None or sent > self.last_sent:
            while self.observations and self.observations[0][0] < received-self.WINDOW_S:
                self.observations.popleft()
            candidate = received-sent
            # Monotone minimum queue: bounded by observations in WINDOW_S,
            # amortised O(1), independent of session duration.
            while self.observations and self.observations[-1][1] >= candidate:
                self.observations.pop()
            self.observations.append((received, candidate))
            floor = self.observations[0][1]
            self.offset = (floor if self.offset is None else
                           min(floor, self.offset + self.MAX_DRIFT *
                               max(0., received-self.last_received)))
            self.last_sent = sent
            self.last_received = received
        mapped = min(received, sample+self.offset)
        if self.last_mapped is not None:
            mapped = (self.last_mapped if sample == self.last_sample else
                      max(self.last_mapped, mapped))
        self.last_sample, self.last_mapped = sample, mapped
        return mapped
