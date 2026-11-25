"""Сборка карточек /admin и общие модалки. Без sqlalchemy и без services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from io import BytesIO

import discord
from discord import ui

from conductor.domain.admin import (
    PICK_EMPTY_VALUE,
    PageControl,
    ReadyIssue,
    parse_duration,
    parse_id_list,
    parse_int_range,
)
from conductor.presentation import copy
from conductor.presentation.interact import acked, instant
from conductor.presentation.layout import denied, message_kwargs, panel, respond
from conductor.presentation.modal import SafeModal
from conductor.presentation.tokens import Tone

PANEL_TIMEOUT = 900.0
DISCORD_SELECT_LIMIT = 25
HOME_SELECT_CHUNK = 12

__all__ = [
    "DISCORD_SELECT_LIMIT",
    "HOME_SELECT_CHUNK",
    "PANEL_TIMEOUT",
    "DurationModal",
    "IdsModal",
    "KeyRanksModal",
    "CountReasonModal",
    "LinesModal",
    "NumberModal",
    "NumbersModal",
    "TextModal",
    "confirm_card",
    "home_card",
    "issues_card",
    "no_pages_card",
    "number_out_of_range",
    "page_card",
    "send_export",
]

PagePick = Callable[[discord.Interaction, str], Awaitable[None]]
ControlPick = Callable[[discord.Interaction, str, str], Awaitable[None]]
HomePick = Callable[[discord.Interaction], Awaitable[None]]
IssueFix = Callable[[discord.Interaction, str], Awaitable[None]]
IssueOpen = Callable[[discord.Interaction, str], Awaitable[None]]
Done = Callable[[discord.Interaction, str], Awaitable[None]]

ISSUE_BUTTON_CAP = 20


def no_pages_card() -> ui.LayoutView:
    return denied(copy.ADMIN_NO_PAGES)


def number_out_of_range(raw: str, *, lo: int, hi: int) -> ui.LayoutView | int:
    parsed = parse_int_range(raw, lo=lo, hi=hi)
    if parsed is None:
        return denied(copy.ADMIN_BAD_NUMBER)
    return parsed


def home_card(
    *,
    fields: Mapping[str, object],
    pages: Sequence[tuple[str, str]],
    on_page: PagePick,
    on_issues: HomePick | None = None,
) -> ui.LayoutView:
    rows: list[ui.ActionRow] = []
    split = len(pages) > HOME_SELECT_CHUNK
    for offset in range(0, len(pages), HOME_SELECT_CHUNK):
        chunk = pages[offset : offset + HOME_SELECT_CHUNK]
        start = offset + 1
        end = offset + len(chunk)
        select = ui.Select(
            placeholder=copy.admin_pages_range(start, end) if split else copy.ADMIN_PAGES,
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=label[:100], value=page_id) for page_id, label in chunk],
        )

        async def picked(interaction: discord.Interaction, chosen: ui.Select = select) -> None:
            await on_page(interaction, chosen.values[0] if chosen.values else "")

        select.callback = instant(picked)
        rows.append(ui.ActionRow(select))
    if on_issues is not None:
        show = ui.Button(label=copy.ADMIN_SHOW_ISSUES, style=discord.ButtonStyle.secondary)

        async def open_issues(interaction: discord.Interaction) -> None:
            await on_issues(interaction)

        show.callback = instant(open_issues)
        rows.append(ui.ActionRow(show))
    return panel(
        copy.ADMIN_HOME_TITLE,
        copy.ADMIN_HOME_BODY,
        fields=fields,
        tone=Tone.INFO,
        rows=tuple(rows),
        timeout=PANEL_TIMEOUT,
    )


def issues_card(
    *,
    groups: Sequence[tuple[str, str, tuple[ReadyIssue, ...]]],
    on_fix: IssueFix,
    on_open: IssueOpen,
    on_home: HomePick,
) -> ui.LayoutView:
    fields: dict[str, object] = {}
    actions: list[tuple[str, str, str]] = []
    for page_id, label, issues in groups:
        if not issues:
            continue
        fields[label] = "; ".join(item.label for item in issues)
        for item in issues:
            if item.skeleton_key:
                actions.append(("fix", page_id, item.skeleton_key))
            if item.fix_control:
                actions.append(("open", page_id, item.fix_control))
    rows: list[ui.ActionRow] = []
    pending: list[ui.Button] = []
    for kind, page_id, payload in actions[:ISSUE_BUTTON_CAP]:
        if kind == "fix":
            button = ui.Button(label=copy.ADMIN_FIX, style=discord.ButtonStyle.success)

            async def fix(interaction: discord.Interaction, key: str = payload) -> None:
                await on_fix(interaction, key)

            button.callback = instant(fix)
        else:
            button = ui.Button(label=copy.ADMIN_OPEN_ISSUE, style=discord.ButtonStyle.primary)

            async def open_page(interaction: discord.Interaction, chosen: str = page_id) -> None:
                await on_open(interaction, chosen)

            button.callback = instant(open_page)
        pending.append(button)
        if len(pending) == 5:
            rows.append(ui.ActionRow(*pending))
            pending = []
    if pending:
        rows.append(ui.ActionRow(*pending))
    back = ui.Button(label=copy.ADMIN_BACK, style=discord.ButtonStyle.secondary)

    async def go_home(interaction: discord.Interaction) -> None:
        await on_home(interaction)

    back.callback = instant(go_home)
    rows.append(ui.ActionRow(back))
    body = copy.ADMIN_NO_ISSUES if not fields else ()
    return panel(
        copy.ADMIN_ISSUES_TITLE,
        body,
        fields=fields or None,
        tone=Tone.WARN if fields else Tone.OK,
        rows=tuple(rows),
        timeout=PANEL_TIMEOUT,
    )


def page_card(
    *,
    title: str,
    fields: Mapping[str, object],
    footnote: str | None,
    controls: Sequence[PageControl],
    on_control: ControlPick,
    on_home: HomePick,
) -> ui.LayoutView:
    rows: list[ui.ActionRow] = []
    pending: list[ui.Button] = []

    def flush() -> None:
        nonlocal pending
        while pending:
            rows.append(ui.ActionRow(*pending[:5]))
            pending = pending[5:]

    for control in controls:
        if control.kind == "button":
            pending.append(_control_button(control, on_control))
            continue
        flush()
        rows.extend(_control_selects(control, on_control))
    flush()
    back = ui.Button(label=copy.ADMIN_BACK, style=discord.ButtonStyle.secondary)

    async def go_home(interaction: discord.Interaction) -> None:
        await on_home(interaction)

    back.callback = instant(go_home)
    rows.append(ui.ActionRow(back))
    return panel(
        title,
        fields=fields,
        footnote=footnote,
        tone=Tone.INFO,
        rows=tuple(rows),
        timeout=PANEL_TIMEOUT,
    )


async def send_export(
    interaction: discord.Interaction,
    payload: bytes,
    *,
    filename: str = "permissions.yml",
) -> None:
    view = panel(copy.CONFIG_TITLE, copy.CONFIG_EXPORTED, tone=Tone.OK)
    kwargs = message_kwargs(view)
    kwargs["file"] = discord.File(BytesIO(payload), filename=filename)
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
        return
    await interaction.response.send_message(**kwargs)


def _control_button(control: PageControl, on_control: ControlPick) -> ui.Button:
    button = ui.Button(label=control.label[:80], style=discord.ButtonStyle.secondary)

    async def clicked(interaction: discord.Interaction, control_id: str = control.id) -> None:
        await on_control(interaction, control_id, "")

    button.callback = instant(clicked)
    return button


_CHANNEL_KINDS = {
    "text": discord.ChannelType.text,
    "voice": discord.ChannelType.voice,
    "category": discord.ChannelType.category,
}


def _channel_types(control: PageControl) -> list[discord.ChannelType]:
    found = [_CHANNEL_KINDS[name] for name in control.channel_types if name in _CHANNEL_KINDS]
    return found or [discord.ChannelType.text]


def _select_value(select: ui.Select | ui.ChannelSelect | ui.RoleSelect, kind: str) -> str:
    if kind in {"select", "role_pick", "channel_pick"}:
        return ",".join(str(value) for value in select.values) if select.values else ""
    return " ".join(str(getattr(item, "id", "")) for item in select.values)


def confirm_card(
    *,
    question: str,
    verb: str,
    on_yes: Callable[[discord.Interaction], Awaitable[None]],
    on_no: Callable[[discord.Interaction], Awaitable[None]],
) -> ui.LayoutView:
    yes = ui.Button(label=verb[:80], style=discord.ButtonStyle.danger)
    no = ui.Button(label=copy.CONFIRM_CANCEL, style=discord.ButtonStyle.secondary)
    yes.callback = acked(on_yes)
    no.callback = acked(on_no)
    return panel(question, tone=Tone.DANGER, rows=(ui.ActionRow(yes, no),), timeout=60.0)


def _control_selects(control: PageControl, on_control: ControlPick) -> list[ui.ActionRow]:
    if control.kind in {"role_pick", "channel_pick"}:
        return _pick_selects(control, on_control)
    return [_control_select(control, on_control)]


def _option_triple(item: tuple[str, ...]) -> tuple[str, str, str]:
    label = item[0] if item else ""
    value = item[1] if len(item) > 1 else ""
    description = item[2] if len(item) > 2 else ""
    return label, value, description


def _pick_selects(control: PageControl, on_control: ControlPick) -> list[ui.ActionRow]:
    raw = [_option_triple(item) for item in control.options]
    if not raw:
        empty = copy.ADMIN_ROLE_NONE if control.kind == "role_pick" else copy.ADMIN_CHANNEL_NONE
        raw = [(empty, PICK_EMPTY_VALUE, "")]
    disabled = len(raw) == 1 and raw[0][1] == PICK_EMPTY_VALUE
    split = len(raw) > DISCORD_SELECT_LIMIT
    rows: list[ui.ActionRow] = []
    for offset in range(0, len(raw), DISCORD_SELECT_LIMIT):
        chunk = raw[offset : offset + DISCORD_SELECT_LIMIT]
        start = offset + 1
        end = offset + len(chunk)
        placeholder = copy.admin_pages_range(start, end) if split else control.label
        selected = {part.strip() for part in (control.current or "").replace(";", ",").split(",") if part.strip()}
        options = []
        for label, value, description in chunk:
            option_kw: dict[str, object] = {
                "label": label[:100],
                "value": value[:100],
                "default": value in selected,
            }
            if description:
                option_kw["description"] = description[:100]
            options.append(discord.SelectOption(**option_kw))
        low = 0 if disabled else max(0, int(control.min_values))
        high = 1 if disabled else max(low, min(int(control.max_values), len(options)))
        select = ui.Select(
            placeholder=str(placeholder)[:150],
            min_values=low,
            max_values=max(low, high),
            options=options,
            disabled=disabled,
        )

        async def picked(
            interaction: discord.Interaction,
            control_id: str = control.id,
            kind: str = control.kind,
            chosen: ui.Select = select,
        ) -> None:
            raw_value = _select_value(chosen, kind)
            if raw_value == PICK_EMPTY_VALUE:
                return
            await on_control(interaction, control_id, raw_value)

        select.callback = instant(picked)
        rows.append(ui.ActionRow(select))
    return rows


def _control_select(control: PageControl, on_control: ControlPick) -> ui.ActionRow:
    low = max(0, int(control.min_values))
    high = max(low, int(control.max_values))
    if control.kind == "channel":
        select: ui.Select | ui.ChannelSelect | ui.RoleSelect | ui.UserSelect = ui.ChannelSelect(
            placeholder=control.label,
            min_values=low,
            max_values=high,
            channel_types=_channel_types(control),
        )
    elif control.kind == "role":
        select = ui.RoleSelect(placeholder=control.label, min_values=low, max_values=high)
    elif control.kind == "user":
        select = ui.UserSelect(placeholder=control.label, min_values=low, max_values=high)
    else:
        selected = {part.strip() for part in (control.current or "").replace(";", ",").split(",") if part.strip()}
        options = [
            discord.SelectOption(
                label=label[:100],
                value=value[:100],
                default=value in selected,
            )
            for label, value, *_rest in control.options
        ]
        select = ui.Select(
            placeholder=control.label,
            min_values=low,
            max_values=high,
            options=options,
        )

    async def picked(interaction: discord.Interaction, control_id: str = control.id, kind: str = control.kind) -> None:
        await on_control(interaction, control_id, _select_value(select, kind))

    select.callback = instant(picked)
    return ui.ActionRow(select)


class NumberModal(SafeModal, title=copy.ADMIN_MODAL_NUMBER):
    value = ui.Label(text=copy.ADMIN_LABEL_NUMBER, component=ui.TextInput(max_length=16))

    def __init__(self, *, lo: int, hi: int, on_done: Done) -> None:
        super().__init__()
        self.lo = lo
        self.hi = hi
        self.on_done = on_done

    async def submit(self, interaction: discord.Interaction) -> None:
        parsed = number_out_of_range(str(self.value.component.value), lo=self.lo, hi=self.hi)
        if not isinstance(parsed, int):
            await respond(interaction, parsed)
            return
        await self.on_done(interaction, str(parsed))


class NumbersModal(SafeModal):
    def __init__(
        self,
        *,
        title: str,
        labels: Sequence[str],
        defaults: Sequence[str],
        on_done: Done,
    ) -> None:
        super().__init__(title=(title or copy.ADMIN_MODAL_NUMBER)[:45])
        self.on_done = on_done
        self._fields: list[ui.TextInput] = []
        padded = list(defaults) + [""] * max(0, len(labels) - len(defaults))
        for label, default in zip(labels, padded, strict=False):
            field = ui.TextInput(max_length=16, default=str(default)[:16] if default else None)
            self.add_item(ui.Label(text=str(label)[:45], component=field))
            self._fields.append(field)

    async def submit(self, interaction: discord.Interaction) -> None:
        await self.on_done(interaction, " ".join(str(field.value) for field in self._fields))


class LinesModal(SafeModal):
    def __init__(self, *, title: str, label: str, default: str, on_done: Done) -> None:
        super().__init__(title=(title or copy.ADMIN_MODAL_TEXT)[:45])
        self.on_done = on_done
        self.field = ui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=1000,
            default=str(default)[:1000] if default else None,
        )
        self.add_item(ui.Label(text=str(label)[:45], component=self.field))

    async def submit(self, interaction: discord.Interaction) -> None:
        await self.on_done(interaction, str(self.field.value))


class CountReasonModal(SafeModal):
    def __init__(
        self,
        *,
        title: str,
        labels: Sequence[str],
        defaults: Sequence[str],
        on_done: Done,
    ) -> None:
        super().__init__(title=(title or copy.ADMIN_MODAL_NUMBER)[:45])
        self.on_done = on_done
        count_label = labels[0] if labels else copy.ADMIN_LABEL_NUMBER
        reason_label = labels[1] if len(labels) > 1 else copy.ADMIN_LABEL_TEXT
        self.count = ui.TextInput(max_length=16, default=str(defaults[0])[:16] if defaults else None)
        self.reason = ui.TextInput(max_length=100, required=False)
        self.add_item(ui.Label(text=str(count_label)[:45], component=self.count))
        self.add_item(ui.Label(text=str(reason_label)[:45], component=self.reason))

    async def submit(self, interaction: discord.Interaction) -> None:
        await self.on_done(interaction, f"{self.count.value}\n{self.reason.value}")


class TextModal(SafeModal, title=copy.ADMIN_MODAL_TEXT):
    value = ui.Label(text=copy.ADMIN_LABEL_TEXT, component=ui.TextInput(max_length=64))

    def __init__(self, *, on_done: Done) -> None:
        super().__init__()
        self.on_done = on_done

    async def submit(self, interaction: discord.Interaction) -> None:
        await self.on_done(interaction, str(self.value.component.value))


class IdsModal(SafeModal, title=copy.ADMIN_MODAL_IDS):
    value = ui.Label(text=copy.ADMIN_LABEL_IDS, component=ui.TextInput(max_length=200))

    def __init__(self, *, on_done: Done) -> None:
        super().__init__()
        self.on_done = on_done

    async def submit(self, interaction: discord.Interaction) -> None:
        parsed = parse_id_list(str(self.value.component.value))
        if parsed is None:
            await respond(interaction, denied(copy.ADMIN_BAD_IDS))
            return
        await self.on_done(interaction, " ".join(str(item) for item in parsed))


class DurationModal(SafeModal, title=copy.ADMIN_MODAL_DURATION):
    value = ui.Label(text=copy.ADMIN_LABEL_DURATION, component=ui.TextInput(max_length=16, placeholder="30м"))

    def __init__(self, *, on_done: Done) -> None:
        super().__init__()
        self.on_done = on_done

    async def submit(self, interaction: discord.Interaction) -> None:
        parsed = parse_duration(str(self.value.component.value))
        if parsed is None:
            await respond(interaction, denied(copy.ADMIN_BAD_DURATION))
            return
        await self.on_done(interaction, str(int(parsed.total_seconds())))


class KeyRanksModal(SafeModal, title=copy.ADMIN_MODAL_KEY):
    key = ui.Label(text=copy.ADMIN_LABEL_KEY, component=ui.TextInput(max_length=64))
    ranks = ui.Label(
        text=copy.ADMIN_LABEL_RANKS,
        component=ui.TextInput(max_length=80, placeholder="special, chief"),
    )

    def __init__(self, *, on_done: Done) -> None:
        super().__init__()
        self.on_done = on_done

    async def submit(self, interaction: discord.Interaction) -> None:
        key = str(self.key.component.value).strip()
        ranks = str(self.ranks.component.value).strip()
        await self.on_done(interaction, f"{key}\n{ranks}")
