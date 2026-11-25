"""Тексты: moderation."""

from __future__ import annotations

from .common import (
    CONFIG_DENIED,
    HIERARCHY_DENIED,
    MOD_DURATION_BAD,
    STALE_BODY,
    format_mod_duration,
    grouped,
    hold_wait,
)
from .tickets import TICKET_FIELD_CLOSED, TICKET_FIELD_OPENED

MOD_TITLE = "Модерация"

MOD_REASON_MODAL = "Причина"

MOD_REASON_LABEL = "Почему, не короче 8 символов"

MOD_FIELD_ACTOR = "Кто"

MOD_FIELD_TARGET = "Кому"

MOD_FIELD_KIND = "Что"

MOD_FIELD_UNTIL = "Срок"

MOD_FIELD_REASON = "Причина"

MOD_FIELD_EVIDENCE = "Доказательства"

MOD_KIND_WARN = "замечание"

MOD_KIND_TIMEOUT = "тайм-аут"

MOD_KIND_MUTE = "мут"

MOD_KIND_UNMUTE = "мут снят"

MOD_KIND_NOTE = "заметка"

MOD_KIND_KICK = "кик"

MOD_KIND_BAN = "бан"

MOD_KIND_UNBAN = "разбан"

MOD_KIND_SOFTBAN = "бан с чисткой"

MOD_KIND_PURGE = "чистка"

MOD_KIND_LOCK = "замок канала"

MOD_REASON_SHORT = "Причина короче 8 символов."

MOD_TIMEOUT_TOO_LONG = "Тайм-аут длиннее 28 суток Discord не ставит."

MOD_LOCK_TOO_LONG = "Замок длиннее 28 суток не ставлю — задайте меньший срок."

MOD_BOT = "Бота этой командой не трогают."

MOD_NO_MUTE_ROLE = "Роль мута не задана. Сначала /admin panel → Мут."

MOD_MUTE_ROLE_SET = "Роль мута задана."

MOD_HISTORY_EMPTY = "У этого человека нет дел."

MOD_HISTORY_TITLE = "Дела {user}"

MOD_FAILED = "Discord не принял санкцию."

MOD_BOT_BELOW = (
    "Discord не примет санкцию: роль бота не выше цели, у цели Administrator или нет права. Наказание не выдано."
)

MOD_SPENT = "Это подтверждение уже использовано."

MOD_EXPIRED = "Подтверждение истекло. Запустите команду снова."

MOD_LOG_CHANNEL = "Каналы логов Conductor чистить нельзя."

MOD_SLOWMODE_BAD = "Медленный режим Discord принимает от 0 до 21600 секунд."

MOD_CONFIRM_KICK = "Исключить {user}?"

MOD_CONFIRM_BAN = "Забанить {user}?"

MOD_CONFIRM_UNBAN = "Снять бан с {user}?"

MOD_CONFIRM_SOFTBAN = "Бан с чисткой для {user}?"

MOD_CONFIRM_BODY = "После подтверждения действие нельзя отменить этой кнопкой."

MOD_CONFIRM_FOOTNOTE = "отменится само {when}"

MOD_VERB_KICK = "Да, исключить"

MOD_VERB_BAN = "Да, забанить"

MOD_VERB_UNBAN = "Да, разбанить"

MOD_VERB_SOFTBAN = "Да, бан с чисткой"

MOD_PURGE_TITLE = "Чищу канал"

MOD_PURGE_DONE = "Канал очищен"

MOD_PURGE_FOOTNOTE = "сообщения старше 14 суток пропускаются"

MOD_PURGE_BATCH = "Пачка"

MOD_PURGE_CHANNEL = "Канал"

MOD_PURGE_ACTOR = "Инициатор"

MOD_PURGE_DELETED = "Удалено"

MOD_PURGE_SKIPPED = "пропущено по возрасту"

MOD_LOCK_SET = "Канал закрыт"

MOD_LOCK_CLEARED = "Замок снят"

MOD_LOCK_MISSING = "На этом канале замка нет."

MOD_QUOTA_DENIED = "Лимит на сегодня исчерпан."

CASE_APPEAL = "Обжаловать"

CASE_APPEAL_MODAL = "Обжалование"

CASE_APPEAL_LABEL = "Почему наказание несправедливо"

CASE_APPEAL_OK = "Обжалование принято — тикет открыт."


def case_appeal_opened(channel_id: int | None = None) -> str:
    if channel_id:
        return f"{CASE_APPEAL_OK}\n<#{int(channel_id)}>"
    return CASE_APPEAL_OK

CASE_APPEAL_ALREADY = "Это дело уже обжаловано."

CASE_APPEAL_LATE = "Срок обжалования истёк."

CASE_APPEAL_KIND = "Это наказание нельзя обжаловать в боте."

