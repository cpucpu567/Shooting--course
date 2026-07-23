from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
import logging
import json
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Стрелковый интенсив API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== ОТДАЧА ФАЙЛОВ ИЗ ПАПКИ fronted =====
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "fronted")

@app.get("/")
async def read_root():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

@app.get("/admin.html")
async def read_admin():
    return FileResponse(os.path.join(FRONTEND_DIR, "admin.html"))

@app.get("/{filename}")
async def get_frontend_file(filename: str):
    file_path = os.path.join(FRONTEND_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="Файл не найден")

# ===== Подключение к PostgreSQL =====
DATABASE_URL = os.getenv("DATABASE_URL")
logger.info(f"DATABASE_URL: {DATABASE_URL}")

def get_db():
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as e:
        logger.error(f"Ошибка подключения к БД: {str(e)}")
        raise HTTPException(500, "Ошибка подключения к базе данных")

def init_db():
    conn = get_db()
    c = conn.cursor()
    
    # 1. Создаём таблицу clients с базовой структурой (без новых колонок)
    c.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            phone TEXT PRIMARY KEY,
            surname TEXT NOT NULL,
            name TEXT NOT NULL,
            visits INTEGER DEFAULT 0,
            experienced TEXT DEFAULT 'newbie',
            newsletter BOOLEAN DEFAULT FALSE,
            total_discounts INTEGER DEFAULT 0,
            last_visit TIMESTAMP,
            referrer TEXT,
            vk_id TEXT
        );
    ''')
    
    # 2. Безопасно добавляем новые колонки (если их нет)
    c.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS tg_id TEXT;")
    c.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS vk_subscribed BOOLEAN DEFAULT FALSE;")
    c.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS tg_subscribed BOOLEAN DEFAULT FALSE;")
    c.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS vk_bonus_issued BOOLEAN DEFAULT FALSE;")
    c.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS tg_bonus_issued BOOLEAN DEFAULT FALSE;")
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id SERIAL PRIMARY KEY,
            surname TEXT NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            referral TEXT,
            tariff TEXT NOT NULL,
            date TEXT NOT NULL,
            time_slot TEXT,
            source TEXT,
            newsletter BOOLEAN DEFAULT FALSE,
            discount INTEGER DEFAULT 0,
            final_price INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (phone) REFERENCES clients(phone) ON DELETE CASCADE
        );
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS dates (
            id SERIAL PRIMARY KEY,
            value TEXT NOT NULL,
            label TEXT NOT NULL,
            group_id TEXT NOT NULL,
            time_slot TEXT,
            max_persons INTEGER DEFAULT 10,
            min_persons INTEGER DEFAULT 5,
            status TEXT DEFAULT 'pending'
        );
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            location TEXT NOT NULL,
            price INTEGER NOT NULL,
            poster_url TEXT,
            min_participants INTEGER DEFAULT 5,
            max_participants INTEGER DEFAULT 15,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS event_bookings (
            id SERIAL PRIMARY KEY,
            event_id INTEGER NOT NULL,
            surname TEXT NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT,
            subscribed BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
            FOREIGN KEY (phone) REFERENCES clients(phone) ON DELETE CASCADE
        );
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS gallery (
            id SERIAL PRIMARY KEY,
            url TEXT NOT NULL,
            type TEXT NOT NULL,
            name TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    ''')
    
    c.execute("SELECT key FROM config WHERE key = 'prices'")
    if not c.fetchone():
        c.execute("INSERT INTO config (key, value) VALUES ('prices', '{\"practice\":{\"base\":5000,\"instructor\":2000},\"basic\":{\"base\":5000,\"instructor\":3500},\"pro\":{\"base\":10000,\"instructor\":3500}}')")
    
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована (старые клиенты сохранены)")

init_db()

# ===== Модели =====
class BookingRequest(BaseModel):
    surname: str
    name: str
    phone: str
    referral: str = ""
    tariff: str
    date: str
    time_slot: str = "full"
    source: str = ""
    newsletter: bool = False
    use_bonus: bool = False
    vk_id: str = ""

class PriceUpdate(BaseModel):
    practice: dict = Field(..., description="{'base': int, 'instructor': int}")
    basic: dict = Field(..., description="{'base': int, 'instructor': int}")
    pro: dict = Field(..., description="{'base': int, 'instructor': int}")

class DateItem(BaseModel):
    value: str
    label: str
    group_id: str
    time_slot: str = "full"
    max_persons: int = 10
    min_persons: int = 5

class EventItem(BaseModel):
    title: str
    description: str
    date: str
    time: str
    location: str
    price: int
    poster_url: str = ""
    min_participants: int = 5
    max_participants: int = 15

class EventBookingRequest(BaseModel):
    event_id: int
    surname: str
    name: str
    phone: str
    email: str = ""
    subscribed: bool = False

class GalleryItem(BaseModel):
    url: str
    type: str
    name: str = ""

class VKCallbackRequest(BaseModel):
    vk_id: str
    phone: str

class TelegramCallbackRequest(BaseModel):
    tg_id: str
    phone: str

class MailingRequest(BaseModel):
    text: str
    platform: str  # "vk", "telegram", "both"

# ===== Вспомогательные функции =====
def get_prices():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key = 'prices'")
    row = c.fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row['value'])
        except json.JSONDecodeError:
            return {"practice": {"base": 5000, "instructor": 2000}, "basic": {"base": 5000, "instructor": 3500}, "pro": {"base": 10000, "instructor": 3500}}
    return {"practice": {"base": 5000, "instructor": 2000}, "basic": {"base": 5000, "instructor": 3500}, "pro": {"base": 10000, "instructor": 3500}}

