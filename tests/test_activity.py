from datetime import UTC, datetime

from conductor.domain.activity import EventKind, PeriodKind, day_bucket, hour_bucket, month_bucket
from conductor.presentation.events import duration_label


def test_buckets_truncate_to_utc_boundaries() -> None:
    moment = datetime(2025, 9, 4, 15, 42, 11, tzinfo=UTC)
    assert hour_bucket(moment) == datetime(2025, 9, 4, 15, 0, tzinfo=UTC)
    assert day_bucket(moment) == datetime(2025, 9, 4, 0, 0, tzinfo=UTC)
    assert month_bucket(moment) == datetime(2025, 9, 1, 0, 0, tzinfo=UTC)


def test_event_and_period_kinds_are_stable_strings() -> None:
    assert EventKind.MESSAGE == "message"
    assert EventKind.LEAVE == "leave"
    assert EventKind.REACTION == "reaction"
    assert EventKind.TICKET_FAQ == "ticket_faq"
    assert EventKind.MESSAGE_EDIT == "message_edit"
    assert PeriodKind.DAY == "day"
    assert PeriodKind.MONTH == "month"


def test_duration_label_is_readable() -> None:
    assert duration_label(8) == "8 сек"
    assert duration_label(125) == "2 мин 5 сек"
    assert duration_label(3725) == "1 ч 2 мин 5 сек"
