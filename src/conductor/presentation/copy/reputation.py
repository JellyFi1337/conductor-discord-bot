"""Тексты: reputation."""

from __future__ import annotations

from collections.abc import Sequence

from .common import ADMIN_REP_RANGE, grouped
from .moderation import MOD_FIELD_REASON, MOD_FIELD_TARGET, mod_kind_label


def rep_tier_span(*, lower: int, upper: int, last: bool) -> str:
    high = 100 if last else max(lower, upper - 1)
    return ADMIN_REP_RANGE.format(low=lower, high=high)


AUTOBAN_REASON = "репутация 0"

APPEAL_DEFAULT = "напишите администрации в личные сообщения"

REP_PARDON_USER = "помилование"

REP_APPEAL_FOOT = "<t:{stamp}:F> · апелляция: {appeal}"

REP_TITLE = "Репутация {user}"

REP_MONTH = "За 30 дней"

REP_NEXT = "Следующий рост"

REP_MONTH_EMPTY = "без списаний"

REP_NEXT_READY = "сейчас"

REP_NEXT_MESSAGES = "через {count} сообщений"

REP_NEXT_VOICE = "через {minutes} мин голосового"

REP_NEXT_WAIT = "не раньше <t:{stamp}:R>"

REP_FOOTNOTE = "уровень {tier} · автобан на 0"

REP_TIER_LINE = "Уровень"

REP_UNTIL = "До следующего"

REP_UNTIL_TOP = "верхний уровень"

REP_DRY_RUN = "сухой прогон: роли и баны не выдаются"

REP_CONFIG_TITLE = "Репутация — пороги"

REP_CONFIG_FOOTNOTE = "меняется в /admin panel → Репутация"

REP_ROLE_GONE = "Репутация: роль удалена с сервера"

REP_STALE_ACTION = "Роль удалили или id устарел. Привяжите заново: /admin panel → Репутация."

REP_CAP_TITLE = "Репутация: предел за час"

REP_DRY_TITLE = "Репутация: сухой прогон"

REP_NONE = "без списания"

REP_NEXT_PARDON = "после помилования"

REP_PARDON_DONE = "Помилован: счёт {score}."

REP_PARDON_UNBAN = "Автобан снят."

REP_PARDON_KEEP_BAN = "Бан следящих не снимаю — его ставил не этот контур."

REP_SINNER_ROLE_SET = "Роль грешника задана."

REP_AUTOBAN_TITLE = "Дело #{number} · Бан"

REP_AUTOBAN_ACTOR = "Conductor · репутация 0"

REP_WOULD_BAN_TITLE = "Репутация: был бы бан"

REP_WOULD_PARDON_TITLE = "Репутация: сухой прогон помилования"

REP_DIAGNOSE = "Репутация: уровень 0 — {at} · было бы банов в сухом прогоне {bans}"


def rep_title(user: str) -> str:
    return REP_TITLE.format(user=user)


def rep_month_line(hits: Sequence[object]) -> str:
    parts: list[str] = []
    for hit in hits:
        kind = str(getattr(hit, "kind", ""))
        total = int(getattr(hit, "total", 0) or 0)
        if total <= 0:
            continue
        parts.append(f"−{total} {mod_kind_label(kind)}")
    return " · ".join(parts) if parts else REP_MONTH_EMPTY


