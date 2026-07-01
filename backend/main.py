from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
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
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except Exception as e:
        logger.error(f"Ошибка подключения к БД: {str(e)}")
        raise HTTPException(500, "Ошибка подключения к базе данных")

def init_db():
    conn = get_db()
    c = conn.cursor()
    
    c.execute('DROP TABLE IF EXISTS bookings CASCADE;')
    c.execute('DROP TABLE IF EXISTS clients CASCADE;')
    c.execute('DROP TABLE IF EXISTS dates CASCADE;')
    c.execute('DROP TABLE IF EXISTS config CASCADE;')
    
    c.execute('''
        CREATE TABLE clients (
            phone TEXT PRIMARY KEY,
            surname TEXT NOT NULL,
            name TEXT NOT NULL,
            visits INTEGER DEFAULT 0,
            experienced TEXT DEFAULT 'newbie',
            newsletter BOOLEAN DEFAULT FALSE,
            total_discounts INTEGER DEFAULT 0,
            last_visit TIMESTAMP,
            referrer TEXT
        );
    ''')
    
    c.execute('''
        CREATE TABLE bookings (
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (phone) REFERENCES clients(phone) ON DELETE CASCADE
        );
    ''')
    
    c.execute('''
        CREATE TABLE dates (
            id SERIAL PRIMARY KEY,
            value TEXT NOT NULL,
            label TEXT NOT NULL,
            group_id TEXT NOT NULL,
            time_slot TEXT,
            max_persons INTEGER DEFAULT 10,
            min_persons INTEGER DEFAULT 5
        );
    ''')
    
    c.execute('''
        CREATE TABLE config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    ''')
    
    c.execute('''
        UPDATE clients 
        SET experienced = 
            CASE 
                WHEN experienced = 'true' OR experienced = '1' THEN 'experienced'
                WHEN experienced = 'false' OR experienced = '0' THEN 'newbie'
                ELSE experienced
            END
        WHERE experienced NOT IN ('newbie', 'experienced', 'pro');
    ''')
    
    c.execute("SELECT key FROM config WHERE key = 'prices'")
    if not c.fetchone():
        c.execute("INSERT INTO config (key, value) VALUES ('prices', '{\"practice\":{\"base\":5000,\"instructor\":2000},\"basic\":{\"base\":5000,\"instructor\":3500},\"pro\":{\"base\":10000,\"instructor\":3500}}')")
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
            return {
                "practice": {"base": 5000, "instructor": 2000},
                "basic": {"base": 5000, "instructor": 3500},
                "pro": {"base": 10000, "instructor": 3500}
            }
    return {
        "practice": {"base": 5000, "instructor": 2000},
        "basic": {"base": 5000, "instructor": 3500},
        "pro": {"base": 10000, "instructor": 3500}
    }

# ===== API =====
@app.api_route("/api/config", methods=["GET", "HEAD"])
async def get_config():
    prices = get_prices()
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT * FROM dates")
    dates = c.fetchall()
    
    result_dates = []
    for d in dates:
        c.execute("SELECT COUNT(*) FROM bookings WHERE date = %s", (d['value'],))
        count = c.fetchone()['count']
        result_dates.append({
            "id": d['id'],
            "value": d['value'],
            "label": d['label'],
            "group": d['group_id'],
            "timeSlot": d['time_slot'],
            "maxPersons": d['max_persons'],
            "minPersons": d['min_persons'],
            "currentCount": count
        })
    
    conn.close()
    return {
        "prices": prices,
        "dates": result_dates
    }

