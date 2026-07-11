from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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
    logger.info("✅ База данных инициализирована")

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
    # Определяем тип заявки: есть ли поле tariff (интенсив) или event_id (событие)
    is_event = hasattr(data, 'event_id') and data.event_id is not None
    
    vk_token = os.getenv("VK_TOKEN", "")
    if vk_token:
        if is_event:
            # Сообщение для СОБЫТИЯ
            msg = f"📅 Новая заявка на событие #{booking_id}\n👤 {data.surname} {data.name}\n📞 {data.phone}\n📧 {data.email or 'не указан'}\n💰 Итог: {final_price} ₽"
        else:
            # Сообщение для ИНТЕНСИВА
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
    user_id = os.getenv("TELEGRAM_USER_ID", "")
    if telegram_token and chat_id and user_id:
        if is_event:
            tg_msg = f"📅 Новая заявка на событие #{booking_id}\n👤 {data.surname} {data.name}\n📞 {data.phone}\n📧 {data.email or 'не указан'}\n💰 Итог: {final_price} ₽"
        else:
            tg_msg = f"🔫 Новая заявка на интенсив #{booking_id}\n👤 {data.surname} {data.name}\n📞 {data.phone}\n🎯 {data.tariff}\n📅 {data.date} {data.time_slot}\n💰 Итог: {final_price} ₽"
        
        try:
            requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", json={"chat_id": chat_id, "text": tg_msg})
            requests.post(f"https://api.telegram.org/bot{telegram_token}/sendMessage", json={"chat_id": user_id, "text": tg_msg})
            logger.info("Telegram: уведомления администратору отправлены")
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
        max_bonus = int(instructor_price * 0.3)
        discount = min(total_discount, max_bonus)
    final_price = base_price + (instructor_price - discount)
    
    conn = get_db()
    c = conn.cursor()
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
    ''', (data.phone, data.surname, data.name, data.newsletter, data.referral, data.vk_id))
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
            
            # 1. Проверяем, не вписал ли этот человек самого себя (защита от дурака)
            if data.referral == data.phone:
                logger.warning(f"Пользователь {data.phone} попытался привести сам себя. Бонус не начислен.")
                conn.close()
            else:
                # 2. Проверяем, не вписал ли этот человек того, кто уже вписал его (эффект «зеркала»)
                c.execute("SELECT referrer FROM clients WHERE phone = %s", (data.referral,))
                row = c.fetchone()
                
                # Если у того, кого привели (referral), в поле referrer уже стоит номер текущего клиента (phone) — это зеркало
                if row and row['referrer'] == data.phone:
                    # Зеркало! Начисляем бонус ТОЛЬКО текущему клиенту (тому, кто реально отправил заявку первым)
                    c.execute("UPDATE clients SET total_discounts = total_discounts + 500 WHERE phone = %s", (data.phone,))
                    logger.info(f"Обнаружена зеркальная запись. Бонус 500 ₽ начислен только инициатору {data.phone}")
                else:
                    # Всё чисто, начисляем обоим
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

    if count >= min_persons:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE bookings SET status = 'confirmed' WHERE date = %s AND tariff = %s", (data.date, data.tariff))
        conn.commit()
        
        c.execute("SELECT phone FROM bookings WHERE date = %s AND tariff = %s", (data.date, data.tariff))
        phones = c.fetchall()
        conn.close()
        
        for row in phones:
            send_client_notification(row['phone'], data.tariff, data.date, data.time_slot, final_price)
    
    return {"id": booking_id, "status": "created", "finalPrice": final_price, "discount": discount}

@app.get("/api/client/status/{phone}")
async def get_client_status(phone: str):
    conn = get_db()
    c = conn.cursor()
    
    # 1. Проверяем, есть ли у клиента записи на ИНТЕНСИВ со статусом pending
    c.execute("SELECT COUNT(*) FROM bookings WHERE phone = %s AND status = 'pending'", (phone,))
    pending_bookings = c.fetchone()['count']
    
    # 2. Проверяем, есть ли у клиента записи на СОБЫТИЯ со статусом pending
    c.execute("SELECT COUNT(*) FROM event_bookings WHERE phone = %s AND status = 'pending'", (phone,))
    pending_events = c.fetchone()['count']
    
    # 3. Получаем данные клиента (визиты, опыт, бонусы)
    c.execute("SELECT visits, experienced, total_discounts FROM clients WHERE phone = %s", (phone,))
    row = c.fetchone()
    conn.close()
    
    # Если клиента вообще нет в базе (новый номер, никогда не записывался)
    if not row:
        return {"level": "newbie", "visits": 0, "bonus": 0, "message": "Вы ещё не были у нас. Записывайтесь!"}
    
    # Специальные сообщения, если человек уже записан, но группа не набралась
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
    
    # Стандартные сообщения для активных пользователей
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
             "referrer": r['referrer'], "vk_id": r['vk_id']} for r in rows]

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
    logger.info(f"📦 Данные: {event.dict()}")
    
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
        
        # Второстепенные действия (не ломают создание события)
        try:
            vk_token = os.getenv("VK_TOKEN", "")
            telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
            
            if vk_token:
                post_text = f"""🎯 НОВОЕ СОБЫТИЕ!

{event.title}
📅 {event.date} в {event.time}
📍 {event.location}
💰 {event.price} ₽
👥 Участников: {event.min_participants}–{event.max_participants}

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

    # Отправляем уведомление администратору о бронировании события
    send_admin_notification(booking_id, data, event['price'], 0)
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM event_bookings WHERE event_id = %s", (data.event_id,))
    count = c.fetchone()['count']
    c.execute("SELECT min_participants FROM events WHERE id = %s", (data.event_id,))
    min_participants = c.fetchone()['min_participants']
    conn.close()
    
    if count >= min_participants:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE event_bookings SET status = 'confirmed' WHERE event_id = %s", (data.event_id,))
        c.execute("UPDATE events SET status = 'confirmed' WHERE id = %s", (data.event_id,))
        conn.commit()
        conn.close()
        
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT phone FROM event_bookings WHERE event_id = %s", (data.event_id,))
        phones = c.fetchall()
        conn.close()
        
        for row in phones:
            send_event_client_notification(row['phone'], event['title'], event['date'], event['time'], event['location'], event['price'])
    
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