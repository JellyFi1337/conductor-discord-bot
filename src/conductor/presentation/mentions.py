"""Единственное место с непустыми AllowedMentions: роли и список людей."""

from __future__ import annotations

from collections.abc import Sequence

import discord

from conductor.domain.discord_limits import MENTIONS_PER_MESSAGE

__all__ = ["going_mentions", "mention_batches", "role_mentions"]


def _unique_ids(user_ids: Sequence[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for raw in user_ids:
        value = int(raw)
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def role_mentions(*role_ids: int | None) -> discord.AllowedMentions:
    ids: list[int] = []
    for role_id in role_ids:
        if not role_id:
            continue
        value = int(role_id)
        if value not in ids:
            ids.append(value)
    if not ids:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=[discord.Object(id=role_id) for role_id in ids],
    )


def going_mentions(user_ids: Sequence[int]) -> discord.AllowedMentions:
    ids = _unique_ids(user_ids)
    if len(ids) > MENTIONS_PER_MESSAGE:
        raise ValueError(
            f"going_mentions: {len(ids)} id при лимите {MENTIONS_PER_MESSAGE}; используйте mention_batches"
        )
    if not ids:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[discord.Object(id=user_id) for user_id in ids],
    )


def mention_batches(
    user_ids: Sequence[int],
    *,
    per: int = MENTIONS_PER_MESSAGE,
) -> list[tuple[str, discord.AllowedMentions]]:
    """Уникальные id по порядку → пачки ≤ per: текст «<@a> <@b> …» и AllowedMentions ровно на эти id."""
    size = min(max(1, int(per)), MENTIONS_PER_MESSAGE)
    ids = _unique_ids(user_ids)
    batches: list[tuple[str, discord.AllowedMentions]] = []
    for start in range(0, len(ids), size):
        chunk = ids[start : start + size]
        text = " ".join(f"<@{user_id}>" for user_id in chunk)
        batches.append((text, going_mentions(chunk)))
    return batches