CASE_APPEAL_REVOKED = "Дело уже отменено."

CASE_APPEAL_FAILED = "Не удалось открыть обжалование."

CASE_KEEP = "Оставить в силе"

CASE_REVOKE_BTN = "Отменить наказание"

CASE_KEEP_MODAL = "Оставить в силе"

CASE_REVOKE_MODAL = "Отменить наказание"

CASE_REVOKE_REASON = "Причина отмены"

CASE_KEEP_REASON = "Почему оставляете в силе"

CASE_EXPLAIN = "Объяснить"

CASE_EXPLAIN_MODAL = "Объяснение"

CASE_EXPLAIN_LABEL = "Почему наказание справедливо"

CASE_EXPLAIN_OK = "Объяснение записано в тикет."

CASE_EXPLAIN_ALREADY = "Объяснение уже отправлено."

CASE_EXPLAIN_DENIED = "Объяснять может только исполнитель дела."

CASE_EXPLAIN_TITLE = "Объяснение следящего"

CASE_EVIDENCE_FILE = "Транскрипт доказательств"

CASE_EVIDENCE_MISSING = "Транскрипта доказательств нет."

CASE_REVOKED_TITLE = "Дело отменено"

MOD_REVOKED = "отменено"

CASE_DM1_DETAIL_SOON = "подробное объяснение придёт отдельным сообщением"

CASE_DM_NO_DETAIL = "ЛС забаненному не доставляется"

EVIDENCE_SEND_REASON = "Отправить причину"

EVIDENCE_RECORD_REASON = "Записать причину"

EVIDENCE_CLOSE = "Закрыть без ЛС"

EVIDENCE_REASON_MODAL = "Причина"

EVIDENCE_REASON_LABEL = "Развёрнутая причина"

EVIDENCE_REASON_SHORT = "Причина: от 20 до 1000 знаков."

EVIDENCE_TITLE = "Доказательства"

EVIDENCE_CLOSED = "Канал доказательств закрыт"

EVIDENCE_ALREADY_CLOSED = "Дело уже закрыто."

EVIDENCE_REASON_SAVED = "Причина записана в дело"

EVIDENCE_DM_DELIVERED = "ЛС доставлено"

EVIDENCE_DM_CLOSED_SHORT = "ЛС закрыто"

EVIDENCE_TRANSCRIPT_IN_ARCHIVE = "транскрипт в архиве"

EVIDENCE_TRANSCRIPT_SAVED = "транскрипт сохранён"

EVIDENCE_CHANNEL_WILL_GO = "канал будет убран"

EVIDENCE_DM_FAILED = "ЛС не доставлены — у участника закрытые сообщения"

EVIDENCE_NO_DM2 = "ЛС с развёрнутой причиной не доставляется"

EVIDENCE_CLOSE_FAILED = "Канал доказательств не закрылся — попробуйте ещё раз."

EVIDENCE_REMIND_TITLE = "Напоминание"

EVIDENCE_ESCALATE_TITLE = "Просроченные доказательства"

EVIDENCE_TRANSCRIPT_TITLE = "Транскрипт дела"

EVIDENCE_DENIAL_NO_CATEGORY = "Категория доказательств не задана. Задайте её в /admin → Молчание."

EVIDENCE_DENIAL_CATEGORY_GONE = "Категория доказательств пропала из Discord."

EVIDENCE_DENIAL_NOT_A_CATEGORY = "Привязан обычный канал вместо категории доказательств."

EVIDENCE_DENIAL_BOT_PERMS = "Боту не хватает права управлять каналами в категории доказательств."

EVIDENCE_DENIAL_HELD = (
    "Дело сохранено. Канал доказательств создастся автоматически, когда Discord разрешит. "
    "Открывать дело заново не нужно."
)

EVIDENCE_HELD_SAVED = (
    "Дело сохранено. Канал доказательств создастся автоматически, когда Discord разрешит ({wait}). "
    "Открывать дело заново не нужно."
)

EVIDENCE_CHANNEL_READY = "Канал доказательств создан: {channel}."

EVIDENCE_DENIAL_FAILED = "Не удалось создать канал доказательств."

EVIDENCE_DENIAL_LABELS = {
    "no_category": EVIDENCE_DENIAL_NO_CATEGORY,
    "category_gone": EVIDENCE_DENIAL_CATEGORY_GONE,
    "not_a_category": EVIDENCE_DENIAL_NOT_A_CATEGORY,
    "bot_perms": EVIDENCE_DENIAL_BOT_PERMS,
    "held": EVIDENCE_DENIAL_HELD,
    "failed": EVIDENCE_DENIAL_FAILED,
}

EVIDENCE_FIXED = "Категория доказательств починена."


