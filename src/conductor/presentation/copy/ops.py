"""Тексты: ops."""

from __future__ import annotations

from conductor.domain.seed import LAYOUT, SEED_STAGES

from .common import hold_action, hold_text
from .ladder import ladder_step
from .roles import REASON_LADDER

DEPLOY_NEED = "владелец приложения"

DEPLOY_DENIED = "Это делает только владелец приложения."

DEPLOY_TITLE = "Значки приложения"

EMOJIS_FALLBACK = "Значки движения: запасной текстовый набор"

EMOJIS_FALLBACK_ACTION = "Владелец приложения: /owner emojis — загрузка разовая, повтор не плодит дубли."


def emojis_ok(loaded: int, total: int) -> str:
    return f"Значки движения загружены ({loaded} из {total})"


def emojis_partial(loaded: int, total: int) -> str:
    return f"Значки движения: загружено {loaded} из {total}, остальное — запасной набор"


def deploy_body(*, created: int, skipped: int, loaded: int, total: int, failed: int = 0) -> str:
    text = (
        f"Создано **{created}**, уже были **{skipped}**. "
        f"Загружено **{loaded}** из **{total}**. Повторный запуск не плодит дубли."
    )
    if failed:
        text += f" Не удалось создать: **{failed}**."
    return text


OWNER_MATCH_ANY = "Любая из ролей"

OWNER_MATCH_ALL = "Все роли сразу"

OWNER_PROTECT_NONE = "Нет"

OWNER_PROTECT_STAFF = "Следящие"

OWNER_PROTECT_OLD = "Роли Old"

OWNER_PROTECT_STAFF_OLD = "Следящие и Old"

OWNER_PROTECT_LABELS = {
    "none": "нет",
    "staff": "следящие",
    "old": "роли Old",
    "staff_old": "следящие и Old",
}

OWNER_LIFT_TITLE = "Ограничения новичка сняты"

OWNER_LIFT_BODY = "Ступень «{step}», стаж {days} дн., репутация {score}. Можно проверять тикеты и набор."


def owner_lift_body(*, days: int, score: int, step: str, verified: bool) -> str:
    extra = " Роль верификации выдана." if verified else ""
    return OWNER_LIFT_BODY.format(step=ladder_step(step), days=days, score=score) + extra


OWNER_REP_TITLE = "Репутация поставлена"

OWNER_REP_BODY = "Было {before}, стало {after}."

OWNER_REP_SAME = "Счёт уже {score}."


def owner_rep_body(*, before: int, after: int) -> str:
    if before == after:
        return OWNER_REP_SAME.format(score=after)
    return OWNER_REP_BODY.format(before=before, after=after)


OWNER_NEED = "владелец бота"

OWNER_DENIED = "Это делает только владелец бота."

OWNER_LIMITS_TITLE = "Блокировки создания"

OWNER_LIMITS_EMPTY = "Сейчас нет блокировок создания на этом сервере."

OWNER_LIMITS_CLEAR = "Снять блокировки"

OWNER_LIMITS_CONFIRM = "Снять все блокировки создания на этом сервере?"

OWNER_LIMITS_WARN = "Если Discord действительно ограничивает, первая попытка вернёт блокировку."

OWNER_LIMITS_DONE = "Блокировки создания сняты."

OWNER_LIMITS_FIELD = "блокировки создания"

OWNER_LIMITS_BUSY = "Самые загруженные ведра Discord"


OWNER_WIPE_CONFIRM = "Очистить базу этого сервера?"

OWNER_WIPE_TITLE = "База очищена"

OWNER_WIPE_EMPTY = "В этой секции не было строк."

OWNER_STRIP_CONFIRM = "Снять роли с участников?"

OWNER_STRIP_TITLE = "Роли сняты"

OWNER_STRIP_VERB = "Снять роли"

OWNER_GRANT_CONFIRM = "Выдать роли участникам?"

OWNER_GRANT_TITLE = "Роли выданы"

