"""Machine-state questions the indexer asks before spending CPU.

Two policies that kensaku has no answer to: a laptop on battery should not
burn its charge on OCR, and a human typing at the keyboard should not pay
frame drops for an index run. Both answers come from the OS in one call.

- battery: psutil.sensors_battery() -> percent/plugged; True when discharging.
- user idle: seconds since the last input event (Win32 GetLastInputInfo),
  the same signal the watcher uses to schedule its work.
"""

from __future__ import annotations


def on_battery() -> bool:
    """True when the machine runs on battery power (or cannot tell: False).

    Indexing a screenshot costs OCR minutes; the policy belongs to the
    caller, this only reports the fact."""
    try:
        import psutil
    except ImportError:
        return False
    battery = psutil.sensors_battery()
    if battery is None:
        return False  # a desktop has no battery to conserve
    return not battery.power_plugged


def seconds_idle() -> float:
    """Seconds since the last keyboard/mouse event, OS-reported."""
    from .idle import seconds_idle as _os_idle

    value = _os_idle()
    return float(value) if value is not None else 0.0