def send_admin_notification(booking_id, data, final_price, discount):
    is_event = hasattr(data, 'event_id') and data.event_id is not None
    
    vk_token = os.getenv("VK_TOKEN", "")
    if vk_token:
        if is_event:
            msg = f"📅 Новая заявка на событие #{booking_id}\n👤 {data.surname} {data.name}\n📞 {data.phone}\n📧 {data.email or 'не указан'}\n💰 Итог: {final_price} ₽"
        else:
            msg = f"🔫 Новая заявка на интенсив #{booking_id}\n👤 {data.surname} {data.name}\n📞 {data.phone}\n🎯 {data.tariff}\n📅 {data.date} {data.time_slot}\n💰 Итог: {final_price} ₽\n📱 Источник: {data.source or 'не указан'}"
        
        try:
            requests.post("https://api.vk.com/method/messages.send", params={
                "access_token": vk_token,
                "v": "5.131",
                "user_id": 304659962,
                "message": msg,
                "random_id": 0
            })
            logger.info("VK: уведомление администратору отправлено")
        except Exception as e:
            logger.error(f"Ошибка VK админу: {str(e)}", exc_info=True)
            
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if telegram_token and chat_id:
        if is_event:
            tg_msg = f"📅 Новая заявка на событие #{booking_id}\n👤 {data.surname} {data.name}\n📞 {data.phone}\n📧 {data.email or 'не указан'}\n💰 Итог: {final_price} ₽"
        else:
            tg_msg = f"🔫 Новая заявка на интенсив #{booking_id}\n👤 {data.surname} {data.name}\n📞 {data.phone}\n🎯 {data.tariff}\n📅 {data.date} {data.time_slot}\n💰 Итог: {final_price} ₽"
        
        try:
            requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", json={"chat_id": chat_id, "text": tg_msg})
            logger.info("Telegram: уведомление администратору отправлено")
        except Exception as e:
            logger.error(f"Ошибка Telegram админу: {str(e)}", exc_info=True)

def send_client_notification(phone, tariff, date, time_slot, final_price):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT vk_id FROM clients WHERE phone = %s", (phone,))
    row = c.fetchone()
    conn.close()
    vk_id = row['vk_id'] if row else None

    msg = f"✅ Ваша запись на «Стрелковый интенсив» подтверждена!\n\n📅 Дата: {date}\n⏱ Время: {time_slot}\n🎯 Курс: {tariff}\n💰 Итог: {final_price} ₽\n\n📍 Место: стрельбище, г. Пермь\n📞 Вопросы: https://vk.com/club239743393"

    vk_token = os.getenv("VK_TOKEN", "")
    if vk_token and vk_id:
        try:
            requests.post("https://api.vk.com/method/messages.send", params={
                "access_token": vk_token,
                "v": "5.131",
                "user_id": vk_id,
                "message": msg,
                "random_id": 0
            })
            logger.info(f"VK: уведомление клиенту {vk_id} отправлено")
        except Exception as e:
            logger.error(f"Ошибка VK клиенту: {str(e)}")
    
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if telegram_token and chat_id:
        try:
            requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", json={"chat_id": chat_id, "text": msg})
            logger.info("Telegram: уведомление клиенту отправлено")
        except Exception as e:
            logger.error(f"Ошибка Telegram клиенту: {str(e)}")

def send_event_client_notification(phone, event_title, event_date, event_time, location, price):
    msg = f"✅ Ваша запись на событие «{event_title}» подтверждена!\n\n📅 Дата: {event_date}\n⏱ Время: {event_time}\n📍 Место: {location}\n💰 Стоимость: {price} ₽\n\n📞 Вопросы: https://vk.com/club239743393"

    vk_token = os.getenv("VK_TOKEN", "")
    if vk_token:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT vk_id FROM clients WHERE phone = %s", (phone,))
        row = c.fetchone()
        conn.close()
        vk_id = row['vk_id'] if row else None

        if vk_id:
            try:
                requests.post("https://api.vk.com/method/messages.send", params={
                    "access_token": vk_token,
                    "v": "5.131",
                    "user_id": vk_id,
                    "message": msg,
                    "random_id": 0
                })
                logger.info(f"VK: уведомление о событии клиенту {vk_id} отправлено")
            except Exception as e:
                logger.error(f"Ошибка VK клиенту (событие): {str(e)}")

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if telegram_token and chat_id:
        try:
            requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", json={"chat_id": chat_id, "text": msg})
            logger.info("Telegram: уведомление о событии клиенту отправлено")
        except Exception as e:
            logger.error(f"Ошибка Telegram клиенту (событие): {str(e)}")

# ===== API: Основные тарифы =====
@app.get("/api/config", include_in_schema=False)
@app.head("/api/config", include_in_schema=False)
async def get_config():
    prices = get_prices()
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM dates")
    dates = c.fetchall()
    
    result_dates = []
    for d in dates:
        c.execute("SELECT COUNT(*) FROM bookings WHERE date = %s AND tariff = (SELECT group_id FROM dates WHERE id = %s)", (d['value'], d['id']))
        count = c.fetchone()['count']
        result_dates.append({
            "id": d['id'],
            "value": d['value'],
            "label": d['label'],
            "group": d['group_id'],
            "timeSlot": d['time_slot'],
            "maxPersons": d['max_persons'],
            "minPersons": d['min_persons'],
            "currentCount": count,
            "status": d['status']
        })
    
    conn.close()
    return {"prices": prices, "dates": result_dates}

