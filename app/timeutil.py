import calendar
import datetime


def shift_months(day: datetime.date, months: int) -> datetime.date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    clamped_day = min(day.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, clamped_day)


def humanize_relative(delta_days: int) -> str:
    if delta_days == 0:
        return "Today"

    future = delta_days > 0
    n = abs(delta_days)
    if n < 14:
        value, unit = n, "day"
    elif n < 60:
        value, unit = n // 7, "week"
    else:
        value, unit = n // 30, "month"

    phrase = f"{value} {unit}{'s' if value != 1 else ''}"
    return f"in {phrase}" if future else f"{phrase} ago"
