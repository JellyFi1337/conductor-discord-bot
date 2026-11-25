from __future__ import annotations

from types import SimpleNamespace

from conductor.domain.access import VOICE_STAFF_OVERRIDE, AccessDecision, Rank
from conductor.presentation.copy import (
    DENIED_TITLE,
    VOICE_FIELD_LOCK,
    VOICE_LOCKED,
    VOICE_NAME,
)
from conductor.presentation.guard import GuardResource
from conductor.presentation.layout import collected_text
from conductor.presentation.persistent import dynamic_items, parse_custom_id
from conductor.presentation.voice_panel import (
    RoomCard,
    VoiceAskButton,
    VoiceGhostButton,
    VoiceLockButton,
    owner_panel_view,
)
from conductor.services.limits import InteractionLimiter


class _Response:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **_kwargs: object) -> None:
        self._done = True

    async def send_message(self, **kwargs: object) -> None:
        self.payloads.append(kwargs)
        self._done = True


def _interaction(
    *,
    user_id: int,
    decision: AccessDecision,
    rooms: object,
) -> SimpleNamespace:
    async def decide_access(**_kwargs: object) -> AccessDecision:
        return decision

    response = _Response()

    async def followup_send(**kwargs: object) -> None:
        response.payloads.append(kwargs)

    return SimpleNamespace(
        guild=SimpleNamespace(id=1),
        user=SimpleNamespace(
            id=user_id,
            roles=(),
            guild_permissions=SimpleNamespace(administrator=False),
            voice=None,
        ),
        client=SimpleNamespace(
            interactions=InteractionLimiter(limit=50, window=60),
            decide_access=decide_access,
            log_pump=SimpleNamespace(submit=lambda *_a, **_k: None),
            config_by_guild=SimpleNamespace(get=lambda *_a, **_k: None),
            rooms=rooms,
        ),
        response=response,
        followup=SimpleNamespace(send=followup_send),
        data={"custom_id": "c:vc:lock:5"},
    )


async def test_stranger_lock_gets_archetype_g_denial() -> None:
    async def guard_resource(_guild_id: int, ref: int | str) -> GuardResource:
        return GuardResource(ref=str(ref), guild_id=1, owner_id=10, channel_id=20)

    interaction = _interaction(
        user_id=99,
        decision=AccessDecision(False, Rank.NONE, need=Rank.SENIOR),
        rooms=SimpleNamespace(guard_resource=guard_resource),
    )
    await VoiceLockButton(5).callback(interaction)  # type: ignore[arg-type]
    text = collected_text(interaction.response.payloads[0]["view"])  # type: ignore[arg-type]
    assert DENIED_TITLE in text
    assert "Это делают старшие следящие и выше." in text


async def test_owner_lock_reaches_service() -> None:
    calls: list[object] = []

    async def guard_resource(_guild_id: int, ref: int | str) -> GuardResource:
        return GuardResource(ref=str(ref), guild_id=1, owner_id=10, channel_id=20)

    async def toggle_lock(guild: object, ref: object) -> bool:
        calls.append((guild, ref))
        return True

    interaction = _interaction(
        user_id=10,
        decision=AccessDecision(False, Rank.NONE),
        rooms=SimpleNamespace(guard_resource=guard_resource, toggle_lock=toggle_lock),
    )
    await VoiceLockButton(5).callback(interaction)  # type: ignore[arg-type]
    assert calls
    text = collected_text(interaction.response.payloads[0]["view"])  # type: ignore[arg-type]
    assert VOICE_LOCKED in text


def test_owner_panel_uses_persistent_custom_ids() -> None:
    view = owner_panel_view(
        RoomCard(
            room_id=12,
            name="катка",
            owner_id=10,
            humans=2,
            limit=5,
            locked=True,
            requests=2,
        )
    )
    ids = [getattr(item, "custom_id", None) for item in view.walk_children()]
    assert "c:vc:name:12" in ids
    assert "c:vc:lock:12" in ids
    assert "c:vc:hide:12" in ids
    assert "c:vc:let:12" in ids
    assert "c:vc:den:12" in ids
    assert "c:vc:ask:12" in ids
    assert "c:vc:reqs:12" in ids
    assert "c:vc:code:12" not in ids
    assert "c:vc:inv:12" in ids
    assert "c:vc:claim:12" in ids
    assert "c:vc:take:12" in ids
    assert "c:vc:xfer:12" in ids
    assert "c:vc:rate:12" in ids
    assert "c:vc:stat:12" in ids
    assert "c:vc:lfg:12" not in ids
    assert "c:vc:want:12" not in ids
    assert "c:vc:mrg:12" in ids
    merged = owner_panel_view(
        RoomCard(room_id=12, name="катка", owner_id=10, humans=2, limit=5, locked=False, merge_id=9)
    )
    merged_ids = [getattr(item, "custom_id", None) for item in merged.walk_children()]
    assert "c:vc:okm:9" in merged_ids
    parsed = parse_custom_id("c:vc:lock:12")
    assert parsed is not None
    assert parsed.kind == "vc"
    text = collected_text(view)
    assert VOICE_NAME in text
    assert VOICE_FIELD_LOCK in text
    assert VoiceLockButton in dynamic_items()
    assert VoiceGhostButton in dynamic_items()
    assert VoiceAskButton in dynamic_items()
    assert VoiceLockButton.access_key == VOICE_STAFF_OVERRIDE