@app.post("/api/booking")
async def create_booking(data: BookingRequest):
    if not data.surname or not data.name or not data.phone:
        raise HTTPException(400, "Заполните все обязательные поля")
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, min_persons, max_persons, status FROM dates WHERE value = %s AND group_id = %s", (data.date, data.tariff))
    date_row = c.fetchone()
    if not date_row:
        conn.close()
        raise HTTPException(400, "Выбранная дата не существует")
    conn.close()
    
    prices = get_prices()
    if data.tariff not in prices:
        raise HTTPException(400, "Неверный тариф")

    price = prices[data.tariff]
    base_price = price["base"]
    instructor_price = price["instructor"]
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT total_discounts FROM clients WHERE phone = %s", (data.phone,))
    client = c.fetchone()
    total_discount = client['total_discounts'] if client else 0
    conn.close()
    
    discount = 0
    if data.use_bonus and total_discount > 0:
        max_bonus = int(instructor_price * 0.20)
        discount = min(total_discount, max_bonus)
    final_price = base_price + (instructor_price - discount)
    
    conn = get_db()
    c = conn.cursor()
    
    vk_id_to_save = data.vk_id if data.vk_id else None
    
    c.execute('''
        INSERT INTO clients (phone, surname, name, visits, experienced, newsletter, referrer, vk_id)
        VALUES (%s, %s, %s, 1, 'newbie', %s, %s, %s)
        ON CONFLICT (phone) DO UPDATE SET
            surname = EXCLUDED.surname,
            name = EXCLUDED.name,
            visits = clients.visits + 1,
            experienced = CASE 
                WHEN clients.visits + 1 >= 4 THEN 'pro' 
                WHEN clients.visits + 1 >= 2 THEN 'experienced' 
                ELSE 'newbie' 
            END,
            newsletter = EXCLUDED.newsletter,
            referrer = COALESCE(EXCLUDED.referrer, clients.referrer),
            vk_id = COALESCE(EXCLUDED.vk_id, clients.vk_id)
    ''', (data.phone, data.surname, data.name, data.newsletter, data.referral, vk_id_to_save))
    conn.commit()
    conn.close()

    if not data.referral:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT visits FROM clients WHERE phone = %s", (data.phone,))
        row = c.fetchone()
        visits = row['visits'] if row else 0
        if visits % 5 == 0:
            c.execute("UPDATE clients SET total_discounts = total_discounts + 500 WHERE phone = %s", (data.phone,))
            conn.commit()
            logger.info(f"Бонус 500 ₽ за {visits}-е посещение")
        conn.close()

    if discount > 0:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE clients SET total_discounts = total_discounts - %s WHERE phone = %s", (discount, data.phone))
        conn.commit()
        conn.close()

    if data.referral:
        try:
            conn = get_db()
            c = conn.cursor()
            if data.referral == data.phone:
                logger.warning(f"Пользователь {data.phone} попытался привести сам себя. Бонус не начислен.")
                conn.close()
            else:
                c.execute("SELECT referrer FROM clients WHERE phone = %s", (data.referral,))
                row = c.fetchone()
                if row and row['referrer'] == data.phone:
                    c.execute("UPDATE clients SET total_discounts = total_discounts + 500 WHERE phone = %s", (data.phone,))
                    logger.info(f"Обнаружена зеркальная запись. Бонус 500 ₽ начислен только инициатору {data.phone}")
                else:
                    c.execute("UPDATE clients SET total_discounts = total_discounts + 500 WHERE phone = %s", (data.phone,))
                    c.execute("UPDATE clients SET total_discounts = total_discounts + 500 WHERE phone = %s", (data.referral,))
                    logger.info(f"Бонус 500 ₽ начислен обоим: {data.phone} и {data.referral}")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка начисления скидки другу: {str(e)}", exc_info=True)

    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO bookings 
        (surname, name, phone, referral, tariff, date, time_slot, source, newsletter, discount, final_price, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    ''', (data.surname, data.name, data.phone, data.referral, data.tariff, data.date, data.time_slot,
          data.source, data.newsletter, discount, final_price, 'pending'))
    booking_id = c.fetchone()['id']
    conn.commit()
    conn.close()

    send_admin_notification(booking_id, data, final_price, discount)

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM bookings WHERE date = %s AND tariff = %s", (data.date, data.tariff))
    count = c.fetchone()['count']
    c.execute("SELECT min_persons FROM dates WHERE value = %s AND group_id = %s", (data.date, data.tariff))
    min_persons = c.fetchone()['min_persons']
    conn.close()

    # ==================== НОВЫЙ БЛОК (отправка в канал + на стену VK при наборе) ====================
    if count == min_persons:
        vk_token = os.getenv("VK_TOKEN", "")
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        channel_id = "-1002612715364"
        
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT phone FROM bookings WHERE date = %s AND tariff = %s", (data.date, data.tariff))
        phones = c.fetchall()
        conn.close()
        
        post_text = f"✅ ИНТЕНСИВ ПОДТВЕРЖДЁН!\n\n📅 Дата: {data.date}\n🎯 Тариф: {data.tariff}\n👥 Участников: {len(phones)} чел.\n\n📍 Место: стрельбище, г. Пермь\n\n📝 Запись и вопросы: https://vk.com/club239743393"
        
        if telegram_token and channel_id:
            try:
                requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", json={
                    "chat_id": channel_id,
                    "text": post_text,
                    "disable_web_page_preview": False
                })
                logger.info("Telegram: уведомление в канал отправлено (1 раз)")
            except Exception as e:
                logger.error(f"Ошибка отправки в Telegram канал: {str(e)}")
        
        if vk_token:
            try:
                requests.post("https://api.vk.com/method/wall.post", params={
                    "access_token": vk_token,
                    "v": "5.131",
                    "owner_id": -239743393,
                    "message": post_text,
                    "from_group": 1
                })
                logger.info("VK: пост на стене опубликован (1 раз)")
            except Exception as e:
                logger.error(f"Ошибка публикации на стене VK: {str(e)}")
    # ==================== КОНЕЦ НОВОГО БЛОКА ====================

    if count >= min_persons:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE bookings SET status = 'confirmed' WHERE date = %s AND tariff = %s", (data.date, data.tariff))
        conn.commit()
        
        # ЛС только новому участнику (если есть vk_id или tg_id)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT vk_id, tg_id FROM clients WHERE phone = %s", (data.phone,))
        client = c.fetchone()
        conn.close()
        
        vk_id = client['vk_id'] if client else None
        tg_id = client['tg_id'] if client else None
        
        msg = f"✅ Ваша запись на «Стрелковый интенсив» подтверждена!\n\n📅 Дата: {data.date}\n⏱ Время: {data.time_slot}\n🎯 Курс: {data.tariff}\n💰 Итог: {final_price} ₽\n\n📍 Место: стрельбище, г. Пермь\n📞 Вопросы: https://vk.com/club239743393"
        
        vk_token = os.getenv("VK_TOKEN", "")
        if vk_token and vk_id:
            try:
                requests.post("https://api.vk.com/method/messages.send", params={
                    "access_token": vk_token,
                    "v": "5.131",
                    "user_id": vk_id,
                    "message": msg,
                    "random_id": 0
                })
                logger.info(f"VK: ЛС отправлено новому участнику {vk_id}")
            except Exception as e:
                logger.error(f"Ошибка VK ЛС: {str(e)}")
        
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if telegram_token and tg_id:
            try:
                requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", json={"chat_id": tg_id, "text": msg})
                logger.info(f"Telegram: ЛС отправлено новому участнику {tg_id}")
            except Exception as e:
                logger.error(f"Ошибка Telegram ЛС: {str(e)}")
        
        conn.close()
    
    return {"id": booking_id, "status": "created", "finalPrice": final_price, "discount": discount}

@app.get("/api/client/status/{phone}")
async def get_client_status(phone: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM bookings WHERE phone = %s AND status = 'pending'", (phone,))
    pending_bookings = c.fetchone()['count']
    c.execute("SELECT COUNT(*) FROM event_bookings WHERE phone = %s AND status = 'pending'", (phone,))
    pending_events = c.fetchone()['count']
    c.execute("SELECT visits, experienced, total_discounts FROM clients WHERE phone = %s", (phone,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        return {"level": "newbie", "visits": 0, "bonus": 0, "message": "Вы ещё не были у нас. Записывайтесь!"}
    
    if pending_bookings > 0:
        return {
            "level": row['experienced'],
            "visits": row['visits'],
            "bonus": row['total_discounts'],
            "message": "✅ Вы уже записаны на интенсив, ждём набора группы!"
        }
    
    if pending_events > 0:
        return {
            "level": row['experienced'],
            "visits": row['visits'],
            "bonus": row['total_discounts'],
            "message": "✅ Вы уже записаны на событие, ждём подтверждения!"
        }
    
    messages = {
        "newbie": "🔰 Отлично! Вы начнёте с основ.",
        "experienced": "⭐ С возвращением! Готовы к скорости?",
        "pro": "🏆 Профессионал! Ждём вас на продвинутый."
    }
    
    return {
        "level": row['experienced'],
        "visits": row['visits'],
        "bonus": row['total_discounts'],
        "message": messages.get(row['experienced'], "")
    }

@app.post("/api/prices")
async def update_prices(data: PriceUpdate):
    for key in ["practice", "basic", "pro"]:
        if data.dict()[key]["base"] < 0 or data.dict()[key]["instructor"] < 0:
            raise HTTPException(400, "Цены не могут быть отрицательными")
    
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE config SET value = %s WHERE key = 'prices'", 
              (json.dumps({"practice": data.practice, "basic": data.basic, "pro": data.pro}),))
    conn.commit()
    conn.close()
    return {"status": "updated"}

@app.post("/api/dates")
async def add_date(date: DateItem):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            INSERT INTO dates (value, label, group_id, time_slot, max_persons, min_persons, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending')
        ''', (date.value, date.label, date.group_id, date.time_slot, date.max_persons, date.min_persons))
        conn.commit()
        conn.close()
        logger.info("Дата успешно добавлена")
        return {"status": "added"}
    except Exception as e:
        logger.error(f"Ошибка при добавлении даты: {str(e)}")
        raise HTTPException(500, f"Ошибка базы данных: {str(e)}")

