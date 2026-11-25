from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from conductor.domain.discord_limits import MENTIONS_PER_MESSAGE, TEXT_DISPLAY_TOTAL
from conductor.presentation.mentions import going_mentions, mention_batches, role_mentions

SRC = Path(__file__).resolve().parents[1] / "src" / "conductor"
CONSTRUCTOR = re.compile(r"AllowedMentions\s*\(")
ALLOWED = {"presentation/mentions.py"}


def test_role_mentions_is_exactly_one_role() -> None:
    mentions = role_mentions(7)
    assert mentions.everyone is False
    assert mentions.users is False
    assert [role.id for role in mentions.roles] == [7]


def test_role_mentions_accepts_several_roles() -> None:
    mentions = role_mentions(7, None, 8, 7)
    assert mentions.everyone is False
    assert mentions.users is False
    assert [role.id for role in mentions.roles] == [7, 8]


def test_role_mentions_none_is_silent() -> None:
    mentions = role_mentions(None)
    assert mentions.everyone is False
    roles = mentions.roles
    assert not roles or roles is False


def test_going_mentions_lists_people_only() -> None:
    mentions = going_mentions((11, 12))
    assert mentions.everyone is False
    assert mentions.roles is False
    assert [user.id for user in mentions.users] == [11, 12]


def test_going_mentions_keeps_order_and_drops_duplicates() -> None:
    mentions = going_mentions((11, 12, 11, 13))
    assert [user.id for user in mentions.users] == [11, 12, 13]


def test_going_mentions_over_limit_is_developer_error() -> None:
    ids = tuple(range(1, MENTIONS_PER_MESSAGE + 2))
    with pytest.raises(ValueError, match="mention_batches"):
        going_mentions(ids)


def test_mention_batches_split_thousand_unique_ids() -> None:
    ids = [10**18 + index for index in range(1000)]
    ids.extend(ids[:10])
    batches = mention_batches(ids)
    assert len(batches) == 10
    seen: list[int] = []
    for text, mentions in batches:
        users = [user.id for user in mentions.users]
        assert len(users) <= MENTIONS_PER_MESSAGE
        assert len(text) <= TEXT_DISPLAY_TOTAL
        assert mentions.everyone is False
        assert mentions.roles is False
        assert text == " ".join(f"<@{user_id}>" for user_id in users)
        seen.extend(users)
    assert seen == [10**18 + index for index in range(1000)]


def test_nonempty_allowed_mentions_live_only_in_mentions_module() -> None:
    hits: list[str] = []
    nonempty: list[str] = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(SRC).as_posix()
        if CONSTRUCTOR.search(text):
            hits.append(rel)
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "AllowedMentions":
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            if _nonempty_mentions(keywords):
                nonempty.append(rel)
    assert set(hits) == ALLOWED
    assert set(nonempty) <= ALLOWED


def _nonempty_mentions(keywords: dict[str, ast.AST]) -> bool:
    everyone = keywords.get("everyone")
    if isinstance(everyone, ast.Constant) and everyone.value is True:
        return True
    for key in ("users", "roles"):
        value = keywords.get(key)
        if value is None:
            continue
        if isinstance(value, ast.Constant) and value.value in (None, False):
            continue
        if isinstance(value, ast.List) and not value.elts:
            continue
        return True
    return False