OWNER_GRANT_VERB = "Выдать роли"

OWNER_OLD_CONFIRM = "Записать в реестр Old?"

OWNER_OLD_TITLE = "Реестр Old обновлён"

OWNER_OLD_VERB = "Записать в реестр"

OWNER_ROLES_NONE = "Нет ролей, которые бот может менять."

OWNER_ROLES_MODE = "Укажите роли или включите снятие всех. Не оба сразу."

OWNER_OLD_NEED_TIER = "Укажите ступень: эта роль не привязана к линейке Old."

OWNER_MEMBERS_NONE = "Некого менять: ботов пропускаем, подходящих людей нет."

OWNER_SEED_TITLE = "Каркас сервера"

OWNER_SEED_CONFIRM = OWNER_SEED_TITLE

OWNER_SEED_VERB = "Подключить"

OWNER_SEED_HINT = (
    "Недостающие строки бот создаёт сам. Если Discord закрыл создание, повторите /owner seed — уже сделанное останется."
)

OWNER_SEED_NOT_SERVED = "Каркас собирается только на серверах из GUILD_IDS этого бота."

OWNER_SEED_NOT_TEST = OWNER_SEED_NOT_SERVED

OWNER_SEED_NO_PERMS = (
    "Боту нужны Manage Roles, Manage Channels, Ban/Kick/Moderate Members, "
    "Move Members, Manage Messages, View Audit Log и Create Instant Invite. "
    "Роль бота должна быть выше ролей каркаса."
)

OWNER_SEED_STAGES = (
    "права бота",
    "находки",
    "права каналов",
    "привязка",
    "панели",
    "данные",
    "перепись",
)

SEED_STAGE_LABELS = dict(zip(SEED_STAGES, OWNER_SEED_STAGES, strict=True))


def seed_stage_label(key: str) -> str:
    return SEED_STAGE_LABELS.get(key, key)


def seed_unbound(name: str) -> str:
    return f"не привязано: {name}"


SEED_STATUS_MISSING = "нет"

SEED_STATUS_FOUND = "найдена"

SEED_STATUS_BOUND = "привязана"

SEED_STATUS_REPAIR = "починить"

SEED_RIGHTS_PUBLIC = "все видят"

SEED_RIGHTS_PUBLIC_RO = "все видят, без отправки"

SEED_RIGHTS_PUBLIC_RW = "все видят и пишут"

SEED_RIGHTS_VERIFIED = "скрыто от @everyone · Верифицирован и следящие видят"

SEED_RIGHTS_STAFF = "только следящие"

SEED_RIGHTS_EVIDENCE = "только Специальный и Главный Следящий"

SEED_RIGHTS_ROLE = "{colour} · {hoist}"

SEED_HOIST_YES = "отдельно"

SEED_HOIST_NO = "в списке"

SEED_SECTION_CHANNELS = "Категории и каналы"

SEED_SECTION_ROLES = "Роли · цвет и hoist"

SEED_OPTIONAL = "по желанию"

SEED_PAGE_CHANNELS = "каналы"

SEED_PAGE_ROLES = "роли"

SEED_BIND_HINT = "Есть найденные строки — бот подключит их сам."

SEED_EMPTY_HINT = "Строки со статусом «нет» бот создаст сам. Если Discord закрыл создание — повторите команду."

SEED_GROUP_HOW = "Как собрать"

SEED_GROUP_WHY = "Почему не всё сразу"

SEED_GROUP_LIMITS = "Лимиты Discord"

SEED_HOW_BODY = (
    "1. Бот создаёт недостающие категории, каналы и роли каркаса. Уже существующие с тем же именем подтягивает.\n"
    "2. Роли: точное имя, цвет из списка, «Отображать отдельно» где написано «отдельно».\n"
    "3. Каркас не удаляет объекты. Свои чаты общения — только в категории «Сервер», после того как она привязана.\n"
    "4. Потолок Discord: 12 ролей за десять минут. Полная сборка с нуля потребует повторных /owner seed."
)

