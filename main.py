import os
import re
import asyncio
import aiosqlite
import logging
from datetime import datetime
from random import randint
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB = os.getenv("DB_PATH", "quizbot.db")

CREATE_TEST_RE = re.compile(r"^\s*(\d+)[^\S\r\n]*(?:тест:|:|\s+)?\s*\+([a-dA-D]+)\s*$", re.IGNORECASE)
SUBMIT_RE = re.compile(r"^\s*(\d{4,6})\s*:\s*([a-dA-D]+)\s*$")

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL,
            answer_key TEXT NOT NULL,
            length INTEGER NOT NULL,
            is_open INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            answers TEXT NOT NULL,
            score INTEGER NOT NULL,
            submitted_at TEXT NOT NULL,
            UNIQUE(test_code, user_id)
        );
        """)
        await db.commit()

async def get_role(user_id: int) -> str | None:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None

async def set_role(user_id: int, role: str):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
            INSERT INTO users(user_id, role, created_at)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET role=excluded.role
        """, (user_id, role, now))
        await db.commit()

async def generate_unique_code() -> str:
    async with aiosqlite.connect(DB) as db:
        while True:
            code = f"{randint(1000, 9999)}"
            cur = await db.execute("SELECT 1 FROM tests WHERE code = ?", (code,))
            if not await cur.fetchone():
                return code

def score_answers(key: str, ans: str) -> int:
    return sum(1 for k, a in zip(key, ans) if k == a)

def norm(s: str) -> str:
    return s.strip().lower()

