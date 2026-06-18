# CLAUDE.md — audit-bot

## 1. О проекте
Telegram-бот для автоматического аудита сайтов малого бизнеса: пользователь присылает URL → бот проверяет 6 честных критериев → отправляет HTML-отчёт клиенту и уведомляет владельца (Александра) с данными лида.  
Цель: лидогенерация для фриланс-разработчика, а не просто инструмент аудита.  
Деплой: Railway (worker-процесс, PostgreSQL из Railway Postgres).

---

## 2. Структура проекта
```
audit-bot/
├── bot.py              # Точка входа, хэндлеры Telegram
├── auditor.py          # Краулер, PageSpeed, SSL, Claude-анализ
├── reporter.py         # Генерация HTML-отчётов (Jinja2)
├── database.py         # asyncpg: CRUD для users и audits
├── config.py           # Переменные окружения (dotenv)
├── templates/
│   ├── client_report.html   # Отчёт для клиента (чистый, без рекомендаций)
│   └── owner_report.html    # Отчёт для Александра (с рекомендациями, приоритетами)
├── env.example         # Шаблон переменных
├── Procfile            # worker: python bot.py
├── nixpacks.toml       # Railway сборка
├── requirements.txt
└── CLAUDE.md           # Этот файл
```

---

## 3. Tech Stack

| Компонент | Библиотека | Версия |
|---|---|---|
| Язык | Python | 3.11+ |
| Telegram Bot | python-telegram-bot | 20.x |
| AI-анализ | anthropic | latest |
| LLM | claude-sonnet-4-6 | `claude-sonnet-4-6` |
| База данных | PostgreSQL (asyncpg) | — |
| HTTP-краулер | httpx + BeautifulSoup | — |
| PageSpeed | Google PageSpeed Insights v5 | API |
| HTML-отчёты | Jinja2 | — |
| Деплой | Railway | worker-тип |

---

## 4. Архитектура

```
Пользователь (Telegram)
        ↓ URL
[bot.py: handle_message]
        ↓
[auditor.py: run_audit(url)]
  _crawl()          → httpx + BeautifulSoup → HTML-данные
  _pagespeed()      → Google PageSpeed API  → performance/seo/fcp/lcp
  _check_security() → ssl + socket          → HTTPS, SSL-валидность
  _claude_analysis()→ Anthropic API         → оценки 1-10, проблемы, рекомендации
        ↓ result{}
[reporter.py]
  generate_client_report() → client_report.html → отправить пользователю
  generate_owner_report()  → owner_report.html  → отправить Александру
        ↓
[database.py]
  complete_audit() → PostgreSQL → сохранить результат, среднее, статус
```

---

## 5. Ключевые решения

| Решение | Почему |
|---|---|
| python-telegram-bot вместо aiogram | Проще для одного модуля без FSM-машины состояний |
| PostgreSQL вместо SQLite | Railway даёт Postgres из коробки; нужен для будущих /leads, follow-up |
| 6 критериев вместо 10 | 4 критерия (формы/навигация/адаптив/контент без JS) — имитация на BeautifulSoup |
| Два формата отчёта | Клиент не должен видеть «цену работ»; Александр видит рекомендации и температуру лида |
| Claude читает crawled-текст | PageSpeed не оценивает оффер и CTA — только AI может прочитать и оценить контент |

---

## 6. Тестирование

**Где:** локально через `python bot.py` с тестовым ботом (@BotFather), тестовая БД (`.env` с локальным Postgres или Railway dev-окружение).

**Перед каждым деплоем проверить:**
- [ ] `/start` → бот отвечает со списком критериев
- [ ] Отправить `https://example.com` → прогресс-сообщения меняются, отчёт приходит
- [ ] Отправить не-URL (`привет`) → бот просит ссылку
- [ ] Повторный аудит того же URL → cooldown 7 дней (или разрешить)
- [ ] Сайт недоступен → бот сообщает об ошибке, не зависает
- [ ] PageSpeed не отвечает 70 сек → graceful degradation, аудит идёт без него
- [ ] Владельцу приходит уведомление + owner_report.html
- [ ] `/delete` → данные пользователя удалены из БД
- [ ] Горячий лид (avg < 5) → помечен в уведомлении владельцу

---

## 7. Документация

| Файл | Содержит | Когда обновлять |
|---|---|---|
| `CLAUDE.md` | Ориентир по проекту для AI | При изменении архитектуры или стека |
| `env.example` | Шаблон переменных | При добавлении новой env-переменной |
| `critique_and_spec_requirements.md` | 14 требований к v1 и v2 | Не трогать — архивный документ решений |
| `research.md` | Анализ конкурентов | Не трогать — архивный документ |
| `templates/*.html` | HTML-шаблоны отчётов | При изменении структуры отчёта |

---

## 8. Commands

```bash
# Установка зависимостей
pip install -r requirements.txt

# Запуск локально
python bot.py

# Деплой на Railway
# Через git push — Railway подхватывает автоматически
git push origin master

# Просмотр логов Railway
railway logs

# Переменные окружения
railway variables set BOT_TOKEN=... ANTHROPIC_API_KEY=...
```

---

## 9. Самообновление

| Файл/таблица | Кто обновляет | Когда |
|---|---|---|
| `users` (PostgreSQL) | `database.ensure_user()` | При каждом сообщении от пользователя |
| `audits` (PostgreSQL) | `database.create/complete/fail_audit()` | При запуске и завершении проверки |
| `/tmp/audit_reports/*.html` | `reporter.generate_*()` | После каждого аудита (временные файлы) |

**Механизм:** `bot.py` → `handle_message()` → вызывает `auditor.run_audit()` → результат пишет в БД через `database.complete_audit()` → `reporter` генерирует HTML во временную папку → бот отправляет файл и удаляет. PostgreSQL — единственный источник правды.

**В новой сессии Claude:** прочитай `CLAUDE.md` (этот файл) → `auditor.py` (логика аудита) → `database.py` (схема БД). Этого достаточно чтобы понять весь проект.
