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
    
    # === Удаляем старые таблицы ===
    c.execute('DROP TABLE IF EXISTS bookings CASCADE;')
    c.execute('DROP TABLE IF EXISTS clients CASCADE;')
    c.execute('DROP TABLE IF EXISTS dates CASCADE;')
    c.execute('DROP TABLE IF EXISTS config CASCADE;')
    
    # === Таблица клиентов (с referrer) ===
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
    
    # === Таблица заявок ===
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
    
    # === Таблица дат ===
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
    
    # === Таблица цен ===
    c.execute('''
        CREATE TABLE config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    ''')
    
    # === Миграция старых статусов ===
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
    
    prices = get_prices()
    if data.tariff not in prices:
        raise HTTPException(400, "Неверный тариф")

    discount = 0
    if data.referral:
        discount += 500

    final_price = max(0, prices[data.tariff] - discount)

    # 1. Регистрируем/обновляем клиента
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

    # 2. Создаём заявку
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

    # 3. Если пришёл друг — начисляем скидку обоим и отправляем уведомления
    if data.referral:
        try:
            conn = get_db()
            c = conn.cursor()
            # Текущему (новому) клиенту — скидка на следующее занятие
            c.execute("UPDATE clients SET total_discounts = total_discounts + 500 WHERE phone = %s", (data.phone,))
            # Привёдшему — скидка на следующее занятие
            c.execute("UPDATE clients SET total_discounts = total_discounts + 500 WHERE phone = %s", (data.referral,))
            conn.commit()
            conn.close()
            logger.info(f"Скидка 500 ₽ начислена обоим")

            # === Уведомление привёдшему (в VK) ===
            vk_token = os.getenv("VK_TOKEN", "")
            if vk_token:
                msg = f"🎉 Поздравляем! Ваш друг {data.surname} {data.name} записался на занятие. Вы получили скидку 500 ₽ на следующее посещение!"
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
                    logger.error(f"Ошибка при отправке уведомления о скидке в VK: {str(e)}")

            # === Уведомление привёдшему (в Telegram) ===
            telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
            user_id = os.getenv("TELEGRAM_USER_ID", "")
            if telegram_token and chat_id and user_id:
                tg_msg = f"🎉 Поздравляем! Ваш друг {data.surname} {data.name} записался на занятие. Вы получили скидку 500 ₽ на следующее посещение!"
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                        json={"chat_id": chat_id, "text": tg_msg}
                    )
                    requests.post(
                        f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                        json={"chat_id": user_id, "text": tg_msg}
                    )
                    logger.info("Telegram: уведомление о скидке отправлено привёдшему")
                except Exception as e:
                    logger.error(f"Ошибка при отправке уведомления о скидке в Telegram: {str(e)}")
        except Exception as e:
            logger.error(f"Ошибка при начислении скидки: {str(e)}")

    # ===== Отправка в VK (админу) =====
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
            logger.info("VK: сообщение отправлено")
        except Exception as e:
            logger.error(f"Ошибка при отправке в VK: {str(e)}")
            
    # ===== Отправка в Telegram (в канал и админу) =====
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
            logger.info("Telegram: уведомления отправлены")
        except Exception as e:
            logger.error(f"Ошибка при отправке в Telegram: {str(e)}")

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