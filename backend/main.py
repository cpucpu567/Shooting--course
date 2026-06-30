from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
import logging
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Стрелковый интенсив API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== Подключение к PostgreSQL =====
DATABASE_URL = os.getenv("DATABASE_URL")
logger.info(f"DATABASE_URL: {DATABASE_URL}")

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db()
    c = conn.cursor()
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
            status TEXT DEFAULT 'new',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS dates (
            id SERIAL PRIMARY KEY,
            value TEXT NOT NULL,
            label TEXT NOT NULL,
            group_id TEXT NOT NULL,
            time_slot TEXT,
            max_persons INTEGER DEFAULT 10,
            min_persons INTEGER DEFAULT 5
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
          # === Удаляем старую таблицу клиентов, если она есть, и создаём новую ===
    c.execute('''
    DROP TABLE IF EXISTS clients CASCADE;

    CREATE TABLE clients (
        phone TEXT PRIMARY KEY,
        surname TEXT NOT NULL,
        name TEXT NOT NULL,
        visits INTEGER DEFAULT 0,
        experienced TEXT DEFAULT 'newbie',
        newsletter BOOLEAN DEFAULT FALSE,
        total_discounts INTEGER DEFAULT 0,
        last_visit TIMESTAMP
    );
''')
    c.execute("SELECT key FROM config WHERE key = 'prices'")
    if not c.fetchone():
        c.execute("INSERT INTO config (key, value) VALUES ('prices', '{\"practice\":7000,\"basic\":8500,\"pro\":13500}')")
    conn.commit()
    conn.close()

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

class PriceUpdate(BaseModel):
    practice: int
    basic: int
    pro: int

class DateItem(BaseModel):
    value: str
    label: str
    group_id: str
    time_slot: str = "full"
    max_persons: int = 10
    min_persons: int = 5

# ===== Вспомогательные функции =====
def get_prices():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key = 'prices'")
    row = c.fetchone()
    conn.close()
    return eval(row['value']) if row else {"practice": 7000, "basic": 8500, "pro": 13500}

# ===== API =====
@app.get("/api/config")
async def get_config():
    prices = get_prices()
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM dates")
    dates = c.fetchall()
    conn.close()
    return {
        "prices": prices,
        "dates": [{"value": r['value'], "label": r['label'], "group": r['group_id'], "timeSlot": r['time_slot'],
                   "maxPersons": r['max_persons'], "minPersons": r['min_persons']} for r in dates]
    }

@app.post("/api/booking")
async def create_booking(data: BookingRequest):
    if not data.surname or not data.name or not data.phone:
        raise HTTPException(400, "Заполните все обязательные поля")
    
    prices = get_prices()
    if data.tariff not in prices:
        raise HTTPException(400, "Неверный тариф")

    discount = 0
    if data.referral:
        discount += 500

    final_price = max(0, prices[data.tariff] - discount)

    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO bookings 
        (surname, name, phone, referral, tariff, date, time_slot, source, newsletter, discount, final_price)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ''', (data.surname, data.name, data.phone, data.referral, data.tariff, data.date, data.time_slot,
          data.source, data.newsletter, discount, final_price))
    booking_id = c.lastrowid
    conn.commit()
    conn.close()

    # ===== Регистрируем/обновляем клиента (новая логика статусов) =====
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO clients (phone, surname, name, visits, experienced, newsletter)
        VALUES (%s, %s, %s, 1, 'newbie', %s)
        ON CONFLICT (phone) DO UPDATE SET
            surname = EXCLUDED.surname,
            name = EXCLUDED.name,
            visits = clients.visits + 1,
            experienced = CASE 
                WHEN clients.visits + 1 >= 4 THEN 'pro' 
                WHEN clients.visits + 1 >= 2 THEN 'experienced' 
                ELSE 'newbie' 
            END,
            newsletter = EXCLUDED.newsletter
    ''', (data.phone, data.surname, data.name, data.newsletter))
    conn.commit()
    conn.close()

    # ===== Отправка в VK (в личные сообщения администратору) =====
    vk_token = os.getenv("VK_TOKEN", "")
    if vk_token:
        msg = f"""
🔫 Новая заявка #{booking_id}
👤 {data.surname} {data.name}
📞 {data.phone}
🎯 {data.tariff}
📅 {data.date} {data.time_slot}
💰 Итог: {final_price} ₽
📱 Источник: {data.source or 'не указан'}
        """
        try:
            response = requests.post(
                "https://api.vk.com/method/messages.send",
                params={
                    "access_token": vk_token,
                    "v": "5.131",
                    "user_id": 304659962,
                    "message": msg,
                    "random_id": 0
                }
            )
            if response.status_code != 200:
                logger.error(f"VK ответил с кодом {response.status_code}: {response.text}")
            else:
                result = response.json()
                if 'error' in result:
                    logger.error(f"VK вернул ошибку: {result['error']}")
                else:
                    logger.info("VK: сообщение успешно отправлено")
        except Exception as e:
            logger.error(f"Ошибка при отправке в VK: {str(e)}")

    return {"id": booking_id, "status": "created", "finalPrice": final_price, "discount": discount}