@app.delete("/api/dates/{id}")
async def delete_date(id: int):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM dates WHERE id = %s", (id,))
        conn.commit()
        conn.close()
        return {"status": "deleted"}
    except Exception as e:
        logger.error(f"Ошибка при удалении даты: {str(e)}")
        raise HTTPException(500, f"Ошибка базы данных: {str(e)}")
        
@app.get("/api/bookings")
async def get_bookings(limit: int = 100, offset: int = 0):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM bookings ORDER BY created_at DESC LIMIT %s OFFSET %s", (limit, offset))
    rows = c.fetchall()
    conn.close()
    return [{"id": r['id'], "surname": r['surname'], "name": r['name'], "phone": r['phone'], "tariff": r['tariff'],
             "date": r['date'], "timeSlot": r['time_slot'], "finalPrice": r['final_price'], "status": r['status'],
             "createdAt": r['created_at']} for r in rows]

@app.get("/api/clients")
async def get_clients(limit: int = 100, offset: int = 0):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM clients ORDER BY surname, name LIMIT %s OFFSET %s", (limit, offset))
    rows = c.fetchall()
    conn.close()
    return [{"phone": r['phone'], "surname": r['surname'], "name": r['name'], "visits": r['visits'], 
             "experienced": r['experienced'], "newsletter": r['newsletter'], 
             "totalDiscounts": r['total_discounts'], "lastVisit": r['last_visit'],
             "referrer": r['referrer'], "vk_id": r['vk_id'], "tg_id": r['tg_id'],
             "vk_subscribed": r['vk_subscribed'], "tg_subscribed": r['tg_subscribed'],
             "vk_bonus_issued": r['vk_bonus_issued'], "tg_bonus_issued": r['tg_bonus_issued']} for r in rows]