def evidence_denial_text(code: str) -> str:
    return EVIDENCE_DENIAL_LABELS.get(code, EVIDENCE_DENIAL_FAILED)


def evidence_held_saved(seconds: object) -> str:
    return EVIDENCE_HELD_SAVED.format(wait=hold_wait(seconds))


def evidence_channel_ready(channel: object) -> str:
    return EVIDENCE_CHANNEL_READY.format(channel=channel)


QUOTA_GROUP_TIMEOUTS = "тайм-аутов"

QUOTA_GROUP_BANS = "банов"

QUOTA_DENIED_TITLE = "Лимит на сегодня"

QUOTA_UNLIMITED = "∞"


def quota_frac(used: int, limit: int) -> str:
    if limit < 0:
        return f"{used}/{QUOTA_UNLIMITED}"
    return f"{used}/{limit}"


def case_dm1_body(
    *,
    number: int,
    kind: str,
    actor_id: int,
    rank: str,
    reason: str,
    duration: str | None,
    expect_detail: bool,
    appeal_contact: str | None = None,
) -> str:
    parts = [f"Дело #{int(number)}", mod_kind_label(kind)]
    if duration:
        parts.append(duration)
    parts.append(f"выдал <@{int(actor_id)}> ({rank})")
    parts.append(f"«{reason}»")
    parts.append(CASE_DM1_DETAIL_SOON if expect_detail else CASE_DM_NO_DETAIL)
    contact = (appeal_contact or "").strip()
    if contact and not expect_detail:
        parts.append(f"апелляция: {contact}")
    return " · ".join(parts)


def case_dm2_body(*, number: int, detail: str) -> str:
    return f"Дело #{int(number)} · причина: «{detail}»"


def case_revoked_target(*, number: int, reason: str) -> str:
    return f"дело #{int(number)} отменено: {reason}"


def case_revoked_actor(*, number: int, by_id: int, reason: str) -> str:
    return f"ваше дело #{int(number)} отменено <@{int(by_id)}>: {reason}"


def case_appeal_header(
    *,
    number: int,
    kind: str,
    short: str,
    detail: str | None,
    appeal: str,
) -> str:
    parts = [mod_case_title(number, kind), f"кратко: «{short}»"]
    if detail:
        parts.append(f"подробно: «{detail}»")
    parts.append(f"обжалование: «{appeal}»")
    return "\n".join(parts)


def case_explain_body(*, actor_id: int, text: str) -> str:
    return f"<@{int(actor_id)}>: {text}"


def appeal_denied(code: str) -> str:
    return {
        "ok": CASE_APPEAL_OK,
        "already": CASE_APPEAL_ALREADY,
        "too_late": CASE_APPEAL_LATE,
        "kind": CASE_APPEAL_KIND,
        "revoked": CASE_APPEAL_REVOKED,
        "missing": STALE_BODY,
        "failed": CASE_APPEAL_FAILED,
    }.get(code, CASE_APPEAL_FAILED)


def evidence_remind_body(actor_id: int) -> str:
    return f"<@{int(actor_id)}>, канал доказательств ждёт причину."


def evidence_escalate_body(role_id: int) -> str:
    return f"<@&{int(role_id)}>, канал доказательств просрочен."


def evidence_transcript_name(number: int) -> str:
    return f"delo-{int(number)}.txt"


def evidence_transcript_header(
    *,
    number: int,
    kind: str,
    target: str,
    actor: str,
    opened: str,
    closed: str,
    reason: str,
    detail: str | None = None,
) -> str:
    lines = [
        mod_case_title(number, kind),
        f"{MOD_FIELD_TARGET}: {target}",
        f"{MOD_FIELD_ACTOR}: {actor}",
        f"{TICKET_FIELD_OPENED}: {opened}",
        f"{TICKET_FIELD_CLOSED}: {closed}",
        f"{MOD_FIELD_REASON}: {reason}",
    ]
    if detail:
        lines.append(f"{EVIDENCE_REASON_LABEL}: {detail}")
    return "\n".join(lines)


def evidence_reason_sent(*, dm_ok: bool, archived: bool) -> str:
    dm = EVIDENCE_DM_DELIVERED if dm_ok else EVIDENCE_DM_CLOSED_SHORT
    archive = EVIDENCE_TRANSCRIPT_IN_ARCHIVE if archived else EVIDENCE_TRANSCRIPT_SAVED
    return f"Причина отправлена: {dm} · {archive} · {EVIDENCE_CHANNEL_WILL_GO}"


def evidence_closed_sent(*, archived: bool) -> str:
    archive = EVIDENCE_TRANSCRIPT_IN_ARCHIVE if archived else EVIDENCE_TRANSCRIPT_SAVED
    return f"Закрыто без ЛС: {archive} · {EVIDENCE_CHANNEL_WILL_GO}"


