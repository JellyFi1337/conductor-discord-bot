"""Шапка группы, модалка приглашения и кнопки. Без sqlalchemy и без services."""

from __future__ import annotations

from typing import Any, ClassVar

import discord
from discord import ui

from conductor.domain.access import GROUP_ARCHIVE_MANAGE
from conductor.domain.discord_limits import ACTION_ROW_BUTTONS
from conductor.domain.groups import ArchiveReason, GroupCard, GroupListing, parse_invitee, silence_deadline
from conductor.domain.mutations import MutationScope
from conductor.presentation import copy
from conductor.presentation.guard import GuardResource, GuardVerdict
from conductor.presentation.interact import run_teardown, split_teardown
from conductor.presentation.layout import denied, empty, panel, respond, update_message
from conductor.presentation.modal import SafeModal
from conductor.presentation.motion import Countdown
from conductor.presentation.persistent import PersistentButton, format_custom_id, register_dynamic_item
from conductor.presentation.roster import roster_preview
from conductor.presentation.tokens import Icon, Tone

PANEL_KIND = "gr"
INVITE_KIND = "gi"

__all__ = [
    "INVITE_KIND",
    "PANEL_KIND",
    "GroupInviteModal",
    "archive_header_view",
    "confirm_archive_view",
    "confirm_purge_view",
    "header_view",
    "invite_dm_view",
    "mine_view",
]


def header_view(card: GroupCard) -> ui.LayoutView:
    invite = ui.Button(
        label=copy.GROUP_INVITE,
        style=discord.ButtonStyle.primary,
        custom_id=format_custom_id(PANEL_KIND, "inv", card.group_id),
    )
    leave = ui.Button(
        label=copy.GROUP_LEAVE,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(PANEL_KIND, "leave", card.group_id),
    )
    archive = ui.Button(
        label=copy.GROUP_ARCHIVE,
        style=discord.ButtonStyle.danger,
        custom_id=format_custom_id(PANEL_KIND, "stop", card.group_id),
    )
    activity = card.last_activity_at or card.created_at
    deadline = silence_deadline(activity, silence_after=card.silence_after) if activity else None
    note = copy.group_owner_line(card.owner_id)
    if deadline is not None:
        note = f"{note} · {copy.group_silence_line(Countdown().render(deadline))}"
    extra = f" · {copy.GROUP_VOICE} <#{card.voice_channel_id}>" if card.voice_channel_id else ""
    return panel(
        card.title,
        copy.group_members_line(card.member_ids) + extra,
        footnote=note,
        rows=(ui.ActionRow(invite, leave, archive),),
        tone=Tone.MUTED,
        icon=Icon.LOCK,
        timeout=None,
    )


def archive_header_view(card: GroupCard) -> ui.LayoutView:
    restore = ui.Button(
        label=copy.GROUP_RESTORE,
        style=discord.ButtonStyle.primary,
        custom_id=format_custom_id(PANEL_KIND, "back", card.group_id),
    )
    purge = ui.Button(
        label=copy.GROUP_PURGE,
        style=discord.ButtonStyle.danger,
        custom_id=format_custom_id(PANEL_KIND, "wipe", card.group_id),
    )
    created = card.created_at.strftime("%Y-%m-%d") if card.created_at is not None else "—"
    archived = card.archived_at.strftime("%Y-%m-%d") if card.archived_at is not None else "—"
    reason = copy.group_archive_reason_label(None if card.archive_reason is None else card.archive_reason.value)
    members = roster_preview(card.member_ids, len(card.member_ids))
    body = (
        f"{copy.group_owner_line(card.owner_id)}\n"
        f"{copy.GROUP_MEMBERS}: {members or '—'}\n"
        f"создана {created} · в архиве с {archived} · {reason}\n"
        f"{copy.group_purge_line(card.purge_after)}"
    )
    return panel(
        f"{copy.GROUP_ARCHIVE_HEADER} «{card.title}»",
        body,
        rows=(ui.ActionRow(restore, purge),),
        tone=Tone.MUTED,
        icon=Icon.LOCK,
        timeout=None,
    )


