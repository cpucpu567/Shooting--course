from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import os
import requests
from datetime import datetime

app = FastAPI(title="Стрелковый интенсив API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def init_db():
    conn = sqlite3.connect('shooting.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            surname TEXT NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            referral TEXT,
            tariff TEXT NOT NULL,
            date TEXT NOT NULL,
            time_slot TEXT,
            source TEXT,
            newsletter BOOLEAN DEFAULT 0,
            discount INTEGER DEFAULT 0,
            final_price INTEGER NOT NULL,
            status TEXT DEFAULT 'new',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS dates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    c.execute("SELECT key FROM config WHERE key = 'prices'")
    if not c.fetchone():
        c.execute("INSERT INTO config (key, value) VALUES ('prices', '{\"practice\":7000,\"basic\":8500,\"pro\":13500}')")
    conn.commit()
    conn.close()

init_db()

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

def get_prices():
    conn = sqlite3.connect('shooting.db')
    c = conn.cursor()
    c.execute("SELECT value FROM config WHERE key = 'prices'")
    row = c.fetchone()
    conn.close()
    return eval(row[0]) if row else {"practice": 7000, "basic": 8500, "pro": 13500}

@app.get("/api/config")
async def get_config():
    prices = get_prices()
    conn = sqlite3.connect('shooting.db')
    c = conn.cursor()
    c.execute("SELECT * FROM dates")
    dates = c.fetchall()
    conn.close()
    return {
        "prices": prices,
        "dates": [{"value": r[1], "label": r[2], "group": r[3], "timeSlot": r[4],
                   "maxPersons": r[5], "minPersons": r[6]} for r in dates]
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

    conn = sqlite3.connect('shooting.db')
    c = conn.cursor()
    c.execute('''
        INSERT INTO bookings 
        (surname, name, phone, referral, tariff, date, time_slot, source, newsletter, discount, final_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (data.surname, data.name, data.phone, data.referral, data.tariff, data.date, data.time_slot,
          data.source, int(data.newsletter), discount, final_price))
    booking_id = c.lastrowid
    conn.commit()
    conn.close()

    # ===== Отправка в VK (в сообщения сообщества) =====
    vk_token = os.getenv("VK_TOKEN", "")
    vk_group_id = os.getenv("VK_GROUP_ID", "")
    if vk_token and vk_group_id:
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
                    "peer_id": -int(vk_group_id),
                    "message": msg,
                    "random_id": 0
                }
            )
        except:
            pass

    return {"id": booking_id, "status": "created", "finalPrice": final_price, "discount": discount}

@app.post("/api/prices")
async def update_prices(data: PriceUpdate):
    conn = sqlite3.connect('shooting.db')
    c = conn.cursor()
    c.execute("UPDATE config SET value = ? WHERE key = 'prices'", 
              (str({"practice": data.practice, "basic": data.basic, "pro": data.pro}),))
    conn.commit()
    conn.close()
    return {"status": "updated"}

@app.post("/api/dates")
async def add_date(date: DateItem):
    conn = sqlite3.connect('shooting.db')
    c = conn.cursor()
    c.execute('''
        INSERT INTO dates (value, label, group_id, time_slot, max_persons, min_persons)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (date.value, date.label, date.group_id, date.time_slot, date.max_persons, date.min_persons))
    conn.commit()
    conn.close()
    return {"status": "added"}

@app.get("/api/bookings")
async def get_bookings():
    conn = sqlite3.connect('shooting.db')
    c = conn.cursor()
    c.execute("SELECT * FROM bookings ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "surname": r[1], "name": r[2], "phone": r[3], "tariff": r[5],
             "date": r[6], "timeSlot": r[7], "finalPrice": r[11], "status": r[12], "createdAt": r[13]}
            for r in rows]