SEED_WHY_BODY = (
    "Discord закрывает сервер после пачки create/delete ролей и каналов. "
    "Блокировка висит на создании, не на правах. Повтор команды безопасен: уже созданное не дублируется."
)

SEED_LIMITS_BODY = (
    "Лимиты сервера: 250 ролей, 500 каналов. Не создавайте больше десятка ролей или полусотни каналов за десять минут. "
    "Не удаляйте пачками и не чистите базу «чтобы начать заново». "
    "Права каналов бот пишет только там, где они ещё не совпадают. "
    "Перепись людей — только когда впервые подключаются роли участников."
)

SEED_WHY = {
    "cat_entry": "Воронка до верификации. Сюда не кладите обычные чаты.",
    "rules": "Текст пишет владелец. @everyone читает и не пишет.",
    "verify": "Карточка «Я человек» и ловушка. @everyone должен писать.",
    "cat_server": "Общие каналы после верификации. Свои чаты добавляйте сюда.",
    "roles": "Селекты ролей по выбору. Меню выложит бот после привязки.",
    "lfg": "Поиск команды. Карточка набора живёт здесь.",
    "polls": "Опросы сообщества. Пишет бот.",
    "video": "Лента новых видео. Фиды добавляет владелец.",
    "stats": "Доска статистики сервера.",
    "tickets": "Панель обращений. Тикеты открываются отсюда.",
    "cat_rooms": "Личные голосовые. Хаб создаёт комнаты внутри и сам их убирает.",
    "hub": "Голосовой вход «создать комнату». Не удаляйте и не клонируйте.",
    "cat_groups": "Игровые группы. Каналы групп бот создаёт по команде.",
    "cat_group_archive": "Архив групп. История для жалоб, виден только персоналу.",
    "cat_tickets": "Открытые обращения. Видны только следящим.",
    "ticket_archive": "Закрытые обращения и транскрипты.",
    "ticket_log": "Журнал открытий, закрытий и ожиданий.",
    "cat_staff": "Служебные логи. Участники не видят.",
    "staff_feed": "Лента действий следящих.",
    "admin_logs": "Кадры, настройки, сигналы.",
    "user_logs": "Входы, выходы, ники, роли, удаления.",
    "automod": "Правила автомодерации и кнопка «Перечитать».",
    "structure": "Уведомления бота о собственном каркасе.",
    "bug_reports": "Отчёты участников об ошибках бота.",
    "evidence_archive": "Закрытые каналы доказательств.",
    "cat_evidence": "Каналы дел. Только Специальный и Главный Следящий.",
    "role_special": "Полный доступ. Выдаётся владельцу сервера, если ранг свободен.",
    "role_chief": "Главный следящий. Выше старшего, ниже специального.",
    "role_senior": "Старший следящий. Между главным и обычным.",
    "role_watcher": "Базовый ранг следящего.",
    "role_ruby": "Высшая ступень Old. Необязательная линейка.",
    "role_emerald": "Ступень Old. Необязательная линейка.",
    "role_sapphire": "Ступень Old. Необязательная линейка.",
    "role_amethyst": "Нижняя ступень Old. Необязательная линейка.",
    "role_friend": "Верх лестницы активности. Бот выдаёт сам.",
    "role_resident": "Ступень «житель». Не снимайте вручную.",
    "role_regular": "Ступень «постоялец». Не снимайте вручную.",
    "role_guest": "Старт лестницы. Выдаётся при переписи, если нет другой ступени.",
    "role_exemplary": "Репутация 5. Имя можно сменить в /admin.",
    "role_respected": "Репутация 4. Имя можно сменить в /admin.",
    "role_reliable": "Репутация 3. При первой привязке бот перепишет людей.",
    "role_ordinary": "Репутация 2. Стартовый уровень.",
    "role_suspicious": "Репутация 1.",
    "role_sinner": "Репутация 0. Автобан при падении сюда.",
    "role_fixer_3": "Фиксер, высшая ступень.",
    "role_fixer_2": "Фиксер, средняя ступень.",
    "role_fixer_1": "Фиксер, первая ступень.",
    "role_ideologist_3": "Идеолог, высшая ступень.",
    "role_ideologist_2": "Идеолог, средняя ступень.",
    "role_ideologist_1": "Идеолог, первая ступень.",
    "role_vigilant_4": "Бдительный, высшая ступень.",
    "role_vigilant_3": "Бдительный, ступень 3.",
    "role_vigilant_2": "Бдительный, ступень 2.",
    "role_vigilant_1": "Бдительный, первая ступень.",
    "role_sub_youtube": "Пинг новых видео. Селект в #роли.",
    "role_sub_polls": "Пинг опросов. Селект в #роли.",
    "role_sub_announce": "Пинг анонсов. Селект в #роли.",
    "role_sub_lfg": "Пинг поиска игры. Селект в #роли.",
    "role_verified": "Открывает категорию «Сервер» и дальше. Выдаётся кнопкой «Я человек».",
    "role_mute": "Не пишет и не говорит. Hoist выключен, цвет как в списке.",
}

