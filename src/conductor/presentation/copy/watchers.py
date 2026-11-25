"""Тексты: watchers."""

from __future__ import annotations

from datetime import datetime

from conductor.presentation.icons import RANK_EMOJI, marked

from .common import HIERARCHY_DENIED, RANK_SENIOR, RANK_SPECIAL, rank_label, yn
from .moderation import mod_kind_label

WATCHERS_TITLE = "Следящие"

WATCHERS_INFO_TITLE = "Следящий"

WATCHERS_EMPTY = "На сервере нет людей с ранговой ролью следящих."

WATCHERS_EMPTY_HINT = "Привяжите роли в /admin panel → Ранги"

WATCHERS_BOT = "Бот не может быть целью этой команды."

WATCHERS_NO_RANK = "У участника нет ранговой роли следящих."

WATCHERS_UNBOUND = "Для этого ранга не привязана роль Discord."

WATCHERS_UNBOUND_HINT = "Привяжите роль в /admin panel → Ранги"

WATCHERS_SPECIAL_OFF = f"{RANK_SPECIAL} через бота не назначается. Умолчание — только вручную в Discord."

WATCHERS_SENIOR_OFF = f"{RANK_SENIOR} назначает следящего, только если это включено."

WATCHERS_DUTY_SELF = "Свою смену отмечают следящие и выше."

WATCHERS_DUTY_TARGET = "Смену отмечают только людям с ранговой ролью."

WATCHERS_APPOINTED = "Ранг выдан"

WATCHERS_DISMISSED = "Ранг снят"

WATCHERS_DUTY_SET = "Смена отмечена"

WATCHERS_NOTE_SAVED = "Заметка записана"

WATCHERS_POLICY_TITLE = "Кадровые правила"

WATCHERS_POLICY_SENIOR = f"{RANK_SENIOR} назначает следящего"

WATCHERS_POLICY_SPECIAL = f"{RANK_SPECIAL} через бота"

WATCHERS_BASIS_KEY = "ключ"

WATCHERS_BASIS_RANK = "ранг"

WATCHERS_BASIS_SELF = "своя смена"

WATCHERS_BASIS_SETTING = "настройка старшего"

WATCHERS_HEADING = {
    "special": "Специальные",
    "chief": "Главные",
    "senior": "Старшие",
    "watcher": "Следящие",
}

WATCHERS_ON_DUTY = "на смене"

WATCHERS_OFF_DUTY = "нет"

WATCHERS_NO_ACTIONS = "пока нет"

WATCHERS_FIELD_RANK = "Ранг"

WATCHERS_FIELD_ROLE = "Роль"

WATCHERS_FIELD_DUTY = "Смена"

WATCHERS_FIELD_ACTIONS = "Последние действия"

WATCHERS_FIELD_NOTES = "Заметки"

WATCHERS_FIELD_QUOTA = "Сегодня"

WATCHERS_FIELD_CASES = "Дел за 7 дней"

WATCHERS_FIELD_REVOKED = "Отменено за 30 дней"

WATCHERS_FIELD_OVERDUE = "Просроченные доказательства"

WATCHERS_FIELD_TICKETS = "Тикеты за 30 дней"

WATCHERS_FIELD_LAST = "Последнее действие"

WATCHERS_FIELD_SUSPENDED = "Приостановка"

WATCHERS_BTN_SUSPEND = "Приостановить"

WATCHERS_BTN_LIFT = "Снять приостановку"

WATCHERS_BTN_NOTE = "Заметка"

WATCHERS_SUSPEND_MODAL = "Приостановить следящего"

WATCHERS_SUSPEND_DAYS = "На сколько дней"

WATCHERS_SUSPEND_DAYS_HINT = "От 1 до 30"

WATCHERS_SUSPEND_REASON = "Почему"

WATCHERS_SUSPEND_BAD_DAYS = "Срок — целое число от 1 до 30 дней."

WATCHERS_SUSPEND_BAD_REASON = "Причина короче 4 символов."

WATCHERS_SUSPEND_ALREADY = "Этот следящий уже приостановлен."

WATCHERS_SUSPEND_MISSING = "Приостановки нет."

WATCHERS_SUSPEND_SELF = "Себя приостановить нельзя."

WATCHERS_SUSPEND_OK = "Доступ следящего приостановлен"

WATCHERS_LIFT_OK = "Приостановка снята"

