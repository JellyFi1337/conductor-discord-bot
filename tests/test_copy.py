"""Тексты для человека живут в presentation/copy, не в cogs/services."""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import fields
from pathlib import Path

from conductor.presentation import copy

SRC = Path(__file__).resolve().parents[1] / "src" / "conductor"
CYR = re.compile(r"[А-Яа-яЁё]")
LOG_FUNCS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})
LOG_OWNERS = frozenset({"log", "logger", "logging"})

ALLOWED_DUPLICATE_VALUES = frozenset(
    {
        "/admin panel",
        "Автомодерация",
        "Верификация",
        "Вес билета",
        "Владелец",
        "Всплеск входов",
        "Выдать",
        "владелец",
        "Выложить карточку",
        "Изменить сроки",
        "Сроки",
        "Голосовые комнаты",
        "Дальше",
        "Доказательства",
        "Достижения",
        "Группы",
        "Мои группы и приглашения",
        "Доступ следящего приостановлен",
        "Досье",
        "Друг",
        "Житель",
        "Закрыть",
        "Заметка",
        "Заметки",
        "Замечание",
        "Заявка на изменение",
        "Иду",
        "Канал",
        "Канал карточки",
        "Кик",
        "Ключ",
        "Когда",
        "Логи",
        "Код",
        "Комментарий",
        "Кто",
        "Модерация",
        "Мут",
        "На месте",
        "Наблюдение",
        "Набор",
        "Набор закрыт.",
        "Назад",
        "Название",
        "Напоминание",
        "Настройка",
        "Не хватает прав",
        "Обжаловать",
        "Обоснование",
        "Оставить в силе",
        "Опросы",
        "Отклонить",
        "Открыть",
        "Открытые тикеты",
        "Отменить",
        "Отменить дело",
        "Отменить наказание",
        "Первые шаги",
        "Передать",
        "Поиск команды",
        "Победителей",
        "Помиловать: вернуть счёт к старту и снять автобан",
        "Постоялец",
        "Починить",
        "Права",
        "Правило",
        "Пригласить",
        "Приоритет",
        "Приостановка снята",
        "Причина",
        "Просроченные доказательства",
        "Ранги",
        "Рекрут-ссылка",
        "Репутация",
        "Роли по выбору",
        "Роль",
        "Роль мута",
        "Роль упоминания",
        "Рост",
        "Сбор",
        "Связи",
        "Сейчас",
        "Сейчас на сервере",
        "Сигналы",
        "Следящие",
        "Срок",
        "Срок обжалования истёк.",
        "Статистика",
        "Статус",
        "Счётчик",
        "Тайм-аут",
        "Текст",
        "Тикеты",
        "Тип",
        "Убрать",
        "Удалить",
        "Участник",
        "Участники",
        "Число",
        "Экспорт",
        "все",
        "закрыт",
        "мин",
        "мут",
        "нет",
        "обычный",
        "открыт",
        "помилование",
        "участников",
        "ч",
    }
)


def _docstring_expr(body: list[ast.stmt]) -> ast.stmt | None:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[0]
    return None


def _is_log_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in LOG_FUNCS:
        return False
    owner = func.value
    if isinstance(owner, ast.Name) and owner.id in LOG_OWNERS:
        return True
    return isinstance(owner, ast.Attribute) and owner.attr in LOG_OWNERS


def _cyrillic_hits(tree: ast.AST) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []

    def walk(node: ast.AST, *, skip: ast.AST | None = None) -> None:
        for child in ast.iter_child_nodes(node):
            if child is skip:
                continue
            if _is_log_call(child):
                continue
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                walk(child, skip=_docstring_expr(child.body))
                continue
            if isinstance(child, ast.Constant) and isinstance(child.value, str) and CYR.search(child.value):
                hits.append((child.lineno, child.value))
            walk(child)

    walk(tree, skip=_docstring_expr(getattr(tree, "body", [])))
    return hits


DOMAIN_SCAN = frozenset(
    {
        "guide.py",
        "seed.py",
        "tickets.py",
        "permissions.py",
        "access.py",
        "reputation.py",
        "automod.py",
        "quotas.py",
        "showcase.py",
    }
)
PRESENTATION_SCAN = frozenset({"loyalty.py", "reputation.py", "ops.py", "diagnose.py"})


def _domain_data_literals() -> set[str]:
    from conductor.domain.access import EVERYONE, KEY_SPECS
    from conductor.domain.seed import LAYOUT, SKELETON
    from conductor.domain.showcase import SHOWCASES
    from conductor.domain.tickets import DEFAULT_CATEGORY_SPECS

    found: set[str] = {EVERYONE}
    found.update(spec.label for spec in KEY_SPECS if CYR.search(spec.label))
    found.update(spec.label for spec in SHOWCASES if CYR.search(spec.label))
    for field in fields(LAYOUT):
        value = getattr(LAYOUT, field.name)
        if isinstance(value, str) and CYR.search(value):
            found.add(value)
        elif isinstance(value, tuple):
            found.update(item for item in value if isinstance(item, str) and CYR.search(item))
    for entry in SKELETON:
        for item in (entry.name, entry.diagnose_label, entry.topic, *entry.aliases):
            if item and CYR.search(item):
                found.add(item)
    for spec in DEFAULT_CATEGORY_SPECS:
        for item in (spec.name, spec.info_text, spec.excluded_user_field, *spec.form_prompts):
            if item and CYR.search(item):
                found.add(item)
    return found


