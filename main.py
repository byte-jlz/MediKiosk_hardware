from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pymongo import MongoClient
from datetime import datetime, timezone
from dotenv import load_dotenv
import os

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = MongoClient(os.getenv("MONGO_URL"))
db = client[os.getenv("DB_NAME", "medikiosk")]
collection = db["measurements"]


class Measurement(BaseModel):
    height_cm: float | None = None
    weight_g: float | None = None
    heart_rate_bpm: float | None = None
    spo2_percent: float | None = None


@app.get("/")
def dashboard():
    return FileResponse("dashboard.html")


@app.post("/measurements")
def save_measurement(data: Measurement):
    doc = data.model_dump()
    doc["timestamp"] = datetime.now(timezone.utc)
    result = collection.insert_one(doc)
    return {"id": str(result.inserted_id), "timestamp": doc["timestamp"]}


@app.get("/measurements")
def get_measurements(limit: int = 10):
    docs = list(collection.find().sort("timestamp", -1).limit(limit))
    for doc in docs:
        doc["_id"] = str(doc["_id"])
    return docs


@app.get("/measurements/latest")
def get_latest():
    doc = collection.find_one(sort=[("timestamp", -1)])
    if not doc:
        raise HTTPException(status_code=404, detail="No measurements found")
    doc["_id"] = str(doc["_id"])
    return doc
