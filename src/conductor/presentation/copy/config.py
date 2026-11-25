"""Тексты: config."""

from __future__ import annotations

from .admin import ADMIN_PAGE_SHOWCASE, READY_ISSUE_UNBOUND
from .common import rank_label
from .diagnose import DIAGNOSE_TAB_SKELETON
from .profile import PROFILE_ROLES
from .stats import STATS_TAB_CHANNELS

CONFIG_TITLE = "Права сервера"

CONFIG_FALLBACK = (
    "Запасной вход по праву Administrator включён, пока ранг Специального никому не привязан. "
    "После привязки вход закроется. Если роль Специального исчезнет, вход откроется снова."
)

CONFIG_UNKNOWN_KEY = "Такого ключа нет в реестре."

CONFIG_BAD_RANKS = "Не получилось прочитать список рангов."

CONFIG_IMPORTED = "Права загружены в базу этого сервера."

CONFIG_EXPORTED = "Текущие права сервера выгружены в файл."

CONFIG_NO_FILE = "Нет файла для загрузки: приложите YAML или положите data/permissions.yml.example."


CONFIG_RANK_FIELD = "Ранги"

CONFIG_KEY_FIELD = "Ключи"

CONFIG_EXTRA_FIELD = "Дополнительные роли"


def config_role_taken(owner: str) -> str:
    return f"Эта роль уже привязана к **{owner}**. Выберите другую."


def config_rank_set(rank: str, role: str) -> str:
    return f"Ранг **{rank_label(rank)}** привязан к {role}."


def config_key_set(key: str, ranks: str) -> str:
    return f"Ключ `{key}`: {ranks}."


def config_role_mentions(role_ids: list[int]) -> str:
    if not role_ids:
        return "—"
    return ", ".join(f"<@&{role_id}>" for role_id in role_ids)


def config_rank_lines(rows: list[tuple[str, str]]) -> str:
    return "\n".join(f"**{rank_label(rank)}:** {value}" for rank, value in rows)


def config_key_line(key: str, ranks: str, extra: str = "") -> str:
    suffix = f" · {extra}" if extra else ""
    return f"`{key}` — {ranks}{suffix}"


ACCESS_UNLOCK_TITLE = "Роль Специального исчезла"

ACCESS_UNLOCK_BODY = (
    "Привязка ранга Специального указывала на роль, которой больше нет. "
    "Запасной вход по праву Administrator снова открыт, пока ранг не привяжут заново."
)


def lockout_no_holder(key: str) -> str:
    return (
        f"Ключ `{key}` после записи не останется ни у одного привязанного ранга. "
        "Сначала привяжите ранг, который должен держать этот ключ."
    )


LOCKOUT_SELF = "Вы потеряете доступ к странице «Права»."

LOCKOUT_GOVERN = "Вы меняете ключ, которым чинят права сервера."

LOCKOUT_CONFIRM_VERB = "Всё равно записать"

SETUP_MAP_TITLE = "Существующие каналы и роли"

SETUP_MAP_OPEN = "Сопоставить существующие"

SETUP_MAP_APPLY = "Применить"

SETUP_MAP_RESET = "Сбросить подбор"

SETUP_MAP_BACK = "Назад"

SETUP_MAP_CREATE = "Создать новый"

SETUP_MAP_SKIP = "Пропустить"

SETUP_MAP_KEEP = "Оставить как есть"

SETUP_MAP_AUTO = "подобрано автоматически"

SETUP_MAP_STALE = "Черновик устарел. Соберите сопоставление заново."

SETUP_MAP_STALE_VERB = "Собрать заново"

SETUP_MAP_HINT = "Имя чужого канала не меняется: бот запоминает id. Неоднозначные строки оставьте себе."

SETUP_MAP_REASONS = {
    "exact": "точное имя",
    "alias": "алиас",
    "normalized": "похожее имя",
    "token": "общее слово",
    "parent": "та же категория",
    "topic": "совпала тема",
}


def setup_map_page(current: int, total: int) -> str:
    return f"Страница {current} из {total}"


def setup_map_score(reason: str, score: float) -> str:
    label = SETUP_MAP_REASONS.get(reason, reason)
    percent = max(0, min(100, int(round(score * 100))))
    return f"{label} · {percent}%"


RIGHTS_BOT_BELOW = "роль бота ниже"


def rights_bot_below(channel: object, target: object) -> str:
    name = getattr(channel, "name", getattr(channel, "id", "?"))
    role = getattr(target, "name", getattr(target, "id", "?"))
    return f"{RIGHTS_BOT_BELOW} {role}: не могу выставить права на {name}"


ADMIN_PAGE_SKELETON = DIAGNOSE_TAB_SKELETON

SKELETON_LEAF_OVERVIEW = "Обзор"

SKELETON_LEAF_CHANNELS = STATS_TAB_CHANNELS

SKELETON_LEAF_ROLES = PROFILE_ROLES

SKELETON_LEAF_SHOWCASE = ADMIN_PAGE_SHOWCASE

SKELETON_LEAF_NOTIFY = "Уведомления"

SKELETON_BUILD = "Собрать сервер"

SKELETON_BUILD_VERB = "Собрать"

SKELETON_BUILD_BUSY = "Сборка уже запущена. Подождите секунду."

SKELETON_BUILD_TITLE = "Сборка сервера"

SKELETON_NOTIFY_ON = "уведомления в #структура включены"

SKELETON_NOTIFY_OFF = "уведомления в #структура выключены"

SKELETON_NOTIFY_TOGGLE = "Переключить уведомления"

SKELETON_CREATE_ROW = "Создать"

SKELETON_PICK_OTHER = "Выбрать другой"

SKELETON_BOUND = "привязано"

SKELETON_UNBOUND = READY_ISSUE_UNBOUND

SKELETON_BUILD_STAGES = ("права бота", "создать", "права каналов", "витрины")

SKELETON_LEAF_LABELS = (
    SKELETON_LEAF_OVERVIEW,
    SKELETON_LEAF_CHANNELS,
    SKELETON_LEAF_ROLES,
    SKELETON_LEAF_SHOWCASE,
    SKELETON_LEAF_NOTIFY,
)


def skeleton_build_plan(*, create: int, adopt: int, repair: int) -> str:
    return (
        f"Создать {create}, усыновить {adopt}, починить {repair}. "
        "Повтор на собранном сервере ничего не ломает, не переименовывает и не плодит дубли."
    )


def skeleton_row_status(name: str, *, bound: bool) -> str:
    mark = SKELETON_BOUND if bound else SKELETON_UNBOUND
    return f"{name}: {mark}"
