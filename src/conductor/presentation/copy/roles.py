"""Тексты: roles."""

from __future__ import annotations

from collections.abc import Sequence

ROLE_MISSING_BOT = "Бот не видит свою роль на сервере"

ROLE_MISSING_BOT_ACTION = "Перезапустите бота и проверьте, что он всё ещё состоит на сервере."

ROLES_UNBOUND = "Управляемые роли Old не привязаны"

ROLES_UNBOUND_ACTION = "Привяжите каждую ступень в /admin panel → Old."

ROLES_STALE_ACTION = "Роль удалили или id устарел. Привяжите заново в /admin panel → Old."

ROLE_BELOW_ACTION = "Поднимите роль бота выше этих ролей в настройках сервера."

ROLE_ABOVE_OK = "Роль бота выше управляемых ролей Old"


def roles_not_found(names: Sequence[str]) -> str:
    return "Не найдены роли: " + ", ".join(names)


def role_below(names: Sequence[str]) -> str:
    return "Роль бота ниже управляемых: " + ", ".join(names)


SELFROLES_TITLE = "Роли по выбору"

SELFROLES_GENERAL_BODY = "общие подписки"

SELFROLES_GAME_BODY = "роли по играм открывают каналы игр"

SELFROLES_PICK = "Выберите роли"

SELFROLES_EMPTY = "В этом меню ещё нет ролей."

SELFROLES_NEED_MENU = "Сначала выберите меню."

SELFROLES_NEED_CHANNEL = "Сначала выберите канал карточки."

SELFROLES_LIMIT = "В меню не больше 25 ролей."

SELFROLES_TAKEN = "Эта роль уже есть в другом меню."

SELFROLES_SPECIAL = "Специальная роль не может стать опцией."

SELFROLES_STAFF = "Ранговая роль следящих не может стать опцией."

SELFROLES_BAD_TAG = "Тег: none, polls, youtube, announce или lfg."

SELFROLES_BAD_LABEL = "Подпись не должна быть пустой."

SELFROLES_NONE = "без изменений"

SELFROLES_OPENED = "Открылось: {channels}"

SELFROLES_OPENS = "открывает: {channels}"


ROLE_SUBSYSTEM_LABELS = {
    "loyalty": "реестр Old",
    "reputation": "репутация",
    "activity": "лестница активности",
    "staff": "ранги следящих",
    "moderation": "модерация",
    "giveaway": "розыгрыш",
    "verification": "верификация",
    "selfroles": "роли по выбору",
    "achievements": "достижения",
}


def role_subsystem_label(subsystem: str) -> str:
    return ROLE_SUBSYSTEM_LABELS.get(subsystem, subsystem)


VERIFICATION_TITLE = "Верификация"

VERIFICATION_OK = "Я человек"

VERIFICATION_RULES = (
    "Нажмите «Я человек», чтобы открыть сервер.",
    "Писать в этом канале нельзя — сообщение даёт бан.",
)

VERIFICATION_WARN_TITLE = "Писать здесь нельзя"

VERIFICATION_WARN_BODY = "Писать здесь нельзя — любое сообщение даёт бан автоматически."

VERIFICATION_BYPASS = "обход ловушки верификации"

VERIFICATION_NEED_ROLE = "Сначала выберите роль верификации на странице /admin → Верификация."

VERIFICATION_NEED_CHANNEL = "Сначала выберите канал верификации."

VERIFICATION_ROLE_FAILED = "Роль не выдана: бот не может изменить эту роль. Следящие уже уведомлены — попробуйте позже."

VERIFICATION_ROLE_FAILED_LOG = "Верификация не выдаёт роль"

ONBOARDING_TITLE = "Первые шаги"

ONBOARDING_ALREADY = "вы уже верифицированы"

ONBOARDING_EMPTY = "Каналы открыты. `/help` расскажет, что здесь можно"

ONBOARDING_ROLES = "выберите роли в {channel}"

ONBOARDING_LFG = "найдите тиммейта в {channel}"

ONBOARDING_TICKETS = "вопросы и жалобы через {channel}"

ONBOARDING_PROFILE = "`/profile` покажет ваш путь"

ONBOARDING_HELP = "`/help` расскажет всё"

JOIN_SPIKE_TITLE = "Всплеск входов"

JOIN_SPIKE_HINT = "проверьте канал верификации; при необходимости `/mod lock` на общие каналы"


def onboarding_lines(
    *,
    already: bool = False,
    roles: str | None = None,
    lfg: str | None = None,
    tickets: str | None = None,
) -> list[str]:
    lines: list[str] = []
    if already:
        lines.append(ONBOARDING_ALREADY)
    extras = [
        ONBOARDING_ROLES.format(channel=roles) if roles else None,
        ONBOARDING_LFG.format(channel=lfg) if lfg else None,
        ONBOARDING_TICKETS.format(channel=tickets) if tickets else None,
    ]
    found = [item for item in extras if item]
    if found:
        lines.extend(found)
        lines.append(ONBOARDING_PROFILE)
        lines.append(ONBOARDING_HELP)
    else:
        lines.append(ONBOARDING_EMPTY)
    return lines


def verification_ban_dm(appeal: str) -> str:
    return f"вы забанены за сообщение в канале верификации; апелляция — {appeal}"


def join_spike_body(joined: int, floor: int) -> str:
    return f"за 10 минут вошло **{int(joined)}** (порог {int(floor)}). {JOIN_SPIKE_HINT}"