@app.post("/api/booking")
async def create_booking(data: BookingRequest):
    if not data.surname or not data.name or not data.phone:
        raise HTTPException(400, "Заполните все обязательные поля")
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM dates WHERE value = %s", (data.date,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(400, "Выбранная дата не существует")
    conn.close()
    
    prices = get_prices()
    if data.tariff not in prices:
        raise HTTPException(400, "Неверный тариф")

    # === Разбиваем цену ===
    price = prices[data.tariff]
    base_price = price["base"]
    instructor_price = price["instructor"]

    # === Загружаем скидку ===
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT total_discounts FROM clients WHERE phone = %s", (data.phone,))
    client = c.fetchone()
    total_discount = client['total_discounts'] if client else 0
    conn.close()
    
    # === Скидка только на инструктора ===
    discount = min(total_discount, instructor_price)
    final_price = base_price + (instructor_price - discount)

    # === 1. Регистрируем клиента ===
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO clients (phone, surname, name, visits, experienced, newsletter, referrer)
        VALUES (%s, %s, %s, 1, 'newbie', %s, %s)
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
            referrer = COALESCE(EXCLUDED.referrer, clients.referrer)
    ''', (data.phone, data.surname, data.name, data.newsletter, data.referral))
    conn.commit()
    conn.close()

    # === Бонус за 5-е, 10-е, 15-е посещение ===
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

    # === 2. Создаём заявку и сразу получаем ID (исправлено для PostgreSQL) ===
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO bookings 
        (surname, name, phone, referral, tariff, date, time_slot, source, newsletter, discount, final_price)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    ''', (data.surname, data.name, data.phone, data.referral, data.tariff, data.date, data.time_slot,
          data.source, data.newsletter, discount, final_price))
    
    booking_id = c.fetchone()['id']  # Правильное получение ID для PostgreSQL
    conn.commit()
    conn.close()

    # === 3. Списание использованной скидки ===
    if discount > 0:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE clients SET total_discounts = total_discounts - %s WHERE phone = %s", (discount, data.phone))
        conn.commit()
        conn.close()

    # === 4. Начисление скидки за приведённого друга ===
    if data.referral:
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE clients SET total_discounts = total_discounts + 500 WHERE phone = %s", (data.phone,))
            c.execute("UPDATE clients SET total_discounts = total_discounts + 500 WHERE phone = %s", (data.referral,))
            conn.commit()
            conn.close()
            logger.info(f"Скидка 500 ₽ начислена обоим")

            # Уведомление привёдшему в VK
            vk_token = os.getenv("VK_TOKEN", "")
            if vk_token:
                msg = f"🎉 Поздравляем! Ваш друг {data.surname} {data.name} записался. Вы получили скидку 500 ₽!"
                try:
                    requests.post(
                        "https://api.vk.com/method/messages.send",
                        params={
                            "access_token": vk_token,
                            "v": "5.131",
                            "user_id": 304659962,
                            "message": msg,
                            "random_id": 0
                        }
                    )
                    logger.info("VK: уведомление о скидке отправлено привёдшему")
                except Exception as e:
                    logger.error(f"Ошибка VK: {str(e)}", exc_info=True)

            # Уведомление привёдшему в Telegram
            telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
            user_id = os.getenv("TELEGRAM_USER_ID", "")
            if telegram_token and chat_id and user_id:
                tg_msg = f"🎉 Поздравляем! Ваш друг {data.surname} {data.name} записался. Вы получили скидку 500 ₽!"
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                        json={"chat_id": chat_id, "text": tg_msg}
                    )
                    requests.post(
                        f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                        json={"chat_id": user_id, "text": tg_msg}
                    )
                    logger.info("Telegram: уведомление о скидке отправлено")
                except Exception as e:
                    logger.error(f"Ошибка Telegram: {str(e)}", exc_info=True)
        except Exception as e:
            logger.error(f"Ошибка начисления скидки другу: {str(e)}", exc_info=True)

    # === 5. Уведомление админу о новой заявке (с правильным ID) ===
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
            requests.post(
                "https://api.vk.com/method/messages.send",
                params={
                    "access_token": vk_token,
                    "v": "5.131",
                    "user_id": 304659962,
                    "message": msg,
                    "random_id": 0
                }
            )
            logger.info("VK: сообщение администратору отправлено")
        except Exception as e:
            logger.error(f"Ошибка VK админу: {str(e)}", exc_info=True)
            
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    user_id = os.getenv("TELEGRAM_USER_ID", "")
    if telegram_token and chat_id and user_id:
        tg_msg = f"""
🔫 Новая заявка #{booking_id}
👤 {data.surname} {data.name}
📞 {data.phone}
🎯 {data.tariff}
📅 {data.date} {data.time_slot}
💰 Итог: {final_price} ₽
        """
        try:
            requests.post(
                f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                json={"chat_id": chat_id, "text": tg_msg}
            )
            requests.post(
                f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                json={"chat_id": user_id, "text": tg_msg}
            )
            logger.info("Telegram: уведомления администратору отправлены")
        except Exception as e:
            logger.error(f"Ошибка Telegram админу: {str(e)}", exc_info=True)

    return {"id": booking_id, "status": "created", "finalPrice": final_price, "discount": discount}

@app.post("/api/prices")
async def update_prices(data: PriceUpdate):
    # Проверяем, что цены неотрицательные и структура правильная
    for key in ["practice", "basic", "pro"]:
        if data.dict()[key]["base"] < 0 or data.dict()[key]["instructor"] < 0:
            raise HTTPException(400, "Цены не могут быть отрицательными")
    
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE config SET value = %s WHERE key = 'prices'", 
              (json.dumps({
                  "practice": data.practice,
                  "basic": data.basic,
                  "pro": data.pro
              }),))
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
             "referrer": r['referrer']} for r in rows]

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