def rep_next_line(wait: object) -> str:
    if bool(getattr(wait, "blocked", False)):
        return REP_NEXT_PARDON
    if bool(getattr(wait, "ready", False)):
        return REP_NEXT_READY
    until = getattr(wait, "until", None)
    messages_left = int(getattr(wait, "messages_left", 0) or 0)
    voice_left = int(getattr(wait, "voice_seconds_left", 0) or 0)
    if until is not None and messages_left == 0 and voice_left == 0:
        return REP_NEXT_WAIT.format(stamp=int(until.timestamp()))
    if messages_left > 0:
        return REP_NEXT_MESSAGES.format(count=grouped(messages_left))
    if voice_left > 0:
        return REP_NEXT_VOICE.format(minutes=grouped(max(1, (voice_left + 59) // 60)))
    return REP_NEXT_READY


def rep_footnote(tier: int, *, dry_run: bool) -> str:
    base = REP_FOOTNOTE.format(tier=tier)
    return f"{base} · {REP_DRY_RUN}" if dry_run else base


def rep_until_line(*, until_next: int | None, next_name: str | None) -> str:
    if until_next is None or not next_name:
        return REP_UNTIL_TOP
    return f"{until_next} до «{next_name}»"


def rep_config_body(panel: object) -> str:
    config = getattr(panel, "config", panel)
    dry = "да" if bool(getattr(config, "dry_run", False)) else "нет"
    voice = int(getattr(config, "voice_seconds_per_point", 1800) or 1800) // 60
    contact = str(getattr(panel, "appeal_contact", "") or "").strip() or "—"
    tiers = tuple(getattr(panel, "tiers", ()) or ())
    if tiers:
        lines = []
        last = len(tiers) - 1
        for index, item in enumerate(tiers):
            role_id = getattr(item, "role_id", None)
            role = f"<@&{role_id}>" if role_id else "не задана"
            span = rep_tier_span(
                lower=int(getattr(item, "lower", 0)),
                upper=int(getattr(item, "upper", 0)),
                last=index == last,
            )
            lines.append(f"**{getattr(item, 'name', index)}** · {span} · {role}")
        tier_block = "\n".join(lines)
    else:
        tier_block = "уровни не заданы"
    return (
        f"**замечание:** −{getattr(config, 'warn_hit', 0)}\n"
        f"**тайм-аут:** −{getattr(config, 'timeout_per_step', 0)} / 10 мин"
        f" ({getattr(config, 'timeout_min', 0)}…{getattr(config, 'timeout_max', 0)})\n"
        f"**мут:** −{getattr(config, 'mute_hit', 0)} · **кик:** −{getattr(config, 'kick_hit', 0)}\n"
        f"**предел за час:** {getattr(config, 'max_hit_per_hour', 0)}\n"
        f"**рост:** +1 / {getattr(config, 'messages_per_point', 0)} сообщ."
        f" · +1 / {voice} мин голосового\n"
        f"{tier_block}\n"
        f"**сухой прогон:** {dry}\n"
        f"**апелляция:** {contact}"
    )


def rep_cap_body(user_id: int, *, applied: int, cut: int, score: int) -> str:
    return f"<@{user_id}>: списано **{applied}**, отрезано **{cut}** сверх часового предела.\n**Счёт:** {score}"


def rep_dry_body(user_id: int, *, applied: int, score: int) -> str:
    return f"<@{user_id}>: счёт стал бы **{score}** (−{applied}). Роли и баны не выдаются."


def rep_pardon_body(*, score: int, unbanned: bool, own_ban: bool) -> str:
    extra = REP_PARDON_UNBAN if unbanned else (REP_PARDON_KEEP_BAN if not own_ban else REP_DRY_RUN)
    return f"{REP_PARDON_DONE.format(score=score)}\n{extra}"


def rep_autoban_body(user_id: int, *, score: int) -> str:
    return (
        f"**{MOD_FIELD_TARGET}:** <@{user_id}>\n"
        f"**{MOD_FIELD_REASON}:** репутация 0\n"
        f"**Кто:** {REP_AUTOBAN_ACTOR}\n"
        f"**Репутация:** {score} → 0"
    )


def rep_would_ban_body(user_id: int, *, score: int, pardon: bool) -> str:
    if pardon:
        return f"<@{user_id}>: счёт стал бы **{score}**. Бан и роль не трогаю."
    return f"<@{user_id}>: счёт **{score}**. В сухом прогоне бан не ставлю."
