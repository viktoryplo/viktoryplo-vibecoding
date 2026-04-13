# Отчёт по лабораторной работе №2

## «Подключение бота к данным»

---

## 1. Описание интеграции

### Какие источники данных выбрали

Для MovieTracker были подключены **два источника данных**:

1. **TMDB API** (The Movie Database) — бесплатный API с базой из миллионов фильмов и сериалов. Используется для поиска, каталога популярных/трендовых фильмов и получения рейтингов.
2. **SQLite** (`tracker.db`) — локальная база данных для хранения личных списков пользователей: watchlist («Буду смотреть»), оценки, отзывы и логи использования.

### Почему именно эти

- **TMDB API** — самый полный бесплатный API для фильмов. Без лимитов на запросы для разработки, поддержка русского языка, не требует оплаты.
- **SQLite** — встроена в Python, не нужен отдельный сервер. Для бота с персональными списками — идеальный выбор.

### Структура данных

**TMDB API — используемые эндпоинты:**

| Эндпоинт | Описание |
|---|---|
| `/search/multi` | Поиск по фильмам и сериалам |
| `/movie/popular` | Популярные фильмы |
| `/trending/all/day` | Тренды дня (фильмы + сериалы) |
| `/movie/top_rated` | Лучшие по рейтингу |
| `/movie/{id}` | Детали фильма (fallback при добавлении) |
| `/tv/{id}` | Детали сериала (fallback при добавлении) |

**SQLite — таблица watchlist:**

| Поле | Тип | Описание |
|---|---|---|
| id | INTEGER PK | ID записи (используется в /rate и /remove) |
| uid | INTEGER | Telegram user ID |
| tmdb_id | INTEGER | ID фильма в TMDB |
| title | TEXT | Название |
| media_type | TEXT | "movie" или "tv" |
| year | TEXT | Год выхода |
| tmdb_rating | REAL | Рейтинг TMDB на момент добавления |
| added_at | TEXT | Дата добавления |

**SQLite — таблица ratings:**

| Поле | Тип | Описание |
|---|---|---|
| id | INTEGER PK | ID записи |
| uid | INTEGER | Telegram user ID |
| tmdb_id | INTEGER | ID фильма в TMDB |
| title | TEXT | Название |
| media_type | TEXT | "movie" или "tv" |
| year | TEXT | Год выхода |
| score | INTEGER | Оценка пользователя (1-10) |
| rated_at | TEXT | Дата оценки |

**SQLite — таблицы feedback и usage_log:**

Хранят отзывы пользователей и логи каждой выполненной команды для статистики.

---

## 2. Промпт для LLM

### Исходный промпт

```
Улучши MovieTracker бота — подключи реальные данные.

Текущий функционал:
- /start с inline-клавиатурой
- /search, /popular, /trending, /top — возвращают захардкоженные списки

Новый функционал:
1. Подключить TMDB API (api.themoviedb.org/3). API-ключ хранить в .env.
   Эндпоинты: /search/multi, /movie/popular, /trending/all/day, /movie/top_rated.
   Язык запросов: ru-RU.

2. При поиске — показывать до 10 результатов с названием, годом, рейтингом и
   кратким описанием. Под результатами — inline-кнопки ➕ для добавления
   в watchlist.

3. SQLite база (tracker.db):
   - watchlist: список «Буду смотреть» (привязка по user_id)
   - ratings: просмотренное с оценкой (1-10)
   - /watchlist — показать список
   - /rate <#id> <score> — оценить и перенести в просмотренное
   - /remove <#id> — удалить из списка

4. /feedback и /stats — сбор отзывов и статистика использования (SQLite).

Требования:
- requests с timeout=10 для API-запросов
- try/except для обработки ошибок API
- contextlib.closing для SQLite-соединений
- Сохранить inline-клавиатуру и CallbackQueryHandler
```

### Итерации

**Итерация 1:** Бот работал, но при нажатии ➕ из старого поиска (после перезапуска) — падал, т.к. кэш `user_data` был пуст. Добавил fallback: если фильма нет в кэше, запрашиваем детали через API `/movie/{id}`.

**Итерация 2:** Описания фильмов обрезались на середине слова. Добавил обрезку до 150 символов с добавлением `…`.

**Итерация 3:** При оценке фильма хотелось видеть визуальное подтверждение. Добавил звёздочки: `⭐⭐⭐⭐` для оценки 8/10.

### Финальный промпт

```
Доработки:
- В callback_handler для add_: если фильма нет в ctx.user_data["cache"],
  делать запрос к /movie/{id} или /tv/{id} как fallback
- Обрезать overview до 150 символов + «…»
- В /rate и /watched — показывать визуальные звёзды (⭐ × min(score, 5))
- В /watchlist — показывать рейтинг TMDB рядом с названием
```

---

## 3. Реализация

### Как работает интеграция

