"""Time-based One Euro smoothing of the final, unrecentered world head point.

No prediction, outlier rejection, validity promotion or frame buffering. A shared
cutoff from the derivative norm avoids axis-dependent response to lateral motion.
"""
import math

import numpy as np


# Sentinel distinguishing "packet key absent" (contract error -> ValueError)
# from "key present with None value" (legitimate lost-packet payload).
_MISSING = object()


class HeadPositionFilter:
    def __init__(self, min_cutoff_hz=8.0, beta=15.0, max_cutoff_hz=40.0,
                 derivative_cutoff_hz=4.0, max_gap_s=0.250, invalid_hold_s=0.100):
        values = (min_cutoff_hz, beta, max_cutoff_hz, derivative_cutoff_hz,
                  max_gap_s, invalid_hold_s)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("filter settings must be finite")
        if (min_cutoff_hz <= 0 or beta < 0 or max_cutoff_hz < min_cutoff_hz
                or derivative_cutoff_hz <= 0 or max_gap_s <= 0
                or not 0 <= invalid_hold_s <= max_gap_s):
            raise ValueError("invalid position filter settings")
        self.settings = dict(min_cutoff_hz=min_cutoff_hz, beta=beta,
                             max_cutoff_hz=max_cutoff_hz,
                             derivative_cutoff_hz=derivative_cutoff_hz,
                             max_gap_s=max_gap_s, invalid_hold_s=invalid_hold_s)
        self.reset()

    def reset(self):
        """Discard history on explicit realignment or callback shutdown."""
        self._identity = None
        self._alignment = None
        self._clear_history()
        self.raw_world_position_m = None
        self.filtered_world_position_m = None

    def _clear_history(self):
        self._raw = None
        self._filtered = None
        self._derivative = np.zeros(3)
        self._last_valid_s = None
        self._lost = False
        self.cutoff_hz = self.settings["min_cutoff_hz"]

    @staticmethod
    def _alpha(cutoff, dt):
        return 1.0 / (1.0 + 1.0 / (2.0 * math.pi * cutoff * dt))

    def _hold_expired(self, dt):
        # Subtracting monotonic timestamps can put exactly 100 ms just above
        # 0.100; do not turn the documented brief-loss boundary into a reset.
        hold = self.settings["invalid_hold_s"]
        return dt > hold and not math.isclose(dt, hold, rel_tol=0, abs_tol=1e-12)

    def invalid(self, now):
        """Keep brief-loss history only; never return a synthetic position."""
        if not math.isfinite(now) or now < 0:
            raise ValueError("invalid filter timestamp")
        self.raw_world_position_m = None
        self.filtered_world_position_m = None
        self._lost = True
        if (self._last_valid_s is not None
                and (now < self._last_valid_s
                      or self._hold_expired(now-self._last_valid_s))):
            self._clear_history()

    def filter_packet(self, packet, alignment, now):
        """Copy a bound packet, changing only a valid final position_m.

        Invalid frames do not advance the last-valid time or derivative; the next
        valid observation therefore uses the actual elapsed capture time.
        """
        if not math.isfinite(now) or now < 0:
            raise ValueError("invalid filter timestamp")
        identity = (None if packet is None else
                    (packet["session_id"], packet["recenter_epoch"]))
        a = None if alignment is None else np.asarray(alignment, float)
        if a is not None and (a.shape != (3, 3) or not np.isfinite(a).all()):
            raise ValueError("invalid filter alignment")
        changed = ((identity is not None and identity != self._identity)
                   or (a is None) != (self._alignment is None)
                   or (a is not None and self._alignment is not None
                       and not np.array_equal(a, self._alignment)))
        if changed:
            self._clear_history()
        if identity is not None:
            self._identity = identity
        self._alignment = None if a is None else a.copy()
        if packet is None:
            self.invalid(now)
            return None
        # B6-08: malformed packets report ValueError like every other input
        # validation here, never a bare KeyError.
        valid = packet.get("valid", _MISSING)
        if valid is _MISSING:
            raise ValueError("position packet missing 'valid'")
        if not valid:
            self.invalid(now)
            return dict(packet)
        position = packet.get("position_m", _MISSING)
        if position is _MISSING:
            raise ValueError("position packet missing 'position_m'")
        raw = np.asarray(position, float)
        if raw.shape != (3,) or not np.isfinite(raw).all():
            raise ValueError("head position must be three finite metres")
        dt = None if self._last_valid_s is None else now-self._last_valid_s
        if dt is not None and (dt <= 0 or dt >= self.settings["max_gap_s"]
                               or math.isclose(dt, self.settings["max_gap_s"],
                                               rel_tol=0, abs_tol=1e-12)
                               or (self._lost and self._hold_expired(dt))):
            self._clear_history()
        if self._raw is None:
            self._filtered = raw.copy()
        else:
            derivative = (raw-self._raw)/dt
            alpha_d = self._alpha(self.settings["derivative_cutoff_hz"], dt)
            self._derivative += alpha_d * (derivative-self._derivative)
            self.cutoff_hz = min(self.settings["max_cutoff_hz"],
                                 self.settings["min_cutoff_hz"]
                                 + self.settings["beta"] * float(np.linalg.norm(self._derivative)))
            self._filtered += self._alpha(self.cutoff_hz, dt) * (raw-self._filtered)
        self._raw = raw.copy()
        self._last_valid_s = now
        self._lost = False
        self.raw_world_position_m = raw.tolist()
        self.filtered_world_position_m = self._filtered.tolist()
        return {**packet, "position_m": self.filtered_world_position_m.copy()}

    def diagnostics(self):
        return {"raw_world_position_m": self.raw_world_position_m,
                "filtered_world_position_m": self.filtered_world_position_m,
                "settings": dict(self.settings), "cutoff_hz": self.cutoff_hz}
