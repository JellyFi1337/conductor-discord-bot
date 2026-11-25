"""Правило зависимостей из docs/ARCHITECTURE.md §1, проверяемое машиной.

Слои держатся не договорённостью, а этим тестом. Домен без Discord — причина,
по которой правила бота тестируются без сети; одна неосторожная строка ломает это
для всего проекта, и заметить её глазами на ревью почти невозможно.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "conductor"

#: Слой -> пакеты, которые ему запрещено импортировать.
FORBIDDEN: dict[str, frozenset[str]] = {
    "domain": frozenset({"discord", "sqlalchemy", "conductor.services", "conductor.cogs", "conductor.db"}),
    "db": frozenset({"discord", "conductor.services", "conductor.cogs"}),
    "presentation": frozenset({"sqlalchemy", "conductor.services", "conductor.cogs"}),
    "services": frozenset({"conductor.cogs"}),
}


def _modules(layer: str) -> list[Path]:
    root = SRC / layer
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_roots(path: Path) -> set[str]:
    """Имена, которые модуль тянет на верхнем уровне и внутри функций."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _violates(imported: str, banned: str) -> bool:
    return imported == banned or imported.startswith(f"{banned}.")


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_does_not_import_upwards(layer: str) -> None:
    banned = FORBIDDEN[layer]
    problems: list[str] = []
    for module in _modules(layer):
        for imported in sorted(_imported_roots(module)):
            hit = next((b for b in banned if _violates(imported, b)), None)
            if hit is not None:
                problems.append(f"{layer}/{module.name} импортирует {imported} (запрещено: {hit})")
    assert not problems, "Нарушено правило слоёв:\n  " + "\n  ".join(problems)


