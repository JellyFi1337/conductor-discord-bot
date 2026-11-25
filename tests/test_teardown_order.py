"""Контракт К5: ответ Discord раньше разрушения канала; ошибки on_action — в CriticalLog."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from conductor.domain.groups import ManageVerdict
from conductor.domain.tickets import CloseVerdict
from conductor.presentation import copy
from conductor.presentation.groups import GroupYesButton
from conductor.presentation.layout import collected_text
from conductor.presentation.moderation import EvidenceCloseButton, EvidenceReasonModal
from conductor.presentation.persistent import PersistentButton
from conductor.presentation.tickets import _finish_close
from conductor.services.errors import CriticalLog


class _JournalResponse:
    def __init__(self, journal: list[str]) -> None:
        self._journal = journal
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self._journal.append("defer")
        self._done = True

    async def send_message(self, **kwargs: object) -> None:
        self._journal.append("send_message")
        self._done = True


class _JournalFollowup:
    def __init__(self, journal: list[str], sent: list[dict[str, object]]) -> None:
        self._journal = journal
        self._sent = sent

    async def send(self, **kwargs: object) -> SimpleNamespace:
        self._journal.append("followup.send")
        self._sent.append(kwargs)
        return SimpleNamespace(id=1)


def _interaction(
    *,
    journal: list[str],
    sent: list[dict[str, object]] | None = None,
    client: object | None = None,
    guild: object | None = None,
    user_id: int = 2,
) -> SimpleNamespace:
    payload = sent if sent is not None else []
    created = datetime.now(UTC) - timedelta(milliseconds=80)
    return SimpleNamespace(
        id=0x4E4F,
        response=_JournalResponse(journal),
        followup=_JournalFollowup(journal, payload),
        client=client,
        guild=guild,
        user=SimpleNamespace(id=user_id),
        command=None,
        data={"custom_id": "c:md:evclose:1"},
        created_at=created,
        type=SimpleNamespace(name="component"),
        namespace=None,
        channel=SimpleNamespace(id=70, name="delo", type=SimpleNamespace(name="text")),
    )


def _teardown(journal: list[str], token: str = "channel.delete"):
    async def teardown() -> None:
        journal.append(token)

    return teardown


async def test_evidence_close_answers_before_delete() -> None:
    journal: list[str] = []
    sent: list[dict[str, object]] = []

    async def close_without_dm(guild: object, evidence_id: int, actor: object) -> object:
        journal.append("service")
        assert evidence_id == 1
        return SimpleNamespace(ok=True), _teardown(journal)

    guild = SimpleNamespace(id=1)
    client = SimpleNamespace(evidence=SimpleNamespace(close_without_dm=close_without_dm))
    interaction = _interaction(journal=journal, sent=sent, client=client, guild=guild)
    button = EvidenceCloseButton(1)

    async def guarded(press: object) -> object:
        await press.response.defer(ephemeral=True, thinking=True)
        return SimpleNamespace(ok=True)

    button._guarded = guarded  # type: ignore[method-assign]
    await button.callback(interaction)  # type: ignore[arg-type]
    assert journal == ["defer", "service", "followup.send", "channel.delete"]


async def test_evidence_reason_answers_before_delete() -> None:
    journal: list[str] = []
    sent: list[dict[str, object]] = []

    async def send_reason(guild: object, evidence_id: int, actor: object, raw: str, **_kw: object) -> object:
        journal.append("service")
        assert evidence_id == 1
        assert raw
        return SimpleNamespace(ok=True), _teardown(journal)

    guild = SimpleNamespace(id=1)
    client = SimpleNamespace(evidence=SimpleNamespace(send_reason=send_reason))
    interaction = _interaction(journal=journal, sent=sent, client=client, guild=guild)
    modal = EvidenceReasonModal(1)
    modal.reason.component._value = "подробное объяснение наказания для участника"
    await modal.on_submit(interaction)  # type: ignore[arg-type]
    assert journal == ["defer", "service", "followup.send", "channel.delete"]


async def test_group_archive_answers_before_teardown() -> None:
    journal: list[str] = []
    sent: list[dict[str, object]] = []

    async def archive(guild: object, group_id: int, *, actor_id: int, reason: object) -> object:
        journal.append("service")
        assert group_id == 4
        assert actor_id == 2
        assert reason is not None
        return ManageVerdict.OK, _teardown(journal)

    guild = SimpleNamespace(id=1)
    client = SimpleNamespace(groups=SimpleNamespace(archive=archive))
    interaction = _interaction(journal=journal, sent=sent, client=client, guild=guild)
    button = GroupYesButton(4)

    async def guarded(press: object) -> object:
        await press.response.defer(ephemeral=True, thinking=True)
        return SimpleNamespace(ok=True)

    button._guarded = guarded  # type: ignore[method-assign]
    await button.callback(interaction)  # type: ignore[arg-type]
    assert journal == ["defer", "service", "followup.send", "channel.delete"]


async def test_ticket_close_answers_before_teardown() -> None:
    journal: list[str] = []
    sent: list[dict[str, object]] = []

    async def close_ticket(
        guild: object,
        ref: object,
        actor: object,
        *,
        reason: str | None = None,
        outcome: str | None = None,
    ) -> object:
        journal.append("service")
        return CloseVerdict.OK, _teardown(journal)

    guild = SimpleNamespace(id=1)
    client = SimpleNamespace(tickets=SimpleNamespace(close_ticket=close_ticket))
    interaction = _interaction(journal=journal, sent=sent, client=client, guild=guild)
    interaction.response._done = True
    await _finish_close(interaction, "8", reason=None)  # type: ignore[arg-type]
    assert journal == ["service", "followup.send", "channel.delete"]


async def test_on_action_error_goes_to_critical_log() -> None:
    journal: list[str] = []
    sent: list[dict[str, object]] = []
    posted: list[object] = []
    bot = SimpleNamespace(
        is_developer=lambda _uid: False,
        config_by_guild={9: SimpleNamespace(admin_log_channel_id=55)},
        log_pump=SimpleNamespace(submit=lambda _channel_id, view=None, **_k: posted.append(view)),
        errors=None,
    )
    bot.errors = CriticalLog(bot)

    class BoomButton(PersistentButton, template=r"^c:md:boom:(?P<ref>\d+)$"):
        kind = "md"
        action = "boom"

        async def on_action(self, interaction: object, verdict: object) -> None:
            raise RuntimeError("boom")

    guild = SimpleNamespace(id=9, name="Hall", owner_id=1, me=None)
    interaction = _interaction(journal=journal, sent=sent, client=bot, guild=guild)
    button = BoomButton(1)

    async def guarded(press: object) -> object:
        await press.response.defer(ephemeral=True, thinking=True)
        return SimpleNamespace(ok=True)

    button._guarded = guarded  # type: ignore[method-assign]
    await button.callback(interaction)  # type: ignore[arg-type]
    assert journal[0] == "defer"
    assert posted
    text = collected_text(sent[0]["view"])  # type: ignore[arg-type]
    assert "E-" in text
    assert copy.ERROR_TITLE_COMPONENT in collected_text(posted[0])  # type: ignore[arg-type]