SEED_GROUP_TITLES = {
    "how": SEED_GROUP_HOW,
    "why": SEED_GROUP_WHY,
    "limits": SEED_GROUP_LIMITS,
    "ranks": "Ранги следящих",
    "old": "Old",
    "ladder": "Лестница активности",
    "reputation": "Репутация",
    "achievements": "Достижения",
    "subscriptions": "Подписки",
    "system": "Системные роли",
}

SEED_GROUP_HINTS = {
    "cat_entry": "Видна всем, в том числе без верификации.",
    "cat_server": "После верификации. Свои каналы общения создавайте только здесь.",
    "cat_rooms": "Личные комнаты. Хаб создаёт и удаляет каналы сам — не трогайте их пачками.",
    "cat_groups": "Игровые группы. Каналы групп создаёт бот по команде.",
    "cat_group_archive": "Архив групп. Виден только персоналу.",
    "cat_tickets": "Открытые обращения. Видны только следящим.",
    "cat_staff": "Служебные логи. Не для участников.",
    "cat_evidence": "Доказательства по делам. Только Специальный и Главный Следящий.",
    "ranks": "Роль бота должна быть выше этих четырёх.",
    "old": "Необязательные. Включите with_old, если используете линейку.",
    "ladder": "Ступени активности. Не снимайте вручную — бот ведёт сам.",
    "reputation": "Уровни счёта 0–5. Имена можно сменить в /admin.",
    "achievements": "Ступени фиксера, идеолога и бдительного. Имена — в /admin.",
    "subscriptions": "Роли из селекта в #роли. Цвет как в списке.",
    "system": "Верифицирован открывает сервер. Мут не пишет и не говорит.",
}


def seed_preview_line(kind: str, label: str) -> str:
    prefix = {"create": "вручную", "repair": "права", "by_name": "по имени"}.get(kind, kind)
    return f"{prefix} · {label}"


def seed_status_label(status: str) -> str:
    return {
        "missing": SEED_STATUS_MISSING,
        "found": SEED_STATUS_FOUND,
        "bound": SEED_STATUS_BOUND,
        "repair": SEED_STATUS_REPAIR,
    }.get(status, status)


def seed_rights_text(key: str, *, colour: str = "", hoist: bool = True) -> str:
    if key == "role":
        return SEED_RIGHTS_ROLE.format(colour=colour, hoist=SEED_HOIST_YES if hoist else SEED_HOIST_NO)
    return {
        "public": SEED_RIGHTS_PUBLIC,
        "public_ro": SEED_RIGHTS_PUBLIC_RO,
        "public_rw": SEED_RIGHTS_PUBLIC_RW,
        "verified": SEED_RIGHTS_VERIFIED,
        "staff": SEED_RIGHTS_STAFF,
        "evidence": SEED_RIGHTS_EVIDENCE,
    }.get(key, key)


