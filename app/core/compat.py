"""Standard-library backports for the oldest Python the gateway supports.

Ubuntu 22.04 LTS ships **Python 3.10** as its `python3`, and 22.04 is a declared
target (docs/DEPLOYMENT.md §0). Everything in here re-exports the real standard
library on 3.11+ and only defines a substitute on 3.10, so the backport code
runs on exactly the one version that needs it and disappears the day 22.04 is
dropped.
"""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # pragma: no cover - only reachable on Python 3.10

    from enum import Enum

    class StrEnum(str, Enum):
        """`enum.StrEnum` (3.11) for 3.10.

        A plain ``class X(str, Enum)`` is **not** a drop-in replacement. From
        3.11 onward ``str(X.A)`` and ``f"{X.A}"`` on a mixed-in enum render as
        ``"X.A"``, while ``StrEnum`` renders ``"a"``. These values are written
        into YAML registry files, JSON responses and log lines, so the two must
        agree byte for byte across versions. Pinning ``__str__``/``__format__``
        to ``str``'s own makes 3.10 produce what 3.11+ produces.
        """

        __str__ = str.__str__
        __format__ = str.__format__

        @staticmethod
        def _generate_next_value_(name, start, count, last_values):  # noqa: ANN001
            # Matches 3.11's StrEnum: `auto()` yields the lower-cased member
            # name. No member uses auto() today; this keeps it true if one does.
            return name.lower()


__all__ = ["StrEnum"]
