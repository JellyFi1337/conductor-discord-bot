"""Тексты: voice."""

from __future__ import annotations

from .common import hold_action, hold_text

VOICE_TITLE = "Голосовые комнаты"

VOICE_HUB_IS_ROOM = "Нельзя назначить хабом временную комнату."

VOICE_HUB_IS_DOCK = "Док нельзя назначить хабом."

VOICE_NO_ROOM = "Сейчас нет временной комнаты, которой можно управлять."

VOICE_NO_HUB = "Сначала назначьте хаб в /admin panel → Голос."

VOICE_CREATE_TITLE = "Открываю комнату"

VOICE_STAGE_RIGHTS = "Проверяю права"

VOICE_STAGE_SAVE = "Сохраняю комнату"

VOICE_STAGE_MOVE = "Пересаживаю"

VOICE_STAGE_PANEL = "Пишу панель"

VOICE_FIELD_OWNER = "Владелец"

VOICE_FIELD_INSIDE = "В комнате"

VOICE_FIELD_LOCK = "Замок"

VOICE_FIELD_CODE = "Код"

VOICE_FIELD_GHOST = "Видимость"

VOICE_GHOST_ON = "скрыта"

VOICE_GHOST_OFF = "видна"

VOICE_LOCK_ON = "🔒 закрыт"

VOICE_LOCK_OFF = "открыт"

VOICE_NAME = "Имя"

VOICE_LIMIT = "Лимит"

VOICE_LOCK = "Запереть"

VOICE_UNLOCK = "Открыть"

VOICE_GHOST = "Скрыть"

VOICE_UNGHOST = "Показать"

VOICE_PERMIT = "Впустить"

VOICE_REJECT = "Отказать"

VOICE_ASK = "Хочу войти"

VOICE_CODE = "Код"

VOICE_INVITE = "Пригласить"

VOICE_TRANSFER = "Передать"

VOICE_KICK = "Выгнать"

VOICE_BAN = "Закрыть вход"

VOICE_CLAIM = "Забрать"

VOICE_TAKE = "Стать владельцем"

VOICE_ACCEPT = "Принять"

VOICE_DECLINE = "Отклонить"

VOICE_REQUESTS_TITLE = "Заявки"

VOICE_REQUESTS_EMPTY = "Заявок нет."

VOICE_PANEL_FOOTNOTE = "кнопки действуют у владельца; следящие не забирают комнату молча"

VOICE_CODE_FOOTNOTE = "код одноразовый, сгорит {when}"

VOICE_LFG_FOOTNOTE = "набор сгорит {when}"

VOICE_PANEL_RESTORED = "Панель комнаты восстановлена."

VOICE_NAME_MODAL = "Имя комнаты"

VOICE_LIMIT_MODAL = "Лимит комнаты"

VOICE_LIMIT_LABEL = "Сколько человек (0 — без лимита)"

VOICE_NAME_INVITE = "В имени нельзя оставлять приглашения и ссылки."

VOICE_NAME_AD = "В имени нельзя оставлять рекламу."

VOICE_NAME_EMPTY = "Имя не может быть пустым."

VOICE_LIMIT_BAD = "Лимит — число от 0 до 99."

VOICE_CLAIM_TOO_SOON = "Владелец ещё не отсутствовал две минуты."

VOICE_CLAIM_HERE = "Владелец сейчас в комнате."

VOICE_CLAIM_OWNER = "Эта комната уже ваша."

VOICE_CLAIM_HAS_ROOM = "У вас уже есть своя комната."

VOICE_TRANSFER_MISSING = "Передать можно человеку из этой комнаты."

VOICE_CLAIM_OK = "Комната теперь ваша."

VOICE_TAKE_OK = "Вы стали владельцем комнаты."

VOICE_KICK_SELF = "Нельзя выгнать владельца."

VOICE_KICK_MISSING = "Этого человека нет в комнате."

VOICE_KICK_OK = "Человек отключён от комнаты. Зайти снова он может; чтобы закрыть вход — «Закрыть вход»."