def confirm_archive_view(group_id: int) -> ui.LayoutView:
    yes = ui.Button(
        label=copy.GROUP_ARCHIVE,
        style=discord.ButtonStyle.danger,
        custom_id=format_custom_id(PANEL_KIND, "yes", group_id),
    )
    no = ui.Button(
        label=copy.CONFIRM_CANCEL,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(PANEL_KIND, "no", group_id),
    )
    return panel(
        copy.GROUP_CONFIRM_ARCHIVE,
        tone=Tone.DANGER,
        rows=(ui.ActionRow(yes, no),),
        timeout=60.0,
        icon=Icon.WARN,
    )


def confirm_purge_view(group_id: int) -> ui.LayoutView:
    yes = ui.Button(
        label=copy.GROUP_PURGE,
        style=discord.ButtonStyle.danger,
        custom_id=format_custom_id(PANEL_KIND, "kill", group_id),
    )
    no = ui.Button(
        label=copy.CONFIRM_CANCEL,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(PANEL_KIND, "no", group_id),
    )
    return panel(
        copy.GROUP_CONFIRM_PURGE,
        tone=Tone.DANGER,
        rows=(ui.ActionRow(yes, no),),
        timeout=60.0,
        icon=Icon.WARN,
    )


def invite_dm_view(group_id: int, owner_id: int, title: str, guild_name: str, guild_url: str) -> ui.LayoutView:
    yes = ui.Button(
        label=copy.GROUP_INVITE_ACCEPT,
        style=discord.ButtonStyle.success,
        custom_id=format_custom_id(INVITE_KIND, "yes", group_id),
    )
    no = ui.Button(
        label=copy.GROUP_INVITE_DECLINE,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id(INVITE_KIND, "no", group_id),
    )
    link = ui.Button(label=copy.GROUP_INVITE_SERVER, style=discord.ButtonStyle.link, url=guild_url)
    return panel(
        copy.GROUP_TITLE,
        copy.group_invite_dm(owner_id, title, guild_name),
        rows=(ui.ActionRow(yes, no, link),),
        tone=Tone.INFO,
        icon=Icon.LOCK,
        timeout=None,
    )


def invite_accepted_view(channel_url: str) -> ui.LayoutView:
    go = ui.Button(label=copy.GROUP_INVITE_OPEN, style=discord.ButtonStyle.link, url=channel_url)
    return panel(
        copy.GROUP_TITLE,
        copy.GROUP_INVITE_ACCEPTED,
        rows=(ui.ActionRow(go),),
        tone=Tone.OK,
        timeout=None,
    )


def mine_view(listing: GroupListing) -> ui.LayoutView:
    lines: list[str] = []
    for card in listing.groups:
        lines.append(copy.group_mine_line(card.title, card.channel_id, card.owner_id == listing.viewer_id))
    retry = None if listing.retry_at is None else Countdown().render(listing.retry_at)
    note = copy.group_quota_line(
        listing.open_count, listing.open_limit, retry, privileged=listing.privileged
    )
    rows: list[ui.ActionRow] = []
    shown = listing.invites[: max(1, ACTION_ROW_BUTTONS - 1)]
    extra = len(listing.invites) - len(shown)
    for invite in shown:
        lines.append(copy.group_pending_line(invite.title, invite.owner_id))
        yes = ui.Button(
            label=copy.GROUP_INVITE_ACCEPT,
            style=discord.ButtonStyle.success,
            custom_id=format_custom_id(INVITE_KIND, "yes", invite.group_id),
        )
        no = ui.Button(
            label=copy.GROUP_INVITE_DECLINE,
            style=discord.ButtonStyle.secondary,
            custom_id=format_custom_id(INVITE_KIND, "no", invite.group_id),
        )
        rows.append(ui.ActionRow(yes, no))
    if extra > 0:
        lines.append(f"и ещё {extra}")
    body = "\n".join(lines) if lines else copy.GROUP_NONE
    return panel(
        copy.GROUP_MINE,
        body,
        footnote=note,
        rows=tuple(rows),
        tone=Tone.MUTED,
        icon=Icon.LOCK,
        timeout=None,
    )


