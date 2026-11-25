"""Тексты: loyalty."""

from __future__ import annotations

LOYALTY_TAB_REGISTRY = "Реестр"

LOYALTY_TAB_WIZARD = "Выдача"

LOYALTY_BTN_CANDIDATES = "Кандидаты"

LOYALTY_BTN_PREVIEW = "Предпросмотр"

LOYALTY_BTN_FREEZE = "Заморозить план"

LOYALTY_BTN_GRANT = "Выдать"

LOYALTY_GRANT_CONFIRM = "Выдать роли Old по замороженному плану?"

LOYALTY_GRANT_VERB = "Выдать"

LOYALTY_NO_DATA = "База участников пуста. Сначала `/admin panel` → Данные → Перепись."

LOYALTY_NO_PLAN = "Сначала «Кандидаты», затем «Заморозить план»."

LOYALTY_NEED_ROLES = "Боту нужно право **Управлять ролями**."

LOYALTY_REGISTRY_BODY = (
    "Реестр — кому роли Old положены. "
    "Ручная выдача и снятие логируются и откатываются. "
    "Кнопка «Выдать» перезапишет реестр и выдаст роли."
)

LOYALTY_UNBOUND_ROLES = "Сначала `/admin panel` → Old для каждой ступени — иначе бот не знает, какие роли считать Old."

LOYALTY_GRANT_FOOT = "Выдача — «Заморозить план», затем «Выдать»"

LOYALTY_PREVIEW_BODY = (
    "Роли **не выдаются**. Кандидаты — участники с **{rule}**. Полный список во вложении `loyalty-preview.txt`."
)

LOYALTY_FROZEN_BODY = (
    "Зафиксировано **{count}** назначений. "
    "Правило то же: **{rule}**, одна роль на человека. "
    "Выдача — «Выдать». Повторная заморозка заменит этот план."
)

LOYALTY_CANDIDATES_TITLE = "Кандидаты на роли преданности"

LOYALTY_REGISTRY_TITLE = "Реестр ролей Old"

LOYALTY_PREVIEW_TITLE = "Превью ролей преданности"

LOYALTY_GRANT_TITLE = "Выдача ролей Old"

LOYALTY_NO_DATA_TITLE = "Нет данных"

LOYALTY_NO_PLAN_TITLE = "Нет плана"

LOYALTY_UNBOUND_TITLE = "Роли не привязаны"

LOYALTY_LOW_TITLE = "Роль бота слишком низко"

LOYALTY_NEED_PERMS_TITLE = "Не хватает прав"

LOYALTY_GRANTED_BODY = "Обновлено **{applied}** · нет на сервере **{skipped}** · ошибок **{errors}**."


def loyalty_granted(batch: int) -> str:
    return f"План #{batch} выдан"


def loyalty_frozen(batch: int) -> str:
    return f"План #{batch} подтверждён"


def loyalty_granted_body(*, applied: int, skipped: int, errors: int) -> str:
    return LOYALTY_GRANTED_BODY.format(applied=applied, skipped=skipped, errors=errors)


def loyalty_plan_rules(*, order: str, rule: str, created: str) -> str:
    return (
        "У одного человека будет **ровно одна** роль. "
        f"Порядок: {order}.\n"
        f"В список входят только участники с **{rule}** "
        "(счётчик — по уже прочитанной истории).\n"
        f"Ruby Old — первые 3 месяца жизни сервера (с {created})."
    )

LOYALTY_FIELD_ELIGIBLE = "Всего к выдаче"

LOYALTY_FIELD_BOTS = "Боты"

LOYALTY_FIELD_TODAY = "Зашли сегодня"

LOYALTY_FIELD_BALANCES = "Сходится"

LOYALTY_BALANCES = "{eligible} + {silent} + {today} + {bots} = **{accounted}** из {considered} в переписи"

LOYALTY_UNBOUND_MISSING = "Не заданы: {missing}.\n`/admin panel` → Old для каждой ступени."

LOYALTY_LOW_BODY = "Роль бота должна быть **выше**: {too_high}"

LOYALTY_FIELD_ROWS = "Записей"

LOYALTY_FIELD_MISMATCH = "Расхождения сейчас"

LOYALTY_FIELD_FIRST = "Первый снимок"

LOYALTY_REVERT_FAIL = "не удалось привести роли Old"

LOYALTY_REVERT_OK = "Роли Old нельзя выдавать и снимать вручную. Действие записано, состояние возвращено по реестру."

LOYALTY_REVERT_STUCK = (
    "Роли Old нельзя выдавать и снимать вручную. Откат не удался — проверьте права бота и позицию его роли."
)