def quota_today(*, timeouts: int, timeout_limit: int, bans: int, ban_limit: int) -> str:
    return f"сегодня {quota_frac(timeouts, timeout_limit)} · {quota_frac(bans, ban_limit)}"


def quota_group_label(group: str) -> str:
    if group == "bans":
        return QUOTA_GROUP_BANS
    return QUOTA_GROUP_TIMEOUTS


def quota_exhausted(*, used: int, limit: int, group: str, resets: str) -> str:
    return f"Лимит на сегодня исчерпан: **{used} из {limit}** {quota_group_label(group)}. Обновится {resets}."


def quota_denied_log(*, user_id: int, used: int, limit: int, group: str) -> str:
    return f"<@{int(user_id)}> упёрся в лимит: **{used} из {limit}** {quota_group_label(group)}."


def quota_cap(limit: int) -> str:
    return QUOTA_UNLIMITED if limit < 0 else str(limit)


def quota_rank_limits(*, timeouts: int, bans: int) -> str:
    return f"{quota_cap(timeouts)} · {quota_cap(bans)}"


MOD_FIELD_CHANNEL = "Канал"

MOD_FIELD_SLOWMODE = "Медленный режим"


def mod_kind_label(kind: str) -> str:
    return {
        "warn": MOD_KIND_WARN,
        "timeout": MOD_KIND_TIMEOUT,
        "mute": MOD_KIND_MUTE,
        "unmute": MOD_KIND_UNMUTE,
        "note": MOD_KIND_NOTE,
        "kick": MOD_KIND_KICK,
        "ban": MOD_KIND_BAN,
        "unban": MOD_KIND_UNBAN,
        "softban": MOD_KIND_SOFTBAN,
        "purge": MOD_KIND_PURGE,
        "lock": MOD_KIND_LOCK,
    }.get(kind, kind)


def mod_case_title(number: int, kind: str) -> str:
    return f"Дело #{number} — {mod_kind_label(kind)}"


def mod_case_footnote(actor_id: int) -> str:
    return f"следящий <@{actor_id}>"


def mod_duration_value(seconds: int, until: str | None = None) -> str:
    text = format_mod_duration(seconds)
    if until:
        return f"{text}, до {until}"
    return text


def mod_history_line(number: int, kind: str, reason: str, *, revoked_by: int | None = None) -> str:
    snippet = reason if len(reason) <= 80 else reason[:79] + "…"
    line = f"**#{number} · {mod_kind_label(kind)}** — {snippet}"
    if revoked_by is None:
        return line
    return f"~~{line}~~ · {MOD_REVOKED} <@{int(revoked_by)}>"


def mod_denied(code: str) -> str:
    return {
        "reason": MOD_REASON_SHORT,
        "duration": MOD_DURATION_BAD,
        "too_long": MOD_TIMEOUT_TOO_LONG,
        "bot": MOD_BOT,
        "hierarchy": HIERARCHY_DENIED,
        "power": MOD_BOT_BELOW,
        "mute_role": MOD_NO_MUTE_ROLE,
        "failed": MOD_FAILED,
        "spent": MOD_SPENT,
        "expired": MOD_EXPIRED,
        "log_channel": MOD_LOG_CHANNEL,
        "slowmode": MOD_SLOWMODE_BAD,
        "missing": MOD_LOCK_MISSING,
        "denied": CONFIG_DENIED,
        "quota": MOD_QUOTA_DENIED,
        "revoked": CASE_APPEAL_REVOKED,
        "appealed": CASE_APPEAL_ALREADY,
    }.get(code, HIERARCHY_DENIED)


def mod_confirm_title(kind: str, user: str) -> str:
    template = {
        "kick": MOD_CONFIRM_KICK,
        "ban": MOD_CONFIRM_BAN,
        "unban": MOD_CONFIRM_UNBAN,
        "softban": MOD_CONFIRM_SOFTBAN,
    }.get(kind, MOD_CONFIRM_BAN)
    return template.format(user=user)


def mod_confirm_verb(kind: str) -> str:
    return {
        "kick": MOD_VERB_KICK,
        "ban": MOD_VERB_BAN,
        "unban": MOD_VERB_UNBAN,
        "softban": MOD_VERB_SOFTBAN,
    }.get(kind, MOD_VERB_BAN)


def mod_purge_done_body(*, deleted: int, skipped: int, channel: str, actor: str) -> str:
    return (
        f"**{MOD_PURGE_DELETED}:** {grouped(deleted)} · **{MOD_PURGE_SKIPPED}:** {grouped(skipped)}\n"
        f"**{MOD_PURGE_CHANNEL}:** {channel} · **{MOD_PURGE_ACTOR}:** {actor}"
    )
