from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class GuildConfig(Base):
    __tablename__ = "guild_configs"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_log_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user_log_keep_days: Mapped[int] = mapped_column(Integer, default=90, server_default="90")
    admin_log_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    staff_feed_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    automod_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    structure_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    structure_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recruit_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    ruby_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    emerald_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sapphire_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    amethyst_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    census_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    loyalty_registry_seeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    staff_digest_week: Mapped[str | None] = mapped_column(String(16), nullable=True)


class GuildLayout(Base):
    """Id категорий и каналов каркаса, у которых нет своей настройки."""

    __tablename__ = "guild_layout"
    __table_args__ = (UniqueConstraint("guild_id", "key", name="uq_guild_layout_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    key: Mapped[str] = mapped_column(String(32))
    channel_id: Mapped[int] = mapped_column(BigInteger)


class MemberRecord(Base):
    __tablename__ = "members"
    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", name="uq_member"),
        Index("ix_members_guild_user", "guild_id", "user_id"),
        Index("ix_members_guild_current", "guild_id", "currently_member"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(40), default="")
    display_name: Mapped[str] = mapped_column(String(80), default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    currently_member: Mapped[bool] = mapped_column(Boolean, default=True)
    first_joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    account_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    booster_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    voice_seconds: Mapped[int] = mapped_column(Integer, default=0)
    invite_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attachment_count: Mapped[int] = mapped_column(Integer, default=0)
    sticker_count: Mapped[int] = mapped_column(Integer, default=0)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    link_count: Mapped[int] = mapped_column(Integer, default=0)


class ActivityBucket(Base):
    __tablename__ = "activity_buckets"
    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "period_start",
            "period_kind",
            "event_kind",
            name="uq_activity_bucket",
        ),
        Index("ix_activity_bucket_lookup", "guild_id", "period_kind", "period_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    period_kind: Mapped[str] = mapped_column(String(16))
    event_kind: Mapped[str] = mapped_column(String(16))
    count: Mapped[int] = mapped_column(Integer, default=0)


class LoyaltyBatch(Base):
    __tablename__ = "loyalty_batches"
    __table_args__ = (Index("ix_loyalty_batches_guild_status", "guild_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="confirmed")
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class LoyaltyRegistry(Base):
    __tablename__ = "loyalty_registry"
    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", name="uq_loyalty_registry"),
        Index("ix_loyalty_registry_guild_user", "guild_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    tier: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoyaltyRoleEvent(Base):
    __tablename__ = "loyalty_role_events"
    __table_args__ = (Index("ix_loyalty_role_events_guild_user", "guild_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    role_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(16))
    reverted: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MemberDaily(Base):
    __tablename__ = "member_daily"
    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", "day", name="uq_member_daily"),
        Index("ix_member_daily_guild_day", "guild_id", "day"),
        Index("ix_member_daily_guild_user_day", "guild_id", "user_id", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    day: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    messages: Mapped[int] = mapped_column(Integer, default=0)
    voice_seconds: Mapped[int] = mapped_column(Integer, default=0)
    reactions: Mapped[int] = mapped_column(Integer, default=0)


class ChannelDaily(Base):
    __tablename__ = "channel_daily"
    __table_args__ = (
        UniqueConstraint("guild_id", "channel_id", "day", "event_kind", name="uq_channel_daily"),
        Index("ix_channel_daily_guild_day", "guild_id", "day"),
        Index("ix_channel_daily_lookup", "guild_id", "channel_id", "day", "event_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    day: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event_kind: Mapped[str] = mapped_column(String(16), default="message")
    count: Mapped[int] = mapped_column(Integer, default=0)


class ChannelScan(Base):
    __tablename__ = "channel_scans"
    __table_args__ = (
        UniqueConstraint("guild_id", "channel_id", name="uq_channel_scan"),
        Index("ix_channel_scans_guild", "guild_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    oldest_message_id: Mapped[int] = mapped_column(BigInteger)
    newest_message_id: Mapped[int] = mapped_column(BigInteger)
    messages_ingested: Mapped[int] = mapped_column(Integer, default=0)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    caught_up: Mapped[bool] = mapped_column(Boolean, default=False)


class InteractionEdge(Base):
    __tablename__ = "interaction_edges"
    __table_args__ = (
        UniqueConstraint("guild_id", "source_id", "target_id", "kind", name="uq_interaction_edge"),
        Index("ix_interaction_edges_source", "guild_id", "source_id"),
        Index("ix_interaction_edges_target", "guild_id", "target_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    count: Mapped[int] = mapped_column(Integer, default=0)
    last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemberChannel(Base):
    __tablename__ = "member_channels"
    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", "channel_id", name="uq_member_channel"),
        Index("ix_member_channels_guild_user", "guild_id", "user_id"),
        Index("ix_member_channels_guild_channel", "guild_id", "channel_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    messages: Mapped[int] = mapped_column(Integer, default=0)
    attachments: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)
    last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IngestedMessage(Base):
    __tablename__ = "ingested_messages"
    __table_args__ = (
        UniqueConstraint("guild_id", "message_id", name="uq_ingested_message"),
        Index("ix_ingested_messages_lookup", "guild_id", "message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)


class IngestedReaction(Base):
    __tablename__ = "ingested_reactions"
    __table_args__ = (UniqueConstraint("guild_id", "message_id", "source_id", "emoji", name="uq_ingested_reaction"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_id: Mapped[int] = mapped_column(BigInteger)
    emoji: Mapped[str] = mapped_column(String(128))


class VoiceSession(Base):
    """Open or closed stretch of time a member spent in any voice channel."""

    __tablename__ = "voice_sessions"
    __table_args__ = (
        Index("ix_voice_sessions_guild_user", "guild_id", "user_id"),
        Index("ix_voice_sessions_open", "guild_id", "user_id", "left_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class Panel(Base):
    """Постоянная панель: переживает рестарт, пока сообщение живо."""

    __tablename__ = "panels"
    __table_args__ = (
        UniqueConstraint("guild_id", "message_id", name="uq_panel_message"),
        Index("ix_panels_guild_state", "guild_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ref: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), default="live")


class RoleChange(Base):
    """Журнал смены ролей через брокер: кто, что, почему, подсистема, итог."""

    __tablename__ = "role_changes"
    __table_args__ = (
        Index("ix_role_changes_guild_user", "guild_id", "user_id"),
        Index("ix_role_changes_guild_created", "guild_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    subsystem: Mapped[str] = mapped_column(String(16))
    add_role_ids: Mapped[str] = mapped_column(Text)
    remove_role_ids: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GuildRank(Base):
    """Привязка ранга прав к роли Discord на одном сервере."""

    __tablename__ = "guild_ranks"
    __table_args__ = (UniqueConstraint("guild_id", "rank", "role_id", name="uq_guild_rank_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    rank: Mapped[str] = mapped_column(String(16))
    role_id: Mapped[int] = mapped_column(BigInteger)


class GuildPermission(Base):
    """Ключ действия и набор рангов (плюс дополнительные роли) для гильдии."""

    __tablename__ = "guild_permissions"
    __table_args__ = (UniqueConstraint("guild_id", "key", name="uq_guild_permission"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    key: Mapped[str] = mapped_column(String(64))
    ranks: Mapped[str] = mapped_column(Text)
    extra_role_ids: Mapped[str] = mapped_column(Text, default="[]")


class GuildAccessState(Base):
    """Состояние доступа гильдии. Замок Administrator считается из привязки Special."""

    __tablename__ = "guild_access_states"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    special_ever_bound: Mapped[bool] = mapped_column(Boolean, default=False)
    senior_appoint: Mapped[bool] = mapped_column(Boolean, default=False)
    special_appoint: Mapped[bool] = mapped_column(Boolean, default=False)


class UnofficialAdminNotice(Base):
    """Один раз написали в лог про Administrator без ранга — больше не спамим."""

    __tablename__ = "unofficial_admin_notices"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    noticed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WatcherDuty(Base):
    """Кто сейчас на смене. Смена не даёт ключей, только видимость."""

    __tablename__ = "watcher_duty"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    set_by: Mapped[int] = mapped_column(BigInteger)


class WatcherNote(Base):
    """Служебная заметка. Не замечание и не дело модерации."""

    __tablename__ = "watcher_notes"
    __table_args__ = (Index("ix_watcher_notes_guild_user_created", "guild_id", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_id: Mapped[int] = mapped_column(BigInteger)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WatcherSuspension(Base):
    """Временная приостановка доступа следящего. Роль ранга не снимается."""

    __tablename__ = "watcher_suspensions"
    __table_args__ = (
        Index("ix_watcher_suspensions_guild_user", "guild_id", "user_id"),
        Index(
            "uq_watcher_suspensions_active",
            "guild_id",
            "user_id",
            unique=True,
            sqlite_where=text("lifted_at IS NULL"),
            postgresql_where=text("lifted_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    set_by: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VoiceHub(Base):
    """Канал-хаб: вход создаёт временную комнату. Не путать с voice_sessions."""

    __tablename__ = "voice_hubs"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    category_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    settings: Mapped[str] = mapped_column(Text, default="{}")


class VoiceRoom(Base):
    """Временная комната, созданная входом в хаб. Не голосовая сессия из 0c."""

    __tablename__ = "voice_rooms"
    __table_args__ = (
        UniqueConstraint("guild_id", "owner_id", name="uq_voice_room_owner"),
        UniqueConstraint("guild_id", "channel_id", name="uq_voice_room_channel"),
        Index("ix_voice_rooms_guild_hub", "guild_id", "hub_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    hub_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    owner_absent_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ghosted: Mapped[bool] = mapped_column(Boolean, default=False)
    text_channel_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
        doc="устарело: всегда NULL, колонка уйдёт в следующем релизе",
    )


class VoiceRoomKick(Base):
    """Человек с закрытым входом не заходит, пока эта комната жива."""

    __tablename__ = "voice_room_kicks"
    __table_args__ = (Index("ix_voice_room_kicks_room", "room_id"),)

    room_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class VoiceRoomRequest(Base):
    """Заявка «хочу войти» при замке. Одна на человека, пока не разобрана."""

    __tablename__ = "voice_room_requests"
    __table_args__ = (UniqueConstraint("room_id", "user_id", name="uq_voice_room_request"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VoiceRoomLfg(Base):
    """Запись в LFG комнаты. Живёт минуты, пока набор открыт."""

    __tablename__ = "voice_room_lfg"
    __table_args__ = (Index("ix_voice_room_lfg_room", "room_id"),)

    room_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class VoiceRoomMerge(Base):
    """Запрос слияния: владельцы обеих комнат должны согласиться."""

    __tablename__ = "voice_room_merges"
    __table_args__ = (UniqueConstraint("from_room_id", name="uq_voice_room_merge_from"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_room_id: Mapped[int] = mapped_column(Integer)
    to_room_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TicketPanel(Base):
    """Постоянная панель тикетов в канале."""

    __tablename__ = "ticket_panels"
    __table_args__ = (UniqueConstraint("guild_id", "channel_id", name="uq_ticket_panel_channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    settings: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(BigInteger)


class TicketCategory(Base):
    """Кнопка категории на панели."""

    __tablename__ = "ticket_categories"
    __table_args__ = (
        UniqueConstraint("panel_id", "name", name="uq_ticket_category_name"),
        Index("ix_ticket_categories_panel", "panel_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    panel_id: Mapped[int] = mapped_column(Integer)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(80))
    position: Mapped[int] = mapped_column(Integer, default=0)
    hint: Mapped[str] = mapped_column(Text, default="")
    form_json: Mapped[str] = mapped_column(Text, default="{}")
    kind: Mapped[str] = mapped_column(String(16), default="general")
    visible_ranks: Mapped[str] = mapped_column(Text, default='["watcher","senior","chief","special"]')
    topic: Mapped[str] = mapped_column(String(32), default="")
    info_text: Mapped[str] = mapped_column(Text, default="")
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    min_tenure_days: Mapped[int] = mapped_column(Integer, default=0)
    min_ladder_step: Mapped[str] = mapped_column(String(16), default="guest")
    excluded_user_field: Mapped[str] = mapped_column(String(80), default="")


class Ticket(Base):
    """Обращение. Закрытие мягкое: строка остаётся."""

    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("guild_id", "number", name="uq_tickets_number"),
        Index("ix_tickets_guild_author_state", "guild_id", "owner_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    number: Mapped[int] = mapped_column(Integer)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    category_id: Mapped[int] = mapped_column(Integer)
    panel_id: Mapped[int] = mapped_column(Integer, index=True)
    state: Mapped[str] = mapped_column(String(16), default="open")
    claimed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    claimed_rank: Mapped[int] = mapped_column(Integer, default=0)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    form_answers: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    autoclose_warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    staff_notes: Mapped[str] = mapped_column(Text, default="[]")
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    subject_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    dm_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    log_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class TicketEvent(Base):
    """Журнал переходов заявки: утверждение, обжалование, напоминание."""

    __tablename__ = "ticket_events"
    __table_args__ = (Index("ix_ticket_events_ticket_created", "ticket_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(Integer, index=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TicketTranscript(Base):
    """Сохранённая переписка закрытого тикета."""

    __tablename__ = "ticket_transcripts"
    __table_args__ = (
        Index("ix_ticket_transcripts_guild_id", "guild_id"),
        Index("ix_ticket_transcripts_created_at", "created_at"),
    )

    ticket_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModSettings(Base):
    """Настройки модерации гильдии: роль мута, квоты и доказательства."""

    __tablename__ = "mod_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mute_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quota_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_category_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    evidence_archive_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    evidence_remind_hours: Mapped[int] = mapped_column(Integer, default=24, server_default="24")


class ModCase(Base):
    """Дело модерации. Сквозная нумерация на гильдию, строка не удаляется."""

    __tablename__ = "mod_cases"
    __table_args__ = (
        UniqueConstraint("guild_id", "number", name="uq_mod_cases_number"),
        Index("ix_mod_cases_guild_target", "guild_id", "owner_id"),
        Index("ix_mod_cases_guild_actor_created", "guild_id", "created_by", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    number: Mapped[int] = mapped_column(Integer)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    actor_rank: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    appeal_ticket_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class MemberWatch(Base):
    """Активное наблюдение за участником: одна запись на человека."""

    __tablename__ = "member_watch"
    __table_args__ = (UniqueConstraint("guild_id", "user_id", name="uq_member_watch_person"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    set_by: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(400))
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AutomodHit(Base):
    """Срабатывание правила автомодерации."""

    __tablename__ = "automod_hits"
    __table_args__ = (Index("ix_automod_hits_guild_user_created", "guild_id", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    rule: Mapped[str] = mapped_column(String(32))
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    case_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VerificationSettings(Base):
    """Канал-ловушка и роль «верифицирован»."""

    __tablename__ = "verification_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rules_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    verified_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    panel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warning_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class SelfRoleMenu(Base):
    """Меню ролей по выбору: подписки или игры."""

    __tablename__ = "self_role_menus"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(80))
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    panel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class StatsBoard(Base):
    """Одна карточка статистики на гильдию."""

    __tablename__ = "stats_boards"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    panel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    roles_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hash: Mapped[str] = mapped_column(String(64), default="")


class SelfRoleOption(Base):
    """Опция меню: одна роль Discord на гильдию."""

    __tablename__ = "self_role_options"
    __table_args__ = (
        UniqueConstraint("guild_id", "role_id", name="uq_self_role_option_role"),
        Index("ix_self_role_options_menu", "menu_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    menu_id: Mapped[int] = mapped_column(Integer)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role_id: Mapped[int] = mapped_column(BigInteger)
    label: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(100), default="")
    emoji: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tag: Mapped[str] = mapped_column(String(16), default="none")
    position: Mapped[int] = mapped_column(Integer, default=0)


class AutomodRequest(Base):
    """Заявка следящих на правку файла автомода."""

    __tablename__ = "automod_requests"
    __table_args__ = (Index("ix_automod_requests_guild_created", "guild_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_id: Mapped[int] = mapped_column(BigInteger)
    rule: Mapped[str] = mapped_column(String(80))
    proposal: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(400))
    status: Mapped[str] = mapped_column(String(16), default="new")
    decided_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StaffAction(Base):
    """Действие следящего: источник отчётов и карточки канала действий."""

    __tablename__ = "staff_actions"
    __table_args__ = (Index("ix_staff_actions_guild_actor_created", "guild_id", "actor_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    actor_rank: Mapped[int] = mapped_column(Integer, default=0)
    action: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModEvidence(Base):
    """Канал доказательств по делу: ЛС, транскрипт, напоминания."""

    __tablename__ = "mod_evidence"
    __table_args__ = (
        UniqueConstraint("case_id", name="uq_mod_evidence_case"),
        Index("ix_mod_evidence_guild_created", "guild_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    case_id: Mapped[int] = mapped_column(Integer)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dm1_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    dm2_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModIntent(Base):
    """Ожидающее подтверждение тяжёлой санкции. Повторное нажатие не исполняет дело дважды."""

    __tablename__ = "mod_intents"
    __table_args__ = (Index("ix_mod_intents_guild_created", "guild_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    target_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChannelLock(Base):
    """Замок канала и медленный режим со сроком снятия."""

    __tablename__ = "channel_locks"
    __table_args__ = (Index("ix_channel_locks_guild_unlock", "guild_id", "unlock_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    slowmode: Mapped[int] = mapped_column(Integer, default=0)
    previous_slowmode: Mapped[int] = mapped_column(Integer, default=0)
    previous_send: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    unlock_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReputationSettings(Base):
    """Пороги и коэффициенты счёта репутации на гильдию."""

    __tablename__ = "reputation_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    start: Mapped[int] = mapped_column(Integer, default=50)
    floor: Mapped[int] = mapped_column(Integer, default=0)
    ceiling: Mapped[int] = mapped_column(Integer, default=100)
    warn_hit: Mapped[int] = mapped_column(Integer, default=5)
    timeout_step_seconds: Mapped[int] = mapped_column(Integer, default=600)
    timeout_per_step: Mapped[int] = mapped_column(Integer, default=2)
    timeout_min: Mapped[int] = mapped_column(Integer, default=2)
    timeout_max: Mapped[int] = mapped_column(Integer, default=20)
    mute_hit: Mapped[int] = mapped_column(Integer, default=10)
    kick_hit: Mapped[int] = mapped_column(Integer, default=0)
    max_hit_per_hour: Mapped[int] = mapped_column(Integer, default=40)
    messages_per_point: Mapped[int] = mapped_column(Integer, default=50)
    voice_seconds_per_point: Mapped[int] = mapped_column(Integer, default=1800)
    growth_interval_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    appeal_contact: Mapped[str] = mapped_column(Text, default="напишите администрации в личные сообщения")


class ReputationTier(Base):
    """Имя и роль одного из шести уровней репутации на гильдию. Диапазоны — константа домена."""

    __tablename__ = "reputation_tiers"
    __table_args__ = (UniqueConstraint("guild_id", "tier", name="uq_reputation_tiers_guild_tier"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    tier: Mapped[int] = mapped_column(Integer)
    lower: Mapped[int] = mapped_column(Integer)
    upper: Mapped[int] = mapped_column(Integer)
    role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str] = mapped_column(String(80))


class Reputation(Base):
    """Текущий счёт участника. Старт 50, пол 0, потолок 100."""

    __tablename__ = "reputation"
    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", name="uq_reputation_user"),
        Index("ix_reputation_guild_user", "guild_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    score: Mapped[int] = mapped_column(Integer, default=50)
    credited_messages: Mapped[int] = mapped_column(Integer, default=0)
    credited_voice_seconds: Mapped[int] = mapped_column(Integer, default=0)
    last_growth_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    banned_for_reputation: Mapped[bool] = mapped_column(Boolean, default=False)


class LadderSettings(Base):
    """Пороги и роли лестницы активности на гильдию."""

    __tablename__ = "ladder_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guest_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    regular_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resident_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    friend_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    regular_messages: Mapped[int] = mapped_column(Integer, default=50)
    regular_voice_seconds: Mapped[int] = mapped_column(Integer, default=0)
    regular_reactions: Mapped[int] = mapped_column(Integer, default=0)
    resident_messages: Mapped[int] = mapped_column(Integer, default=500)
    resident_voice_seconds: Mapped[int] = mapped_column(Integer, default=180000)
    resident_days: Mapped[int] = mapped_column(Integer, default=30)
    resident_strong_messages: Mapped[int] = mapped_column(Integer, default=0)
    resident_strong_voice_seconds: Mapped[int] = mapped_column(Integer, default=0)
    friend_tenure_days: Mapped[int] = mapped_column(Integer, default=365)
    friend_messages: Mapped[int] = mapped_column(Integer, default=1000)
    friend_voice_seconds: Mapped[int] = mapped_column(Integer, default=360000)
    friend_reactions: Mapped[int] = mapped_column(Integer, default=0)


class LadderProgress(Base):
    """Текущая основная ступень участника. Понижение только вручную — бот его не делает."""

    __tablename__ = "ladder"
    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", name="uq_ladder_user"),
        Index("ix_ladder_guild_user", "guild_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    step: Mapped[str] = mapped_column(String(16), default="guest")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GiveawaySettings(Base):
    """Белый список приз-ролей и формула веса билета на гильдию."""

    __tablename__ = "giveaway_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    prize_role_ids: Mapped[str] = mapped_column(Text, default="[]")
    weight_messages: Mapped[int] = mapped_column(Integer, default=50)
    weight_voice_minutes: Mapped[int] = mapped_column(Integer, default=30)
    weight_max: Mapped[int] = mapped_column(Integer, default=10)


class Giveaway(Base):
    """Розыгрыш. Таймер живёт в ends_at, не в памяти процесса."""

    __tablename__ = "giveaways"
    __table_args__ = (
        Index("ix_giveaways_guild_ends", "guild_id", "ends_at"),
        Index("ix_giveaways_guild_channel", "guild_id", "channel_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    prize: Mapped[str] = mapped_column(String(200))
    prize_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    winners_count: Mapped[int] = mapped_column(Integer, default=1)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_shown_count: Mapped[int] = mapped_column(Integer, default=0)
    rules_json: Mapped[str] = mapped_column(Text, default="{}")


class GiveawayEntry(Base):
    """Участник розыгрыша. Повторное нажатие не плодит строку."""

    __tablename__ = "giveaway_entries"
    __table_args__ = (
        UniqueConstraint("giveaway_id", "user_id", name="uq_giveaway_entry"),
        Index("ix_giveaway_entries_giveaway", "giveaway_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    giveaway_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GiveawayWinner(Base):
    """Победитель. Перевыбор пишет новый round, прошлых не трогает."""

    __tablename__ = "giveaway_winners"
    __table_args__ = (
        UniqueConstraint("giveaway_id", "user_id", name="uq_giveaway_winner"),
        Index("ix_giveaway_winners_giveaway", "giveaway_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    giveaway_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    round: Mapped[int] = mapped_column(Integer, default=1)
    place: Mapped[str] = mapped_column(String(16), default="main")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PollSettings(Base):
    """Канал опросов и роль упоминания. Пустая роль — тег polls из меню подписок."""

    __tablename__ = "poll_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ping_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Poll(Base):
    """Опрос. Срок в ends_at; анонимность — kind, не отображение в сервисе."""

    __tablename__ = "polls"
    __table_args__ = (
        Index("ix_polls_guild_ends", "guild_id", "ends_at"),
        Index("ix_polls_guild_channel", "guild_id", "channel_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    question: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16), default="public")
    allow_change: Mapped[bool] = mapped_column(Boolean, default=True)
    options_json: Mapped[str] = mapped_column(Text)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deanonymized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deanonymized_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PollVote(Base):
    """Один голос на человека. Смена обновляет option_index, не плодит строку."""

    __tablename__ = "poll_votes"
    __table_args__ = (
        UniqueConstraint("poll_id", "user_id", name="uq_poll_vote"),
        Index("ix_poll_votes_poll", "poll_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    poll_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    option_index: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class YoutubeFeed(Base):
    """RSS-фид канала YouTube. Первый опрос только запоминает last_video_id."""

    __tablename__ = "youtube_feeds"
    __table_args__ = (UniqueConstraint("guild_id", "yt_channel_id", name="uq_youtube_feed_channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    yt_channel_id: Mapped[str] = mapped_column(String(24))
    yt_channel_title: Mapped[str] = mapped_column(String(200), default="")
    discord_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ping_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    template: Mapped[str] = mapped_column(Text)
    last_video_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LfgSettings(Base):
    """Канал набора и сроки. Без канала карточка не выкладывается."""

    __tablename__ = "lfg_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ttl_minutes: Mapped[int] = mapped_column(Integer, default=120)
    ping_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=15)
    per_user_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=30)


class LfgPost(Base):
    """Набор в #поиск-команды. need — сколько людей кроме автора."""

    __tablename__ = "lfg_posts"
    __table_args__ = (
        Index("ix_lfg_posts_guild_created", "guild_id", "created_at"),
        Index("ix_lfg_posts_guild_author", "guild_id", "author_id", "closed_at"),
        Index("ix_lfg_posts_room", "room_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    author_id: Mapped[int] = mapped_column(BigInteger, index=True)
    game_role_id: Mapped[int] = mapped_column(BigInteger)
    need: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(100), default="")
    room_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    game_pinged: Mapped[bool] = mapped_column(Boolean, default=False)
    lfg_pinged: Mapped[bool] = mapped_column(Boolean, default=False)


class LfgMember(Base):
    """Кто нажал «Иду». Автор в список не входит."""

    __tablename__ = "lfg_members"

    post_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Gathering(Base):
    """Сбор. Места и лист ожидания считаются по RSVP, не в памяти процесса."""

    __tablename__ = "gatherings"
    __table_args__ = (
        Index("ix_gatherings_guild_starts", "guild_id", "starts_at"),
        Index("ix_gatherings_guild_channel", "guild_id", "channel_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    seats: Mapped[int] = mapped_column(Integer, default=20)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_ping_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voice_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GatheringRsvp(Base):
    """Ответ на сбор. Лишние «иду» ждут в waitlisted, пока не освободится место."""

    __tablename__ = "gathering_rsvps"
    __table_args__ = (
        UniqueConstraint("gathering_id", "user_id", name="uq_gathering_rsvp"),
        Index("ix_gathering_rsvps_gathering", "gathering_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gathering_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    waitlisted: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GroupHub(Base):
    """Категория приватных групп. Не хаб войса и не панель тикетов."""

    __tablename__ = "group_hubs"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category_id: Mapped[int] = mapped_column(BigInteger)
    archive_category_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    silence_seconds: Mapped[int] = mapped_column(Integer, default=604800)
    archive_days: Mapped[int] = mapped_column(Integer, default=30)
    min_reputation: Mapped[int] = mapped_column(Integer, default=50)
    daily_limit: Mapped[int] = mapped_column(Integer, default=1)
    open_limit: Mapped[int] = mapped_column(Integer, default=3)


class PrivateGroup(Base):
    """Приватная группа. Владелец в БД; канал не тикет и не временная комната."""

    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint("guild_id", "channel_id", name="uq_groups_channel"),
        Index("ix_groups_guild_owner", "guild_id", "owner_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(String(80))
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    voice_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    archive_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    silence_warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GroupMember(Base):
    """Участник группы. Видит канал, пока строка жива."""

    __tablename__ = "group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_group_member"),
        Index("ix_group_members_group", "group_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)


class GroupArchiveShelf(Base):
    """Дополнительная категория архива групп, когда основная заполнена."""

    __tablename__ = "group_archive_shelves"

    category_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BugReport(Base):
    """Отчёт участника об ошибке бота."""

    __tablename__ = "bug_reports"
    __table_args__ = (Index("ix_bug_reports_guild_user_created", "guild_id", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    did: Mapped[str] = mapped_column(String(400))
    happened: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GroupInvite(Base):
    """Приглашение в группу. Решение приходит из ЛС, не из канала группы."""

    __tablename__ = "group_invites"

    group_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    invited_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(12))
    dm_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Reminder(Base):
    """Личное напоминание. Доставка после рестарта читает due_at."""

    __tablename__ = "reminders"
    __table_args__ = (Index("ix_reminders_guild_user_due", "guild_id", "user_id", "due_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    body: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PulseAlertSettings(Base):
    """Вкл/выкл сигналов пульса и пороги. Vanity и каталоги сюда не входят."""

    __tablename__ = "pulse_alert_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    voice_floor: Mapped[int] = mapped_column(Integer, default=5)
    leave_floor: Mapped[int] = mapped_column(Integer, default=5)
    authors_pct: Mapped[int] = mapped_column(Integer, default=50)
    join_spike_floor: Mapped[int] = mapped_column(Integer, default=15)


class ReputationEvent(Base):
    """Изменение счёта. На дело — одно событие hit и одно revoke по kind."""

    __tablename__ = "reputation_events"
    __table_args__ = (
        Index(
            "uq_reputation_events_case_kind",
            "guild_id",
            "case_id",
            "kind",
            unique=True,
            sqlite_where=text("case_id IS NOT NULL"),
            postgresql_where=text("case_id IS NOT NULL"),
        ),
        Index("ix_reputation_events_guild_user_created", "guild_id", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    case_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    delta: Mapped[int] = mapped_column(Integer, default=0)
    cut: Mapped[int] = mapped_column(Integer, default=0)
    score_after: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AchievementDef(Base):
    """Ступень достижения на гильдию: порог, имя, роль."""

    __tablename__ = "achievement_defs"
    __table_args__ = (UniqueConstraint("guild_id", "key", "stage", name="uq_achievement_defs_guild_key_stage"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    key: Mapped[str] = mapped_column(String(16))
    stage: Mapped[int] = mapped_column(Integer)
    threshold: Mapped[int] = mapped_column(Integer)
    role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str] = mapped_column(String(80))


class AchievementProgress(Base):
    """Счётчик достижения участника."""

    __tablename__ = "achievement_progress"
    __table_args__ = (UniqueConstraint("guild_id", "user_id", "key", name="uq_achievement_progress_user_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    key: Mapped[str] = mapped_column(String(16))
    count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AchievementGrant(Base):
    """Выданная ступень. Нужна, чтобы +5 не повторялось при сверке."""

    __tablename__ = "achievement_grants"
    __table_args__ = (UniqueConstraint("guild_id", "user_id", "key", "stage", name="uq_achievement_grants_user_stage"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    key: Mapped[str] = mapped_column(String(16))
    stage: Mapped[int] = mapped_column(Integer)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AchievementSource(Base):
    """Обработанный тикет. Повтор того же ticket_id ничего не делает."""

    __tablename__ = "achievement_sources"
    __table_args__ = (UniqueConstraint("guild_id", "ticket_id", name="uq_achievement_sources_ticket"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ticket_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(BigInteger)
    key: Mapped[str] = mapped_column(String(16))


class RecruitLink(Base):
    """Бессрочный инвайт, привязанный к человеку. Одна активная на владельца."""

    __tablename__ = "recruit_links"
    __table_args__ = (
        UniqueConstraint("code", name="uq_recruit_links_code"),
        Index("ix_recruit_links_owner", "guild_id", "owner_user_id"),
        Index(
            "uq_recruit_links_active_owner",
            "guild_id",
            "owner_user_id",
            unique=True,
            sqlite_where=text("revoked_at IS NULL AND owner_user_id <> 0"),
            postgresql_where=text("revoked_at IS NULL AND owner_user_id <> 0"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    code: Mapped[str] = mapped_column(String(64))
    owner_user_id: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class RateProfileState(Base):
    """Выученный допуск скрытых лимитов создания на гильдию."""

    __tablename__ = "rate_profiles"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bucket: Mapped[str] = mapped_column(String(16), primary_key=True)
    allowance: Mapped[int] = mapped_column(Integer)
    frozen_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_429_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RenameState(Base):
    """Лимит переименования канала: не чаще 2 раз за 10 минут."""

    __tablename__ = "rename_state"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), default="")
    pending_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    renamed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    previous_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