def seed_manual_one(label: str) -> str:
    return f"создайте вручную: {label}"


def seed_manual_missing(count: int) -> str:
    return f"Не найдено {count} строк каркаса — создайте вручную по списку /owner seed."


def seed_parent_missing(label: str) -> str:
    return f"{label}: нет родительской категории"


def seed_create_failed(label: str, exc: object) -> str:
    return f"{label}: не создано ({exc})"


def seed_blueprint_summary(*, missing: int, found: int, bound: int, repair: int) -> str:
    return f"Нет {missing} · найдено {found} · привязано {bound} · права {repair}."


def seed_connect_line(*, by_name: int, repaired: int) -> str:
    return f"Подключено сейчас: {by_name} по имени · права {repaired}."


def seed_result_line(*, created: int, held: int, failed: int) -> str:
    return f"Создано сейчас: {created} · отложено {held} · ошибок {failed}."


def seed_why(key: str) -> str:
    return SEED_WHY.get(key, "")


def seed_group_title(key: str) -> str:
    cats = {
        "cat_entry": LAYOUT.cat_entry,
        "cat_server": LAYOUT.cat_server,
        "cat_rooms": LAYOUT.cat_rooms,
        "cat_groups": LAYOUT.cat_groups,
        "cat_group_archive": LAYOUT.cat_group_archive,
        "cat_tickets": LAYOUT.cat_tickets,
        "cat_staff": LAYOUT.cat_staff,
        "cat_evidence": LAYOUT.cat_evidence,
    }
    return cats.get(key) or SEED_GROUP_TITLES.get(key, key)


def seed_group_hint(key: str) -> str:
    return SEED_GROUP_HINTS.get(key, "")


def seed_in_category(name: str) -> str:
    return f"в категории «{name}»"


def seed_also_named(names: str) -> str:
    return f"также: {names}"


OWNER_SEED_SERVER_HINT = "добавьте свои каналы сюда — права унаследуются"

OWNER_SEED_BOUND = {}

OWNER_WIPE_HINTS = {
    "all": "Только этот сервер. Схема и другие гильдии не трогаются. Каналы Discord остаются.",
    "telemetry": "Счётчики, история ingest и голосовые сессии. Состав и настройки остаются.",
    "members": "Таблица состава. Следующая перепись заполнит заново.",
    "loyalty": "Реестр Old, планы и журнал. Роли Old на людях бот снимет по пустому реестру.",
    "access": "Ранги, ключи и заметки следящих. Дефолты прав бот восстановит.",
    "voice": "Хабы и комнаты в базе. Голосовые каналы Discord не удаляются.",
    "tickets": "Панели и тикеты в базе. Каналы Discord не удаляются.",
    "moderation": "Дела, муты и замки каналов в базе.",
    "reputation": "Счёт, события и уровни репутации. После очистки счета стартуют с 50.",
    "ladder": "Ступени активности и прогресс.",
    "achievements": "Счётчики, гранты и роли достижений. Определения ступеней тоже.",
    "social": "Розыгрыши, опросы, сборы и группы в базе.",
    "config": "Привязки логов, ролей Old, журнал смены ролей и напоминания. Каналы Discord остаются.",
}

OWNER_WIPE_LABELS = {
    "all": "всё",
    "telemetry": "телеметрию",
    "members": "состав",
    "loyalty": "реестр Old",
    "access": "права",
    "voice": "голос",
    "tickets": "тикеты",
    "moderation": "модерацию",
    "reputation": "репутацию",
    "ladder": "лестницу",
    "achievements": "достижения",
    "social": "сборы",
    "config": "настройки",
}

OWNER_WIPE_CHOICES = {
    "all": "Всё на этом сервере",
    "telemetry": "Телеметрия",
    "members": "Состав",
    "loyalty": "Реестр Old",
    "access": "Права и следящие",
    "voice": "Голосовые комнаты",
    "tickets": "Тикеты",
    "moderation": "Модерация",
    "reputation": "Репутация",
    "ladder": "Лестница активности",
    "achievements": "Достижения",
    "social": "Розыгрыши, опросы, сборы, группы",
    "config": "Настройки сервера",
}