@app.post("/api/prices")
async def update_prices(data: PriceUpdate):
    if data.practice < 0 or data.basic < 0 or data.pro < 0:
        raise HTTPException(400, "Цены не могут быть отрицательными")
    
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE config SET value = %s WHERE key = 'prices'", 
              (str({"practice": data.practice, "basic": data.basic, "pro": data.pro}),))
    conn.commit()
    conn.close()
    return {"status": "updated"}

@app.post("/api/dates")
async def add_date(date: DateItem):
    logger.info(f"Попытка добавить дату: {date}")
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            INSERT INTO dates (value, label, group_id, time_slot, max_persons, min_persons)
            VALUES (%s, %s, %s, %s, %s, %s)
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
    logger.info(f"Попытка удалить дату с id={id}")
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM dates WHERE id = %s", (id,))
        conn.commit()
        conn.close()
        logger.info(f"Дата с id={id} удалена")
        return {"status": "deleted"}
    except Exception as e:
        logger.error(f"Ошибка при удалении даты: {str(e)}")
        raise HTTPException(500, f"Ошибка базы данных: {str(e)}")
        
@app.get("/api/bookings")
async def get_bookings():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM bookings ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return [{"id": r['id'], "surname": r['surname'], "name": r['name'], "phone": r['phone'], "tariff": r['tariff'],
             "date": r['date'], "timeSlot": r['time_slot'], "finalPrice": r['final_price'], "status": r['status'],
             "createdAt": r['created_at']} for r in rows]
@app.get("/api/clients")
async def get_clients():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM clients ORDER BY surname, name")
    rows = c.fetchall()
    conn.close()
    return [{"phone": r['phone'], "surname": r['surname'], "name": r['name'], "visits": r['visits'], 
             "experienced": r['experienced'], "newsletter": r['newsletter'], 
             "totalDiscounts": r['total_discounts'], "lastVisit": r['last_visit']} for r in rows]

@app.post("/api/clients/{phone}")
async def update_client_status(phone: str, data: dict):
    new_status = data.get("experienced")
    if new_status not in ["newbie", "experienced", "pro"]:
        raise HTTPException(400, "Неверный статус")
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE clients SET experienced = %s WHERE phone = %s", (new_status, phone))
    conn.commit()
    conn.close()
    return {"status": "updated"}

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
        if visits >= 2:
            return {"allow": True, "message": "Практика доступна (после 2-х посещений)"}
        else:
            return {"allow": False, "message": "❌ Практика требует минимум 2 посещения (сначала Базовый)"}
    elif tariff == 'pro':
        if visits >= 4:
            return {"allow": True, "message": "Расширенный курс доступен (после 4-х посещений)"}
        else:
            return {"allow": False, "message": "❌ Расширенный курс требует минимум 4 посещения (Базовый + 3 Практики)"}
    return {"allow": False, "message": "Неизвестный тариф"}