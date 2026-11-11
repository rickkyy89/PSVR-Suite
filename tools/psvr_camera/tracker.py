"""Live tracking pipeline: source -> detector -> estimator -> filter -> sender.

Extracted from the CLI so the whole pipeline can be exercised with an injected
fake source, detector and sender. The loop is bounded by captured frames *and*
seconds, refuses a frame whose size does not match the calibrated intrinsics,
stops after a run of camera read failures (no busy-spin), and always emits a
final ``valid:false`` sample on exit or disconnect so a receiver never holds a
stale metric pose.

A sample is timestamped with the source's capture time
(``last_frame_monotonic``) when the source exposes one — the native PS Eye
adapter and the OpenCV latest-frame adapter do — so the filter and the
receiver see when the sensor delivered the frame, not the instant before the
blocking read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .estimator import PositionResult
from .protocol import PositionSample


@dataclass
class TrackConfig:
    frames: int = 0  # 0 = unlimited; counts every captured frame, not valid ones
    max_seconds: float = 0.0  # 0 = unlimited
    marker_id: int = 0
    max_read_failures: int = 30
    expected_size: Optional[tuple] = None  # (width, height) from the intrinsics
    source_name: str = "aruco"


@dataclass
class TrackSummary:
    captured_frames: int
    valid_frames: int
    sent: int
    read_failures: int
    duration_s: float
    exit_reason: str
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "captured_frames": self.captured_frames,
            "valid_frames": self.valid_frames,
            "sent": self.sent,
            "read_failures": self.read_failures,
            "duration_s": self.duration_s,
            "exit_reason": self.exit_reason,
            "error": self.error,
        }


def missing_result() -> PositionResult:
    return PositionResult(
        valid=False, state="lost", reason="marker not found", source="aruco"
    )


def _capture_stamp(source, now: float) -> float:
    """Prefer the source's own capture timestamp over the pre-read loop clock.

    ``source.last_frame_monotonic`` is set by the adapters that publish frames
    from a reader thread (native PS Eye and the OpenCV latest-frame wrapper).
    It is the moment the sensor delivered the frame, which is what a
    time-based filter and a latency-sensitive receiver must use; ``now`` is
    only a fallback for sources that do not report one.
    """
    stamp = getattr(source, "last_frame_monotonic", None)
    if stamp is None:
        return now
    try:
        stamp = float(stamp)
    except (TypeError, ValueError):
        return now
    if stamp != stamp or stamp <= 0.0:  # NaN or unusable clock
        return now
    return stamp


def sample_from(result, position_filter, sequences, now: float) -> PositionSample:
    if result.valid:
        smoothed = position_filter.update(result.position_world_m, now)
        return PositionSample(
            seq=sequences.next(),
            monotonic_s=now,
            valid=True,
            state="tracking",
            source=result.source,
            position_m=[float(v) for v in smoothed],
            orientation_wxyz=result.orientation_world_wxyz,
            reprojection_px=result.reprojection_px,
            inliers=result.inliers,
            ambiguity_ratio=result.ambiguity_ratio,
        )
    fresh = position_filter.mark_missing(now)
    if not fresh:
        position_filter.reset()
    return PositionSample(
        seq=sequences.next(),
        monotonic_s=now,
        valid=False,
        state=result.state,
        source=result.source,
        note=result.reason,
    )


def final_invalid_sample(
    sequences,
    position_filter,
    now: float,
    note: str,
    state: str = "lost",
    source: str = "aruco",
) -> PositionSample:
    """Build the terminating ``valid:false`` sample and clear any stale pose."""
    position_filter.reset()
    if state not in ("lost", "error", "ambiguous", "calibrating"):
        state = "lost"
    return PositionSample(
        seq=sequences.next(),
        monotonic_s=now,
        valid=False,
        state=state,
        source=source,
        note=note,
    )


def run_track(
    source,
    detector,
    estimator,
    sender,
    sequences,
    position_filter,
    config: TrackConfig,
    on_sample: Optional[Callable] = None,
    now_fn: Callable[[], float] = None,
    sleep_fn: Callable[[float], None] = None,
) -> TrackSummary:
    """Run the bounded tracking loop. Returns a :class:`TrackSummary`.

    ``source`` needs ``read()``; ``detector`` needs ``detect(frame)``;
    ``sender`` may be ``None`` or need ``send_sample(sample)``. ``on_sample`` is
    called for every emitted sample (including the final invalid one) for
    logging.
    """
    import time as _time

    now_fn = now_fn or _time.monotonic
    sleep_fn = sleep_fn or _time.sleep
    started = now_fn()
    captured = 0
    valid_frames = 0
    sent = 0
    read_failures = 0
    consecutive_failures = 0
    exit_reason = "completed"
    error: Optional[str] = None

    def emit(sample: PositionSample) -> None:
        nonlocal sent, error
        if sender is not None:
            try:
                sender.send_sample(sample)
                sent += 1
            except Exception as exc:  # noqa: BLE001 - reported via the summary
                error = error or f"send failed: {exc}"
                raise
        if on_sample is not None:
            on_sample(sample)

    try:
        while True:
            now = now_fn()
            if config.max_seconds and now - started >= config.max_seconds:
                exit_reason = "max_seconds"
                break
            if config.frames and captured >= config.frames:
                exit_reason = "frames"
                break
            ok, frame = source.read()
            if not ok or frame is None:
                read_failures += 1
                consecutive_failures += 1
                if consecutive_failures >= max(int(config.max_read_failures), 1):
                    error = (
                        f"camera read failed {consecutive_failures} times in a row"
                    )
                    exit_reason = "read_failure"
                    break
                sleep_fn(0.005)
                continue
            consecutive_failures = 0
            if config.expected_size is not None:
                shape = getattr(frame, "shape", None)
                if shape is None or len(shape) < 2:
                    error = "captured frame has no usable shape"
                    exit_reason = "resolution_mismatch"
                    break
                actual = (int(shape[1]), int(shape[0]))
                expected = (
                    int(config.expected_size[0]),
                    int(config.expected_size[1]),
                )
                if actual != expected:
                    error = (
                        f"live frame {actual[0]}x{actual[1]} does not match "
                        f"calibrated intrinsics image_size "
                        f"{expected[0]}x{expected[1]}; refusing to estimate a "
                        "wrong-metric pose"
                    )
                    exit_reason = "resolution_mismatch"
                    break
            captured += 1
            detected = detector.detect(frame)
            corners = detected.get(int(config.marker_id))
            result = missing_result() if corners is None else estimator.estimate(corners)
            if result.valid:
                valid_frames += 1
            stamp = _capture_stamp(source, now)
            try:
                emit(sample_from(result, position_filter, sequences, stamp))
            except Exception:
                exit_reason = "transport_error"
                break
    except KeyboardInterrupt:
        exit_reason = "interrupted"
    finally:
        # Always terminate the stream with an explicit invalid sample so the
        # receiver does not hold a stale pose after exit or disconnect.
        try:
            final = final_invalid_sample(
                sequences,
                position_filter,
                now_fn(),
                note=error or exit_reason,
                state="error" if error else "lost",
                source=config.source_name,
            )
            if sender is not None:
                try:
                    sender.send_sample(final)
                    sent += 1
                except Exception as exc:  # noqa: BLE001
                    error = error or f"final send failed: {exc}"
            if on_sample is not None:
                on_sample(final)
        except Exception:  # noqa: BLE001 - never mask the original outcome
            pass

    return TrackSummary(
        captured_frames=captured,
        valid_frames=valid_frames,
        sent=sent,
        read_failures=read_failures,
        duration_s=round(now_fn() - started, 3),
        exit_reason=exit_reason,
        error=error,
    )


__all__ = [
    "TrackConfig",
    "TrackSummary",
    "run_track",
    "sample_from",
    "missing_result",
    "final_invalid_sample",
]
