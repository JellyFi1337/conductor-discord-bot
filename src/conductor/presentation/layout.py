"""Набор Components V2. Карточка собирается отсюда, не в cog'е.

Инварианты держит этот модуль: без content/embed в сигнатуре, AllowedMentions.none()
по умолчанию, сумма текста ≤ 4000, больше 40 компонентов — ошибка разработчика.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any

import discord
import structlog
from discord import ui

from conductor.domain.discord_limits import (
    BUTTON_LABEL,
    COMPONENTS_PER_MESSAGE,
    TEXT_DISPLAY_TOTAL,
    UPLOAD_BYTES_DEFAULT,
)
from conductor.domain.uploads import fit_upload
from conductor.presentation import copy
from conductor.presentation.icons import render_icon
from conductor.presentation.persistent import format_custom_id
from conductor.presentation.tokens import Icon, Tone, meter

log = structlog.get_logger("conductor.layout")

TEXT_LIMIT = TEXT_DISPLAY_TOTAL
PAGE_LIMIT = 3800
COMPONENT_LIMIT = COMPONENTS_PER_MESSAGE
BUTTON_LABEL_LIMIT = BUTTON_LABEL

__all__ = [
    "COMPONENT_LIMIT",
    "PAGE_LIMIT",
    "TEXT_LIMIT",
    "Card",
    "CardFile",
    "FileCard",
    "LayoutBudgetError",
    "card_file",
    "channel_kwargs",
    "collected_text",
    "command_failure",
    "confirm",
    "denied",
    "disabled_copy",
    "edit_card",
    "edit_kwargs",
    "edit_response",
    "empty",
    "error_code",
    "event",
    "failure",
    "kv",
    "listing",
    "message_kwargs",
    "meter",
    "stale",
    "paginate",
    "panel",
    "report_ui_error",
    "respond",
    "send_card",
    "update_message",
    "upload_cards",
    "upload_limit",
    "wait",
]


class LayoutBudgetError(RuntimeError):
    """Больше 40 компонентов в одном сообщении. Разбейте на страницы, не ужимайте."""


class Card(ui.LayoutView):
    """LayoutView, который нельзя отправить вместе с content: этого ключа нет в kwargs."""

    def __init__(
        self,
        *,
        timeout: float | None = 180.0,
        mentions: discord.AllowedMentions | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.allowed_mentions = discord.AllowedMentions.none() if mentions is None else mentions

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: ui.Item[Any]) -> None:
        await report_ui_error(interaction, error, item)


@dataclass(frozen=True, slots=True)
class CardFile:
    """Байты вложения. discord.File из них собирается заново на каждую отправку."""

    filename: str
    data: bytes


class FileCard(Card):
    """Карточка с файлами: помощники layout прикладывают их при каждой отправке."""

    def __init__(
        self,
        files: Sequence[CardFile],
        *,
        timeout: float | None = 180.0,
        mentions: discord.AllowedMentions | None = None,
    ) -> None:
        super().__init__(timeout=timeout, mentions=mentions)
        self.files = tuple(files)

    def fresh_files(self) -> list[discord.File]:
        return [discord.File(BytesIO(item.data), filename=item.filename) for item in self.files]


def card_file(filename: str, data: bytes | str) -> CardFile:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return CardFile(filename=filename, data=payload)


def display(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    text = str(value).strip()
    return text if text else "—"


def kv(pairs: Mapping[str, object]) -> str:
    return "\n".join(f"**{key}:** {display(value)}" for key, value in pairs.items())


def listing(rows: Sequence[str], page: int, pages: int) -> str:
    page = max(1, page)
    pages = max(1, pages)
    shown = "\n".join(list(rows)[:10])
    if pages == 1:
        return shown
    marker = copy.listing_page(min(page, pages), pages)
    return f"{shown}\n-# {marker}" if shown else f"-# {marker}"


def paginate(text: str, limit: int = PAGE_LIMIT) -> list[str]:
    if limit < 1:
        limit = 1
    if len(text) <= limit:
        return [text]
    pages: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            pages.append(rest)
            break
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 4:
            cut = limit
        pages.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    return pages or [""]


def error_code(interaction: discord.Interaction) -> str:
    try:
        return f"E-{int(interaction.id) & 0xFFFF:04X}"
    except (TypeError, ValueError, AttributeError):
        return "E-0000"


def collected_text(view: ui.LayoutView) -> str:
    chunks: list[str] = []
    for item in view.walk_children():
        content = getattr(item, "content", None)
        if isinstance(content, str) and content:
            chunks.append(content)
        label = getattr(item, "label", None)
        if isinstance(label, str) and label:
            chunks.append(label)
    return "\n".join(chunks)


def _allowed_mentions(
    view: ui.LayoutView,
    mentions: discord.AllowedMentions | None,
) -> discord.AllowedMentions:
    stored = getattr(view, "allowed_mentions", None)
    return mentions or stored or discord.AllowedMentions.none()


def _with_files(kwargs: dict[str, Any], view: ui.LayoutView, key: str) -> dict[str, Any]:
    if isinstance(view, FileCard):
        kwargs[key] = view.fresh_files()
    return kwargs


def message_kwargs(
    view: ui.LayoutView,
    *,
    ephemeral: bool = True,
    mentions: discord.AllowedMentions | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "view": view,
        "ephemeral": ephemeral,
        "allowed_mentions": _allowed_mentions(view, mentions),
    }
    return _with_files(kwargs, view, "files")


def channel_kwargs(
    view: ui.LayoutView,
    *,
    mentions: discord.AllowedMentions | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "view": view,
        "allowed_mentions": _allowed_mentions(view, mentions),
    }
    return _with_files(kwargs, view, "files")


def edit_kwargs(
    view: ui.LayoutView,
    *,
    mentions: discord.AllowedMentions | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "view": view,
        "allowed_mentions": _allowed_mentions(view, mentions),
    }
    return _with_files(kwargs, view, "attachments")


async def respond(
    interaction: discord.Interaction,
    view: ui.LayoutView,
    *,
    ephemeral: bool = True,
    mentions: discord.AllowedMentions | None = None,
    **extra: Any,
) -> None:
    kwargs = message_kwargs(view, ephemeral=ephemeral, mentions=mentions)
    kwargs.update(extra)
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
        return
    await interaction.response.send_message(**kwargs)


async def send_card(
    target: Any,
    view: ui.LayoutView,
    *,
    mentions: discord.AllowedMentions | None = None,
    **extra: Any,
) -> discord.Message:
    kwargs = channel_kwargs(view, mentions=mentions)
    kwargs.update(extra)
    return await target.send(**kwargs)


async def edit_card(
    message: discord.Message,
    view: ui.LayoutView,
    *,
    mentions: discord.AllowedMentions | None = None,
    **extra: Any,
) -> discord.Message:
    kwargs = edit_kwargs(view, mentions=mentions)
    kwargs.update(extra)
    return await message.edit(**kwargs)


async def update_message(
    interaction: discord.Interaction,
    view: ui.LayoutView,
    *,
    mentions: discord.AllowedMentions | None = None,
    **extra: Any,
) -> None:
    kwargs = edit_kwargs(view, mentions=mentions)
    kwargs.update(extra)
    if interaction.response.is_done():
        await interaction.edit_original_response(**kwargs)
        return
    await interaction.response.edit_message(**kwargs)


async def edit_response(
    interaction: discord.Interaction,
    view: ui.LayoutView,
    *,
    mentions: discord.AllowedMentions | None = None,
    **extra: Any,
) -> None:
    kwargs = edit_kwargs(view, mentions=mentions)
    kwargs.update(extra)
    await interaction.edit_original_response(**kwargs)


def upload_limit(guild: object | None = None) -> int:
    raw = getattr(guild, "filesize_limit", None)
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else UPLOAD_BYTES_DEFAULT


def upload_cards(
    title: str,
    body: str | Sequence[str] = (),
    *,
    filename: str,
    data: bytes | str,
    limit: int,
    **kwargs: Any,
) -> tuple[ui.LayoutView, ...]:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    parts = fit_upload(filename, payload, limit)
    total = len(parts)
    footnote = kwargs.pop("footnote", None)
    cards: list[ui.LayoutView] = []
    for index, (name, blob) in enumerate(parts, start=1):
        note = footnote
        if total > 1:
            marker = copy.upload_part(index, total)
            note = f"{footnote} · {marker}" if footnote else marker
        cards.append(panel(title, body, files=(card_file(name, blob),), footnote=note, **kwargs))
    return tuple(cards)


def panel(
    title: str,
    body: str | Sequence[str] = (),
    *,
    tone: Tone = Tone.INFO,
    fields: Mapping[str, object] | None = None,
    footnote: str | None = None,
    rows: Sequence[ui.ActionRow] = (),
    thumbnail_url: str | None = None,
    files: Sequence[CardFile] = (),
    mentions: discord.AllowedMentions | None = None,
    timeout: float | None = 180.0,
    icon: str | None = None,
) -> ui.LayoutView:
    mark = render_icon(tone.icon if icon is None else icon)
    heading = f"## {mark} {title}".strip() if mark else f"## {title}"
    parts = [heading]
    body_text = _body_text(body)
    if body_text:
        parts.append(body_text)
    if fields:
        parts.append(kv(fields))
    if footnote:
        note = footnote.removeprefix("-#").strip()
        parts.append(f"-# {note}")
    text = "\n".join(part for part in parts if part)
    if len(text) > TEXT_LIMIT:
        text = text[: TEXT_LIMIT - 1] + "…"

    children: list[ui.Item[Any]] = []
    display_item = ui.TextDisplay(text)
    if thumbnail_url:
        children.append(ui.Section(display_item, accessory=ui.Thumbnail(thumbnail_url)))
    else:
        children.append(display_item)
    children.extend(ui.File(f"attachment://{item.filename}") for item in files)
    if rows:
        children.append(ui.Separator())
        children.extend(rows)

    try:
        container = ui.Container(*children, accent_colour=tone.colour)
        view: Card = (
            FileCard(files, timeout=timeout, mentions=mentions)
            if files
            else Card(timeout=timeout, mentions=mentions)
        )
        view.add_item(container)
    except ValueError as exc:
        raise LayoutBudgetError(
            "Карточка собрала больше 40 компонентов. Разбейте на страницы, не ужимайте.",
        ) from exc
    if view.total_children_count > COMPONENT_LIMIT:
        raise LayoutBudgetError(
            "Карточка собрала больше 40 компонентов. Разбейте на страницы, не ужимайте.",
        )
    return view


def event(
    title: str,
    body: str | Sequence[str] = (),
    *,
    tone: Tone,
    fields: Mapping[str, object] | None = None,
    source: str,
    when: datetime | None = None,
    thumbnail_url: str | None = None,
    icon: str | None = None,
) -> ui.LayoutView:
    """Архетип F: статичная карточка в лог-канал. Без кнопок и без Tone.RUNNING."""
    if tone is Tone.RUNNING:
        raise ValueError("карточка лога статична: Tone.RUNNING запрещён")
    moment = when if when is not None else discord.utils.utcnow()
    footnote = f"{source} · {discord.utils.format_dt(moment, 'T')}"
    return panel(
        title,
        body,
        tone=tone,
        fields=fields,
        footnote=footnote,
        thumbnail_url=thumbnail_url,
        timeout=None,
        icon=icon,
    )


def confirm(
    question: str,
    *,
    verb: str,
    custom_id: str,
    tone: Tone = Tone.DANGER,
) -> ui.LayoutView:
    row = ui.ActionRow(
        ui.Button(
            label=_button_label(verb),
            style=discord.ButtonStyle.danger,
            custom_id=custom_id,
        ),
        ui.Button(
            label=copy.CONFIRM_CANCEL,
            style=discord.ButtonStyle.secondary,
            custom_id=f"{custom_id}:cancel",
        ),
    )
    return panel(question, tone=tone, rows=(row,), timeout=60.0, icon=Icon.WARN)


def denied(reason: str, *, need: str | None = None) -> ui.LayoutView:
    footnote = copy.denied_need(need) if need else None
    return panel(copy.DENIED_TITLE, reason, tone=Tone.MUTED, footnote=footnote)


def wait() -> ui.LayoutView:
    return panel(copy.WAIT_TITLE, copy.WAIT_BODY, tone=Tone.MUTED)


def stale() -> ui.LayoutView:
    return panel(copy.STALE_CARD, copy.STALE_BODY, tone=Tone.MUTED)


def failure(code: str) -> ui.LayoutView:
    button = ui.Button(
        label=copy.BUG_REPORT_BUTTON,
        style=discord.ButtonStyle.secondary,
        custom_id=format_custom_id("bug", "new", code),
    )
    return panel(
        copy.FAILURE_TITLE,
        copy.FAILURE_BODY,
        tone=Tone.DANGER,
        footnote=copy.failure_footnote(code),
        rows=(ui.ActionRow(button),),
    )


def command_failure(interaction: discord.Interaction) -> ui.LayoutView:
    return failure(error_code(interaction))


async def report_ui_error(
    interaction: discord.Interaction,
    error: BaseException,
    item: ui.Item[Any] | None = None,
) -> None:
    """Ошибка карточки или модалки: CriticalLog и карточка E-XXXX, если ответ ещё возможен."""
    reporter = getattr(getattr(interaction.client, "errors", None), "report_interaction", None)
    if callable(reporter):
        try:
            await reporter(interaction, error, notify_user=True, item=item)
            return
        except Exception:
            log.exception("ui_error_report_failed")
    try:
        await respond(interaction, command_failure(interaction))
    except Exception:
        log.warning("ui_error_notify_unsent", code=error_code(interaction))


def empty(what: str, *, hint: str | None = None) -> ui.LayoutView:
    return panel(copy.EMPTY_TITLE, what, tone=Tone.MUTED, footnote=hint, icon="")


def disabled_copy(view: ui.LayoutView) -> ui.LayoutView:
    for item in view.walk_children():
        if hasattr(item, "disabled"):
            item.disabled = True
    return view


def _body_text(body: str | Sequence[str]) -> str:
    if isinstance(body, str):
        return body
    return "\n".join(display(part) for part in body if part is not None and str(part) != "")


def _button_label(text: str) -> str:
    text = text.strip()
    if len(text) <= BUTTON_LABEL_LIMIT:
        return text
    return text[: BUTTON_LABEL_LIMIT - 1] + "…"
