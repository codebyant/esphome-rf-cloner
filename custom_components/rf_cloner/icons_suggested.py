"""Icon suggestions for a newly named command.

Only ever a suggestion: it pre-fills the icon field in the learn and edit forms, and the user
overwrites it by typing something else. Nothing here is applied to a command that already has an
icon, and nothing here overrides an icon the user set on the entity registry record.

Every name in this module exists in Material Design Icons; a guess that does not resolve would
render as a blank tile, so the tables stay deliberately small and literal rather than clever.
"""

from __future__ import annotations

from .const import DEFAULT_COMMAND_ICON, TARGET_TYPE_GENERIC

# What a target of each type suggests when nothing in the command's name is recognised. A target
# type is a much weaker signal than the name, so this is only the last step before the default.
_BY_TARGET_TYPE: dict[str, str] = {
    "fan": "mdi:fan",
    "gate": "mdi:gate",
    "light": "mdi:lightbulb",
    "cover": "mdi:window-shutter",
    "tv": "mdi:television",
    "air_conditioner": "mdi:air-conditioner",
}

# Keywords recognised in any command name, in order. The first hit wins, so the list runs from
# the most specific phrasing to the least: "speed_up" has to be tried before "up".
#
# Command names are restricted to ASCII letters, digits, underscore, hyphen and full stop, which
# is why matching against a lowercased name with separators folded to spaces is enough.
_BY_KEYWORD: tuple[tuple[tuple[str, ...], str], ...] = (
    (("speed up", "speedup", "faster", "speed plus"), "mdi:fan-plus"),
    (("speed down", "speeddown", "slower", "speed minus"), "mdi:fan-minus"),
    (("speed 1", "speed1", "low"), "mdi:fan-speed-1"),
    (("speed 2", "speed2", "medium", "med"), "mdi:fan-speed-2"),
    (("speed 3", "speed3", "high", "max"), "mdi:fan-speed-3"),
    (("vol up", "volume up", "vol plus"), "mdi:volume-plus"),
    (("vol down", "volume down", "vol minus"), "mdi:volume-minus"),
    (("light", "lamp", "luz"), "mdi:lightbulb"),
    (("power", "on off", "onoff", "toggle"), "mdi:power"),
    (("open", "abrir"), "mdi:gate-open"),
    (("close", "shut", "fechar"), "mdi:gate"),
    (("stop", "halt", "parar"), "mdi:stop"),
    (("up", "raise", "subir"), "mdi:arrow-up"),
    (("down", "lower", "descer"), "mdi:arrow-down"),
    (("fan", "ventilador"), "mdi:fan"),
    (("cool", "cold"), "mdi:snowflake"),
    (("heat", "warm"), "mdi:fire"),
    (("swing", "oscillate"), "mdi:swap-vertical"),
    (("play",), "mdi:play"),
    (("pause",), "mdi:pause"),
)

# Target types that read a bare "open"/"close"/"stop" as a shutter rather than a gate.
_COVER_LIKE = frozenset({"cover"})
_COVER_OVERRIDES: dict[str, str] = {
    "mdi:gate-open": "mdi:window-shutter-open",
    "mdi:gate": "mdi:window-shutter",
}


def _normalise(name: str) -> str:
    """Fold a command name to the lowercase, space-separated form the keywords are written in."""
    folded = name.lower()
    for separator in ("_", "-", "."):
        folded = folded.replace(separator, " ")
    return f" {' '.join(folded.split())} "


def suggest_icon(command_name: str, target_type: str = TARGET_TYPE_GENERIC) -> str:
    """Suggest an icon for a command called `command_name` on a target of `target_type`.

    Falls through three steps: a keyword in the name, the target's type, then the generic default.
    Always returns something, so a form always has a value to offer.
    """
    haystack = _normalise(command_name)
    for keywords, icon in _BY_KEYWORD:
        if any(f" {keyword} " in haystack for keyword in keywords):
            if target_type in _COVER_LIKE:
                return _COVER_OVERRIDES.get(icon, icon)
            return icon
    return _BY_TARGET_TYPE.get(target_type, DEFAULT_COMMAND_ICON)