def wipe_label(section: object) -> str:
    return OWNER_WIPE_LABELS[str(section)]


def wipe_verb(section: object) -> str:
    return f"Очистить {wipe_label(section)}"


def wipe_hint(section: object) -> str:
    return OWNER_WIPE_HINTS[str(section)]


def wipe_done_body(*, section: object, rows: int) -> str:
    if rows <= 0:
        return OWNER_WIPE_EMPTY
    return f"Секция: {wipe_label(section)}. Строк удалено: {rows}."


def seed_preview_body(
    *,
    create: int,
    reuse: int,
    repair: int,
    by_name: object = (),
    details: object = (),
) -> str:
    body = seed_blueprint_summary(missing=create, found=len(tuple(by_name or ())), bound=reuse, repair=repair)
    extra = "\n".join(str(item) for item in details if item)
    if extra:
        return f"{body}\n{extra}\n{OWNER_SEED_HINT}"
    return f"{body}\n{OWNER_SEED_HINT}"


def seed_done_body(
    *,
    created: int = 0,
    reused: int = 0,
    repaired: int = 0,
    created_roles: int = 0,
    reused_roles: int = 0,
    created_channels: int = 0,
    reused_channels: int = 0,
    created_categories: int = 0,
    bound: object = (),
    notes: object = (),
    special_granted: bool = False,
    humans: int = 0,
    by_name: object = (),
    missing: object = (),
    held: object = (),
    failed: object = (),
) -> str:
    extra = "\n".join(str(note) for note in notes if note)
    special = "да" if special_granted else "нет"
    named = [str(item) for item in by_name if item]
    missed = [str(item) for item in missing if item]
    held_keys = [str(item) for item in held if item]
    failed_keys = [str(item) for item in failed if item]
    body = (
        f"Создано {created} (категорий {created_categories}, каналов {created_channels}, ролей {created_roles}). "
        f"Подключено {len(tuple(bound or ()))}, найдено {reused}, починено {repaired}. "
        f"Роли уже на сервере: {reused_roles}. Каналы уже на сервере: {reused_channels}.\n"
        f"Ранг специальный владельцу сервера: {special}. Перепись людей: {humans}."
    )
    if named:
        body = f"{body}\nПодключены по имени: {', '.join(named)}."
    if missed:
        body = f"{body}\nЕщё нет на сервере: {len(missed)}."
    if held_keys:
        body = f"{body}\nОтложено из-за лимита Discord: {len(held_keys)}."
    if failed_keys:
        body = f"{body}\nНе создано из-за ошибки Discord: {len(failed_keys)}."
    if extra:
        return f"{body}\n{extra}"
    return body


def mass_filter_hint(
    *,
    have: str = "",
    have_mode: str = "any",
    without: str = "",
    without_mode: str = "any",
    protect: str = "none",
    skip_timeout: bool = False,
    skip_pending: bool = False,
    skip_boost: bool = False,
) -> str:
    parts: list[str] = []
    if have:
        word = "любую из" if have_mode == "any" else "все"
        parts.append(f"Только у кого есть {word}: {have}.")
    if without:
        word = "любую из" if without_mode == "any" else "все"
        parts.append(f"Не трогаем, если есть {word}: {without}.")
    if protect and protect != "none":
        parts.append(f"Автоисключение: {OWNER_PROTECT_LABELS.get(protect, protect)}.")
    extras = [
        name for flag, name in ((skip_timeout, "тайм-аут"), (skip_pending, "заявки"), (skip_boost, "бустеры")) if flag
    ]
    if extras:
        parts.append(f"Ещё пропуск: {', '.join(extras)}.")
    return " ".join(parts)