def test_every_layer_is_actually_checked() -> None:
    """Новый пакет-слой не должен появиться мимо этого теста."""
    packages = {p.name for p in SRC.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    packages.discard("__pycache__")
    unchecked = packages - set(FORBIDDEN) - {"cogs"}
    assert not unchecked, f"Слой без правила зависимостей: {sorted(unchecked)}"


def test_domain_has_no_side_effects_on_import() -> None:
    """В domain/ не должно быть работы на импорте: ни клиентов, ни соединений, ни задач."""
    problems: list[str] = []
    for module in _modules("domain"):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                problems.append(f"domain/{module.name}: вызов на верхнем уровне модуля")
    assert not problems, "\n".join(problems)


_ROLE_MUTATIONS = frozenset({"add_roles", "remove_roles"})
_ROLE_BROKER = SRC / "services" / "role_broker.py"


def _role_mutation_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _ROLE_MUTATIONS:
            lines.append(node.lineno)
    return lines


def test_add_roles_only_in_role_broker() -> None:
    """И1: add_roles/remove_roles вызываются только в services/role_broker.py."""
    problems: list[str] = []
    broker_hits = _role_mutation_calls(_ROLE_BROKER)
    for path in sorted(SRC.rglob("*.py")):
        if "migrations" in path.parts or path == _ROLE_BROKER:
            continue
        for line in _role_mutation_calls(path):
            rel = path.relative_to(SRC).as_posix()
            problems.append(f"{rel}:{line}")
    assert not problems, "add_roles/remove_roles вне брокера:\n  " + "\n  ".join(problems)
    assert broker_hits, "role_broker.py должен вызывать add_roles и remove_roles"


_SESSION_SCOPE = "session_scope"
_INTERACTION_HTTP = frozenset({"respond", "ack"})
_INTERACTION_METHODS = frozenset({"send", "send_message", "defer", "edit_message"})
_INTERACTION_OWNERS = frozenset({"response", "followup"})


def _attr_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_session_scope(node: ast.AsyncWith) -> bool:
    for item in node.items:
        ctx = item.context_expr
        if isinstance(ctx, ast.Call) and _attr_name(ctx.func) == _SESSION_SCOPE:
            return True
    return False


def _is_interaction_http(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name) and func.id in _INTERACTION_HTTP:
        return True
    if isinstance(func, ast.Attribute) and func.attr in _INTERACTION_METHODS:
        owner = func.value
        if isinstance(owner, ast.Attribute) and owner.attr in _INTERACTION_OWNERS:
            return True
    return False


def _interaction_http_in_session(path: Path) -> list[int]:
    """И5: await respond / ack / interaction.response|followup внутри session_scope."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[int] = []

    def walk(node: ast.AST, *, inside: bool) -> None:
        if isinstance(node, ast.AsyncWith):
            nested = inside or _is_session_scope(node)
            for child in ast.iter_child_nodes(node):
                walk(child, inside=nested)
            return
        if (
            inside
            and isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and _is_interaction_http(node.value)
        ):
            hits.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            walk(child, inside=inside)

    walk(tree, inside=False)
    return hits


_I5_LAYERS = ("cogs", "presentation", "services")


def test_cogs_do_not_respond_inside_session_scope() -> None:
    """И5: карточка не держит транзакцию на время HTTP. Коги, presentation и services."""
    problems: list[str] = []
    for layer in _I5_LAYERS:
        for path in sorted((SRC / layer).glob("*.py")):
            if path.name == "__init__.py":
                continue
            for line in _interaction_http_in_session(path):
                problems.append(f"{layer}/{path.name}:{line}")
    assert not problems, "Discord HTTP внутри session_scope:\n  " + "\n  ".join(problems)


_HEAVY_NAMES = frozenset({"session_scope", "outbound", "_discord"})
_ACK_HANDLER_NAMES = frozenset({"callback", "on_submit", "on_action"})
_ACK_SKIP_DEFS = frozenset({"ack", "respond", "run_guard"})
_COMMAND_DECORATORS = frozenset({"command", "keyed"})


def _is_command_decorator(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name) and target.id in _COMMAND_DECORATORS:
        return True
    return isinstance(target, ast.Attribute) and target.attr in _COMMAND_DECORATORS


def _is_interaction_handler(fn: ast.AsyncFunctionDef) -> bool:
    if fn.name in _ACK_SKIP_DEFS:
        return False
    if fn.name in _ACK_HANDLER_NAMES:
        return True
    if fn.name.startswith("on_"):
        return False
    if fn.name.startswith("_"):
        return False
    return any(_is_command_decorator(item) for item in fn.decorator_list)


def _is_ack_expr(node: ast.expr) -> bool:
    current = node
    if isinstance(current, ast.UnaryOp) and isinstance(current.op, ast.Not):
        current = current.operand
    if isinstance(current, ast.Await):
        current = current.value
    return isinstance(current, ast.Call) and _attr_name(current.func) == "ack"


def _heavy_lines(node: ast.AST) -> list[int]:
    hits: list[int] = []

    def walk(item: ast.AST) -> None:
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            return
        if (isinstance(item, ast.Name) and item.id in _HEAVY_NAMES) or (
            isinstance(item, ast.Attribute) and item.attr in _HEAVY_NAMES
        ):
            hits.append(item.lineno)
        for child in ast.iter_child_nodes(item):
            walk(child)

    walk(node)
    return hits


def _ack_before_heavy_violations(fn: ast.AsyncFunctionDef) -> list[int]:
    seen_ack = False
    problems: list[int] = []

    def visit(stmt: ast.stmt) -> None:
        nonlocal seen_ack
        if seen_ack or isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            return
        if isinstance(stmt, ast.If) and _is_ack_expr(stmt.test):
            seen_ack = True
            return
        if isinstance(stmt, ast.Assign) and _is_ack_expr(stmt.value):
            seen_ack = True
            return
        if isinstance(stmt, ast.Expr) and _is_ack_expr(stmt.value):
            seen_ack = True
            return
        if isinstance(stmt, ast.Return) and stmt.value is not None and _is_ack_expr(stmt.value):
            seen_ack = True
            return
        problems.extend(_heavy_lines(stmt))
        if isinstance(stmt, ast.If):
            for child in (*stmt.body, *stmt.orelse):
                visit(child)
            return
        if isinstance(stmt, ast.Try):
            for child in (*stmt.body, *stmt.orelse, *stmt.finalbody):
                visit(child)
            for handler in stmt.handlers:
                for child in handler.body:
                    visit(child)
            return
        if isinstance(stmt, ast.For | ast.AsyncFor | ast.While | ast.With | ast.AsyncWith):
            for child in stmt.body:
                visit(child)
            if isinstance(stmt, ast.For | ast.AsyncFor | ast.While):
                for child in stmt.orelse:
                    visit(child)

    for statement in fn.body:
        visit(statement)
        if seen_ack:
            break
    return problems


def _handler_ack_violations(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or not _is_interaction_handler(node):
            continue
        for line in _ack_before_heavy_violations(node):
            found.append((node.name, line))
    return found


def test_handlers_ack_before_session_or_discord() -> None:
    """Первый тяжёлый вызов в команде/callback — только после ack."""
    problems: list[str] = []
    for layer in ("cogs", "presentation"):
        for path in sorted((SRC / layer).glob("*.py")):
            if path.name == "__init__.py":
                continue
            for name, line in _handler_ack_violations(path):
                problems.append(f"{layer}/{path.name}:{line} {name}")
    assert not problems, "session_scope/outbound/_discord до ack:\n  " + "\n  ".join(problems)


def test_scanner_catches_session_scope_before_ack() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "from discord import app_commands",
                "",
                "class Cog:",
                "    @app_commands.command()",
                "    async def history(self, interaction):",
                "        async with session_scope(sessions) as session:",
                "            await session.execute(1)",
                "        if not await ack(interaction):",
                "            return",
            ]
        )
    )
    fn = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "history")
    assert _is_interaction_handler(fn)
    assert _ack_before_heavy_violations(fn)


_COG_LINE_LIMIT = 400
_SQL_CALLS = frozenset({"select", "update"})


def test_cogs_stay_under_line_limit() -> None:
    problems: list[str] = []
    for path in sorted((SRC / "cogs").glob("*.py")):
        if path.name == "__init__.py":
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > _COG_LINE_LIMIT:
            problems.append(f"{path.name}: {lines}")
    assert not problems, "cog длиннее 400 строк:\n  " + "\n  ".join(problems)


def _sql_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _attr_name(node.func)
        if name in _SQL_CALLS:
            hits.append(node.lineno)
    return hits


def test_cogs_do_not_run_sql() -> None:
    problems: list[str] = []
    for path in sorted((SRC / "cogs").glob("*.py")):
        if path.name == "__init__.py":
            continue
        for line in _sql_calls(path):
            problems.append(f"{path.name}:{line}")
    assert not problems, "select/update в cog:\n  " + "\n  ".join(problems)


def test_domain_traffic_is_stdlib_only() -> None:
    path = SRC / "domain" / "traffic.py"
    imported = _imported_roots(path)
    banned = ("discord", "aiohttp", "sqlalchemy")
    hits = [
        name
        for name in sorted(imported)
        if any(name == item or name.startswith(f"{item}.") for item in banned)
    ]
    assert not hits, f"domain/traffic.py тянет запрещённое: {hits}"


_DISCORD_PRIVATES = frozenset(
    {
        "_buckets",
        "_global_over",
        "_HTTPClient__session",
        "_max_ratelimit_timeout",
    }
)


def test_src_does_not_touch_discord_py_privates() -> None:
    problems: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in _DISCORD_PRIVATES:
                # Единственная граница совместимости с фиксированной веткой SDK.
                # HTTPClient и живые buckets по-прежнему недоступны сервисам.
                if path.name == "discord_compat.py" and node.attr == "_max_ratelimit_timeout":
                    continue
                rel = path.relative_to(SRC).as_posix()
                problems.append(f"{rel}:{node.lineno} {node.attr}")
            if isinstance(node, ast.Constant) and node.value in _DISCORD_PRIVATES:
                rel = path.relative_to(SRC).as_posix()
                problems.append(f"{rel}:{node.lineno} {node.value}")
    assert not problems, "приватные атрибуты discord.py в src/:\n  " + "\n  ".join(problems)