VOICE_KICK_STAFF = "Нельзя выгнать персонал: следящие проверяют комнаты."

VOICE_KICK_BOT = "Нельзя выгнать бота."

VOICE_BAN_OK = "Вход закрыт. Человек отключён, если был в комнате."

VOICE_BAN_ADMIN = (
    "У человека право «Администратор»: Discord пропускает запрет. Бот будет отключать его при каждом входе."
)

VOICE_TRANSFER_OK = "Владение передано."

VOICE_LOCKED = "Комната заперта."

VOICE_OPENED = "Комната открыта."

VOICE_GHOSTED = "Комната скрыта от всех."

VOICE_VISIBLE = "Комната снова видна."

VOICE_PERMITTED = "Человеку открыт вход."

VOICE_REJECTED = "Вход этому человеку закрыт."

VOICE_ASK_OK = "Заявка отправлена владельцу."

VOICE_ASK_OPEN = "Комната открыта — просто зайдите."

VOICE_ASK_PENDING = "Заявка уже ждёт разбора."

VOICE_ASK_KICKED = "Вход в эту комнату вам закрыт."

VOICE_ASK_OWNER = "Это ваша комната."

VOICE_ASK_INSIDE = "Вы уже в комнате."

VOICE_JOIN_OK = "Код принят. Заходите в комнату."

VOICE_JOIN_DEAD = "Код уже не действует."

VOICE_JOIN_MISSING = "Такого кода нет."

VOICE_JOIN_KICKED = "Вас выгнали из этой комнаты."

VOICE_CODE_ISSUED = "Новый код комнаты"

VOICE_INVITE_OK = "Одноразовое приглашение"

VOICE_RENAMED = "Имя комнаты обновлено."


VOICE_LIMIT_SET = "Лимит комнаты обновлён."

VOICE_STAFF_LOG = "Голосовые комнаты"

VOICE_LOG_FOOTNOTE = "переопределение персоналом"

VOICE_BITRATE = "Битрейт"

VOICE_STATUS = "Статус"

VOICE_LFG = "Ищем"

VOICE_LFG_JOIN = "Записаться"

VOICE_MERGE = "Слить с комнатой"

VOICE_MERGE_ACCEPT = "Принять слияние"

VOICE_FIELD_LFG = "Набор"

VOICE_BITRATE_MODAL = "Битрейт комнаты"

VOICE_STATUS_MODAL = "Статус комнаты"

VOICE_LFG_MODAL = "Ищем людей"

VOICE_BITRATE_LABEL = "Кбит/с (8–384)"

VOICE_STATUS_LABEL = "Статус канала"

VOICE_LFG_LABEL = "Сколько человек ищем (1–99)"

VOICE_BITRATE_BAD = "Битрейт — число от 8 до 384."

VOICE_STATUS_BAD = "В статусе нельзя оставлять ссылки и рекламу."

VOICE_BITRATE_OK = "Битрейт комнаты обновлён."

VOICE_STATUS_OK = "Статус комнаты обновлён."

VOICE_STATUS_CLEARED = "Статус комнаты снят."

VOICE_LFG_STARTED = "Набор открыт. Кто хочет — жмёт «Записаться»."

VOICE_LFG_STOPPED = "Набор закрыт."

VOICE_LFG_OK = "Вы в списке желающих."

VOICE_LFG_IDLE = "Сейчас никто не ищет людей."

VOICE_LFG_FULL = "Набор уже набран."

VOICE_LFG_INSIDE = "Вы уже в списке."

VOICE_LFG_NEED = "Сколько человек ищем — число от 1 до 99."

VOICE_MERGE_OK = "Запрос на слияние отправлен. Нужно согласие владельца."

VOICE_MERGE_SELF = "Нельзя слить комнату саму с собой."

VOICE_MERGE_MISSING = "Слить можно только с другой временной комнатой."

VOICE_MERGE_PENDING = "Запрос на слияние уже ждёт ответа."

VOICE_MERGE_DONE = "Люди перенесены. Комнаты слиты."