async def main():
    if not BOT_TOKEN:
        raise SystemExit("Установите переменную окружения BOT_TOKEN")

    await init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def cmd_start(m: Message):
        await m.answer(
            "Привет! Я тест-бот.\n\n"
            "Выберите роль:\n"
            "• /register author — я составитель тестов\n"
            "• /register participant — я участник\n\n"
            "Примеры:\n"
            "Создание теста (составитель):  `10 тест: +abcdabcdab`\n"
            "Сдача ответов (участник):       `1596:abcdabcdab`\n"
            "Завершить тест (составитель):   `/finish 1596`",
            parse_mode="Markdown"
        )

    @dp.message(Command("register"))
    async def cmd_register(m: Message):
        parts = m.text.split()
        if len(parts) < 2 or parts[1] not in ("author", "participant"):
            await m.answer("Используйте: /register author  или  /register participant")
            return
        await set_role(m.from_user.id, parts[1])
        await m.answer(f"Роль установлена: {parts[1]} ✅")

    @dp.message(Command("finish"))
    async def cmd_finish(m: Message):
        role = await get_role(m.from_user.id)
        if role != "author":
            await m.answer("Команда доступна только составителю.")
            return
        parts = m.text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await m.answer("Используйте: /finish <код_теста>")
            return
        code = parts[1]
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT owner_id, is_open, answer_key FROM tests WHERE code = ?", (code,))
            row = await cur.fetchone()
            if not row:
                await m.answer("Тест с таким кодом не найден.")
                return
            owner_id, is_open, answer_key = row
            if owner_id != m.from_user.id:
                await m.answer("Вы не являетесь владельцем этого теста.")
                return
            if not is_open:
                await m.answer("Тест уже завершён.")
                return

            await db.execute("UPDATE tests SET is_open = 0 WHERE code = ?", (code,))
            cur = await db.execute("""
                SELECT user_id, answers, score
                FROM submissions
                WHERE test_code = ?
                ORDER BY score DESC, submitted_at ASC
            """, (code,))
            rows = await cur.fetchall()
            await db.commit()

        if not rows:
            await m.answer(f"Тест {code} закрыт. Участников не было.")
            return

        lines = [f"Тест {code} закрыт. Результаты:"]
        for i, (uid, ans, sc) in enumerate(rows[:10], 1):
            lines.append(f"{i}. user {uid}: {sc}/{len(answer_key)}")
        await m.answer("\n".join(lines))

    @dp.message(F.text.regexp(CREATE_TEST_RE))
    async def handle_create(m: Message, regexp):
        role = await get_role(m.from_user.id)
        if role != "author":
            await m.answer("Создавать тесты может только составитель. Зарегистрируйтесь: /register author")
            return

        n_str, key_raw = regexp.group(1), regexp.group(2)
        n = int(n_str)
        key = norm(key_raw)
        if not all(c in "abcd" for c in key):
            await m.answer("Ключ должен содержать только буквы a/b/c/d (после знака '+').")
            return
        if len(key) != n:
            await m.answer(f"Длина ключа ({len(key)}) должна совпадать с количеством вопросов ({n}).")
            return

        code = await generate_unique_code()
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(DB) as db:
            await db.execute("""
                INSERT INTO tests(code, owner_id, answer_key, length, is_open, created_at)
                VALUES(?, ?, ?, ?, 1, ?)
            """, (code, m.from_user.id, key, n, now))
            await db.commit()

        await m.answer(
            "✅ Тест создан!\n\n"
            f"Код теста: *{code}*\n"
            f"Длина: {n}\n"
            "Разошлите код участникам.\n\n"
            "Участник отвечает так:  `код:егоответы`  (например `"
            f"{code}:{'a'*n}`)\n"
            f"Чтобы завершить тест:  `/finish {code}`",
            parse_mode="Markdown"
        )

    @dp.message(F.text.regexp(SUBMIT_RE))
    async def handle_submit(m: Message, regexp):
        code, ans_raw = regexp.group(1), regexp.group(2)
        answers = norm(ans_raw)
        if not all(c in "abcd" for c in answers):
            await m.answer("Ответы должны состоять только из a/b/c/d.")
            return

        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT answer_key, is_open FROM tests WHERE code = ?", (code,))
            row = await cur.fetchone()
            if not row:
                await m.answer("Тест с таким кодом не найден.")
                return
            key, is_open = row
            if not is_open:
                await m.answer("Тест уже завершён. Новые ответы не принимаются.")
                return
            if len(answers) != len(key):
                await m.answer(f"Длина ваших ответов ({len(answers)}) должна быть {len(key)}.")
                return

            sc = score_answers(key, answers)
            now = datetime.utcnow().isoformat()
            try:
                await db.execute("""
                    INSERT INTO submissions(test_code, user_id, answers, score, submitted_at)
                    VALUES(?, ?, ?, ?, ?)
                """, (code, m.from_user.id, answers, sc, now))
                await db.commit()
            except aiosqlite.IntegrityError:
                await m.answer("Вы уже отправили ответы на этот тест. Повторная попытка не разрешена.")
                return

        await m.answer(f"Принято! Ваш результат: {sc}/{len(answers)} ✅")

    @dp.message(Command("results"))
    async def cmd_results(m: Message):
        parts = m.text.split()
        if len(parts) < 2:
            await m.answer("Используйте: /results <код_теста>")
            return
        code = parts[1]
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT answer_key, owner_id, is_open FROM tests WHERE code = ?", (code,))
            row = await cur.fetchone()
            if not row:
                await m.answer("Тест не найден.")
                return
            key, owner_id, is_open = row
            cur = await db.execute("""
                SELECT user_id, score, submitted_at
                FROM submissions
                WHERE test_code = ?
                ORDER BY score DESC, submitted_at ASC
            """, (code,))
            rows = await cur.fetchall()

        status = "открыт" if is_open else "закрыт"
        if not rows:
            await m.answer(f"Тест {code} ({status}). Пока без попыток.")
            return

        lines = [f"Тест {code} ({status}). Итоги:"]
        for i, (uid, sc, ts) in enumerate(rows[:15], 1):
            lines.append(f"{i}. user {uid}: {sc}/{len(key)}")
        await m.answer("\n".join(lines))

    @dp.message()
    async def fallback(m: Message):
        await m.answer(
            "Не понял сообщение.\n\n"
            "Создать тест (составитель):  `10 тест: +abcdabcdab`\n"
            "Сдать ответы (участник):     `1596:abcdabcdab`\n"
            "Завершить тест:              `/finish 1596`\n"
            "Роли: /register author  или  /register participant",
            parse_mode="Markdown"
        )

    logging.info("Bot is running…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