WATCHERS_LIFT_MANUAL = "снята вручную"

WATCHERS_LIFT_EXPIRED = "срок истёк"

WATCHERS_SUSPEND_DM_TITLE = "Доступ следящего приостановлен"

WATCHERS_LIFT_DM_TITLE = "Приостановка снята"

WATCHERS_DUTY_SUSPENDED = "Приостановленный следящий не выходит на смену."

WATCHERS_NOTE_MODAL = "Заметка о следящем"

WATCHERS_NOTE_LABEL = "Текст заметки"

WATCHERS_DIGEST_TITLE = "Еженедельный дайджест"

WATCHERS_DIGEST_FOOTNOTE = "канал действий · без пинга"

WATCHERS_STAFF_LOG = "Состав следящих"

WATCHERS_UNOFFICIAL = "Внештатный администратор"

WATCHERS_UNOFFICIAL_BODY = "Участник с правом Administrator, но без ранговой роли следящих."

WATCHERS_LOG_FOOTNOTE = "кадры"


def watchers_count_title(count: int) -> str:
    return f"{WATCHERS_TITLE} · {count}"


def watchers_watched(count: int) -> str:
    return f"под наблюдением: {count} человек"


def watchers_heading(rank: str) -> str:
    title = marked(RANK_EMOJI.get(rank, ""), WATCHERS_HEADING.get(rank, rank))
    return f"### {title}"


def watchers_member_line(
    name: str,
    *,
    on_duty: bool,
    cases: int,
    today: str | None = None,
    rank: str | None = None,
    revoked: int | None = None,
    overdue: int | None = None,
    tickets: int | None = None,
    rating: float | None = None,
    last_action: str | None = None,
    suspended: str | None = None,
) -> str:
    parts = [f"**{name}**"]
    if rank:
        parts.append(rank)
    parts.append(WATCHERS_ON_DUTY if on_duty else WATCHERS_OFF_DUTY)
    if today:
        parts.append(today)
    parts.append(watchers_cases(cases))
    if revoked is not None:
        parts.append(watchers_revoked(revoked))
    if overdue is not None:
        parts.append(watchers_overdue(overdue))
    if tickets is not None:
        parts.append(watchers_tickets_closed(tickets, rating))
    if last_action:
        parts.append(last_action)
    if suspended:
        parts.append(suspended)
    return " · ".join(parts)


def watchers_revoked(count: int) -> str:
    if count == 1:
        return "1 отмена за 30 дней"
    if 2 <= count <= 4:
        return f"{count} отмены за 30 дней"
    return f"{count} отмен за 30 дней"


def watchers_overdue(count: int) -> str:
    if count == 1:
        return "1 просроченное доказательство"
    if 2 <= count <= 4:
        return f"{count} просроченных доказательства"
    return f"{count} просроченных доказательств"


def watchers_tickets_closed(count: int, rating: float | None) -> str:
    if count == 1:
        label = "1 тикет закрыт"
    elif 2 <= count <= 4:
        label = f"{count} тикета закрыто"
    else:
        label = f"{count} тикетов закрыто"
    if rating is None:
        return label
    return f"{label} · {rating:.1f}"


def watchers_suspended_until(when: str) -> str:
    return f"приостановлен до {when}"


def watchers_suspend_details(*, days: int, reason: str) -> str:
    return f"{days} дн. · {reason}"


def watchers_suspend_dm(when: str, reason: str) -> str:
    return f"До {when}: {reason}. Обратитесь к главному или специальному следящему."


def watchers_lift_dm(how: str) -> str:
    return f"Доступ следящего снова действует ({how})."


def watchers_suspended_deny(*, until: datetime, reason: str) -> str:
    when = f"<t:{int(until.timestamp())}:F>"
    return f"ваш доступ следящего приостановлен до {when}: {reason}; обратитесь к главному или специальному следящему."


def watchers_action_line(when: str, label: str, details: str | None = None) -> str:
    if details:
        return f"{when} · {label}: {details}"
    return f"{when} · {label}"


