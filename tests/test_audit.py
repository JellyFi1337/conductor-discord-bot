from types import SimpleNamespace

from conductor.presentation.audit import (
    SKIP_IF_USER_LOGS_SAME_CHANNEL,
    format_audit_changes,
    format_audit_extra,
    format_audit_value,
)


class _Proxy:
    def __init__(self, **kwargs: object) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __str__(self) -> str:
        return "<discord.audit_logs._AuditLogProxyMemberMoveOrMessageDelete object at 0x19F6A7EA660>"


def test_move_extra_shows_channel_and_count_not_repr() -> None:
    extra = _Proxy(channel=SimpleNamespace(id=42), count=1)
    text = "\n".join(format_audit_extra(extra))
    assert "0x19F6A7EA660" not in text
    assert "_AuditLogProxy" not in text
    assert "42" in text
    assert "1" in text


def test_empty_integration_extra_is_omitted() -> None:
    extra = _Proxy(integration_type=None)
    assert format_audit_extra(extra) == []


def test_role_grant_does_not_print_empty_before_arrow() -> None:
    entry = SimpleNamespace(
        before={"roles": []},
        after={"roles": [SimpleNamespace(id=7, mention="<@&7>")]},
        extra=_Proxy(integration_type=None),
        reason=None,
    )
    text = format_audit_changes(entry)
    assert "→" not in text
    assert "Выданы" in text
    assert "<@&7>" in text
    assert "_AuditLogProxy" not in text


def test_proxy_repr_is_never_used_as_a_value() -> None:
    assert format_audit_value(_Proxy(count=3)) == "—"


def test_same_channel_skips_nick_and_message_delete_duplicates() -> None:
    assert {"member_update", "message_delete", "message_bulk_delete"} <= SKIP_IF_USER_LOGS_SAME_CHANNEL
