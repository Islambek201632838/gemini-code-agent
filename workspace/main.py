from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List

import stats

app = FastAPI(
    title="Stats API",
    description="REST API для статистических функций из stats.py"
)


class NumbersInput(BaseModel):
    numbers: List[float]


class ClampInput(BaseModel):
    value: float
    low: float
    high: float


@app.get("/")
def read_root():
    return {"message": "Добро пожаловать в Stats API!"}


@app.post("/mean")
def calculate_mean(data: NumbersInput):
    if not data.numbers:
        raise HTTPException(status_code=400, detail="Список чисел не должен быть пустым")
    return {"result": stats.mean(data.numbers)}


@app.post("/median")
def calculate_median(data: NumbersInput):
    if not data.numbers:
        raise HTTPException(status_code=400, detail="Список чисел не должен быть пустым")
    return {"result": stats.median(data.numbers)}


@app.post("/clamp")
def calculate_clamp(data: ClampInput):
    return {"result": stats.clamp(data.value, data.low, data.high)}
