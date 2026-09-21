import datetime

import pytest

from app.timeutil import humanize_relative, shift_months

D = datetime.date


@pytest.mark.parametrize(
    "start,months,expected",
    [
        (D(2026, 1, 15), 1, D(2026, 2, 15)),
        (D(2026, 1, 31), 1, D(2026, 2, 28)),  # clamp to short month
        (D(2024, 1, 31), 1, D(2024, 2, 29)),  # leap year
        (D(2026, 12, 15), 1, D(2027, 1, 15)),  # year rollover
        (D(2026, 1, 15), -1, D(2025, 12, 15)),  # backwards across year
        (D(2026, 3, 31), -1, D(2026, 2, 28)),
        (D(2026, 5, 10), 0, D(2026, 5, 10)),
        (D(2026, 1, 15), 24, D(2028, 1, 15)),
        (D(2026, 1, 15), -13, D(2024, 12, 15)),
        (D(2026, 11, 30), 3, D(2027, 2, 28)),
    ],
)
def test_shift_months(start, months, expected):
    assert shift_months(start, months) == expected


@pytest.mark.parametrize(
    "delta,expected",
    [
        (0, "Today"),
        (1, "in 1 day"),
        (-1, "1 day ago"),
        (5, "in 5 days"),
        (-13, "13 days ago"),
        (14, "in 2 weeks"),
        (-14, "2 weeks ago"),
        (20, "in 2 weeks"),
        (59, "in 8 weeks"),
        (-59, "8 weeks ago"),
        (60, "in 2 months"),
        (-90, "3 months ago"),
        (400, "in 13 months"),
    ],
)
def test_humanize_relative(delta, expected):
    assert humanize_relative(delta) == expected