@app.delete("/api/clients/{phone}")
async def delete_client(phone: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM clients WHERE phone = %s", (phone,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}

@app.get("/api/client/access/{phone}")
async def check_tariff_access(phone: str, tariff: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT visits FROM clients WHERE phone = %s", (phone,))
    row = c.fetchone()
    conn.close()
    visits = row['visits'] if row else 0
    
    if tariff == 'basic':
        return {"allow": True, "message": "Базовый доступен всем"}
    elif tariff == 'practice':
        if visits >= 1:
            return {"allow": True, "message": "Практика доступна (после 1-го посещения)"}
        else:
            return {"allow": False, "message": "❌ Сначала пройдите Базовый курс (1 занятие)"}
    elif tariff == 'pro':
        if visits >= 4:
            return {"allow": True, "message": "Продвинутый доступен (после 4-х посещений)"}
        else:
            return {"allow": False, "message": "❌ Продвинутый требует минимум 4 посещения (Базовый + 3 Практики)"}
    return {"allow": False, "message": "Неизвестный тариф"}

# ===== API: Галерея =====
@app.get("/api/gallery")
async def get_gallery():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM gallery ORDER BY uploaded_at DESC")
    rows = c.fetchall()
    conn.close()
    return [{"id": r['id'], "url": r['url'], "type": r['type'], "name": r['name'], "uploadedAt": r['uploaded_at']} for r in rows]

@app.post("/api/gallery")
async def add_gallery_item(item: GalleryItem):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            INSERT INTO gallery (url, type, name)
            VALUES (%s, %s, %s)
            RETURNING id
        ''', (item.url, item.type, item.name))
        item_id = c.fetchone()['id']
        conn.commit()
        conn.close()
        return {"id": item_id, "status": "created"}
    except Exception as e:
        logger.error(f"Ошибка при добавлении в галерею: {str(e)}")
        raise HTTPException(500, f"Ошибка базы данных: {str(e)}")

@app.delete("/api/gallery/{id}")
async def delete_gallery_item(id: int):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM gallery WHERE id = %s", (id,))
        conn.commit()
        conn.close()
        return {"status": "deleted"}
    except Exception as e:
        logger.error(f"Ошибка при удалении из галереи: {str(e)}")
        raise HTTPException(500, f"Ошибка базы данных: {str(e)}")

# ===== API: События =====
@app.get("/api/events")
async def get_events():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM events ORDER BY date ASC")
    rows = c.fetchall()
    conn.close()
    return [{"id": r['id'], "title": r['title'], "description": r['description'], "date": r['date'],
             "time": r['time'], "location": r['location'], "price": r['price'], 
             "posterUrl": r['poster_url'], 
             "minParticipants": r['min_participants'], "maxParticipants": r['max_participants'], 
             "status": r['status']} for r in rows]

@app.post("/api/events")
async def create_event(event: EventItem):
    logger.info(f"📥 Получен запрос на создание события: {event.title}")
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            INSERT INTO events (title, description, date, time, location, price, poster_url, min_participants, max_participants, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
            RETURNING id
        ''', (event.title, event.description, event.date, event.time, event.location, event.price, event.poster_url, event.min_participants, event.max_participants))
        event_id = c.fetchone()['id']
        conn.commit()
        conn.close()
        logger.info(f"✅ Событие успешно создано: {event.title} (ID: {event_id})")
        
        try:
            vk_token = os.getenv("VK_TOKEN", "")
            telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
            
            if vk_token:
                post_text = f"""🎯 НОВОЕ СОБЫТИЕ!

{event.title}
📅 {event.date} в {event.time}
📍 {event.location}
💰 {event.price} ₽👥 Участников: {event.min_participants}–{event.max_participants}

{event.description}

Запись в сообществе: https://vk.com/club239743393"""
                try:
                    requests.post("https://api.vk.com/method/wall.post", params={
                        "access_token": vk_token,
                        "v": "5.131",
                        "owner_id": -239743393,
                        "message": post_text,
                        "from_group": 1
                    })
                    logger.info("✅ Пост о событии опубликован на стене VK")
                except Exception as e:
                    logger.error(f"❌ Ошибка публикации поста VK: {str(e)}")
            
            if telegram_token and chat_id:
                tg_msg = f"""🎯 НОВОЕ СОБЫТИЕ!

{event.title}
📅 {event.date} в {event.time}
📍 {event.location}
💰 {event.price} ₽
👥 {event.min_participants}–{event.max_participants} чел.

{event.description}

Запись: https://vk.com/club239743393"""
                try:
                    requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", json={
                        "chat_id": chat_id,
                        "text": tg_msg,
                        "disable_web_page_preview": False
                    })
                    logger.info("✅ Сообщение о событии отправлено в Telegram")
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки в Telegram: {str(e)}")
            
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT phone, vk_id FROM clients WHERE newsletter = TRUE")
            subscribers = c.fetchall()
            conn.close()
            
            for sub in subscribers:
                msg = f"""📢 У нас новое событие!

{event.title}
📅 {event.date} в {event.time}
📍 {event.location}
💰 {event.price} ₽

{event.description}

Запись: https://vk.com/club239743393"""
                if vk_token and sub['vk_id']:
                    try:
                        requests.post("https://api.vk.com/method/messages.send", params={
                            "access_token": vk_token,
                            "v": "5.131",
                            "user_id": sub['vk_id'],
                            "message": msg,
                            "random_id": 0
                        })
                    except Exception as e:
                        logger.error(f"❌ Ошибка отправки подписчику VK: {str(e)}")
        except Exception as e:
            logger.error(f"⚠️ Ошибка при отправке уведомлений (не критично): {str(e)}")
        
        return {"id": event_id, "status": "created"}
    except psycopg2.IntegrityError as e:
        logger.error(f"❌ Ошибка целостности данных при создании события: {str(e)}", exc_info=True)
        raise HTTPException(400, f"Ошибка целостности данных: {str(e)}")
    except Exception as e:
        logger.error(f"❌ Внутренняя ошибка при создании события: {str(e)}", exc_info=True)
        raise HTTPException(500, f"Внутренняя ошибка: {str(e)}")