**TMDB API:**
Функция `tmdb(endpoint, **params)` — универсальный запрос к API. Добавляет `api_key` и `language=ru-RU` автоматически. Используется всеми функциями: `search_multi()`, `get_popular()`, `get_trending()`, `get_top_rated()`.

Результаты парсятся через `_parse_results()` — извлекает title, year, rating, overview, media_type. Возвращает до 10 элементов.

**Inline-кнопки ➕:**
При показе результатов поиска/каталога формируются кнопки с `callback_data = f"add_{type}_{tmdb_id}"`. При нажатии — `callback_handler` достаёт данные из кэша `ctx.user_data["cache"]` (или запрашивает из API как fallback) и сохраняет в SQLite.

**SQLite watchlist:**
При вызове `/rate <#id> <score>` фильм удаляется из `watchlist` и добавляется в `ratings` с оценкой пользователя. Это создаёт два отдельных списка: «хочу посмотреть» и «уже посмотрел».

### Ключевые фрагменты кода

**Запрос к TMDB API:**
```python
def tmdb(endpoint: str, **params) -> dict:
    params["api_key"] = os.getenv("TMDB_API_KEY", "")
    params.setdefault("language", "ru-RU")
    resp = requests.get(f"{TMDB_BASE}{endpoint}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()
```

**Добавление в watchlist через inline-кнопку:**
```python
# callback_data: "add_movie_12345"
parts = cb.split("_", 2)
media_type, tmdb_id = parts[1], int(parts[2])

cache = ctx.user_data.get("cache", {})
item = cache.get(tmdb_id)

if not item:  # fallback — запрос к API
    d = tmdb(f"/movie/{tmdb_id}")
    item = {"title": d.get("title"), ...}

ok = wl_add(uid, tmdb_id, item["title"], ...)
```

**Оценка фильма:**
```python
def rate_item(uid, item_id, score):
    row = c.execute("SELECT * FROM watchlist WHERE id=? AND uid=?", ...).fetchone()
    if not row:
        return None
    c.execute("INSERT INTO ratings (...) VALUES (...)", ...)
    c.execute("DELETE FROM watchlist WHERE id=?", (item_id,))
    c.commit()
    return row["title"]
```

### Используемые библиотеки

| Библиотека | Версия | Назначение |
|---|---|---|
| python-telegram-bot | 20.7 | Telegram Bot API (async) |
| python-dotenv | 1.0.1 | Переменные окружения |
| requests | 2.32.3 | HTTP-запросы к TMDB API |
| sqlite3 (stdlib) | — | Хранение watchlist и оценок |

---

## 4. Тестирование

### Примеры запросов и ответов

**Поиск:**
```
Пользователь: /search Интерстеллар

Бот:
🔍 Результаты по «Интерстеллар»:

1. 🎬 Интерстеллар (2014) ⭐ 8.7
    Когда засуха, пыльные бури и вымирание растений приводят…

[➕ 1] [➕ 2] [➕ 3]

Нажмите ➕, чтобы добавить в «Буду смотреть»
```

**Watchlist:**
```
Пользователь: /watchlist

Бот:
📋 Буду смотреть:

#1 🎬 Интерстеллар (2014) ⭐ 8.7
#2 📺 Во все тяжкие (2008) ⭐ 8.9

/rate <#id> <1-10> — оценить
/remove <#id> — убрать из списка
```

**Оценка:**
```
Пользователь: /rate 1 9

Бот:
✅ Интерстеллар — 9/10 ⭐⭐⭐⭐⭐
Перенесено в «Просмотрено».
```

### Скриншоты работы

*(Вставить скриншоты: /search с кнопками ➕, /popular, /watchlist, /rate, /watched)*

### Видео-демо

*(Ссылка на YouTube / Google Drive)*

---

## 5. Трудности и решения

| Проблема | Решение |
|---|---|
| TMDB API возвращает `null` в `overview` для некоторых фильмов | Добавил `(item.get("overview") or "")` вместо `item["overview"]` |
| При нажатии ➕ после перезапуска бота — кэш пуст | Fallback: запрос к `/movie/{id}` или `/tv/{id}` если данных нет в `user_data` |
| Callback data для кнопок ограничена 64 байтами | Используем короткий формат `add_movie_12345` вместо длинных строк |
| Одновременные запросы к SQLite — `database is locked` | Отдельное соединение на каждую операцию + `closing()` |

---

## 6. Выводы

### Что получилось хорошо
- Поиск работает мгновенно, результаты на русском языке
- Inline-кнопки ➕ — добавление в один тап, без ввода команд
- Оценка автоматически переносит фильм из watchlist в watched
- Fallback через API делает бота устойчивым к перезапускам

### Что можно улучшить
- Добавить пагинацию для длинных списков
- Показывать постеры фильмов (ссылки на изображения)
- Рекомендации на основе оценённых фильмов
- Кэширование популярных запросов
