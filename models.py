from enum import Enum
from typing import Any, Optional, List

# your “typo → canonical” map
_POWER_ALIASES = {
    "UK": "BRITAIN",
    "BRIT": "BRITAIN",
    "ENGLAND": "BRITAIN",
    "England": "BRITAIN",
    "Britain": "BRITAIN",
    "GERMAN": "GERMANY",
    "EGMANY": "GERMANY",
    "Germany": "GERMANY",
    "France": "FRANCE",
    "Italy": "ITALY",
    "USSR": "SOVIET",
    "SOVIET UNION": "SOVIET",
    "SOVIET_UNION": "SOVIET",
    "RUSSIA": "SOVIET",
    "Russia": "SOVIET",
    "Soviet Union": "SOVIET",
    "Soviet": "SOVIET",
    "Poland": "POLAND",
    "Spain": "SPAIN",
    "US": "USA",
    "AMERICA": "USA",
    "UNITED STATES": "USA",
    "UNITED_STATES": "USA",
    "United States": "USA",
    "America": "USA",
}

POWERS_ORDER: List[str] = ["BRITAIN", "FRANCE", "GERMANY", "ITALY", "POLAND", "SOVIET", "SPAIN", "USA"]

class PowerEnum(str, Enum):
    BRITAIN = "BRITAIN"
    FRANCE = "FRANCE"
    GERMANY = "GERMANY"
    ITALY = "ITALY"
    POLAND = "POLAND"
    SOVIET = "SOVIET"
    SPAIN = "SPAIN"
    USA = "USA"

    @classmethod
    def _missing_(cls, value: Any) -> Optional["Enum"]:
        """
        Called when you do PowerEnum(value) and `value` isn't one of the raw enum values.
        Here we normalize strings to upper‐stripped, apply aliases, then retry.
        """
        if isinstance(value, str):
            normalized = value.upper().strip()
            # apply any synonyms/typos
            normalized = _POWER_ALIASES.get(normalized, normalized)
            # look up in the normal value→member map
            member = cls._value2member_map_.get(normalized)
            if member is not None:
                return member

        # by default, let Enum raise the ValueError
        return super()._missing_(value)
