from __future__ import annotations

from datetime import UTC, datetime

TENURE_BUCKETS: tuple[tuple[int | None, str], ...] = (
    (7, "до 7 дней"),
    (30, "7–30 дней"),
    (90, "1–3 месяца"),
    (365, "3–12 месяцев"),
    (730, "1–2 года"),
    (None, "2+ года"),
)


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def age_days(moment: datetime, now: datetime | None = None) -> int:
    now = as_utc(now or datetime.now(UTC))
    return max(0, (now - as_utc(moment)).days)


def age_label(moment: datetime, now: datetime | None = None) -> str:
    days = age_days(moment, now)
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "1 день"
    if days < 30:
        return f"{days} дн."
    months = days // 30
    if days < 365:
        return f"{months} мес."
    years = days / 365.25
    return f"{years:.1f} г."


def tenure_bucket(joined_at: datetime, now: datetime | None = None) -> str:
    days = age_days(joined_at, now)
    for limit, label in TENURE_BUCKETS:
        if limit is None or days < limit:
            return label
    return TENURE_BUCKETS[-1][1]


WEEKDAY_NAMES = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def weekday_name(moment: datetime) -> str:
    return WEEKDAY_NAMES[as_utc(moment).weekday()]