def selfroles_result(*, granted: Sequence[str], removed: Sequence[str]) -> str:
    parts: list[str] = []
    if granted:
        parts.append("Выдано: " + " · ".join(granted))
    if removed:
        parts.append("Снято: " + " · ".join(removed))
    return " · ".join(parts) if parts else SELFROLES_NONE


def selfroles_opened(channels: Sequence[str]) -> str:
    return SELFROLES_OPENED.format(channels=" · ".join(channels))


def selfroles_opens(channels: Sequence[str]) -> str:
    return SELFROLES_OPENS.format(channels=" · ".join(channels))


def selfroles_member_line(title: str, labels: Sequence[str]) -> str:
    return f"{title}: " + " · ".join(labels)


def roles_done_body(
    *,
    changed: int,
    failed: int,
    skipped: int,
    registry: int,
    grant: bool,
    skipped_roles: str = "",
    skipped_require: int = 0,
    skipped_except: int = 0,
    skipped_protect: int = 0,
    skipped_timeout: int = 0,
    skipped_pending: int = 0,
    skipped_boost: int = 0,
) -> str:
    verb = "получили роли" if grant else "лишились ролей"
    line = f"Людей {verb}: {changed}."
    if failed:
        line = f"{line} Не вышло: {failed}."
    detail = [
        f"нет нужной роли {skipped_require}" if skipped_require else "",
        f"исключение {skipped_except}" if skipped_except else "",
        f"автоисключение {skipped_protect}" if skipped_protect else "",
        f"тайм-аут {skipped_timeout}" if skipped_timeout else "",
        f"заявка {skipped_pending}" if skipped_pending else "",
        f"буст {skipped_boost}" if skipped_boost else "",
    ]
    bits = [item for item in detail if item]
    if skipped and bits:
        line = f"{line} Пропущено: {skipped} — {', '.join(bits)}."
    elif skipped:
        line = f"{line} Пропущено: {skipped}."
    if registry:
        line = f"{line} Записей реестра Old: {registry}."
    if skipped_roles:
        line = f"{line}\nБот не может менять: {skipped_roles}."
    return line


ROLE_BELOW_HINT = "ниже управляемых"


REASON_BY_PREFIX = "по команде "

REASON_TICKET = "тикет"

REASON_EVIDENCE = "дело"

REASON_TICKET_CLOSE = "тикет закрыт"

REASON_ACHIEVEMENT = "достижение"

REASON_GIVEAWAY_PRIZE = "приз розыгрыша"

REASON_GIVEAWAY_CONSOLATION = "утешение розыгрыша"

REASON_GROUP = "группа"

REASON_GROUP_ARCHIVE = "архив группы"

REASON_GROUP_PURGE = "удаление группы из архива"

REASON_GROUP_RESTORE = "возврат группы из архива"

REASON_LADDER = "лестница: "

REASON_REP_PARDON = "помилование репутации, актёр "

REASON_REP_LEVEL = "репутация: уровень "

REASON_LOCK_CLEAR = "снятие замка"

REASON_LOCK_EXPIRED = "истёк срок замка"

REASON_SELF_ROLES = "Conductor: роли по выбору"

REASON_VERIFY = "Conductor: верификация"

REASON_VOICE = "Conductor: временная комната"

REASON_VOICE_EMPTY = "Conductor: пустая временная комната"

REASON_VOICE_TEXT = "Conductor: связанный текст комнаты"

REASON_VOICE_OVERFLOW = "Conductor: дополнительная категория комнат"

REASON_VOICE_EXTRA = "Conductor: лишняя временная комната"

REASON_OWNER_STRIP = "Conductor: владелец, снятие ролей"

REASON_OWNER_GRANT = "Conductor: владелец, выдача ролей"

REASON_OWNER_LIFT = "Conductor: владелец, снятие ограничений новичка"

REASON_LOYALTY_REVERT = "Conductor: откат ручного изменения роли Old"

REASON_LOYALTY_GRANT = "Conductor: выдача роли преданности"


ROLE_GUILD_GONE = "сервер недоступен"

ROLE_MEMBER_GONE = "участник не на сервере"


def role_overlap(role_id: int, owner: str) -> str:
    return f"роль {role_id} уже занята подсистемой {owner}"


def role_missing(role_id: object) -> str:
    return f"роль {role_id} не найдена"


def role_skipped_body(role_ids: Sequence[int], *, hint: str) -> str:
    mentions = ", ".join(f"<@&{int(role_id)}>" for role_id in role_ids)
    if mentions and hint:
        return f"{mentions}. {hint}"
    return mentions or hint


def role_managed(name: object) -> str:
    return f"роль {name} управляется интеграцией"


def role_above(name: object) -> str:
    return f"роль {name} выше роли бота"


def actor_reason(actor_id: object | None, text: str) -> str:
    body = (text or "").strip()
    try:
        who = int(actor_id) if actor_id is not None else 0
    except (TypeError, ValueError):
        who = 0
    if who:
        prefix = f"{REASON_BY_PREFIX}{who}"
        combined = f"{prefix}: {body}" if body else prefix
    else:
        combined = body
    return combined[:512]


def reason_ladder(key: object) -> str:
    return f"{REASON_LADDER}{key}"


def reason_rep_pardon(actor_id: object) -> str:
    return f"{REASON_REP_PARDON}{actor_id}"


def reason_rep_level(level: object) -> str:
    return f"{REASON_REP_LEVEL}{level}"