VOICE_MERGE_HUB = "Нельзя копировать сам хаб — укажите шаблонный канал."

VOICE_PRESET_NAME = "Новые комнаты называются по шаблону имени."

VOICE_PRESET_CLONE = "Новые комнаты копируют битрейт и права шаблона."

VOICE_DENIAL_NO_HUB = "Хаб не назначен. Задайте его в /admin → Голос."

VOICE_DENIAL_HUB_GONE = "Канал хаба пропал из Discord."

VOICE_DENIAL_NO_CATEGORY = "У хаба нет категории — комнаты некуда создавать."

VOICE_DENIAL_BOT_PERMS = (
    "Не хватает прав на хаб: боту — каналы, перемещение и вход; "
    "участникам — видеть и подключаться."
)

VOICE_DENIAL_HELD = "Сейчас нельзя создать комнату. Подождите и зайдите в хаб снова."

VOICE_DENIAL_RATE = "Слишком часто. Подождите пару секунд и зайдите в хаб снова."

VOICE_DENIAL_LIMIT = "Нельзя создать комнату: категория или сервер заполнены."

VOICE_DENIAL_FAILED = "Не удалось открыть комнату."

VOICE_DENIAL_LABELS = {
    "no_hub": VOICE_DENIAL_NO_HUB,
    "hub_gone": VOICE_DENIAL_HUB_GONE,
    "no_category": VOICE_DENIAL_NO_CATEGORY,
    "bot_perms": VOICE_DENIAL_BOT_PERMS,
    "held": VOICE_DENIAL_HELD,
    "rate": VOICE_DENIAL_RATE,
    "limit": VOICE_DENIAL_LIMIT,
    "failed": VOICE_DENIAL_FAILED,
}

VOICE_HUB_CATEGORY_HEALED = "Категория хаба обновлена по реальному каналу."

VOICE_HUB_FIXED = "Хаб починен."

VOICE_HUB_HEALTH_OK = "хаб готов"


def voice_denial_text(code: str) -> str:
    return VOICE_DENIAL_LABELS.get(code, VOICE_DENIAL_FAILED)


def voice_hold_text(source: str, seconds: float, *, route: str = "") -> str:
    return hold_text(source, seconds, action=hold_action("channel"), route=route)


def voice_hub_set(channel: str) -> str:
    return f"Хаб задан: {channel}. Вход в этот канал создаёт временную комнату."


def voice_room_title(name: str) -> str:
    return f"Комната «{name}»"


def voice_inside(humans: int, limit: int) -> str:
    if limit <= 0:
        return f"{humans}"
    return f"{humans}/{limit}"


def voice_lock_state(locked: bool) -> str:
    return VOICE_LOCK_ON if locked else VOICE_LOCK_OFF


def voice_ghost_state(ghosted: bool) -> str:
    return VOICE_GHOST_ON if ghosted else VOICE_GHOST_OFF


def voice_code_value(code: str | None) -> str:
    return code if code else "—"


def voice_requests_label(count: int) -> str:
    return f"{VOICE_REQUESTS_TITLE} {count}" if count else VOICE_REQUESTS_TITLE


def voice_code_footnote(when: str) -> str:
    return VOICE_CODE_FOOTNOTE.format(when=when)


def voice_lfg_footnote(when: str) -> str:
    return VOICE_LFG_FOOTNOTE.format(when=when)


def voice_lfg_state(need: int, signed: int) -> str:
    return f"{signed}/{need}"


def voice_lfg_label(need: int | None) -> str:
    return f"{VOICE_LFG} +{need}" if need else VOICE_LFG


def voice_preset_name(template: str) -> str:
    return f"{VOICE_PRESET_NAME}\n`{template}`"


def voice_preset_clone(channel: str, template: str) -> str:
    return f"{VOICE_PRESET_CLONE}\n{channel}\n`{template}`"


def voice_staff_take(*, who: str, actor: str, room: str) -> str:
    return f"**Комната:** {room}\n**Было:** {who}\n**Стало:** {actor}\n**Основание:** ключ voice.staff_override"