def _groups(interaction: discord.Interaction) -> Any:
    return getattr(interaction.client, "groups", None)


def _labeled_value(label: object) -> str:
    component = getattr(label, "component", None)
    value = getattr(component, "value", None)
    return "" if value is None else str(value)


class GroupInviteModal(SafeModal, title=copy.GROUP_MODAL_INVITE):
    def __init__(self, group_id: int) -> None:
        super().__init__()
        self.group_id = group_id
        self._who = ui.Label(
            text=copy.GROUP_FIELD_USER,
            component=ui.TextInput(
                max_length=40,
                required=True,
                placeholder=copy.GROUP_USER_PLACEHOLDER,
                style=discord.TextStyle.short,
            ),
        )
        self.add_item(self._who)

    async def submit(self, interaction: discord.Interaction) -> None:
        user_id = parse_invitee(_labeled_value(self._who))
        if user_id is None:
            await respond(interaction, denied(copy.GROUP_BAD_INVITE))
            return
        groups = _groups(interaction)
        invite = getattr(groups, "invite", None)
        guild = interaction.guild
        if not callable(invite) or guild is None:
            await respond(interaction, empty(copy.STALE_BODY))
            return
        result = await invite(guild, self.group_id, actor_id=interaction.user.id, user_id=user_id)
        await _ack_manage(interaction, result, ok=copy.GROUP_INVITED)


class GroupMixin:
    kind = PANEL_KIND
    need: ClassVar[str] = "open"
    public: ClassVar[bool] = False
    access_key: ClassVar[str | None] = None
    mutation_scope: ClassVar[Any] = None
    ref: str

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        groups = _groups(self._interaction)
        getter = getattr(groups, "guard_resource", None)
        if not callable(getter):
            return None
        return await getter(guild_id, ref, need=type(self).need)

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        return None


async def _ack_manage(interaction: discord.Interaction, result: object, *, ok: str) -> None:
    payload = result
    verdict = getattr(payload, "verdict", getattr(payload, "value", payload))
    if str(verdict) == "ok":
        dm_sent = getattr(payload, "dm_sent", True)
        text = ok if dm_sent else copy.GROUP_INVITE_CLOSED
        await respond(interaction, panel(copy.GROUP_TITLE, text, tone=Tone.OK))
        return
    if str(verdict) == "already":
        await respond(interaction, panel(copy.GROUP_TITLE, copy.GROUP_ALREADY, tone=Tone.OK))
        return
    if str(verdict) == "bot":
        await respond(interaction, denied(copy.GROUP_BOT))
        return
    if str(verdict) == "full":
        await respond(interaction, denied(copy.GROUP_FULL))
        return
    if str(verdict) == "failed":
        await respond(interaction, denied(copy.GROUP_ARCHIVE_NO_SHELF))
        return
    if str(verdict) == "owner":
        await respond(interaction, denied(copy.GROUP_OWNER_LEAVE))
        return
    if str(verdict) == "stranger":
        await respond(interaction, denied(copy.GROUP_NOT_MEMBER))
        return
    if str(verdict) == "denied":
        await respond(interaction, denied(copy.GROUP_STRANGER))
        return
    if str(verdict) == "cooldown":
        retry = getattr(payload, "retry_at", None)
        stamp = copy.GROUP_LATER if retry is None else Countdown().render(retry)
        await respond(interaction, denied(copy.group_invite_cooldown(stamp)))
        return
    if str(verdict) == "expired":
        await respond(interaction, denied(copy.GROUP_INVITE_EXPIRED))
        return
    await respond(interaction, empty(copy.STALE_BODY))


