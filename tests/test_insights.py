from datetime import UTC, datetime, timedelta

from conductor.domain.insights import age_days, age_label, tenure_bucket, weekday_name
from conductor.presentation.events import voice_flag_changes
from conductor.presentation.text import chunk_lines


class _Voice:
    def __init__(self, **values: object) -> None:
        self.mute = False
        self.deaf = False
        self.self_mute = False
        self.self_deaf = False
        self.self_stream = False
        self.self_video = False
        self.suppress = False
        for key, value in values.items():
            setattr(self, key, value)


def test_tenure_and_age_labels() -> None:
    now = datetime(2026, 9, 4, tzinfo=UTC)
    assert age_days(now - timedelta(days=3), now) == 3
    assert age_label(now, now) == "сегодня"
    assert age_label(now - timedelta(days=1), now) == "1 день"
    assert tenure_bucket(now - timedelta(days=2), now) == "до 7 дней"
    assert tenure_bucket(now - timedelta(days=40), now) == "1–3 месяца"
    assert tenure_bucket(now - timedelta(days=400), now) == "1–2 года"
    assert tenure_bucket(now - timedelta(days=800), now) == "2+ года"


def test_weekday_uses_utc() -> None:
    friday = datetime(2026, 9, 4, tzinfo=UTC)
    assert weekday_name(friday) == "пт"


def test_voice_flag_changes_describe_mute_and_stream() -> None:
    before = _Voice()
    after = _Voice(self_mute=True, self_stream=True)
    changes = voice_flag_changes(before, after)
    assert any("микрофон" in item for item in changes)
    assert any("стрим" in item for item in changes)


def test_chunk_lines_keeps_all_entries_under_limit() -> None:
    lines = [f"user-{index}" for index in range(50)]
    chunks = chunk_lines(lines, 40)
    assert "".join(chunk.replace("\n", "") for chunk in chunks) == "".join(lines)
    assert all(len(chunk) <= 40 for chunk in chunks)