def watchers_digest_body(
    *,
    top_cases: list[tuple[int, int]],
    revoked_total: int,
    revoked_who: list[tuple[int, int]],
    overdue: int,
    quota: int,
    closed_no_dm: int,
    tickets_opened: int,
    tickets_closed: int,
    rating: float | None,
    unanswered: list[int],
    pending: int,
    appeals_filed: int,
    appeals_granted: int,
    automod_granted: int,
    joined: int,
    verified: int,
    trapped: int,
    left: int,
    automod_rules: list[tuple[str, int]],
    automod_channels: list[tuple[int, int]],
) -> str:
    top = ", ".join(f"<@{user_id}> {total}" for user_id, total in top_cases) or "—"
    revoked = ", ".join(f"<@{user_id}> {total}" for user_id, total in revoked_who) or "—"
    numbers = ", ".join(f"#{number}" for number in unanswered) or "нет"
    hits = ", ".join(f"{name} {total}" for name, total in automod_rules) or "—"
    channels = ", ".join(f"<#{channel_id}> {total}" for channel_id, total in automod_channels) or "—"
    score = "—" if rating is None else f"{rating:.1f}"
    return (
        f"**Следящие**\n"
        f"топ по делам: {top}\n"
        f"отменено: {revoked_total} ({revoked})\n"
        f"просроченные доказательства: {overdue}\n"
        f"отказы по квоте: {quota}\n"
        f"закрыто без ЛС: {closed_no_dm}\n"
        f"**Тикеты**\n"
        f"открыто {tickets_opened} · закрыто {tickets_closed} · оценка {score}\n"
        f"без ответа персонала > 24 ч: {numbers}\n"
        f"заявок на утверждении: {pending}\n"
        f"**Обжалования**\n"
        f"подано {appeals_filed} · удовлетворено {appeals_granted}\n"
        f"из них автомод: {automod_granted}\n"
        f"**Новички**\n"
        f"вошло {joined} · верифицировалось {verified} · ловушка {trapped} · ушло в первые сутки {left}\n"
        f"**Автомод**\n"
        f"по правилам: {hits}\n"
        f"топ каналов: {channels}"
    )


def watchers_cases(count: int) -> str:
    if count == 1:
        return "1 дело за неделю"
    if 2 <= count <= 4:
        return f"{count} дела за неделю"
    return f"{count} дел за неделю"


def watchers_rank_change(*, who: str, before: str, after: str) -> str:
    return f"**Кто:** {who}\n**Было:** {before}\n**Стало:** {after}"


def watchers_staff_action(
    *,
    who: str,
    action: str,
    actor: str,
    basis: str,
) -> str:
    return f"**Кто:** {who}\n**Действие:** {action}\n**Кто сделал:** {actor}\n**Основание:** {basis}"


def watchers_basis(*, rank: str, key: str | None = None, extra: str | None = None) -> str:
    parts = [f"{WATCHERS_BASIS_RANK} {rank_label(rank)}"]
    if key:
        parts.append(f"{WATCHERS_BASIS_KEY} `{key}`")
    if extra:
        parts.append(extra)
    return " · ".join(parts)


def watchers_note_line(when: str, author: str, body: str) -> str:
    return f"{when} · {author}: {body}"


def watchers_policy_body(*, senior: bool, special: bool) -> str:
    return f"**{WATCHERS_POLICY_SENIOR}:** {yn(senior)}\n**{WATCHERS_POLICY_SPECIAL}:** {yn(special)}"


def watchers_block(code: str) -> str:
    return {
        "bot": WATCHERS_BOT,
        "hierarchy": HIERARCHY_DENIED,
        "special": WATCHERS_SPECIAL_OFF,
        "senior": WATCHERS_SENIOR_OFF,
        "no_rank": WATCHERS_NO_RANK,
        "unbound": WATCHERS_UNBOUND,
    }.get(code, HIERARCHY_DENIED)


def watchers_appoint_action(rank: str) -> str:
    return f"назначен: {rank_label(rank)}"


def watchers_dismiss_action(rank: str) -> str:
    return f"снят: {rank_label(rank)}"


def watchers_duty_action(on: bool) -> str:
    return WATCHERS_ON_DUTY if on else WATCHERS_OFF_DUTY


STAFF_FEED_TITLE = "Действие следящего"

STAFF_FEED_SOURCE = "канал действий"

STAFF_FEED_WHO = "Кто"

STAFF_FEED_ACTION = "Действие"

STAFF_FEED_WHERE = "Кому/где"

STAFF_FEED_DETAILS = "Подробности"

STAFF_ACTION_CLAIM = "тикет: взял"