def test_cogs_and_services_keep_cyrillic_out_of_literals() -> None:
    problems: list[str] = []
    data = _domain_data_literals()
    for layer in ("cogs", "services"):
        for path in sorted((SRC / layer).rglob("*.py")):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for line, value in _cyrillic_hits(tree):
                problems.append(f"{layer}/{path.relative_to(SRC / layer).as_posix()}:{line} {value!r}")
    for path in sorted((SRC / "domain").glob("*.py")):
        if path.name not in DOMAIN_SCAN:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, value in _cyrillic_hits(tree):
            if value in data:
                continue
            problems.append(f"domain/{path.name}:{line} {value!r}")
    for path in sorted((SRC / "presentation").glob("*.py")):
        if path.name not in PRESENTATION_SCAN:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, value in _cyrillic_hits(tree):
            problems.append(f"presentation/{path.name}:{line} {value!r}")
    assert not problems, "Кириллица вне copy:\n  " + "\n  ".join(problems)


def test_copy_cmd_constants_are_referenced() -> None:
    defined: set[str] = set()
    for path in sorted((SRC / "presentation" / "copy").glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.startswith("CMD_")
            ):
                defined.add(node.targets[0].id)
    used: set[str] = set()
    files = list(SRC.rglob("*.py")) + list((SRC.parents[1] / "tests").rglob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.startswith("CMD_"):
                used.add(node.attr)
            elif isinstance(node, ast.Name) and node.id.startswith("CMD_") and isinstance(node.ctx, ast.Load):
                used.add(node.id)
    unused = sorted(defined - used)
    assert not unused, "Неиспользуемые CMD_*:\n  " + "\n  ".join(unused)


def test_scanner_flags_cyrillic_literal_and_skips_doc_and_log() -> None:
    tree = ast.parse(
        "\n".join(
            [
                '"""Модуль с кириллицей в docstring."""',
                "log = object()",
                "",
                "def helper():",
                '    """Функция."""',
                "    log.info('ключ', label='поле лога')",
                "    return 'человеку'",
            ]
        )
    )
    hits = _cyrillic_hits(tree)
    assert hits == [(7, "человеку")]


def _string_assignments(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            found.append((target.id, node.value.value.strip()))
    return found


def test_duplicate_copy_values_are_listed() -> None:
    values: dict[str, list[str]] = defaultdict(list)
    root = SRC / "presentation" / "copy"
    files = [root / "__init__.py"] if not root.is_dir() else sorted(root.rglob("*.py"))
    if not root.is_dir():
        files = [SRC / "presentation" / "copy.py"]
    for path in files:
        if path.name == "__init__.py":
            continue
        for name, value in _string_assignments(path):
            if value:
                values[value].append(name)
    unexpected = {
        value: names for value, names in values.items() if len(names) > 1 and value not in ALLOWED_DUPLICATE_VALUES
    }
    assert not unexpected, "Дубликаты без исключения:\n  " + "\n  ".join(
        f"{value!r}: {', '.join(sorted(names))}" for value, names in sorted(unexpected.items())
    )


def test_merged_aliases_share_canonical_values() -> None:
    assert copy.REP_CONFIG_BAD is copy.CONFIG_BAD
    assert copy.LADDER_CONFIG_BAD is copy.CONFIG_BAD
    assert copy.GIVEAWAY_CONFIG_BAD is copy.CONFIG_BAD
    assert copy.VOICE_STAGE_CHANNEL is copy.STAGE_CHANNEL
    assert copy.TICKET_STAGE_CHANNEL is copy.STAGE_CHANNEL
    assert copy.STATS_TABS is copy.TABS
    assert copy.PROFILE_TABS is copy.TABS
    assert copy.LOYALTY_TABS is copy.TABS
    assert copy.ADMIN_BAD_DURATION is copy.DURATION_BAD
    assert copy.MOD_DURATION_BAD is copy.DURATION_BAD
    assert copy.CMD_ADMIN_IMPORT is copy.CMD_IMPORT
    assert copy.CMD_CONFIG_IMPORT is copy.CMD_IMPORT
    assert copy.STATS_CENSUS_NEVER is copy.CENSUS_NEVER
    assert copy.PULSE_CENSUS_NEVER is copy.CENSUS_NEVER
    assert copy.DIAGNOSE_PULSE_ALERTS_NEVER is copy.NEVER
    assert copy.PULSE_NEVER is copy.NEVER
    assert copy.MOD_CANCELLED is copy.CANCELLED
    assert copy.OWNER_CANCELLED is copy.CANCELLED
    assert copy.VOICE_NAME_LABEL is copy.RENAME_LABEL
    assert copy.TICKET_RENAME_LABEL is copy.RENAME_LABEL
    assert copy.OWNER_SEED_CONFIRM is copy.OWNER_SEED_TITLE


def test_hold_text_forbids_ban_word_except_discord() -> None:
    for source in ("library", "shared", "local"):
        text = copy.hold_text(source, 90, action="создавать каналы")
        assert "запретил" not in text
    assert "запретил" not in copy.hold_text("discord", 90, action="создавать каналы")


def test_no_reopen_case_phrase() -> None:
    needle = "откройте дело снова"
    hits: list[str] = []
    roots = [SRC, SRC.parents[1] / "docs"]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in {".py", ".md"}:
                continue
            text = path.read_text(encoding="utf-8")
            if needle in text.lower():
                hits.append(path.as_posix())
    assert hits == []
