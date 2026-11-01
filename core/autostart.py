"""Start with Windows, quietly, in the tray.

A search box you have to launch is a search box you do not use. This registers
the app under HKCU\\...\\Run — one value, no installer, no COM, and Windows
shows it in Task Manager -> Startup where it can be switched off like any other
program.

`pythonw.exe` matters: launched with `python.exe` the app would come up behind
a black console window that never closes, which is the opposite of a program.
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AutoFind"


def supported() -> bool:
    return sys.platform == "win32"


def launcher_target(hidden: bool = True) -> tuple[str, str]:
    """(executable, arguments) that start the app without a console window."""
    if getattr(sys, "frozen", False):
        target = sys.executable
    else:
        python = Path(sys.executable)
        # pythonw has no console; fall back to python if it is not there.
        windowed = python.with_name("pythonw.exe")
        target = str(windowed if windowed.exists() else python)
    root = Path(__file__).resolve().parent.parent
    arguments = f'"{root / "run.py"}"'
    if hidden:
        arguments += " --hidden"
    return target, arguments


def launcher_command(hidden: bool = True) -> str:
    """The exact command line the login entry runs, as one string."""
    target, arguments = launcher_target(hidden)
    return f'"{target}" {arguments}'


def status() -> dict:
    """What the registry says right now, and what it would say if enabled."""
    out = {
        "supported": supported(),
        "enabled": False,
        "command": launcher_command(),
        "name": VALUE_NAME,
    }
    if not supported():
        return out
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
        out["enabled"] = True
        out["registered"] = str(value)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return out


def enable(hidden: bool = True) -> str:
    """Write the Run value and return the command that was registered."""
    if not supported():
        raise RuntimeError("autostart is Windows-only for now")
    import winreg

    command = launcher_command(hidden)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
    return command


def disable() -> bool:
    """Remove the Run value. True when something was actually removed."""
    if not supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