def strip_hint(*, mode: object, roles: str, members: int, filters: str = "") -> str:
    target = "все роли, которые бот может снять" if str(mode) == "all" else roles
    extra = f" {filters}" if filters else ""
    return (
        f"С участников будет снято: {target}. Людей: {members}.{extra} "
        "Ботов не трогаем. Роли Old сначала уйдут из реестра, иначе watchdog вернёт их."
    )


def grant_hint(*, roles: str, members: int, filters: str = "") -> str:
    scope = filters if filters else "Всем людям на сервере."
    return f"Выдать: {roles}. Людей: {members}. {scope} Если среди ролей есть Old, реестр обновится до выдачи."


def old_import_hint(*, role: str, tier: str, members: int) -> str:
    return (
        f"Держатели {role} попадут в реестр как {tier}. Людей: {members}. "
        "Ботов пропускаем. Привязанную роль Old бот затем выровняет по реестру."
    )


def old_import_done_body(
    *,
    added: int,
    updated: int,
    same: int,
    bots: int,
    enforced: int,
    failed: int,
    unbound: bool,
) -> str:
    line = f"Новых: {added}. Обновлено: {updated}."
    if same:
        line = f"{line} Уже были: {same}."
    if bots:
        line = f"{line} Ботов пропущено: {bots}."
    if enforced:
        line = f"{line} Роли выровнены: {enforced}."
    if failed:
        line = f"{line} Не вышло: {failed}."
    if unbound:
        line = f"{line} Роли Old ещё не привязаны — сначала `/admin panel`."
    return line


OWNER_ROLES_ALL = "все доступные"

SEED_MISSING_ROW = "нет такой строки каркаса"

SEED_EXECUTE_FAILED = "подключение каркаса оборвалось, повторите команду"

SEED_CREATE_HELD = "Создание отложено. Повторите сборку — уже созданное останется."
SEED_TIME_BUDGET = (
    "Время сборки истекло. Созданные объекты остаются на сервере; настройка завершена не полностью. "
    "Повторите сборку, чтобы продолжить."
)
SEED_BUILD_WAIT = "Ожидание лимитов — автоматически. Сборка займёт не больше примерно 3 минут."

SEED_ROLE_SPACE = (
    "позиции ролей: не хватает места под ролью бота — "
    "поднимите роль бота выше остальных и снова /owner seed"
)

SEED_CENSUS_DOWN = "перепись недоступна"

SEED_VERIFY_CARDS = "верификация: карточки не отправились"

SEED_LFG_CARD = "поиск команды: карточка не отправилась"

SEED_STATS_CARD = "статистика: карточка не отправилась"

SEED_TICKET_PANEL = "тикеты: панель не отправилась"

SEED_AUTOMOD_CARD = "автомод: карточка не отправилась"

SEED_NOT_GRANTED = "не выдана"


SEED_RIGHTS = "права "

SEED_ROLE_POS = "позиции ролей: "

SEED_BIND = "привязка: "

SEED_VERIFY = "верификация: "

SEED_SELFROLES = "роли по выбору: "

SEED_HUB = "хаб: "

SEED_OLD = "Old защита: "

SEED_MUTE = "мут: "

SEED_LADDER = REASON_LADDER

SEED_REP = "репутация: "

SEED_ACHIEVE = "достижения: "

SEED_LFG = "поиск команды: "

SEED_POLLS = "опросы: "

SEED_STATS = "статистика: "

SEED_TICKETS = "тикеты: "

SEED_AUTOMOD = "автомод: "

SEED_DATA = "сид данных: "

SEED_SPECIAL = "ранг специальный: "

SEED_CENSUS = "перепись: "

SEED_GUEST = "гость "

SEED_RELIABLE = "надёжный "


def seed_wait(seconds: object) -> str:
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        value = 0.0
    if value >= 3600:
        hours = max(1, int(round(value / 3600)))
        return f"около {hours} ч"
    if value >= 60:
        minutes = max(1, int(round(value / 60)))
        return f"около {minutes} мин"
    return "около минуты"