@app.delete("/api/events/{id}")
async def delete_event(id: int):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM events WHERE id = %s", (id,))
        conn.commit()
        conn.close()
        return {"status": "deleted"}
    except Exception as e:
        logger.error(f"Ошибка при удалении события: {str(e)}")
        raise HTTPException(500, f"Ошибка базы данных: {str(e)}")

@app.post("/api/event/booking")
async def create_event_booking(data: EventBookingRequest):
    if not data.surname or not data.name or not data.phone:
        raise HTTPException(400, "Заполните все обязательные поля")
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, min_participants, max_participants, title, date, time, location, price FROM events WHERE id = %s", (data.event_id,))
    event = c.fetchone()
    if not event:
        conn.close()
        raise HTTPException(400, "Событие не найдено")
    conn.close()

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT visits, experienced, total_discounts FROM clients WHERE phone = %s", (data.phone,))
    client = c.fetchone()
    
    if client:
        c.execute("UPDATE clients SET surname = %s, name = %s, newsletter = newsletter WHERE phone = %s",
                  (data.surname, data.name, data.phone))
        if client['visits'] > 0:
            c.execute("UPDATE clients SET total_discounts = total_discounts + 500 WHERE phone = %s", (data.phone,))
            logger.info(f"✅ Действующий член клуба (+500 ₽ за событие): {data.phone}")
    else:
        if data.subscribed:
            c.execute('''
                INSERT INTO clients (phone, surname, name, visits, experienced, newsletter, total_discounts)
                VALUES (%s, %s, %s, 0, 'newbie', FALSE, 500)
            ''', (data.phone, data.surname, data.name))
            logger.info(f"✅ Новый член клуба через событие (подписка +500 ₽): {data.phone}")
        else:
            c.execute('''
                INSERT INTO clients (phone, surname, name, visits, experienced, newsletter, total_discounts)
                VALUES (%s, %s, %s, 0, 'newbie', FALSE, 0)
            ''', (data.phone, data.surname, data.name))
            logger.info(f"ℹ️ Новый клиент через событие (без подписки, 0 ₽): {data.phone}")
    
    conn.commit()
    conn.close()

    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO event_bookings (event_id, surname, name, phone, email, subscribed, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'pending')
        RETURNING id
    ''', (data.event_id, data.surname, data.name, data.phone, data.email, data.subscribed))
    booking_id = c.fetchone()['id']
    conn.commit()
    conn.close()

    send_admin_notification(booking_id, data, event['price'], 0)
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM event_bookings WHERE event_id = %s", (data.event_id,))
    count = c.fetchone()['count']
    c.execute("SELECT min_participants FROM events WHERE id = %s", (data.event_id,))
    min_participants = c.fetchone()['min_participants']
    conn.close()

    # ==================== НОВЫЙ БЛОК (отправка в канал + на стену VK при наборе) ====================
    if count == min_participants:
        vk_token = os.getenv("VK_TOKEN", "")
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        channel_id = "-1002612715364"
        
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT phone FROM event_bookings WHERE event_id = %s", (data.event_id,))
        phones = c.fetchall()
        conn.close()
        
        post_text = f"✅ СОБЫТИЕ ПОДТВЕРЖДЁНО!\n\n📅 Дата: {event['date']} в {event['time']}\n📍 Место: {event['location']}\n🎯 Название: {event['title']}\n👥 Участников: {len(phones)} чел.\n\n📝 Запись и вопросы: https://vk.com/club239743393"
        
        if telegram_token and channel_id:
            try:
                requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", json={
                    "chat_id": channel_id,
                    "text": post_text,
                    "disable_web_page_preview": False
                })
                logger.info("Telegram: уведомление в канал о событии отправлено (1 раз)")
            except Exception as e:
                logger.error(f"Ошибка отправки в Telegram канал: {str(e)}")
        
        if vk_token:
            try:
                requests.post("https://api.vk.com/method/wall.post", params={
                    "access_token": vk_token,
                    "v": "5.131",
                    "owner_id": -239743393,
                    "message": post_text,
                    "from_group": 1
                })
                logger.info("VK: пост о событии на стене опубликован (1 раз)")
            except Exception as e:
                logger.error(f"Ошибка публикации на стене VK: {str(e)}")
    # ==================== КОНЕЦ НОВОГО БЛОКА ====================

    if count >= min_participants:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE event_bookings SET status = 'confirmed' WHERE event_id = %s", (data.event_id,))
        c.execute("UPDATE events SET status = 'confirmed' WHERE id = %s", (data.event_id,))
        conn.commit()
        conn.close()
        
        # ЛС только новому участнику (если есть vk_id или tg_id)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT vk_id, tg_id FROM clients WHERE phone = %s", (data.phone,))
        client = c.fetchone()
        conn.close()
        
        vk_id = client['vk_id'] if client else None
        tg_id = client['tg_id'] if client else None
        
        msg = f"✅ Ваша запись на событие «{event['title']}» подтверждена!\n\n📅 Дата: {event['date']}\n⏱ Время: {event['time']}\n📍 Место: {event['location']}\n💰 Стоимость: {event['price']} ₽\n\n📞 Вопросы: https://vk.com/club239743393"
        
        vk_token = os.getenv("VK_TOKEN", "")
        if vk_token and vk_id:
            try:
                requests.post("https://api.vk.com/method/messages.send", params={
                    "access_token": vk_token,
                    "v": "5.131",
                    "user_id": vk_id,
                    "message": msg,
                    "random_id": 0
                })
                logger.info(f"VK: ЛС о событии отправлено новому участнику {vk_id}")
            except Exception as e:
                logger.error(f"Ошибка VK ЛС: {str(e)}")
        
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if telegram_token and tg_id:
            try:
                requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", json={"chat_id": tg_id, "text": msg})
                logger.info(f"Telegram: ЛС о событии отправлено новому участнику {tg_id}")
            except Exception as e:
                logger.error(f"Ошибка Telegram ЛС: {str(e)}")
    
    return {"id": booking_id, "status": "created"}

@app.get("/api/event/bookings/{event_id}")
async def get_event_bookings(event_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM event_bookings WHERE event_id = %s ORDER BY created_at DESC", (event_id,))
    rows = c.fetchall()
    conn.close()
    return [{"id": r['id'], "surname": r['surname'], "name": r['name'], "phone": r['phone'], "email": r['email'],
             "subscribed": r['subscribed'], "status": r['status'], "createdAt": r['created_at']} for r in rows]

# ===== API: Подписки и callback'и =====
@app.post("/api/callback/vk")
async def vk_callback(request: Request):
    try:
        body = await request.json()
    except Exception:
        return PlainTextResponse("error")
    
    logger.info(f"📥 VK Callback: {body}")
    
    if body.get("type") == "confirmation":
        return PlainTextResponse("13f009d9")
    
    if body.get("type") == "group_join":
        user_id = body.get("object", {}).get("user_id")
        if user_id:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT phone, vk_subscribed, vk_bonus_issued FROM clients WHERE vk_id = %s", (str(user_id),))
            client = c.fetchone()
            
            if client:
                if not client['vk_subscribed']:
                    c.execute("UPDATE clients SET vk_subscribed = TRUE WHERE phone = %s", (client['phone'],))
                    conn.commit()
                    logger.info(f"✅ VK подписка подтверждена для {client['phone']} (vk_id: {user_id})")
                    if not client['vk_bonus_issued']:
                        c.execute("UPDATE clients SET total_discounts = total_discounts + 250, vk_bonus_issued = TRUE WHERE phone = %s", (client['phone'],))
                        conn.commit()
                        logger.info(f"✅ VK бонус 250 ₽ начислен: {client['phone']}")
                conn.close()
            else:
                logger.info(f"ℹ️ VK group_join: пользователь {user_id} не найден в базе")
            return {"ok": True}
    
    if body.get("type") == "message_new":
        user_id = body.get("object", {}).get("user_id")
        text = body.get("object", {}).get("text", "").strip()
    
        # Проверяем: начинается с phone_ ИЛИ равно слову БОНУС (без регистра)
        if user_id and (text.startswith("phone_") or text.upper() == "БОНУС"):
        phone = text.replace("phone_", "").strip()
        
            # Если это слово БОНУС — пытаемся найти клиента по последнему известному номеру, либо оставляем пустым для ручного ввода
            if text.upper() == "БОНУС":
                conn = get_db()
                c = conn.cursor()
                c.execute("SELECT phone FROM clients WHERE vk_id = %s", (str(user_id),))
                row = c.fetchone()
                conn.close()
                if row:
                    phone = row['phone']
                else:
                    # Если не нашли, возвращаем ошибку, но не ломаем сервер
                    logger.warning(f"Пользователь {user_id} написал БОНУС, но номер не найден")
                    return {"ok": True}

            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT phone, vk_subscribed, vk_bonus_issued FROM clients WHERE phone = %s", (phone,))
            client = c.fetchone()
        
            if client:
                c.execute("UPDATE clients SET vk_id = %s WHERE phone = %s", (str(user_id), phone))
                if not client['vk_subscribed']:
                    # Проверяем реальную подписку через VK API
                    vk_token = os.getenv("VK_TOKEN", "")
                    group_id = "239743393"
                    is_member = False
                    if vk_token:
                        try:
                            resp = requests.get("https://api.vk.com/method/groups.isMember", params={
                            "access_token": vk_token,
                            "v": "5.131",
                            "group_id": group_id,
                            "user_id": user_id
                            })
                            result = resp.json()
                            is_member = result.get("response", 0) == 1
                        except Exception as e:
                            logger.error(f"Ошибка VK API: {e}")
                
                    if is_member:
                        c.execute("UPDATE clients SET vk_subscribed = TRUE WHERE phone = %s", (phone,))
                        if not client['vk_bonus_issued']:
                            c.execute("UPDATE clients SET total_discounts = total_discounts + 250, vk_bonus_issued = TRUE WHERE phone = %s", (phone,))
                            logger.info(f"✅ VK бонус 250 ₽ начислен через бота: {phone}")
                        else:
                            logger.info(f"ℹ️ VK подписка подтверждена для {phone}, бонус уже выдан")
                conn.commit()
            conn.close()
            return {"ok": True}

@app.post("/api/callback/telegram")
async def telegram_callback(data: TelegramCallbackRequest):
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT phone, tg_subscribed, tg_bonus_issued FROM clients WHERE phone = %s", (data.phone,))
    client = c.fetchone()
    if not client:
        conn.close()
        raise HTTPException(404, "Клиент не найден")
    
    if client['tg_subscribed'] and client['tg_bonus_issued']:
        conn.close()
        return {"status": "skipped", "reason": "already subscribed and bonus issued"}
    
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    channel_id = "-1002612715364"
    
    is_member = False
    if telegram_token and channel_id:
        try:
            resp = requests.get(f"https://api.telegram.org/bot{telegram_token}/getChatMember", params={
                "chat_id": channel_id,
                "user_id": data.tg_id
            })
            result = resp.json()
            status = result.get("result", {}).get("status", "")
            is_member = status in ("member", "administrator", "creator")
        except Exception as e:
            logger.error(f"Ошибка Telegram API: {e}")
    
    if is_member:
        c.execute("UPDATE clients SET tg_subscribed = TRUE, tg_id = %s WHERE phone = %s", (data.tg_id, data.phone))
        if not client['tg_bonus_issued']:
            c.execute("UPDATE clients SET total_discounts = total_discounts + 250, tg_bonus_issued = TRUE WHERE phone = %s", (data.phone,))
            logger.info(f"✅ Telegram бонус 250 ₽ начислен: {data.phone}")
        
        conn.commit()
        conn.close()
        return {"status": "success", "bonus": 250}
    
    c.execute("UPDATE clients SET tg_id = %s WHERE phone = %s", (data.tg_id, data.phone))
    conn.commit()
    conn.close()
    return {"status": "not_subscribed", "tg_id": data.tg_id}

@app.get("/api/client/subscription-status")
async def get_subscription_status(phone: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT vk_subscribed, tg_subscribed, vk_bonus_issued, tg_bonus_issued,
               vk_id, tg_id, total_discounts
        FROM clients WHERE phone = %s
    """, (phone,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        return {
            "vk_subscribed": False,
            "tg_subscribed": False,
            "vk_bonus_issued": False,
            "tg_bonus_issued": False,
            "vk_id": None,
            "tg_id": None,
            "total_discounts": 0
        }
    
    return {
        "vk_subscribed": row['vk_subscribed'],
        "tg_subscribed": row['tg_subscribed'],
        "vk_bonus_issued": row['vk_bonus_issued'],
        "tg_bonus_issued": row['tg_bonus_issued'],
        "vk_id": row['vk_id'],
        "tg_id": row['tg_id'],
        "total_discounts": row['total_discounts']
    }

@app.post("/api/mailing")
async def send_mailing(data: MailingRequest):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT phone, vk_id, tg_id FROM clients WHERE newsletter = TRUE")
    clients = c.fetchall()
    conn.close()
    
    sent_vk = 0
    sent_tg = 0
    skipped_no_id = []
    
    vk_token = os.getenv("VK_TOKEN", "")
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    
    for client in clients:
        phone = client['phone']
        vk_id = client['vk_id']
        tg_id = client['tg_id']
        
        if data.platform in ("vk", "both") and vk_id and vk_token:
            try:
                requests.post("https://api.vk.com/method/messages.send", params={
                    "access_token": vk_token,
                    "v": "5.131",
                    "user_id": vk_id,
                    "message": data.text,
                    "random_id": 0
                })
                sent_vk += 1
            except Exception as e:
                logger.error(f"VK ошибка для {phone}: {e}")
        
        if data.platform in ("telegram", "both") and tg_id and telegram_token:
            try:
                requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", json={
                    "chat_id": tg_id,
                    "text": data.text
                })
                sent_tg += 1
            except Exception as e:
                logger.error(f"Telegram ошибка для {phone}: {e}")
        
        if not vk_id and not tg_id:
            skipped_no_id.append(phone)
    
    return {
        "sent_vk": sent_vk,
        "sent_tg": sent_tg,
        "skipped_no_id": skipped_no_id,
        "total_newsletter": len(clients)
    }

# ===== API: Ручное редактирование бонусов (для админа) =====
@app.post("/api/client/bonus")
async def update_client_bonus(phone: str, amount: int):
    if amount < 0:
        raise HTTPException(400, "Сумма бонуса не может быть отрицательной")
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT phone FROM clients WHERE phone = %s", (phone,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(404, "Клиент не найден")
    
    c.execute("UPDATE clients SET total_discounts = %s WHERE phone = %s", (amount, phone))
    conn.commit()
    conn.close()
    
    logger.info(f"Админ вручную установил бонусы для {phone}: {amount} ₽")
    return {"status": "updated", "phone": phone, "new_total": amount}