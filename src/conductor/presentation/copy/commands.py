"""Тексты: commands."""

from __future__ import annotations

CMD_DIAGNOSE = "Самопроверка: настройка сервера и состояние сейчас"

CMD_HELP = "Справка по командам и памятка участника"

CMD_HELP_RANK = "Карточка этого ранга следящих"

CMD_ADMIN_PANEL = "Панель настройки сервера"

CMD_STATS = "Сейчас, сводка, участники, каналы и связи"

CMD_STATS_USER = "Досье этого человека, даже если уже вышел"

CMD_PROFILE = "Репутация и активность"

CMD_PROFILE_USER = "Чужая карточка — следящим"

CMD_LOYALTY = "Реестр Old и выдача ролей"

CMD_PULSE_VOICE_FLOOR = "Медиана голоса в час пик, от которой пустота — сигнал"

CMD_PULSE_LEAVE_FLOOR = "Минимум выходов за сутки для инверсии"

CMD_PULSE_AUTHORS_PCT = "Процент медианы авторов за 7 дней"

CMD_PULSE_JOIN_SPIKE = "Входов за 10 минут для сигнала о всплеске"

CMD_DOSSIER = "Досье"

CMD_WATCHERS_LIST = "Состав следящих; с участником — карточка"

CMD_WATCHERS_INFO = "Карточка следящего: метрики, лента, приостановка"

CMD_WATCHERS_APPOINT = "Выдать ранговую роль следящего"

CMD_WATCHERS_DISMISS = "Снять ранговую роль следящего"

CMD_WATCHERS_DUTY = "Отметить смену"

CMD_WATCHERS_NOTE = "Служебная заметка о следящем"


CMD_VOICE = "Перевыложить панель владельца комнаты"


CMD_MOD_WARN = "Замечание участнику"

CMD_MOD_TIMEOUT = "Тайм-аут участнику"

CMD_MOD_MUTE = "Выдать роль мута"

CMD_MOD_UNMUTE = "Снять роль мута"

CMD_MOD_HISTORY = "История дел участника"

CMD_MOD_KICK = "Исключить участника с сервера"

CMD_MOD_BAN = "Забанить участника"

CMD_MOD_UNBAN = "Снять бан"

CMD_MOD_PURGE = "Очистить сообщения канала"

CMD_MOD_LOCK = "Замок канала; 0 или снять — снять замок"

CMD_MOD_PARDON = "Помиловать: вернуть счёт к старту и снять автобан"

CMD_MOD_CHANNEL = "Канал"

CMD_MOD_AFTER = "Начиная после этого сообщения"

CMD_MOD_SLOWMODE = "Медленный режим, секунд (0–21600)"

CMD_MOD_USER = "Участник"

CMD_MOD_REASON = "Причина, не короче 8 символов"

CMD_MOD_DURATION = "Срок: 30м, 2ч, 1д или минуты числом"


CMD_REP_MESSAGES = "Сообщений на +1"

CMD_REP_VOICE = "Минут голосового на +1"


CMD_ACTIVITY_REG_MSG = "Сообщений на Постояльца"

CMD_ACTIVITY_REG_VOICE = "Минут голосового на Постояльца"

CMD_ACTIVITY_REG_REACT = "Реакций на Постояльца"

CMD_ACTIVITY_RES_MSG = "Сообщений на Жителя"

CMD_ACTIVITY_RES_VOICE = "Часов голосового на Жителя"

CMD_ACTIVITY_RES_DAYS = "Дней на сервере для Жителя"

CMD_ACTIVITY_RES_STRONG_MSG = "Сообщений, если только они"

CMD_ACTIVITY_RES_STRONG_VOICE = "Часов голосового, если только они"

CMD_ACTIVITY_FRIEND_DAYS = "Дней на сервере для Друга"

CMD_ACTIVITY_FRIEND_MSG = "Сообщений для Друга"

CMD_ACTIVITY_FRIEND_VOICE = "Часов голосового для Друга"

CMD_ACTIVITY_FRIEND_REACT = "Реакций для Друга"


CMD_GIVEAWAY = "Запустить розыгрыш в этом канале"

CMD_GIVEAWAY_PRIZE_ROLE = "Роль-приз из белого списка"

CMD_GIVEAWAY_REQUIRE_ROLE = "Нужная роль для участия"

CMD_GIVEAWAY_WEIGHT = "Вес билета за активность"

CMD_GIVEAWAY_WEIGHT_MSG = "Сообщений на один дополнительный билет"

CMD_GIVEAWAY_WEIGHT_VOICE = "Минут войса на один дополнительный билет"

CMD_GIVEAWAY_WEIGHT_MAX = "Потолок билетов на человека"

CMD_GIVEAWAY_EXTRA_ROLE = "Утешительная роль из белого списка"

CMD_RECRUIT_LINK = "Выдать человеку бессрочную рекрут-ссылку"

CMD_RECRUIT_REPORT = "Список ссылок или карточка одной"