STAFF_ACTION_CLOSE = "тикет: закрыл"

STAFF_ACTION_APPROVE_REQ = "тикет: на утверждение"

STAFF_ACTION_APPROVE = "тикет: утвердил"

STAFF_ACTION_APPEAL = "тикет: обжалование"

STAFF_ACTION_UPHOLD = "тикет: оставил в силе"

STAFF_ACTION_OVERTURN = "тикет: отменил решение"

STAFF_ACTION_REASON = "тикет: причина изменена"

STAFF_ACTION_APPOINT = "назначил следящего"

STAFF_ACTION_DISMISS = "снял следящего"

STAFF_ACTION_DUTY = "смена"

STAFF_ACTION_NOTE = "заметка о следящем"

STAFF_ACTION_WATCH_SET = "наблюдение"

STAFF_ACTION_WATCH_HIT = "под наблюдением"

STAFF_ACTION_QUOTA = "отказ по квоте"

STAFF_ACTION_BYPASS = "обход лимита"

STAFF_ACTION_EXTERNAL = "санкция вне бота"

STAFF_ACTION_REVEAL = "опрос: раскрытие"

STAFF_ACTION_GIVEAWAY = "розыгрыш"

STAFF_ACTION_LFG_CLOSE = "закрыл набор"

STAFF_ACTION_GROUP_PURGE = "удалил группу из архива"

STAFF_ACTION_FORGED = "подделка кнопки"

STAFF_ACTION_PARDON = "помилование"

STAFF_ACTION_REVOKE = "отмена дела"

STAFF_ACTION_SUSPEND = "приостановил следящего"

STAFF_ACTION_LIFT = "снял приостановку"

STAFF_ACTION_AUTOMOD = "автомод"

STAFF_ACTION_JOIN_SPIKE = "всплеск входов"


def staff_feed_chief(role_id: int) -> str:
    return f"-# <@&{int(role_id)}>"


def staff_action_label(action: str) -> str:
    if action == "case.pardon":
        return STAFF_ACTION_PARDON
    if action == "case.revoke":
        return STAFF_ACTION_REVOKE
    if action.startswith("case."):
        return mod_kind_label(action.removeprefix("case."))
    return {
        "ticket.claim": STAFF_ACTION_CLAIM,
        "ticket.close": STAFF_ACTION_CLOSE,
        "ticket.approve_req": STAFF_ACTION_APPROVE_REQ,
        "ticket.approve": STAFF_ACTION_APPROVE,
        "ticket.appeal": STAFF_ACTION_APPEAL,
        "ticket.uphold": STAFF_ACTION_UPHOLD,
        "ticket.overturn": STAFF_ACTION_OVERTURN,
        "ticket.reason": STAFF_ACTION_REASON,
        "watch.appoint": STAFF_ACTION_APPOINT,
        "watch.dismiss": STAFF_ACTION_DISMISS,
        "watch.duty": STAFF_ACTION_DUTY,
        "watch.note": STAFF_ACTION_NOTE,
        "watcher.suspend": STAFF_ACTION_SUSPEND,
        "watcher.lift": STAFF_ACTION_LIFT,
        "watch.set": STAFF_ACTION_WATCH_SET,
        "watch.hit": STAFF_ACTION_WATCH_HIT,
        "automod.hit": STAFF_ACTION_AUTOMOD,
        "join.spike": STAFF_ACTION_JOIN_SPIKE,
        "quota.denied": STAFF_ACTION_QUOTA,
        "bypass": STAFF_ACTION_BYPASS,
        "external_sanction": STAFF_ACTION_EXTERNAL,
        "poll.deanonymize": STAFF_ACTION_REVEAL,
        "giveaway.start": STAFF_ACTION_GIVEAWAY,
        "lfg.close": STAFF_ACTION_LFG_CLOSE,
        "group.purge": STAFF_ACTION_GROUP_PURGE,
        "guard.forged": STAFF_ACTION_FORGED,
    }.get(action, action)


WATCHERS_REASON_PREFIX = "кадры: "

WATCHERS_WHO = "**Кто:** "

UNOFFICIAL_ADMIN = "внештатный администратор"


def watchers_reason(action: str) -> str:
    return f"{WATCHERS_REASON_PREFIX}{action}"


def watchers_who_line(mention: str) -> str:
    return f"{WATCHERS_WHO}{mention}"