def seed_rate_limited(kind: str, seconds: object) -> str:
    wait = seed_wait(seconds)
    return (
        f"{seed_blocked(kind, seconds)} Подождите {wait}. "
        "Не удаляйте пачками и не чистите базу «чтобы начать заново»."
    )


def deploy_created_before_limit(created: int) -> str:
    return f"Успели создать: {int(created)}."


def seed_blocked(kind: str, seconds: object, *, source: str = "library", route: str = "") -> str:
    try:
        wait = max(0.0, float(seconds))
    except (TypeError, ValueError):
        wait = 0.0
    bucket = "role" if kind == "role" else "emoji" if kind == "emoji" else "channel"
    return hold_text(source, wait, action=hold_action(bucket), route=route)


def seed_note(prefix: str, detail: object) -> str:
    return f"{prefix}{detail}"


def seed_rights(name: object, error: object) -> str:
    return f"{SEED_RIGHTS}{name}: {error}"


def seed_grant(prefix: str, user_id: object, error: object) -> str:
    return f"{prefix}{user_id}: {error}"


BLUEPRINT_PAGE = "Файл настройки"

BLUEPRINT_WHAT = "Полная настройка сервера Conductor. Заполните и верните командой /owner apply."

BLUEPRINT_RULES = (
    "Поля, начинающиеся с подчёркивания, — пояснения. Бот их не читает, можно не трогать.",
    "null означает «не настроено». Пустая строка — это не null.",
    "Идентификаторы каналов и ролей — числа Discord (включите Режим разработчика и скопируйте ID).",
    "Вместо id можно написать имя в поле рядом — бот найдёт объект по имени.",
    "Раздел, которого нет в файле, не изменяется. Чтобы очистить поле, поставьте null.",
)

BLUEPRINT_APPLY = "Применить файл"

BLUEPRINT_CHECK = "Проверить файл"

BLUEPRINT_TITLE = "План файла настройки"

BLUEPRINT_DONE = "Файл настройки применён"

BLUEPRINT_BLOCKED = "Файл не применён: есть ошибки."

BLUEPRINT_EMPTY = "Изменений нет."

BLUEPRINT_TOO_LARGE = "Файл больше 1 МиБ — не читаю."

BLUEPRINT_BAD_TYPE = "Нужен JSON-файл."

BLUEPRINT_BAD_JSON = "Не получилось разобрать JSON."

BLUEPRINT_ISSUES = {
    "bad_json": BLUEPRINT_BAD_JSON,
    "bad_type": "Значение не того типа.",
    "bad_version": "Версия формата новее, чем умеет этот бот.",
    "not_snowflake": "Нужен числовой id Discord.",
    "out_of_range": "Число вне допустимого диапазона.",
    "bad_choice": "Значение не из списка допустимых.",
    "rank_taken": "Эта роль уже назначена другому рангу.",
    "lockout": "Так нельзя: ключ управления останется без держателя.",
    "required_null": "Обязательное поле стоит в null.",
    "foreign_guild": "Id указывает на объект другого сервера.",
    "missing_object": "Такого объекта нет на этом сервере.",
    "ambiguous_name": "По имени нашлось несколько объектов.",
    "empty_section": "Раздел есть, но пуст.",
    "too_large": BLUEPRINT_TOO_LARGE,
    "bad_content_type": BLUEPRINT_BAD_TYPE,
    "numbering_gone": "нумерация больше не поддерживается",
}


def blueprint_issue_text(code: str) -> str:
    return BLUEPRINT_ISSUES.get(code, code)


def blueprint_plan_body(*, changes: int, creates: int, errors: int, warns: int, unknown: int) -> str:
    return (
        f"Изменений: **{changes}**. Создать: **{creates}**. "
        f"Ошибки: **{errors}**. Предупреждения: **{warns}**. Неизвестные пути: **{unknown}**."
    )


def blueprint_report_body(*, applied: int, skipped: int, failed: int) -> str:
    return f"Записано **{applied}**, пропущено **{skipped}**, не вышло **{failed}**."