CMD_RECRUIT_USER = "Кому принадлежит ссылка"

CMD_RECRUIT_CHANNEL = "Канал инвайта, если не канал по умолчанию"

CMD_RECRUIT_REVOKE = "Отозвать активную ссылку"

CMD_RECRUIT_CODE = "Код ссылки"

CMD_RECRUIT_OWNER = "Владелец ссылки"

CMD_POLL_START = "Запустить опрос в этом канале"

CMD_POLL_REVEAL = "Раскрыть голоса анонимного опроса"

CMD_POLL_KIND = "Тип опроса"

CMD_POLL_CHANGE = "Можно ли сменить голос до срока"

CMD_EVENT = "Объявить сбор в этом канале"

CMD_EVENT_SEATS = "Мест: 0 — без ограничения, до 10 000"

CMD_GROUP = "Создать приватную группу"

CMD_GROUP_NAME = "Название"

CMD_GROUP_MEMBERS = "Участники: @a @b …"

CMD_GROUP_VOICE = "Парный голосовой канал"

CMD_GROUPS = "Мои группы и приглашения"

CMD_BUG = "Сообщить об ошибке на этом сервере"

CMD_REMIND = "Напоминание: с текстом создать, без — список"

CMD_REMIND_WHEN = "Когда: 10m или 2026-09-05 18:00"

CMD_REMIND_TEXT = "О чём напомнить"


CMD_OWNER_WIPE = "Очистить базу этого сервера: всё или секцию"

CMD_OWNER_STRIP = "Снять роли: выбранные или все, с исключениями и фильтрами"

CMD_OWNER_GRANT = "Выдать роли, с исключениями и умными фильтрами"

CMD_OWNER_OLD_IMPORT = "Записать держателей роли в реестр Old"

CMD_OWNER_SEED = "Каркас сервера: создать недостающее и подтянуть"

CMD_OWNER_SEED_OLD = "Включить в список роли Old"

CMD_OWNER_SEED_MODE = "Создать недостающее или только подтянуть"

CMD_OWNER_SEED_MODE_BUILD = "создать недостающее"

CMD_OWNER_SEED_MODE_ADOPT = "только подтянуть"

CMD_OWNER_EMOJIS = "Загрузить значки движения приложения"

CMD_OWNER_LIMITS = "Показать и снять блокировки создания каналов"

CMD_OWNER_EXPORT = "Выгрузить настройку сервера в JSON"

CMD_OWNER_EXPORT_SECTION = "Какой раздел выгрузить"

CMD_OWNER_EXPORT_DOCS = "Включить пояснения в файл"

CMD_OWNER_APPLY = "Проверить или применить JSON-файл настройки"

CMD_OWNER_APPLY_FILE = "JSON-файл настройки"

CMD_OWNER_APPLY_MODE = "Проверить или сразу применить"

CMD_OWNER_LIFT = "Снять ограничения новичка для тестов"

CMD_OWNER_LIFT_USER = "Участник"

CMD_OWNER_REP = "Поставить указанную репутацию участнику"

CMD_OWNER_REP_USER = "Участник"

CMD_OWNER_REP_SCORE = "Какой счёт поставить"

CMD_OWNER_SECTION = "Что очистить"

CMD_OWNER_ROLE = "Роль"

CMD_OWNER_ROLE2 = "Ещё роль"

CMD_OWNER_ROLE3 = "Третья роль"

CMD_OWNER_ALL_ROLES = "Снять все роли, которые сейчас есть у людей"

CMD_OWNER_HAVE = "Только у кого есть эта роль"

CMD_OWNER_HAVE2 = "Ещё обязательная роль"

CMD_OWNER_HAVE3 = "Третья обязательная роль"

CMD_OWNER_HAVE_MODE = "Обязательные роли: любая или все сразу"

CMD_OWNER_WITHOUT = "Не трогать, если есть эта роль"

CMD_OWNER_WITHOUT2 = "Ещё роль-исключение"

CMD_OWNER_WITHOUT3 = "Третье исключение"

CMD_OWNER_WITHOUT_MODE = "Исключения: любая или все сразу"

CMD_OWNER_PROTECT = "Автоисключение: следящие и Old из настроек"

CMD_OWNER_SKIP_TIMEOUT = "Пропустить тех, кто в тайм-ауте"

CMD_OWNER_SKIP_PENDING = "Пропустить заявки на вступление"

CMD_OWNER_SKIP_BOOST = "Пропустить бустеров"

CMD_OWNER_TIER = "Ступень Old. Пусто — если роль уже привязана"


CMD_ADMIN_IMPORT_FILE = "YAML с ranks и actions; если пусто — example"

CMD_WATCHERS_APPOINT_USER = "Кого назначить"

CMD_WATCHERS_APPOINT_RANK = "Ранг из модели прав"

CMD_WATCHERS_DUTY_STATE = "on или off"

CMD_WATCHERS_DUTY_USER = "Чужая смена — для главного и выше"