@register_dynamic_item
class GroupInviteButton(GroupMixin, PersistentButton, template=r"^c:gr:inv:(?P<ref>\d+)$"):
    action = "inv"
    access_key = None
    need = "open"
    acks_first = False

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await interaction.response.send_modal(GroupInviteModal(int(self.ref)))


@register_dynamic_item
class GroupLeaveButton(GroupMixin, PersistentButton, template=r"^c:gr:leave:(?P<ref>\d+)$"):
    action = "leave"
    public = True
    access_key = None
    need = "open"
    mutation_scope = MutationScope.CHANNELS_GROUPS

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        groups = _groups(interaction)
        leave = getattr(groups, "leave", None)
        if not callable(leave) or interaction.guild is None:
            return
        result = await leave(interaction.guild, int(self.ref), actor_id=interaction.user.id)
        body, teardown = split_teardown(result)
        ok = copy.GROUP_ARCHIVED if teardown is not None else copy.GROUP_LEFT
        await _ack_manage(interaction, body, ok=ok)
        await run_teardown(teardown)


@register_dynamic_item
class GroupStopButton(GroupMixin, PersistentButton, template=r"^c:gr:stop:(?P<ref>\d+)$"):
    action = "stop"
    access_key = None
    need = "open"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await respond(interaction, confirm_archive_view(int(self.ref)))


@register_dynamic_item
class GroupYesButton(GroupMixin, PersistentButton, template=r"^c:gr:yes:(?P<ref>\d+)$"):
    action = "yes"
    access_key = GROUP_ARCHIVE_MANAGE
    need = "open"
    mutation_scope = MutationScope.CHANNELS_GROUPS

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        groups = _groups(interaction)
        archive = getattr(groups, "archive", None)
        if not callable(archive) or interaction.guild is None:
            return
        resource = getattr(verdict, "resource", None)
        owner_id = None if resource is None else getattr(resource, "owner_id", None)
        reason = ArchiveReason.OWNER if owner_id == interaction.user.id else ArchiveReason.STAFF
        result = await archive(interaction.guild, int(self.ref), actor_id=interaction.user.id, reason=reason)
        body, teardown = split_teardown(result)
        await _ack_manage(interaction, body, ok=copy.GROUP_ARCHIVED)
        await run_teardown(teardown)


@register_dynamic_item
class GroupNoButton(GroupMixin, PersistentButton, template=r"^c:gr:no:(?P<ref>\d+)$"):
    action = "no"
    access_key = None
    need = "any"
    public = True

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await respond(interaction, panel(copy.GROUP_TITLE, copy.GROUP_ARCHIVE_ABORT, tone=Tone.OK))


@register_dynamic_item
class GroupRestoreButton(GroupMixin, PersistentButton, template=r"^c:gr:back:(?P<ref>\d+)$"):
    action = "back"
    access_key = GROUP_ARCHIVE_MANAGE
    need = "archived"
    mutation_scope = MutationScope.CHANNELS_GROUPS

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        groups = _groups(interaction)
        restore = getattr(groups, "restore_from_archive", None)
        if not callable(restore) or interaction.guild is None:
            return
        result = await restore(interaction.guild, int(self.ref), actor_id=interaction.user.id)
        body, teardown = split_teardown(result)
        await _ack_manage(interaction, body, ok=copy.GROUP_RESTORED)
        await run_teardown(teardown)


@register_dynamic_item
class GroupPurgeButton(GroupMixin, PersistentButton, template=r"^c:gr:wipe:(?P<ref>\d+)$"):
    action = "wipe"
    access_key = GROUP_ARCHIVE_MANAGE
    need = "archived"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        await respond(interaction, confirm_purge_view(int(self.ref)))


