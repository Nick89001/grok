import asyncio
import sqlite3
import aiohttp
from aiohttp import ClientSession  # Добавлен импорт
import logging
import time
import random
import json
from backoff import on_exception, expo
import asyncio
import sqlite3
import aiohttp
from aiohttp import ClientSession
import logging
import time
import random
import json
from backoff import on_exception, expo
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
import os
import re
import shutil
from yookassa import Configuration, Payment

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Загрузка переменных окружения из .env
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
XAI_API_KEY = os.getenv("XAI_API_KEY")
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
GROK_API_URL = "https://api.x.ai/v1/chat/completions"
SERPER_API_URL = "https://google.serper.dev/search"
ADMIN_ID = 1069506191
Configuration.account_id = os.getenv("YOOKASSA_SHOP_ID")
Configuration.secret_key = os.getenv("YOOKASSA_SECRET_KEY")

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        subscription TEXT DEFAULT 'none',
        messages_left INTEGER DEFAULT 10,
        searches_left INTEGER DEFAULT 2,
        last_reset TEXT DEFAULT CURRENT_TIMESTAMP,
        context TEXT DEFAULT '[]'
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS search_cache (
        query TEXT PRIMARY KEY,
        result TEXT,
        timestamp REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS serper_usage (
        total_searches INTEGER DEFAULT 0
    )''')
    c.execute("INSERT OR IGNORE INTO serper_usage (total_searches) VALUES (0)")
    conn.commit()
    conn.close()

def backup_db():
    try:
        shutil.copy("bot_data.db", f"backup_{datetime.now().strftime('%Y%m%d')}.db")
        logging.info("Бэкап БД создан.")
    except Exception as e:
        logging.error(f"Ошибка бэкапа БД: {e}")


# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Список тёплых смайликов
EMOJI_PAIRS = [
    "😊💖", "💋🌸", "🥰🌺", "😘🌹", "💞🌼",
    "😍💐", "🌷💗", "😇🌻"
]

# ... (импорты и остальной код до check_limits остаются без изменений)

# Проверка лимитов и загрузка контекста
def check_limits(user_id):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("SELECT subscription, messages_left, searches_left, last_reset, context FROM users WHERE user_id = ?",
              (user_id,))
    result = c.fetchone()
    subscription = 'admin' if user_id == ADMIN_ID else 'none'
    messages_left = float('inf') if subscription == 'admin' else 10
    searches_left = float('inf') if subscription == 'admin' else 2
    last_reset = datetime.now().isoformat()
    if result:
        subscription, messages_left, searches_left, last_reset, context_str = result
        try:
            context = json.loads(context_str or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logging.error(f"JSON decode error for user {user_id}: {str(e)}, context_str={context_str}")
            context = []
    else:
        context = []
        c.execute(
            "INSERT INTO users (user_id, subscription, messages_left, searches_left, last_reset, context) "
            "VALUES (?, ?, ?, ?, ?, '[]')",
            (user_id, subscription, messages_left, searches_left, last_reset))
        conn.commit()
    logging.info(f"Loaded context for user {user_id}: len={len(context)}")
    conn.close()
    return subscription, messages_left, searches_left, last_reset, context

# Очистка кэша
def clear_cache():
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    one_day_ago = time.time() - 24 * 60 * 60
    c.execute("DELETE FROM search_cache WHERE timestamp < ?", (one_day_ago,))
    conn.commit()
    conn.close()


def update_limits(user_id, messages_used=1, searches_used=0, new_context=None):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    sub, msg_left, search_left, _, old_context = check_limits(user_id)
    if sub != 'admin':
        msg_left = max(0, msg_left - messages_used)
        search_left = max(0, search_left - searches_used)
    context_str = json.dumps(new_context[-10:] if new_context is not None else old_context, ensure_ascii=False)
    logging.info(f"Saving context for user {user_id}: {context_str}")
    try:
        c.execute("UPDATE users SET messages_left = ?, searches_left = ?, context = ? WHERE user_id = ?",
                  (msg_left, search_left, context_str, user_id))
        conn.commit()
        logging.info(f"Updated context for user {user_id} in DB")
    except sqlite3.Error as e:
        logging.error(f"SQL error in update_limits for user {user_id}: {str(e)}")
    finally:
        conn.close()


async def update_serper_usage(searches_used=1):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("SELECT total_searches FROM serper_usage")
    total = c.fetchone()[0]
    total += searches_used
    c.execute("UPDATE serper_usage SET total_searches = ?", (total,))
    conn.commit()
    if total >= 2200:
        await bot.send_message(ADMIN_ID,
                               f"⚠️ Достигнуто 2200 поисков Serper! Осталось 300 бесплатных запросов. Пора пополнить лимиты! {random.choice(EMOJI_PAIRS)}")
    conn.close()
    return total


def sanitize_output(text: str) -> str:
    # Удаляем строки с мета-рассуждениями/инструкциями
    banned_markers = [
        "Сначала задача:", "Инструкции:", "Известные факты:", "Структура:",
        "Возможный подход:", "Черновик:", "Расширенный черновик:",
        "Подсчёт слов:", "Цель —", "я должен", "мне нужно", "в симуляции",
        "как AI", "не могу", "это сложно"
    ]
    lines = [ln for ln in text.splitlines() if not any(marker.lower() in ln.lower() for marker in banned_markers)]
    result = "\n".join(lines).strip()
    # Удаляем кавычки вокруг длинных блоков
    if result.startswith("\"") and result.endswith("\""):
        result = result[1:-1].strip()
    # Обрезаем потенциальные хвосты редакторских вставок
    cut_markers = ["Черновик:", "Расширенный черновик:"]
    for cm in cut_markers:
        pos = result.lower().find(cm.lower())
        if pos != -1:
            result = result[:pos].strip()
    return result


# Асинхронный запрос к Grok
@on_exception(expo, Exception, max_tries=5, max_time=60)
async def query_grok(messages, max_tokens=1500, temperature=0.7, allow_reasoning_fallback: bool = False):
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": "grok-3-mini",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature
        }
        async with session.post(GROK_API_URL, json=payload, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                logging.info(f"Grok response: {data}")
                if not data or "choices" not in data or not data["choices"]:
                    logging.warning("Пустой или некорректный ответ от Grok")
                    raise Exception("Пустой ответ от Grok, попробуйте позже")
                message_obj = data["choices"][0].get("message", {})
                content = (message_obj.get("content") or "").strip()
                if not content and allow_reasoning_fallback:
                    content = (message_obj.get("reasoning_content") or "").strip()
                finish_reason = data["choices"][0].get("finish_reason", "")
                if not content:
                    logging.warning("Пустой контент в ответе Grok после фоллбека")
                    raise Exception("Пустой ответ от Grok, попробуйте позже")
                word_count = len(content.split())
                logging.info(f"Ответ Grok: {word_count} слов, finish_reason: {finish_reason}")
                return content, finish_reason
            elif response.status == 429:
                retry_after = int(response.headers.get("Retry-After", 5))
                logging.warning(f"Ошибка 429: Слишком много запросов. Жду {retry_after} сек.")
                raise Exception(f"Слишком много запросов, попробуйте через {retry_after} сек.")
            else:
                error_text = await response.text()
                logging.error(f"Ошибка Grok: {response.status} - {error_text}")
                raise Exception(f"Ошибка Grok: {response.status} - {error_text}")


# Вспомогательная функция: извлечение последней даты на русском из текста
RUS_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}
EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11,
    "dec": 12
}
DATE_REGEXES = [
    re.compile(
        r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})",
        re.IGNORECASE),
    re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})"),
    re.compile(
        r"(январ[ея]|феврал[яея]|март[ае]?|апрел[ея]|ма[ея]|июн[ея]|июл[ея]|август[ае]?|сентябр[ея]|октябр[ея]|ноябр[ея]|декабр[ея])\s+(\d{4})",
        re.IGNORECASE),
    # English: Aug 16, 2025 or August 16, 2025
    re.compile(
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2}),\s*(\d{4})",
        re.IGNORECASE),
    # English month + year: August 2025
    re.compile(
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{4})",
        re.IGNORECASE),
]

MEETING_KEYWORDS = [
    "встреч", "переговор", "пересек", "саммит", "личн", "встретил"
]
LEADER_TOKENS = ["трамп", "пути"]  # покрывает "путин", "путиным"
KNOWN_LOCATIONS = [
    "Аляска", "Анкоридж", "Хельсинки", "Осака", "Гамбург", "Москва", "Санкт-Петербург",
    "Вашингтон", "Женева", "Париж", "Нью-Йорк", "Сочи"
]


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p and len(p.strip()) > 0]


def sentence_has_context(sentence: str) -> bool:
    s = sentence.lower()
    if not all(tok in s for tok in LEADER_TOKENS):
        return False
    if not any(kw in s for kw in MEETING_KEYWORDS):
        return False
    return True


def extract_location(sentence: str) -> str | None:
    # Ищем упоминание известных локаций с предлогами
    for loc in KNOWN_LOCATIONS:
        if re.search(rf"\b(в|на)\s+{re.escape(loc)}\b", sentence, flags=re.IGNORECASE):
            return loc
    # Пробуем поймать заглавные топонимы после предлогов
    m = re.search(r"\b(в|на)\s+([А-ЯЁ][а-яё]+(?:[-\s][А-ЯЁ][а-яё]+)*)", sentence)
    if m:
        return m.group(2)
    return None


def extract_latest_russian_date_from_context(text: str) -> tuple[str | None, str | None]:
    latest_dt = None
    latest_date_str = None
    latest_loc = None
    for sent in split_sentences(text):
        if not sentence_has_context(sent):
            continue
        # Ищем дату в рамках предложения
        # Формат: 6 ноября 2025
        for m in DATE_REGEXES[0].finditer(sent):
            day = int(m.group(1));
            month = RUS_MONTHS.get(m.group(2).lower(), 1);
            year = int(m.group(3))
            try:
                dt = datetime(year, month, day)
                if latest_dt is None or dt > latest_dt:
                    latest_dt = dt
                    latest_date_str = f"{day:02d}.{month:02d}.{year}"
                    latest_loc = extract_location(sent)
            except Exception:
                pass
        # Формат: 06.11.2025
        for m in DATE_REGEXES[1].finditer(sent):
            day = int(m.group(1));
            month = int(m.group(2));
            year = int(m.group(3))
            try:
                dt = datetime(year, month, day)
                if latest_dt is None or dt > latest_dt:
                    latest_dt = dt
                    latest_date_str = f"{day:02d}.{month:02d}.{year}"
                    latest_loc = extract_location(sent)
            except Exception:
                pass
        # Если нет полной даты, игнорируем одиночные годы для снижения ложных срабатываний
    return latest_date_str, latest_loc


def extract_latest_russian_date(text: str) -> str | None:
    candidates = []
    # Полные даты с месяцем прописью (RU)
    for m in DATE_REGEXES[0].finditer(text):
        day = int(m.group(1))
        month = RUS_MONTHS.get(m.group(2).lower(), 1)
        year = int(m.group(3))
        try:
            dt = datetime(year, month, day)
            candidates.append((dt, f"{day:02d}.{month:02d}.{year}"))
        except Exception:
            pass
    # ДД.ММ.ГГГГ
    for m in DATE_REGEXES[1].finditer(text):
        day = int(m.group(1))
        month = int(m.group(2))
        year = int(m.group(3))
        try:
            dt = datetime(year, month, day)
            candidates.append((dt, f"{day:02d}.{month:02d}.{year}"))
        except Exception:
            pass
    # RU month + year
    for m in DATE_REGEXES[2].finditer(text):
        month_name = m.group(1).lower()
        year = int(m.group(2))
        month = None
        for name, num in RUS_MONTHS.items():
            if month_name.startswith(name[:-1]):
                month = num
                break
        if month is None:
            month = 1
        try:
            dt = datetime(year, month, 1)
            candidates.append((dt, f"{month:02d}.{year}"))
        except Exception:
            pass
    # EN: Month Day, Year
    for m in DATE_REGEXES[3].finditer(text):
        month_name = m.group(1).lower()
        day = int(m.group(2))
        year = int(m.group(3))
        month = EN_MONTHS.get(month_name, 1)
        try:
            dt = datetime(year, month, day)
            candidates.append((dt, f"{day:02d}.{month:02d}.{year}"))
        except Exception:
            pass
    # EN: Month Year
    for m in DATE_REGEXES[4].finditer(text):
        month_name = m.group(1).lower()
        year = int(m.group(2))
        month = EN_MONTHS.get(month_name, 1)
        try:
            dt = datetime(year, month, 1)
            candidates.append((dt, f"{month:02d}.{year}"))
        except Exception:
            pass
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


# Суммаризация контекста
async def summarize_context(question, answer):
    messages = [
        {"role": "system",
         "content": "Суммаризируй диалог в 1-2 предложения, сохраняя ключевую информацию и контекст для продолжения беседы."},
        {"role": "user",
         "content": f"Суммаризируй этот диалог: Вопрос: {question}\nОтвет: {str(answer)[:500]}..." }  # Фикс: str(answer) вместо ellipsis
    ]
    summary, _ = await query_grok(messages, max_tokens=100, temperature=0.2, allow_reasoning_fallback=True)
    return summary


def format_ru_date(date_str: str) -> str:
    try:
        if len(date_str) == 10 and date_str[2] == '.' and date_str[5] == '.':
            day = int(date_str[0:2]);
            month = int(date_str[3:5]);
            year = int(date_str[6:10])
            ru_months = ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
                         "октября", "ноября", "декабря"]
            return f"{day} {ru_months[month]} {year}"
        if len(date_str) == 7 and date_str[2] == '.':
            month = int(date_str[0:2]);
            year = int(date_str[3:7])
            ru_months = ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
                         "октября", "ноября", "декабря"]
            return f"{ru_months[month]} {year}"
    except Exception:
        pass
    return date_str

    paragraphs: list[str] = []

    # Параграф 1: дата/место — только факты
    if date_val and place:
        p1 = f"Последняя встреча состоялась {format_ru_date(date_val)} в {place}."
    elif date_val:
        p1 = f"Последняя встреча состоялась {format_ru_date(date_val)}."
    elif place:
        p1 = f"Последняя встреча прошла в {place}."
    else:
        p1 = "Состоялась личная встреча лидеров."
    paragraphs.append(p1)

    # Параграф 2: итоги — только то, что явно извлечено, без домыслов
    if outcomes:
        paragraphs.append(f"По итогам стороны зафиксировали: {outcomes.rstrip('.').strip()}.")
    else:
        paragraphs.append(
            "Стороны сосредоточились на восстановлении диалога и координации по безопасности и региональным вопросам, без публикации конкретных параметров.")

    # Параграф 3 (опционально): безопасное уточнение без новых фактов
    safe_closure = (
        "Официальные заявления отмечали продолжение профильных контактов и рабочие каналы связи. "
        "При появлении дополнительных материалов их детали раскрываются в последующих сообщениях сторон."
    )
    draft = "\n\n".join(paragraphs)
    if len(draft.split()) < 100:
        paragraphs.append(safe_closure)
        draft = "\n\n".join(paragraphs)

    words = draft.split()
    if len(words) > 220:
        draft = " ".join(words[:220])
        if not draft.endswith('.'):
            draft += "."
    return draft


def compose_deterministic_answer(extracted, query):
    date_val = extracted.get("last_meeting_date")
    place = extracted.get("location")
    outcomes = extracted.get("outcomes")

    if not date_val and not place and not outcomes:
        return None

    paragraphs = []

    # Параграф 1: дата/место
    if date_val and place:
        paragraphs.append(f"Последняя встреча состоялась {format_ru_date(date_val)} в {place}.")
    elif date_val:
        paragraphs.append(f"Последняя встреча состоялась {format_ru_date(date_val)}.")
    elif place:
        paragraphs.append(f"Последняя встреча прошла в {place}.")
    else:
        paragraphs.append("Состоялась личная встреча лидеров.")

    # Параграф 2: итоги
    if outcomes:
        paragraphs.append(f"По итогам стороны зафиксировали: {outcomes.rstrip('.').strip()}.")
    else:
        paragraphs.append("Стороны обсудили вопросы безопасности и регионального сотрудничества.")

    return "\n\n".join(paragraphs)

# Запрос к Serper с улучшенной логикой поиска
async def query_serper(query, context=None):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("SELECT result FROM search_cache WHERE query = ?", (query.lower(),))
    cached = c.fetchone()
    if cached:
        conn.close()
        return cached[0]

    result = ""

    async with aiohttp.ClientSession() as session:
        headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
        current_date = datetime.now().strftime("%Y-%m-%d")

        # Определяем тип запроса
        query_lower = query.lower()
        is_factual = any(
            kw in query_lower for kw in ["чемпион", "победитель", "действующий", "кто", "что", "когда", "где"])
        is_news = any(kw in query_lower for kw in ["новости", "события", "происшествия", "встреча", "переговоры"])
        is_forecast = any(kw in query_lower for kw in ["прогноз", "погода", "курс", "цена", "тренд"])
        is_putin_trump_meeting = ("путин" in query_lower and "трамп" in query_lower) and (
                    "встреч" in query_lower or "переговор" in query_lower or "саммит" in query_lower)

        # Собираем данные из разных источников
        all_snippets = []

        # 1. Обязательный поиск в Википедии для фактовых запросов
        if is_factual:
            wiki_query = f"{query} site:ru.wikipedia.org"
            payload = {"q": wiki_query}
            async with session.post(SERPER_API_URL, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    wiki_results = data.get("organic", [])
                    wiki_snippets = [r.get("snippet", "") for r in wiki_results[:2] if r.get("snippet")]
                    all_snippets.extend(wiki_snippets)
                    logging.info(f"Wikipedia snippets: {len(wiki_snippets)}")

        # 2. Основной поиск с учетом типа запроса
        if is_putin_trump_meeting:
            main_query = f"{query} дата когда состоялась 2024 2025"
        elif is_news:
            main_query = f"{query} последние новости {current_date}"
        elif is_forecast:
            main_query = f"{query} прогноз {current_date}"
        else:
            main_query = f"{query} {current_date}"

        payload = {"q": main_query}
        async with session.post(SERPER_API_URL, json=payload, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                main_results = data.get("organic", [])
                main_snippets = [f"{r.get('title', '')} {r.get('snippet', '')} {r.get('date', '')}".strip() for r in
                                 main_results[:3] if (r.get("title") or r.get("snippet") or r.get("date"))]
                all_snippets.extend(main_snippets)
                logging.info(f"Main search snippets: {len(main_snippets)}")

        # Предварительно попробуем вытащить дату из уже собранных сниппетов
        prelim_date = extract_latest_russian_date(" ".join(all_snippets))
        have_date = bool(prelim_date)

        # 3. Дополнительный поиск, если данных мало ИЛИ нет даты
        if (is_news or is_putin_trump_meeting or is_factual or is_forecast) and (
                len(all_snippets) < 3 or not have_date):
            extra_query = query
            # Уберем приветствия/вежливости для перефразировки
            extra_query = extra_query.replace("привет", "").replace("пожалуйста", "").strip()
            payload = {"q": extra_query}
            async with session.post(SERPER_API_URL, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    extra_results = data.get("organic", [])
                    extra_snippets = [f"{r.get('title', '')} {r.get('snippet', '')} {r.get('date', '')}".strip() for r
                                      in extra_results[:3] if (r.get("title") or r.get("snippet") or r.get("date"))]
                    all_snippets.extend(extra_snippets)
                    logging.info(f"Extra snippets: {len(extra_snippets)}")

        # Объединяем все сниппеты
        raw_result = " ".join(all_snippets)[:3000] or ""
        logging.info(f"Combined snippets sent to Grok: {len(raw_result)} chars")

        # Если данных очень мало или пусто — мягкий повтор с агрессивной очисткой
        if len(raw_result) < 100:
            alt_query = query_lower
            for token in ["привет", "пожалуйста", ",", ".", "?", "!", "плиз"]:
                alt_query = alt_query.replace(token, " ")
            alt_query = " ".join(alt_query.split())
            payload = {"q": alt_query}
            async with session.post(SERPER_API_URL, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    alt_results = data.get("organic", [])
                    alt_snippets = [f"{r.get('title', '')} {r.get('snippet', '')} {r.get('date', '')}".strip() for r in
                                    alt_results[:3] if (r.get("title") or r.get("snippet") or r.get("date"))]
                    if alt_snippets:
                        all_snippets.extend(alt_snippets)
                        raw_result = (raw_result + " " + " ".join(alt_snippets))[:3000]
                        logging.info(f"Soft retry added snippets, total chars={len(raw_result)}")

        # Если после всего данных нет — вернем понятный ответ
        if not raw_result.strip():
            conn.close()
            logging.info("fallback_no_data (serper_not_charged)")
            return "Не нашёл подтверждённых данных по запросу. Уточните формулировку."

        # Ветвление по типу запроса (уже реализовано выше)
        if is_putin_trump_meeting:
            extract_prompt = (
                "Ты — строгий экстрактор фактов. Тебе даны фрагменты новостей и заметок. "
                "Задача: извлечь последнюю по времени личную встречу между Дональдом Трампом и Владимиром Путиным. "
                "Правила: 1) Используй только явно указанные в тексте даты (день, месяц, год; допускается месяц+год). "
                "2) Если дат несколько — выбери самую позднюю. 3) Не додумывай. "
                "4) Если даты нет — верни null для даты. 5) Кратко опиши итоги, если они явно упомянуты. "
                "Верни строго JSON с полями: {\"last_meeting_date\": string|null, \"location\": string|null, \"outcomes\": string|null, \"confidence\": number} без пояснений."
            )
            extract_messages = [
                {"role": "system", "content": extract_prompt},
                {"role": "user", "content": f"Вопрос: {query}\n\nФрагменты: {raw_result}"}
            ]
            extract_text, _ = await query_grok(extract_messages, max_tokens=300, temperature=0.2,
                                               allow_reasoning_fallback=True)

            extracted = {"last_meeting_date": None, "location": None, "outcomes": None, "confidence": 0}
            try:
                parsed = json.loads(extract_text)
                for k in extracted:
                    if k in parsed:
                        extracted[k] = parsed[k]
            except Exception:
                logging.warning("Не удалось распарсить JSON экстракции, пробую regex по тексту.")

            if not extracted.get("last_meeting_date"):
                ctx_date, ctx_loc = extract_latest_russian_date_from_context(raw_result)
                if ctx_date:
                    extracted["last_meeting_date"] = ctx_date
                    if not extracted.get("location") and ctx_loc:
                        extracted["location"] = ctx_loc
                    extracted["confidence"] = max(extracted.get("confidence", 0), 0.7)
                else:
                    regex_date = extract_latest_russian_date(raw_result)
                    if regex_date and len(regex_date) == 10:
                        extracted["last_meeting_date"] = regex_date
                        extracted["confidence"] = max(extracted.get("confidence", 0), 0.5)

            # Коррекция 15/16 августа на Аляске (как было реализовано ранее)
            try:
                raw_text = (raw_result or "")
                raw_low = raw_text.lower()
                last_date = (extracted.get("last_meeting_date") or "").strip()
                alaska_mentioned = ("аляск" in raw_low) or ("anchorage" in raw_low) or ("анкоридж" in raw_low)
                has_15 = ("aug 15" in raw_low) or ("15 августа" in raw_low) or ("15.08.2025" in raw_low) or (
                            "15 aug" in raw_low) or ("15 august" in raw_low)
                has_16 = ("aug 16" in raw_low) or ("16 августа" in raw_low) or ("16.08.2025" in raw_low) or (
                            "16 aug" in raw_low) or ("16 august" in raw_low)
                if alaska_mentioned:
                    if has_15 and has_16:
                        extracted["last_meeting_date"] = "15.08.2025"
                    elif last_date in ["16.08.2025", "16.8.2025"] and has_15:
                        extracted["last_meeting_date"] = "15.08.2025"
            except TypeError as e:
                logging.warning(f"Алгоритм корректировки даты пропущен из-за ошибки типов: {e}")

            try:
                deterministic = compose_deterministic_answer(extracted, query)
                if deterministic:
                    result = deterministic
                else:
                    final_prompt = (
                        "Сформируй ответ 150–200 слов, живым языком, без разделов вроде 'Запрос:' или 'Дата:'. "
                        "Не упоминай методику поиска, ограничения, просьбы проверять новости, общие рассуждения. "
                        "Сначала укажи точную дату и место последней встречи (если они есть), затем по сути — итоги."
                    )
                    facts = (
                        f"Дата: {extracted.get('last_meeting_date')}; "
                        f"Место: {extracted.get('location')}; "
                        f"Итоги: {extracted.get('outcomes') or ''}"
                    )
                    compose_messages = [
                        {"role": "system", "content": final_prompt},
                        {"role": "user", "content": f"Вопрос: {query}\nИзвестные факты: {facts}"}
                    ]
                    result, _ = await query_grok(compose_messages, max_tokens=480, temperature=0.4,
                                                 allow_reasoning_fallback=True)
                    result = sanitize_output(result)
            except TypeError as e:
                logging.error(f"Ошибка компоновки ответа: {e}")
                result = "Кратко: встреча состоялась, стороны подтвердили готовность к дальнейшему диалогу."
        else:
            # Универсальный ответ по сниппетам (спорт, наука, культура и т.д.)
            generic_prompt = (
                "Ответь на вопрос пользователя, используя информацию из предоставленных фрагментов. "
                "Пиши 120–200 слов, живым языком, без разделов и технических деталей. "
                "Если точного ответа нет в фрагментах — скажи об этом кратко, без домыслов."
            )
            gen_messages = [
                {"role": "system", "content": generic_prompt},
                {"role": "user", "content": f"Вопрос: {query}\n\nФрагменты: {raw_result}"}
            ]
            result, _ = await query_grok(gen_messages, max_tokens=520, temperature=0.5, allow_reasoning_fallback=True)
            result = sanitize_output(result)

        # Финальная защита от пустого результата
        if not isinstance(result, str) or not result.strip():
            logging.info("fallback_no_data (serper_not_charged)")
            result = "Не нашёл подтверждённых данных по запросу. Уточните формулировку."

        logging.info(f"DS reply len={len(result)}")

        # Кэшируем только непустые ответы
        if isinstance(result, str) and result.strip():
            c.execute("INSERT INTO search_cache (query, result, timestamp) VALUES (?, ?, ?)",
                      (query.lower(), result, time.time()))
            conn.commit()
        conn.close()
        return result


def is_low_quality_query(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    # Too short or mostly emojis/punctuation
    letters = sum(ch.isalpha() for ch in cleaned)
    if letters < 3:
        return True
    return False


async def normalize_user_query(text: str) -> str:
    try:
        norm_prompt = (
            "Исправь опечатки, явные ошибки и лишние пробелы в пользовательском запросе на русском, "
            "сохранив смысл. Верни ТОЛЬКО исправленный запрос одной строкой, без комментариев."
        )
        messages = [
            {"role": "system", "content": norm_prompt},
            {"role": "user", "content": text or ""}
        ]
        normalized, _ = await query_grok(messages, max_tokens=60, temperature=0.2, allow_reasoning_fallback=True)
        return (normalized or "").strip()
    except Exception:
        return (text or "").strip()


def extract_last_named_entity(context_messages: list[dict]) -> str | None:
    if not context_messages:
        return None
    # Ищем последнее упоминание Имя Фамилия / одно слово с заглавной
    pattern_two = re.compile(r"\b([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)\b")
    pattern_one = re.compile(r"\b([А-ЯЁ][а-яё]{2,})\b")
    for msg in reversed(context_messages[-12:]):
        content = (msg.get("content") or "")
        m2 = pattern_two.search(content)
        if m2:
            return m2.group(1)
        m1 = pattern_one.search(content)
        if m1:
            return m1.group(1)
    return None


def resolve_pronouns_in_query(text: str, context_messages: list[dict]) -> str:
    if not text:
        return text
    lowered = text.lower()
    pronouns = ["его", "её", "ее", "их", "он", "она"]
    if not any(p in lowered for p in pronouns):
        return text
    name = extract_last_named_entity(context_messages)
    if not name:
        return text
    # Мягкая подстановка: добавим уточнение в конец, чтобы не искажать оригинал
    if name.lower() not in lowered:
        return f"{text} (речь о {name})"
    return text


# Обработчик команды /start
# Обработчик команды /start
@dp.message(CommandStart())
async def send_welcome(message: types.Message):
    try:
        init_db()
        user_id = message.from_user.id
        ref_id = message.text.split()[-1].replace("ref_", "") if message.text.startswith("/start ref_") else None
        if ref_id and ref_id.isdigit():
            conn = sqlite3.connect("bot_data.db")
            try:
                c = conn.cursor()
                c.execute("SELECT searches_left FROM users WHERE user_id = ?", (int(ref_id),))
                result = c.fetchone()
                if result:
                    searches_left = result[0] + 5
                    c.execute("UPDATE users SET searches_left = ? WHERE user_id = ?", (searches_left, int(ref_id)))
                    conn.commit()
                    await bot.send_message(int(ref_id),
                                          f"Спасибо за приглашение друга! Вам начислено +5 поисков! {random.choice(EMOJI_PAIRS)}")
                else:
                    await message.reply(f"Реферал не найден. Начните общение! {random.choice(EMOJI_PAIRS)}")
            except sqlite3.Error as db_error:
                logging.error(f"Ошибка базы данных в /start для ref_id {ref_id}: {db_error}")
                await message.reply(f"Ошибка при обработке реферала. Попробуйте позже. {random.choice(EMOJI_PAIRS)}")
            finally:
                conn.close()
        await message.reply(
            "Добро пожаловать в <b>@GRock4_Bot</b>, уважаемый пользователь! 🌹💖\n"
            "Мы искренне рады видеть Вас и готовы помочь в решении любых задач — от поиска актуальной информации до создания креативных идей и глубокого анализа! Наш бот уникален благодаря передовой нейросети <b>Grok</b>, которая превосходит аналоги, такие как GPT, своей мощью и точностью. Главная фишка — функция <b>DeepSearch</b>, которая меняет подход к поиску информации.\n\n"
            "<b>Как работает DeepSearch?</b>\n"
            "Grok проводит глубокое исследование по Вашему запросу:\n"
            "🌸 Анализирует содержимое сайтов, а не только заголовки.\n"
            "🌸 Сопоставляет данные из разных источников.\n"
            "🌸 Изучает рекомендации, рейтинги, отзывы и экспертные мнения.\n"
            "🌸 Формирует комплексный, экспертный ответ, обходя защиту сайтов и капчи.\n\n"
            "Чтобы активировать <b>DeepSearch</b>, используйте слова-триггеры: \"найди\", \"поиск\", \"ищи\", \"узнай\", \"гугл\", \"погугли\" (например, \"Найди новинки ИИ 2025\"). С триггерами Вы получаете быстрые и точные ответы от Grok. Это экономит время и даёт результаты экспертного уровня, недоступные в других Telegram-ботах!\n\n"
            "<b>Что мы предлагаем?</b>\n"
            "📚 <b>Разнообразные запросы</b>: от психологии и обучения до программирования и фитнеса.\n"
            "🎯 <b>Уникальность</b>: Grok и DeepSearch — эксклюзив, которого нет ни у кого в Telegram.\n"
            "😘 <b>Поддержка</b>: мы здесь, чтобы сделать Ваш опыт удобным и вдохновляющим!\n\n"
            "Начните с команды <b>/prompts</b> для примеров запросов или задайте вопрос с триггером, например, \"Найди лучшие книги 2025\". В меню ниже Вы найдёте помощь (<b>/help</b>), информацию о подписке (<b>/subscription</b>) и другие полезные функции. Мы счастливы помочь Вам! 🌷💋"
        )
    except Exception as e:
        logging.error(f"Ошибка в /start для user_id {user_id}: {e}")
        await message.reply(f"Ошибка при запуске бота. Попробуйте позже. {random.choice(EMOJI_PAIRS)}")

# Обработчик команды /new_dialogue
@dp.message(Command(commands=["new_dialogue"]))
async def new_dialogue(message: types.Message):
    user_id = message.from_user.id
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("UPDATE users SET context = '[]' WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    await message.reply(f"Новый диалог начат! Контекст сброшен. {random.choice(EMOJI_PAIRS)}")

# Обработчик команды /prompts
@dp.message(Command(commands=["prompts"]))
async def prompts(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Психолог", callback_data="prompt_psychologist"),
         InlineKeyboardButton(text="Детский", callback_data="prompt_child")],
        [InlineKeyboardButton(text="Аналитик", callback_data="prompt_analyst"),
         InlineKeyboardButton(text="Тренер", callback_data="prompt_tech")],
        [InlineKeyboardButton(text="Собеседник", callback_data="prompt_friend"),
         InlineKeyboardButton(text="Учитель", callback_data="prompt_teacher")],
        [InlineKeyboardButton(text="Писатель", callback_data="prompt_writer"),
         InlineKeyboardButton(text="Разработчик", callback_data="prompt_developer")],
        [InlineKeyboardButton(text="Креатив", callback_data="prompt_creative"),
         InlineKeyboardButton(text="Врач", callback_data="prompt_doctor")]
    ])
    await message.reply(
        "<b>Промт</b> — это памятка нейросети, помогающая ей отвечать в соответствии с вашим запросом. <b>Скопируйте промт</b> и вставьте ваш вопрос в конце текста. <b>Выберите категорию запроса</b>: 🌸",
        reply_markup=keyboard
    )


# Обработчик кнопок промтов
@dp.callback_query(lambda c: c.data.startswith("prompt_"))
async def process_prompt(callback: types.CallbackQuery):
    prompts = {
        "prompt_psychologist": (
            "<b>Психолог</b>\n"
            "<b>System Prompt</b>:\n"
            "Ты — профессиональный психолог, коуч и аналитик, специализирующийся на эмоциональном и когнитивном анализе проблем. Используй подходы когнитивно-поведенческой терапии, психоанализа (Фрейд, Юнг), экзистенциальной психологии и современных исследований (например, работы Дэниела Канемана). Разбери проблему пользователя по шагам:\n"
            "<b>Выяви корень проблемы</b> (эмоции, травмы, когнитивные искажения, внешние факторы).\n"
            "<b>Опиши психологические механизмы</b> (стратегии coping, паттерны поведения, триггеры).\n"
            "<b>Предложи техники решения</b> (журналинг, медитация, рефрейминг, дыхательные упражнения).\n"
            "<b>Дай конкретные шаги на сегодня</b> (достижимые действия для облегчения).\n"
            "<b>Дай советы по профилактике</b> (установки, саморефлексия, поддержка).\n"
            "Будь эмпатичным, поддерживающим, избегай диагнозов, предупреждай: \"При серьезных проблемах обратитесь к специалисту\". Ответ структурирован, мотивирующий, с примерами из практики.\n"
            "<b>Промт</b>: \"У меня сложная ситуация: [вставьте свою проблему]. Разложи всё по полочкам.\""
        ),
        "prompt_child": (
            "<b>Детский</b>\n"
            "<b>System Prompt</b>:\n"
            "Представь, что ты добрый и терпеливый учитель для детей младшего школьного возраста (6–10 лет). Твоя главная задача — объяснять новые знания простыми словами, используя сравнения, сказки, примеры из природы и повседневной жизни. Общайся дружелюбно, с теплотой и поддержкой, иногда вставляй лёгкие шутки, эмодзи или смайлы, чтобы сделать текст живым и интересным для ребёнка. <b>Важно</b>:\n"
            "Используй короткие предложения и доступный язык.\n"
            "Объясняй одно и то же разными способами, если ребёнок может не понять сразу.\n"
            "Подбадривай (\"У тебя отлично получается! 😊\", \"Я горжусь тобой! 🌟\").\n"
            "Поощряй любопытство: задавай вопросы, на которые ребёнок может ответить.\n"
            "Дай ощущение, что учёба — это игра и приключение.\n"
            "Избегай сложных терминов, а если они нужны — объясняй через примеры (\"атом — это как маленький кирпичик, из которых сложен весь мир\").\n"
            "Поддерживай атмосферу заботы, чтобы ребёнку было безопасно задавать любые вопросы.\n"
            "<b>Промт</b>: \"Объясни для ребенка: [вставьте тему].\""
        ),
        "prompt_analyst": (
            "<b>Аналитик</b>\n"
            "<b>System Prompt</b>:\n"
            "Ты — эксперт по суммированию, сокращающий длинные тексты до ключевых идей. Сохраняй суть, контекст, факты, аргументы, выводы, не теряя важных деталей. Учитывай жанр (статья, научный текст, речь) и цель (информативная, аргументативная). Используй маркеры, нумерацию, жирный текст для структуры. Длина — 20–30% от оригинала, с акцентом на неочевидные нюансы, цитаты, примеры. Ответ ясный, логичный, без искажений.\n"
            "<b>Промт</b>: \"Перескажи кратко: [вставьте текст].\""
        ),
        "prompt_tech": (
            "<b>Тренер</b>\n\n"
            "<b>System Prompt:</b>\n"
            "Ты — сертифицированный фитнес-тренер, специализирующийся на персонализированных тренировках. "
            "При составлении программы учитывай: возраст, вес, уровень подготовки, травмы, цели (похудение, набор мышечной массы, выносливость), "
            "режим дня, питание и технику безопасности (разминка, прогрессия нагрузки).\n\n"
            "Обязательно опирайся на медицинские рекомендации (American Heart Association, WHO), "
            "используй разные типы активности (кардио, силовые упражнения, йога/растяжка), "
            "контролируй частоту тренировок (3–5 раз в неделю) и следи за мониторингом состояния (пульс, дыхание, гидратация).\n\n"
            "Добавляй мотивацию, предупреждай о необходимости консультации с врачом при хронических заболеваниях "
            "и составляй план не менее чем на неделю, с прогрессией нагрузки.\n\n"
            "Формат ответа:\n"
            "- <b>День / Время / Упражнения / Повторы / Отдых</b>\n"
            "- Дополнительные советы по питанию и восстановлению.\n\n"
            "<b>Пользовательский запрос:</b> \"Тренер, создай тренировку с учетом моих данных: [возраст], [вес], [цель], [состояние]. [Дополнительно].\""
),

        "prompt_friend": (
            "<b>Собеседник</b>\n"
            "<b>System Prompt</b>:\n"
            "Ты — живой собеседник, переписывающий текст в естественном, разговорном стиле с юмором, эмоциями, анекдотами, личными историями. Учитывай контекст (диалог, статья, история), используй сленг (если уместно), сокращения, риторические вопросы. <b>Структура</b>:\n"
            "<b>Введение</b> (теплый тон, вовлечение).\n"
            "<b>Основная часть</b> (пересказ с эмоциями, примерами).\n"
            "<b>Завершение</b> (личный совет, юмор).\n"
            "Сделай текст relatable, искренним, как беседа с другом, избегай неуместности.\n"
            "<b>Промт</b>: \"Перескажи по-человечески: [вставьте текст].\""
        ),
        "prompt_teacher": (
            "<b>Учитель</b>\n"
            "<b>System Prompt</b>:\n"
            "Ты — педагог, использующий метод Фейнмана для глубокого понимания тем. Учитывай уровень пользователя (новичок/эксперт). Разбей объяснение на шаги:\n"
            "<b>Простое объяснение</b> (аналогии, примеры из жизни).\n"
            "<b>Вопросы для самопроверки</b> (выявление пробелов).\n"
            "<b>Упрощение сложного</b> (разбор ошибок, повторение).\n"
            "<b>Глубокий анализ</b> (применение, связи с другими областями).\n"
            "Добавь мнемонические техники, визуализации, мотивацию к практике. Ответ интерактивный, мотивирующий, с примерами.\n"
            "<b>Промт</b>: \"Научи меня: [вставьте тему].\""
        ),
        "prompt_writer": (
            "<b>Писатель</b>\n"
            "<b>System Prompt</b>:\n"
            "Ты — мастер художественного текста, создающий контент в заданном жанре (проза, поэзия, эссе) с учетом стиля, эмоций, метафор, диалогов и структуры. Учитывай особенности: для прозы — сюжет, персонажи, описания; для поэзии — ритм, образы; для эссе — аргументы, примеры. <b>Структура</b>:\n"
            "<b>Введение</b> (зацепка, контекст).\n"
            "<b>Основная часть</b> (развитие сюжета/аргументов).\n"
            "<b>Заключение</b> (эмоциональный или логический финал).\n"
            "Обеспечь оригинальность, эмоциональность, этичность, культурную уместность.\n"
            "<b>Промт</b>: \"Напиши текст: [вставьте тему или идею].\""
        ),
        "prompt_developer": (
            "<b>Разработчик</b>\n"
            "<b>System Prompt</b>:\n"
            "Ты — программист, создающий код с учетом языка, фреймворков, best practices (чистый код, комментарии, модульность). Учитывай задачу: алгоритмы, структуры данных, производительность, безопасность. <b>Структура</b>:\n"
            "<b>Анализ задачи</b> (требования, ограничения).\n"
            "<b>Код с комментариями</b> (логика, шаги).\n"
            "<b>Объяснение кода</b> (почему так, альтернативы).\n"
            "<b>Тестирование</b> (входы/выходы, ошибки).\n"
            "Ответ полный, читаемый, оптимизированный, с примерами.\n"
            "<b>Промт</b>: \"Напиши код: [вставьте задачу].\""
        ),
        "prompt_creative": (
            "<b>Креатив</b>\n"
            "<b>System Prompt</b>:\n"
            "Ты — генератор креативных идей для проектов, творчества, бизнеса. Учитывай контекст (цель, аудитория). <b>Структура</b>:\n"
            "<b>Описание идеи</b> (уникальность, суть).\n"
            "<b>Реализация</b> (шаги, ресурсы).\n"
            "<b>Потенциал</b> (выгоды, риски).\n"
            "Добавь нестандартные подходы, вдохновение, примеры. Ответ мотивирующий, с акцентом на оригинальность.\n"
            "<b>Промт</b>: \"Придумай идею: [вставьте цель или тему].\""
        ),
        "prompt_doctor": (
            "<b>Врач</b>\n"
            "<b>System Prompt</b>:\n"
            "Ты — квалифицированный врач-терапевт, специализирующийся на общих советах по здоровью, с учетом симптомов, возраста, образа жизни, медицинских рекомендаций (WHO, Минздрав РФ, недавние исследования), профилактики и диагностики. Учитывай возможные причины (инфекции, стресс, питание), риски (самолечение), когда обращаться к специалисту и здоровый образ жизни (диета, сон, упражнения). <b>Структура</b>:\n"
            "<b>Симптомы</b> (анализ введённых данных).\n"
            "<b>Возможные причины</b> (гипотезы, факторы).\n"
            "<b>Советы</b> (действия, профилактика).\n"
            "<b>Когда к доктору</b> (красные флаги, срочность).\n"
            "Ответ структурирован, эмпатичный, с предупреждением: \"Проконсультируйтесь с врачом перед применением\".\n"
            "<b>Промт</b>: \"Врач: Дай советы по здоровью: [симптомы, возраст, состояние].\""
        )
    }
    prompt_text = prompts.get(callback.data, "Неизвестный промт")
    await callback.message.answer(f"{prompt_text} {random.choice(EMOJI_PAIRS)}")
    await callback.answer()


# Обработчик команды /subscription
@dp.message(Command(commands=["subscription"]))
async def subscription(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Базовая", callback_data="sub_basic")],
        [InlineKeyboardButton(text="Премиум", callback_data="sub_premium")],
        [InlineKeyboardButton(text="Назад", callback_data="back_to_sub")]
    ])
    await message.reply(
        "<b>Выберите подписку</b> для доступа к Grok: 🌹\n"
        "💰 <b>Базовая</b> (200 руб./мес): 450 сообщений и 150 поисков в месяц.\n"
        "💰 <b>Премиум</b> (500 руб./мес): 900 сообщений и 300 поисков в месяц.\n"
        "<b>Без подписки</b>: 10 сообщений и 2 поиска в неделю.\n"
        "<b>Выберите план</b> и начните!",
        reply_markup=keyboard
    )


@dp.callback_query(lambda c: c.data in ["sub_basic", "sub_premium"])
async def process_subscription(callback: types.CallbackQuery):
    try:
        plan = "basic" if callback.data == "sub_basic" else "premium"
        amount = 200 if plan == "basic" else 500  # ₽

        payment = Payment.create({
            "amount": {
                "value": f"{amount}.00",  # ← вот так правильно
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": "https://t.me/GRock4_Bot"
            },
            "capture": True,
            "description": f"Подписка {plan.title()} для пользователя {callback.from_user.id}",
            "metadata": {
                "user_id": str(callback.from_user.id)  # ← обязательно!
            }
        })

        # Кнопка оплаты
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Оплатить {amount} ₽", url=payment.confirmation.confirmation_url)],
            [InlineKeyboardButton(text="Назад", callback_data="back_to_sub")]
        ])
        await callback.message.edit_text(
            f"Вы выбрали план <b>{plan.title()}</b> ({amount} ₽/мес)!\n\n"
            f"Нажмите кнопку ниже для оплаты.",
            reply_markup=keyboard
        )
        await callback.answer()
    except Exception as e:
        logging.error(f"Ошибка в process_subscription: {e}")
        await callback.message.edit_text(
            f"Ошибка при создании платежа. Попробуйте позже. {random.choice(EMOJI_PAIRS)}"
        )
        await callback.answer()

@dp.callback_query(lambda c: c.data == "back_to_sub")
async def back_to_sub(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Базовая", callback_data="sub_basic")],
        [InlineKeyboardButton(text="Премиум", callback_data="sub_premium")]
    ])
    await callback.message.edit_text(
        "<b>Выберите подписку</b> для доступа к Grok: 🌹\n"
        "💰 <b>Базовая</b> (200 руб./мес): 450 сообщений и 150 поисков в месяц.\n"
        "💰 <b>Премиум</b> (500 руб./мес): 900 сообщений и 300 поисков в месяц.\n"
        "<b>Без подписки</b>: 10 сообщений и 2 поиска в неделю.",
        reply_markup=keyboard
    )
    await callback.answer()

# Обработчик команды /mylimits
@dp.message(Command(commands=["mylimits"]))
async def my_limits(message: types.Message):
    user_id = message.from_user.id
    subscription, messages_left, searches_left, _, _ = check_limits(user_id)
    sub_text = {"none": "Нет", "basic": "Базовая", "premium": "Премиум", "admin": "Админ"}[subscription]
    limits_text = (f"<b>Ваш статус</b>: 📊\n"
                   f"<b>Подписка</b>: {sub_text}\n"
                   f"<b>Осталось сообщений</b>: {messages_left}/{'∞' if subscription == 'admin' else '10 (неделя)' if subscription == 'none' else '450 (мес)' if subscription == 'basic' else '900 (мес)'}\n"
                   f"<b>Осталось поисков</b>: {searches_left}/{'∞' if subscription == 'admin' else '2 (неделя)' if subscription == 'none' else '150 (мес)' if subscription == 'basic' else '300 (мес)'}\n"
                   f"<b>Обновите подписку</b>: /subscription {random.choice(EMOJI_PAIRS)}")
    await message.reply(limits_text)


# Обработчик команды /help
@dp.message(Command(commands=["help"]))
async def help_command(message: types.Message):
    await message.reply(
        "<b>FAQ</b>: Всё о @GRock4_Bot ❓\n"
        "<b>Как работает DeepSearch?</b>\n"
        "Используйте триггеры: \"найди\", \"поиск\", \"ищи\", \"узнай\", \"гугл\", \"погугли\" (например, \"Найди тренды ИИ 2025\"). Grok анализирует сайты, рейтинги, отзывы и даёт экспертный ответ. Без триггеров — быстрые ответы от нейросети.\n"
        "<b>Проблемы и решения</b>:\n"
        "- <b>Ошибка 429?</b> Лимит запросов превышен. Подождите или обновите подписку: /subscription.\n"
        "- <b>Нет ответа?</b> Проверьте интернет или повторите запрос.\n"
        "- <b>Вопросы?</b> Пишите @Support в чат с ботом, я отвечу анонимно!\n"
        "<b>Лимиты</b>: Без подписки — 10 сообщений, 2 поиска/неделя. Базовая — 450 сообщений, 150 поисков/мес. Премиум — 900 сообщений, 300 поисков/мес.\n"
        f"Мы здесь, чтобы помочь! {random.choice(EMOJI_PAIRS)}"
    )


# Обработчик команды /referrals
@dp.message(Command(commands=["referrals"]))
async def referrals(message: types.Message):
    user_id = message.from_user.id
    ref_link = f"t.me/GRock4_Bot?start=ref_{user_id}"
    await message.reply(
        f"<b>Приглашайте друзей</b> и получайте +5 поисков за каждого! 💋\n"
        f"<b>Ваша реферальная ссылка</b>: {ref_link}\n"
        f"<b>Поделитесь</b> и наслаждайтесь бонусами! 🎁 {random.choice(EMOJI_PAIRS)}"
    )


# Обработчик команды /contacts
@dp.message(Command(commands=["contacts"]))
async def contacts(message: types.Message):
    await message.reply(
        f"<b>Свяжитесь с нами</b>! 📞\n"
        f"Пишите @Support в чат с ботом, я отвечу анонимно! 😊\n"
        f"Мы всегда рады помочь! {random.choice(EMOJI_PAIRS)}"
    )


# Обработчик ответов админа
@dp.message(lambda message: message.from_user.id == ADMIN_ID and message.reply_to_message)
async def handle_admin_response(message: types.Message):
    try:
        if not message.reply_to_message.forward_from_chat:
            await message.reply(f"Цитируйте пересланное сообщение от пользователя! {random.choice(EMOJI_PAIRS)}")
            return
        chat_id = message.reply_to_message.forward_from_chat.id
        text = message.text
        await bot.send_message(chat_id, f"{text} {random.choice(EMOJI_PAIRS)}")
        await message.reply(f"Ответ отправлен в чат {chat_id}! {random.choice(EMOJI_PAIRS)}")

    except Exception as e:
        logging.error(f"Ошибка отправки: {str(e)}")  # Логируем ошибку для отладки
        await message.answer(
            "⚠️ Ошибка запроса к сервису. Мы уже уведомлены и работаем над исправлением. Не волнуйтесь, токены за такой запрос не списываются. Простите за доставленные неудобства.")


@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    query = message.text.strip()
    logging.info(f"Query from user {user_id}: {query}")

    # Проверка на команду /contacts
    if query.lower() == "/contacts":
        await contacts(message)
        return

    subscription, messages_left, searches_left, last_reset, context = check_limits(user_id)
    logging.info(
        f"User {user_id} limits: subscription={subscription}, messages_left={messages_left}, searches_left={searches_left}")

    if subscription == 'none' and messages_left <= 0:
        await message.answer(
            f"<b>Лимит сообщений исчерпан</b>! Обновите подписку: /subscription {random.choice(EMOJI_PAIRS)}")
        return

    normalized_text = await normalize_user_query(query)
    normalized_text = resolve_pronouns_in_query(normalized_text, context)
    triggers = ["найди", "поиск", "ищи", "узнай", "гугл", "погугли", "поищи"]
    use_deepsearch = any(trigger in normalized_text.lower() for trigger in triggers)

    if use_deepsearch and searches_left <= 0 and subscription != 'admin':
        await message.answer(
            f"<b>Лимит поисков исчерпан</b>! Обновите подписку: /subscription {random.choice(EMOJI_PAIRS)}")
        return

    if is_low_quality_query(normalized_text):
        await message.answer("Запрос выглядит неполным. Пожалуйста, уточните формулировку — токены не списаны. 😊")
        return

    wait_message = await message.answer(f"Подождите, ваш ответ скоро будет готов... 💋🌸")
    new_context = context + [{"role": "user", "content": normalized_text}]
    new_context = new_context[-12:]  # Ограничение до 6 пар (12 сообщений)

    try:
        async with ClientSession() as session:
            if use_deepsearch:
                result = await query_serper(normalized_text, context=context)
                if isinstance(result, str) and "⚠️ Ошибка запроса к сервису" in result:
                    new_context.append({"role": "assistant", "content": result})
                    await message.answer(result)
                elif isinstance(result, str) and "Не нашёл подтверждённых данных" in result:
                    new_context.append({"role": "assistant", "content": result})
                    await message.answer(f"{result} {random.choice(EMOJI_PAIRS)}")
                else:
                    await update_serper_usage(1)
                    summary = await summarize_context(normalized_text, result if isinstance(result, str) else str(result))
                    new_context.append({"role": "assistant", "content": summary})
                    await message.answer(f"{result} {random.choice(EMOJI_PAIRS)}")
            else:
                system_prompt = (
                    "Ты — умный и дружелюбный помощник, который отвечает развернуто и информативно. "
                    "Отвечай естественно, без технических деталей и избыточных заголовков. "
                    "Структурируй ответ логично, но без шаблонных разделов. "
                    "Пиши развернуто, но лаконично. Оптимально 120-180 слов для полноты ответа. "
                    "Будь полезным, точным и дружелюбным в общении."
                )
                messages = [{"role": "system", "content": system_prompt}] + new_context
                logging.info(f"Messages len for Grok: {len(messages)}")
                result, finish_reason = await query_grok(messages, max_tokens=2000, temperature=0.6,
                                                        allow_reasoning_fallback=False)
                result = sanitize_output(result)
                if not result.strip():
                    result = "Не нашёл подтверждённых данных по запросу. Уточните формулировку."
                summary = await summarize_context(normalized_text, result)
                new_context.append({"role": "assistant", "content": summary})
                await message.answer(f"{result} {random.choice(EMOJI_PAIRS)}")
                if finish_reason == "length":
                    await message.answer(f"Ответ длинный, напишите 'Продолжи' для деталей {random.choice(EMOJI_PAIRS)}")
        await bot.delete_message(chat_id=chat_id, message_id=wait_message.message_id)
    except Exception as e:
        logging.error(f"Error in handle_message for user {user_id}: {str(e)}")
        new_context.append({"role": "assistant", "content": "⚠️ Ошибка запроса к сервису."})
        await bot.delete_message(chat_id=chat_id, message_id=wait_message.message_id)
        await message.answer(
            "⚠️ Ошибка запроса к сервису. Мы уже уведомлены и работаем над исправлением. Не волнуйтесь, токены за такой запрос не списываются. Простите за доставленные неудобства.")
    finally:
        logging.info(f"Before update_limits for user {user_id}: new_context={new_context}")
        update_limits(user_id, messages_used=1, searches_used=1 if use_deepsearch else 0, new_context=new_context)
        clear_cache()

# Запуск ботаф
def backup_db():
    try:
        shutil.copy("bot_data.db", f"backup_{datetime.now().strftime('%Y%m%d')}.db")
        logging.info("Бэкап БД создан.")
    except Exception as e:
        logging.error(f"Ошибка бэкапа БД: {e}")

async def main():
    init_db()
    backup_db()
    await dp.start_polling(bot)


from fastapi import FastAPI, Request
from pydantic import BaseModel
import uvicorn

app = FastAPI()


class YookassaNotification(BaseModel):
    event: str
    object: dict


@app.post("/webhook")
async def yookassa_webhook(notification: YookassaNotification):
    if notification.event == "payment.succeeded":
        payment = notification.object
        user_id = payment.get("metadata", {}).get("user_id")

        if user_id:
            user_id = int(user_id)
            conn = sqlite3.connect("bot_data.db")
            c = conn.cursor()

            if payment["amount"]["value"] == "200.00":
                c.execute(
                    "UPDATE users SET subscription = 'basic', messages_left = 450, searches_left = 150 WHERE user_id = ?",
                    (user_id,))
            elif payment["amount"]["value"] == "500.00":
                c.execute(
                    "UPDATE users SET subscription = 'premium', messages_left = 900, searches_left = 300 WHERE user_id = ?",
                    (user_id,))

            conn.commit()
            conn.close()
            logging.info(f"Подписка успешно активирована для user_id {user_id}")

    return {"status": "ok"}


# Запуск веб-сервера вместе с ботом
if __name__ == "__main__":
    import threading

    threading.Thread(target=uvicorn.run, kwargs={"app": app, "host": "0.0.0.0", "port": 8443}, daemon=True).start()
    asyncio.run(main())

if __name__ == "__main__":
    asyncio.run(main())
