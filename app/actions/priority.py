"""The priority a FIRMS event is created with: by confidence, raised to red for a strong fire."""

import math
from typing import Any, Mapping, Optional

# EarthRanger's priority values, by the colour they show as.
PRIORITY_VALUES = {"gray": 0, "green": 100, "amber": 200, "red": 300}
RED = PRIORITY_VALUES["red"]


def event_priority(
    confidence: Optional[str],
    frp: Any,
    *,
    by_confidence: Mapping[str, str],
    red_frp_threshold: Optional[float],
) -> Optional[int]:
    """Priority for one detection, or None to leave the destination's default.

    `by_confidence` maps a FIRMS confidence value to a colour. A fire radiative power (MW) at or above
    `red_frp_threshold` makes the event red whatever its confidence; None turns that off.
    """
    if red_frp_threshold is not None and isinstance(frp, (int, float)) and not isinstance(frp, bool):
        if not math.isnan(frp) and frp >= red_frp_threshold:
            return RED
    colour = by_confidence.get(confidence)
    return PRIORITY_VALUES.get(colour)
