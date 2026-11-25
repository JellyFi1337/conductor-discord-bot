"""Карточки дел модерации. Без sqlalchemy и без services."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import discord
import structlog
from discord import ui

from conductor.domain.access import MOD_EVIDENCE_CLOSE, MOD_SETTINGS
from conductor.domain.moderation import (
    CONFIRM_TTL,
    DETAIL_REASON_MAX,
    DETAIL_REASON_MIN,
    REASON_MAX,
    AppealVerdict,
    CaseKind,
    ModOutcome,
    ModVerdict,
    StoredCase,
    dm_deliverable,
    needs_confirmation,
    needs_hierarchy,
    protected_log_channels,
    purge_channel_blocked,
    sanitize_detail_reason,
)
from conductor.domain.mutations import MutationScope
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource, GuardVerdict
from conductor.presentation.interact import ack, instant, run_teardown, split_teardown
from conductor.presentation.layout import (
    card_file,
    denied,
    error_code,
    listing,
    panel,
    respond,
    update_message,
    upload_cards,
    upload_limit,
)
from conductor.presentation.modal import SafeModal
from conductor.presentation.motion import Countdown, InteractionTarget, Progress
from conductor.presentation.persistent import PersistentButton, format_custom_id, register_dynamic_item
from conductor.presentation.tokens import Icon, Tone

_REASON_LIMIT = REASON_MAX
_PANEL_KIND = "md"
log = structlog.get_logger("conductor.moderation")


def purge_log_view(
    case: StoredCase,
    *,
    channel: str,
    actor: str,
    deleted: int,
    skipped: int,
) -> ui.LayoutView:
    return panel(
        copy.MOD_PURGE_DONE,
        copy.mod_purge_done_body(deleted=deleted, skipped=skipped, channel=channel, actor=actor),
        tone=Tone.OK,
        footnote=copy.mod_case_title(case.number, case.kind),
        timeout=None,
        icon=Icon.DONE,
    )


def heavy_confirm_view(pending_id: int, kind: CaseKind | str, target: str, *, created_at: datetime) -> ui.LayoutView:
    until = created_at + CONFIRM_TTL
    row = ui.ActionRow(
        ui.Button(
            label=copy.mod_confirm_verb(str(kind)),
            style=discord.ButtonStyle.danger,
            custom_id=format_custom_id(_PANEL_KIND, "yes", pending_id),
        ),
        ui.Button(
            label=copy.CONFIRM_CANCEL,
            style=discord.ButtonStyle.secondary,
            custom_id=format_custom_id(_PANEL_KIND, "no", pending_id),
        ),
    )
    return panel(
        copy.mod_confirm_title(str(kind), target),
        copy.MOD_CONFIRM_BODY,
        tone=Tone.DANGER,
        rows=(row,),
        timeout=60.0,
        icon=Icon.WARN,
        footnote=copy.MOD_CONFIRM_FOOTNOTE.format(when=Countdown().render(until)),
    )


def case_dm_view(
    case: StoredCase,
    *,
    rank: str,
    duration: str | None = None,
    detail: str | None = None,
    appeal_button: bool = False,
    appeal_contact: str | None = None,
) -> ui.LayoutView:
    expect_detail = dm_deliverable(case.kind)
    if detail is None:
        body = copy.case_dm1_body(
            number=case.number,
            kind=case.kind,
            actor_id=case.created_by,
            rank=rank,
            reason=case.reason,
            duration=duration,
            expect_detail=expect_detail,
            appeal_contact=appeal_contact,
        )
    else:
        body = copy.case_dm2_body(number=case.number, detail=detail)
    rows: tuple[ui.ActionRow, ...] = ()
    if appeal_button:
        rows = (
            ui.ActionRow(
                ui.Button(
                    label=copy.CASE_APPEAL,
                    style=discord.ButtonStyle.secondary,
                    custom_id=format_custom_id(_PANEL_KIND, "appeal", case.id),
                )
            ),
        )
    return panel(
        copy.mod_case_title(case.number, case.kind),
        body,
        tone=Tone.DANGER,
        icon=Icon.CASE,
        rows=rows,
        timeout=None,
    )


def evidence_channel_view(
    case: StoredCase,
    *,
    evidence_id: int,
    actor: str,
    target: str,
    duration: str | None,
    dm1_ok: bool,
    expect_detail: bool,
) -> ui.LayoutView:
    fields: dict[str, object] = {
        copy.MOD_FIELD_ACTOR: actor,
        copy.MOD_FIELD_TARGET: target,
        copy.MOD_FIELD_KIND: copy.mod_kind_label(case.kind),
        copy.MOD_FIELD_REASON: case.reason,
    }
    if duration:
        fields[copy.MOD_FIELD_UNTIL] = duration
    if not dm1_ok:
        fields[copy.EVIDENCE_TITLE] = copy.EVIDENCE_DM_FAILED
    elif not expect_detail:
        fields[copy.EVIDENCE_TITLE] = copy.EVIDENCE_NO_DM2
    reason_label = copy.EVIDENCE_SEND_REASON if expect_detail else copy.EVIDENCE_RECORD_REASON
    row = ui.ActionRow(
        ui.Button(
            label=reason_label,
            style=discord.ButtonStyle.primary,
            custom_id=format_custom_id(_PANEL_KIND, "reason", evidence_id),
        ),
        ui.Button(
            label=copy.EVIDENCE_CLOSE,
            style=discord.ButtonStyle.secondary,
            custom_id=format_custom_id(_PANEL_KIND, "evclose", evidence_id),
        ),
    )
    footnote = None if expect_detail else copy.CASE_DM_NO_DETAIL
    return panel(
        copy.mod_case_title(case.number, case.kind),
        tone=Tone.WARN,
        icon=Icon.CASE,
        fields=fields,
        rows=(row,),
        footnote=footnote,
        timeout=None,
    )


def case_receipt(case: StoredCase, *, until: datetime | None = None, note: str | None = None) -> ui.LayoutView:
    fields: dict[str, object] = {copy.MOD_FIELD_TARGET: f"<@{case.owner_id}>"}
    if case.duration_seconds:
        stamp = None if until is None else discord.utils.format_dt(until, "t")
        fields[copy.MOD_FIELD_UNTIL] = copy.mod_duration_value(case.duration_seconds, stamp)
    fields[copy.MOD_FIELD_REASON] = case.reason
    if note:
        fields[copy.MOD_FIELD_EVIDENCE] = note
    return panel(
        copy.mod_case_title(case.number, case.kind),
        tone=Tone.DANGER,
        icon=Icon.CASE,
        fields=fields,
        footnote=copy.mod_case_footnote(case.created_by),
        timeout=None,
    )


def history_card(title: str, pages: Sequence[Sequence[str]], page: int) -> ui.LayoutView:
    total = max(1, len(pages))
    current = max(1, min(page, total))
    chunk = list(pages[current - 1]) if pages else []
    body = listing(chunk, current, total)
    rows: tuple[ui.ActionRow, ...] = ()
    if total > 1:
        prev = ui.Button(label="←", style=discord.ButtonStyle.secondary, disabled=current <= 1)
        nxt = ui.Button(label="→", style=discord.ButtonStyle.secondary, disabled=current >= total)

        async def back(interaction: discord.Interaction) -> None:
            await update_message(interaction, history_card(title, pages, current - 1))

        async def forward(interaction: discord.Interaction) -> None:
            await update_message(interaction, history_card(title, pages, current + 1))

        prev.callback = instant(back)
        nxt.callback = instant(forward)
        rows = (ui.ActionRow(prev, nxt),)
    return panel(title, body, tone=Tone.INFO, rows=rows)


def outcome_view(outcome: ModOutcome) -> ui.LayoutView:
    if outcome.verdict is ModVerdict.OK and outcome.case is not None:
        return case_receipt(outcome.case, until=outcome.until, note=outcome.note)
    if (
        outcome.verdict is ModVerdict.QUOTA
        and outcome.quota_used is not None
        and outcome.quota_limit is not None
        and outcome.quota_resets_at is not None
        and outcome.quota_group is not None
    ):
        return denied(
            copy.quota_exhausted(
                used=outcome.quota_used,
                limit=outcome.quota_limit,
                group=outcome.quota_group,
                resets=discord.utils.format_dt(outcome.quota_resets_at, "R"),
            )
        )
    return denied(copy.mod_denied(outcome.verdict.value))


async def deliver_case(
    interaction: discord.Interaction,
    target: discord.abc.User,
    *,
    kind: CaseKind,
    reason: str,
    duration: str | None = None,
) -> None:
    if needs_confirmation(kind):
        await deliver_heavy(interaction, target, kind=kind, reason=reason)
        return
    guild = interaction.guild
    moderation = getattr(interaction.client, "moderation", None)
    apply = getattr(moderation, "apply", None)
    if guild is None or not callable(apply):
        await respond(interaction, denied(copy.STALE_BODY))
        return
    # Таймаут → запись → ЛС → канал улик → карточка: четыре исходящих вызова, ждать ответ нельзя.
    if not await ack(interaction):
        return
    try:
        outcome = await apply(
            guild,
            interaction.user,
            target,
            kind=kind,
            reason=reason,
            duration_raw=duration,
        )
    except Exception:
        log.exception("mod_apply_failed", guild_id=guild.id, kind=str(kind))
        await respond(interaction, denied(copy.MOD_FAILED))
        return
    await respond(interaction, outcome_view(outcome))


async def deliver_heavy(
    interaction: discord.Interaction,
    target: discord.abc.User,
    *,
    kind: CaseKind,
    reason: str,
) -> None:
    guild = interaction.guild
    moderation = getattr(interaction.client, "moderation", None)
    begin = getattr(moderation, "begin_heavy", None)
    if guild is None or not callable(begin):
        await respond(interaction, denied(copy.STALE_BODY))
        return
    outcome = await begin(guild, interaction.user, target, kind=kind, reason=reason)
    if outcome.verdict is ModVerdict.OK and outcome.pending_id is not None:
        await respond(
            interaction,
            heavy_confirm_view(outcome.pending_id, kind, target.mention, created_at=datetime.now(UTC)),
        )
        return
    await respond(interaction, outcome_view(outcome))


async def run_purge(
    interaction: discord.Interaction,
    channel: discord.abc.GuildChannel,
    *,
    reason: str,
    user: discord.Member | None = None,
    after: discord.Message | None = None,
) -> None:
    guild = interaction.guild
    moderation = getattr(interaction.client, "moderation", None)
    purge = getattr(moderation, "purge", None)
    if guild is None or not callable(purge):
        await respond(interaction, denied(copy.STALE_BODY))
        return
    configs = getattr(interaction.client, "config_by_guild", None)
    config = None if configs is None else configs.get(guild.id)
    protected = protected_log_channels(
        None if config is None else getattr(config, "user_log_channel_id", None),
        None if config is None else getattr(config, "admin_log_channel_id", None),
    )
    if purge_channel_blocked(channel.id, protected):
        await respond(interaction, denied(copy.MOD_LOG_CHANNEL))
        return
    if after is not None and getattr(after.channel, "id", None) != channel.id:
        await respond(interaction, denied(copy.STALE_BODY))
        return
    if not await ack(interaction):
        return
    progress = Progress(
        InteractionTarget(interaction),
        title=copy.MOD_PURGE_TITLE,
        body=copy.MOD_PURGE_FOOTNOTE,
        footnote=copy.MOD_PURGE_FOOTNOTE,
        messages_label=copy.MOD_PURGE_BATCH,
        registry=getattr(interaction.client, "motion", None),
        code=error_code(interaction),
    )

    async def on_batch(deleted: int, skipped: int, batch: int) -> None:
        total = max(deleted + skipped, 1)
        await progress.step(deleted, total=total, messages=batch)

    async with progress:
        await progress.start()
        outcome = await purge(
            guild,
            interaction.user,
            channel,
            reason=reason,
            user_id=None if user is None else user.id,
            after_id=None if after is None else after.id,
            on_batch=on_batch,
        )
        if outcome.verdict is ModVerdict.OK:
            await progress.finish(
                panel(
                    copy.MOD_PURGE_DONE,
                    copy.mod_purge_done_body(
                        deleted=outcome.deleted,
                        skipped=outcome.skipped,
                        channel=channel.mention,
                        actor=interaction.user.mention,
                    ),
                    tone=Tone.OK,
                )
            )
            return
        await progress.finish(outcome_view(outcome))


_edit = update_message


async def _defer_update(interaction: discord.Interaction) -> bool:
    """Ответить «обновлю карточку» до долгой цепочки HTTP: бан, канал улик, ЛС — это больше 3 секунд."""
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer()
    except discord.HTTPException:
        return interaction.response.is_done()
    return True


def _moderation(interaction: discord.Interaction) -> object:
    return getattr(interaction.client, "moderation", None)


class CaseReasonModal(SafeModal, title=copy.MOD_REASON_MODAL):
    reason = ui.Label(
        text=copy.MOD_REASON_LABEL,
        component=ui.TextInput(min_length=8, max_length=_REASON_LIMIT, required=True),
    )

    def __init__(self, kind: CaseKind, target: discord.abc.User, *, duration: str | None = None) -> None:
        super().__init__()
        self.kind = kind
        self.target = target
        self.duration = duration

    async def submit(self, interaction: discord.Interaction) -> None:
        await deliver_case(
            interaction,
            self.target,
            kind=self.kind,
            reason=_labeled_value(self.reason),
            duration=self.duration,
        )


def _labeled_value(label: object) -> str:
    component = getattr(label, "component", None)
    value = getattr(component, "value", None)
    return "" if value is None else str(value)


@register_dynamic_item
class HeavyYesButton(PersistentButton, template=r"^c:md:yes:(?P<ref>\d+)$"):
    kind = _PANEL_KIND
    action = "yes"
    mutation_scope = MutationScope.MEMBERS_MODERATION

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        loader = getattr(_moderation(self._interaction), "guard_intent", None)
        if not callable(loader):
            return None
        return await loader(guild_id, ref)

    async def hierarchy_needed(self, interaction: discord.Interaction) -> bool:
        kind_of = getattr(_moderation(interaction), "intent_kind", None)
        guild = interaction.guild
        if guild is None or not callable(kind_of):
            return False
        kind = await kind_of(guild.id, self.ref)
        return bool(kind) and needs_hierarchy(kind)

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        guild = interaction.guild
        moderation = _moderation(interaction)
        confirm = getattr(moderation, "confirm_heavy", None)
        if guild is None or not callable(confirm):
            return
        outcome = await confirm(guild, int(self.ref))
        await update_message(interaction, outcome_view(outcome))


@register_dynamic_item
class HeavyNoButton(PersistentButton, template=r"^c:md:no:(?P<ref>\d+)$"):
    kind = _PANEL_KIND
    action = "no"

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        loader = getattr(_moderation(self._interaction), "guard_intent", None)
        if not callable(loader):
            return None
        return await loader(guild_id, ref)

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        guild = interaction.guild
        moderation = _moderation(interaction)
        cancel = getattr(moderation, "cancel_heavy", None)
        if guild is not None and callable(cancel):
            await cancel(guild.id, int(self.ref))
        await update_message(interaction, panel(copy.MOD_TITLE, copy.MOD_CANCELLED, tone=Tone.MUTED))


def _evidence(interaction: discord.Interaction) -> object:
    return getattr(interaction.client, "evidence", None)


async def _finish_evidence(
    interaction: discord.Interaction,
    packed: object,
    ok_text: str,
    *,
    tone: Tone,
) -> None:
    body, teardown = split_teardown(packed)
    if not getattr(body, "ok", False):
        await respond(interaction, denied(getattr(body, "error", None) or copy.STALE_BODY))
        return
    summary = getattr(body, "detail", None) or ok_text
    await respond(interaction, panel(copy.EVIDENCE_TITLE, summary, tone=tone))
    await run_teardown(teardown)


class EvidenceReasonModal(SafeModal, title=copy.EVIDENCE_REASON_MODAL):
    reason = ui.Label(
        text=copy.EVIDENCE_REASON_LABEL,
        component=ui.TextInput(min_length=DETAIL_REASON_MIN, max_length=DETAIL_REASON_MAX, required=True),
    )

    def __init__(self, evidence_id: int) -> None:
        super().__init__()
        self.evidence_id = evidence_id

    async def submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        evidence = _evidence(interaction)
        send = getattr(evidence, "send_reason", None)
        if guild is None or not callable(send):
            await respond(interaction, denied(copy.STALE_BODY))
            return
        cleaned = sanitize_detail_reason(_labeled_value(self.reason))
        if cleaned is None:
            await respond(interaction, denied(copy.EVIDENCE_REASON_SHORT))
            return
        result = await send(guild, self.evidence_id, interaction.user, cleaned)
        await _finish_evidence(interaction, result, copy.EVIDENCE_REASON_SAVED, tone=Tone.OK)


@register_dynamic_item
class EvidenceReasonButton(PersistentButton, template=r"^c:md:reason:(?P<ref>\d+)$"):
    kind = _PANEL_KIND
    action = "reason"
    access_key = MOD_EVIDENCE_CLOSE
    mutation_scope = MutationScope.MEMBERS_MODERATION
    acks_first = False

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        loader = getattr(_evidence(self._interaction), "guard_resource", None)
        if not callable(loader):
            return None
        return await loader(guild_id, ref)

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await interaction.response.send_modal(EvidenceReasonModal(int(self.ref)))


@register_dynamic_item
class EvidenceCloseButton(PersistentButton, template=r"^c:md:evclose:(?P<ref>\d+)$"):
    kind = _PANEL_KIND
    action = "evclose"
    access_key = MOD_EVIDENCE_CLOSE
    mutation_scope = MutationScope.MEMBERS_MODERATION

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        loader = getattr(_evidence(self._interaction), "guard_close_resource", None)
        if not callable(loader):
            return None
        return await loader(guild_id, ref)

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        guild = interaction.guild
        close = getattr(_evidence(interaction), "close_without_dm", None)
        if guild is None or not callable(close):
            return
        result = await close(guild, int(self.ref), interaction.user)
        await _finish_evidence(interaction, result, copy.EVIDENCE_CLOSED, tone=Tone.MUTED)


def revoke_target_dm_view(case: StoredCase, *, reason: str) -> ui.LayoutView:
    return panel(
        copy.CASE_REVOKED_TITLE,
        copy.case_revoked_target(number=case.number, reason=reason),
        tone=Tone.OK,
        icon=Icon.DONE,
        timeout=None,
    )


def revoke_actor_dm_view(case: StoredCase, *, by_id: int, reason: str) -> ui.LayoutView:
    return panel(
        copy.CASE_REVOKED_TITLE,
        copy.case_revoked_actor(number=case.number, by_id=by_id, reason=reason),
        tone=Tone.WARN,
        icon=Icon.CASE,
        timeout=None,
    )


def appeal_header_view(
    case: StoredCase,
    *,
    ticket_id: int,
    detail: str | None,
    appeal: str,
) -> ui.LayoutView:
    rows = (
        ui.ActionRow(
            ui.Button(
                label=copy.CASE_KEEP,
                style=discord.ButtonStyle.secondary,
                custom_id=format_custom_id("tk", "keep", ticket_id),
            ),
            ui.Button(
                label=copy.CASE_REVOKE_BTN,
                style=discord.ButtonStyle.danger,
                custom_id=format_custom_id("tk", "caserevoke", ticket_id),
            ),
            ui.Button(
                label=copy.CASE_EVIDENCE_FILE,
                style=discord.ButtonStyle.secondary,
                custom_id=format_custom_id(_PANEL_KIND, "evfile", case.id),
            ),
        ),
    )
    return panel(
        copy.TICKET_KIND_APPEAL.capitalize(),
        copy.case_appeal_header(
            number=case.number,
            kind=case.kind,
            short=case.reason,
            detail=detail,
            appeal=appeal,
        ),
        tone=Tone.WARN,
        icon=Icon.CASE,
        rows=rows,
        timeout=None,
    )


def explain_dm_view(ticket_id: int) -> ui.LayoutView:
    row = ui.ActionRow(
        ui.Button(
            label=copy.CASE_EXPLAIN,
            style=discord.ButtonStyle.secondary,
            custom_id=format_custom_id(_PANEL_KIND, "explain", ticket_id),
        )
    )
    return panel(
        copy.CASE_EXPLAIN_TITLE,
        copy.CASE_EXPLAIN_LABEL,
        tone=Tone.INFO,
        rows=(row,),
        timeout=None,
    )


def explain_card_view(*, actor_id: int, text: str) -> ui.LayoutView:
    return panel(
        copy.CASE_EXPLAIN_TITLE,
        copy.case_explain_body(actor_id=actor_id, text=text),
        tone=Tone.INFO,
        timeout=None,
    )


class CaseAppealModal(SafeModal, title=copy.CASE_APPEAL_MODAL):
    reason = ui.Label(
        text=copy.CASE_APPEAL_LABEL,
        component=ui.TextInput(min_length=DETAIL_REASON_MIN, max_length=DETAIL_REASON_MAX, required=True),
    )

    def __init__(self, case_id: int) -> None:
        super().__init__()
        self.case_id = case_id

    async def submit(self, interaction: discord.Interaction) -> None:
        cleaned = sanitize_detail_reason(_labeled_value(self.reason))
        if cleaned is None:
            await respond(interaction, denied(copy.EVIDENCE_REASON_SHORT))
            return
        tickets = getattr(interaction.client, "tickets", None)
        opener = getattr(tickets, "open_case_appeal", None)
        moderation = _moderation(interaction)
        loader = getattr(moderation, "get_case", None)
        if not callable(opener) or not callable(loader):
            await respond(interaction, denied(copy.STALE_BODY))
            return
        case = await loader(self.case_id)
        if case is None:
            await respond(interaction, denied(copy.STALE_BODY))
            return
        guild = interaction.guild or interaction.client.get_guild(case.guild_id)
        if guild is None:
            await respond(interaction, denied(copy.PANEL_GUILD_ONLY))
            return
        member = guild.get_member(interaction.user.id)
        if member is None:
            await respond(interaction, denied(copy.STALE_BODY))
            return
        result = await opener(guild, member, self.case_id, text=cleaned)
        verdict = getattr(result, "verdict", AppealVerdict.FAILED)
        if verdict is AppealVerdict.OK:
            body = copy.case_appeal_opened(getattr(result, "channel_id", None))
            await respond(interaction, panel(copy.CASE_APPEAL, body, tone=Tone.OK))
            return
        await respond(interaction, denied(copy.appeal_denied(str(verdict))))


class CaseExplainModal(SafeModal, title=copy.CASE_EXPLAIN_MODAL):
    reason = ui.Label(
        text=copy.CASE_EXPLAIN_LABEL,
        component=ui.TextInput(min_length=DETAIL_REASON_MIN, max_length=DETAIL_REASON_MAX, required=True),
    )

    def __init__(self, ticket_id: int) -> None:
        super().__init__()
        self.ticket_id = ticket_id

    async def submit(self, interaction: discord.Interaction) -> None:
        cleaned = sanitize_detail_reason(_labeled_value(self.reason))
        if cleaned is None:
            await respond(interaction, denied(copy.EVIDENCE_REASON_SHORT))
            return
        tickets = getattr(interaction.client, "tickets", None)
        explain = getattr(tickets, "explain_case_appeal", None)
        if not callable(explain):
            await respond(interaction, denied(copy.STALE_BODY))
            return
        result = await explain(self.ticket_id, interaction.user.id, cleaned)
        token = str(getattr(result, "value", result))
        if token == "ok":
            await respond(interaction, panel(copy.CASE_EXPLAIN_TITLE, copy.CASE_EXPLAIN_OK, tone=Tone.OK))
            return
        if token == "already":
            await respond(interaction, denied(copy.CASE_EXPLAIN_ALREADY))
            return
        if token == "denied":
            await respond(interaction, denied(copy.CASE_EXPLAIN_DENIED))
            return
        await respond(interaction, denied(copy.STALE_BODY))


@register_dynamic_item
class CaseAppealButton(PersistentButton, template=r"^c:md:appeal:(?P<ref>\d+)$"):
    kind = _PANEL_KIND
    action = "appeal"
    allow_dm = True
    public = True
    mutation_scope = MutationScope.CHANNELS_TICKETS
    acks_first = False

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        loader = getattr(_moderation(self._interaction), "guard_case_owner", None)
        if not callable(loader):
            return None
        return await loader(ref)

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await interaction.response.send_modal(CaseAppealModal(int(self.ref)))


@register_dynamic_item
class CaseExplainButton(PersistentButton, template=r"^c:md:explain:(?P<ref>\d+)$"):
    kind = _PANEL_KIND
    action = "explain"
    allow_dm = True
    public = True
    acks_first = False

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        loader = getattr(getattr(self._interaction.client, "tickets", None), "guard_explain_dm", None)
        if not callable(loader):
            return None
        return await loader(ref)

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await interaction.response.send_modal(CaseExplainModal(int(self.ref)))


@register_dynamic_item
class CaseEvidenceFileButton(PersistentButton, template=r"^c:md:evfile:(?P<ref>\d+)$"):
    kind = _PANEL_KIND
    action = "evfile"
    public = True

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        loader = getattr(_moderation(self._interaction), "get_case", None)
        if not callable(loader):
            return None
        case = await loader(int(ref))
        if case is None or int(case.guild_id) != int(guild_id):
            return None
        return GuardResource(ref=str(case.id), guild_id=case.guild_id)

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        tickets = getattr(interaction.client, "tickets", None)
        getter = getattr(tickets, "appeal_evidence_file", None)
        if not callable(getter):
            await respond(interaction, denied(copy.CASE_EVIDENCE_MISSING))
            return
        payload = await getter(int(self.ref))
        if payload is None:
            await respond(interaction, denied(copy.CASE_EVIDENCE_MISSING))
            return
        name, text = payload
        media = card_file(str(name), text)
        cards = upload_cards(
            copy.CASE_EVIDENCE_FILE,
            filename=media.filename,
            data=media.data,
            limit=upload_limit(interaction.guild),
            timeout=None,
        )
        for card in cards:
            await respond(interaction, card)


def evidence_denial_card(code: str) -> ui.LayoutView:
    button = EvidenceFixButton("cat_evidence", label=copy.DIAGNOSE_REPAIR)
    return panel(
        copy.DIAGNOSE_FEATURE_EVIDENCE,
        copy.evidence_denial_text(code),
        tone=Tone.WARN,
        icon=Icon.WARN,
        rows=(ui.ActionRow(button),),
        timeout=None,
    )


@register_dynamic_item
class EvidenceFixButton(PersistentButton, template=r"^c:md:evfix:(?P<ref>\d+|[a-z0-9_]{1,32})$"):
    kind = "md"
    action = "evfix"
    access_key = MOD_SETTINGS

    def __init__(self, ref: int | str, *, item: ui.Button | None = None, label: str | None = None) -> None:
        super().__init__(
            ref,
            item=item,
            kind=self.kind,
            action=self.action,
            label=label or copy.DIAGNOSE_REPAIR,
            style=discord.ButtonStyle.primary,
        )

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        # Ресурс кнопки — категория доказательств этой гильдии; без него run_guard отвечает «устарело».
        if int(guild_id) <= 0 or ref != "cat_evidence":
            return None
        return GuardResource(ref=ref, guild_id=int(guild_id))

    async def on_action(self, interaction: discord.Interaction, verdict: object) -> None:
        del verdict
        guild = interaction.guild
        repair = getattr(getattr(interaction.client, "evidence", None), "repair_category", None)
        if guild is None or not callable(repair):
            return
        denial = await repair(guild, int(interaction.user.id))
        if denial is None:
            body, tone = copy.EVIDENCE_FIXED, Tone.OK
        else:
            body, tone = copy.evidence_denial_text(str(getattr(denial, "value", denial))), Tone.WARN
        await respond(interaction, panel(copy.DIAGNOSE_FEATURE_EVIDENCE, body, tone=tone))

