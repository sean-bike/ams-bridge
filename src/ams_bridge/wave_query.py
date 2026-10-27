"""Queries over native axes. No resampling, unit guessing or extrapolation."""
from bisect import bisect_left, bisect_right
from decimal import Decimal, InvalidOperation, localcontext

from .common import BridgeError


def decimal(value):
    try:
        if isinstance(value, bool):
            raise ValueError()
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError()
        return number
    except (ValueError, InvalidOperation) as exc:
        raise BridgeError(f"invalid finite coordinate: {value!r}") from exc


def factor(signal):
    if signal["time_unit"] == "s":
        return Decimal(1)
    scale = signal["timescale"]
    return Decimal(scale["magnitude"]) * Decimal({"s": "1", "ms": "1e-3", "us": "1e-6",
                                               "ns": "1e-9", "ps": "1e-12", "fs": "1e-15"}[scale["unit"]])


def coordinate(signal, value, unit):
    value = decimal(value)
    if unit == "tick":
        if signal["kind"] != "digital" or value != value.to_integral_value():
            raise BridgeError("tick coordinates require integral digital ticks")
        return value
    if unit != "s":
        raise BridgeError("time_unit must be s or tick")
    scale = factor(signal)
    with localcontext() as context:
        context.prec = max(context.prec, len(value.as_tuple().digits) + len(scale.as_tuple().digits) + 2)
        return value / scale


def bounds(signal, window, unit):
    if signal["coverage"] is None:
        raise BridgeError(f"{signal['name']}: no points", "empty_waveform")
    lo, hi = map(decimal, signal["coverage"])
    if window is None:
        return lo, hi
    a, b = (coordinate(signal, t, unit) for t in window)
    if a > b or a < lo or b > hi:
        raise BridgeError(f"{signal['name']}: window exceeds observed coverage {signal['coverage']}", "out_of_range")
    return a, b


def selected_points(signal, window=None, unit="s", *, include_end=False):
    lo, hi = bounds(signal, window, unit)
    closed = window is None or include_end
    if lo == hi and not closed:
        return []
    points = signal["points"]
    axis = [decimal(p["time"]) for p in points]
    a, b = bisect_left(axis, lo), (bisect_right if closed else bisect_left)(axis, hi)
    result = points[a:b]
    return result