@register_dynamic_item
class GroupPurgeYesButton(GroupMixin, PersistentButton, template=r"^c:gr:kill:(?P<ref>\d+)$"):
    action = "kill"
    access_key = GROUP_ARCHIVE_MANAGE
    need = "archived"
    mutation_scope = MutationScope.CHANNELS_GROUPS

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        groups = _groups(interaction)
        purge = getattr(groups, "purge", None)
        if not callable(purge) or interaction.guild is None:
            return
        result = await purge(interaction.guild, int(self.ref), actor_id=interaction.user.id)
        body, teardown = split_teardown(result)
        await _ack_manage(interaction, body, ok=copy.GROUP_PURGED)
        await run_teardown(teardown)


class GroupInviteMixin:
    kind = INVITE_KIND
    public: ClassVar[bool] = False
    access_key: ClassVar[str | None] = None
    mutation_scope: ClassVar[Any] = None
    allow_dm: ClassVar[bool] = True
    ref: str

    async def load_resource(self, *, guild_id: int, ref: str) -> GuardResource | None:
        groups = _groups(self._interaction)
        getter = getattr(groups, "invite_guard", None)
        if not callable(getter):
            return None
        return await getter(guild_id, ref, user_id=int(self._interaction.user.id))

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        return None


async def _guild_for_invite(interaction: discord.Interaction, resource: GuardResource | None) -> Any:
    if interaction.guild is not None:
        return interaction.guild
    if resource is None:
        return None
    getter = getattr(interaction.client, "get_guild", None)
    if not callable(getter):
        return None
    return getter(int(resource.guild_id))


def _channel_url(guild: Any, channel_id: int) -> str:
    gid = int(getattr(guild, "id", 0) or 0)
    return f"https://discord.com/channels/{gid}/{int(channel_id)}"


@register_dynamic_item
class GroupInviteYesButton(GroupInviteMixin, PersistentButton, template=r"^c:gi:yes:(?P<ref>\d+)$"):
    action = "yes"
    mutation_scope = MutationScope.CHANNELS_GROUPS

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        groups = _groups(interaction)
        accept = getattr(groups, "accept_invite", None)
        guild = await _guild_for_invite(interaction, getattr(verdict, "resource", None))
        if not callable(accept) or guild is None:
            return
        result = await accept(guild, int(self.ref), actor_id=interaction.user.id)
        verdict_value = getattr(result, "value", result)
        if str(verdict_value) == "ok":
            channel_id = getattr(groups, "channel_id_of", None)
            cid = await channel_id(guild.id, int(self.ref)) if callable(channel_id) else None
            if interaction.guild is None and cid:
                await update_message(interaction, invite_accepted_view(_channel_url(guild, int(cid))))
                return
            await respond(interaction, panel(copy.GROUP_TITLE, copy.GROUP_INVITE_ACCEPTED, tone=Tone.OK))
            return
        await _ack_manage(interaction, result, ok=copy.GROUP_INVITE_ACCEPTED)


@register_dynamic_item
class GroupInviteNoButton(GroupInviteMixin, PersistentButton, template=r"^c:gi:no:(?P<ref>\d+)$"):
    action = "no"

    async def on_action(self, interaction: discord.Interaction, verdict: GuardVerdict) -> None:
        groups = _groups(interaction)
        decline = getattr(groups, "decline_invite", None)
        guild = await _guild_for_invite(interaction, getattr(verdict, "resource", None))
        if not callable(decline) or guild is None:
            return
        result = await decline(guild, int(self.ref), actor_id=interaction.user.id)
        if str(getattr(result, "value", result)) == "ok" and interaction.guild is None:
            await update_message(interaction, panel(copy.GROUP_TITLE, copy.GROUP_INVITE_DECLINED, tone=Tone.MUTED))
            return
        await _ack_manage(interaction, result, ok=copy.GROUP_INVITE_DECLINED)
