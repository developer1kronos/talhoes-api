"""API de talhões.

GET  /talhoes?car=SP-3543402-...        -> perímetro do CAR + talhões (GeoJSON)
POST /talhoes  {"geometry": {...}}      -> talhões dentro de um polígono qualquer
GET  /                                  -> página de teste com mapa
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import car, pipeline

app = FastAPI(title="API de Talhões", version="0.1.0",
              description="Delimitação automática de talhões via CAR + Sentinel-2")


class Parametros(BaseModel):
    meses: int = Field(12, ge=3, le=24, description="Janela de imagens (meses para trás)")
    area_min_ha: float = Field(2.0, ge=0.5, le=50, description="Menor talhão aceito")
    sensibilidade: float = Field(
        0.7, ge=0.2, le=2.0,
        description="Limiar de fusão. Menor = mais talhões (divide mais); maior = menos talhões",
    )


class PedidoGeometria(Parametros):
    geometry: dict = Field(..., description="Polygon ou MultiPolygon GeoJSON em EPSG:4326")


def _erro(e: Exception):
    if isinstance(e, ValueError):
        raise HTTPException(422, str(e))
    if isinstance(e, car.CarNotFound):
        raise HTTPException(404, str(e))
    if isinstance(e, pipeline.SemImagem):
        raise HTTPException(409, str(e))
    raise HTTPException(502, f"Falha ao consultar fonte externa: {e}")


@app.get("/talhoes")
async def talhoes_por_car(
    car_codigo: str = Query(..., alias="car", examples=["SP-3543402-188EDB67444F4A428CDAC2174BDFDD9D"]),
    meses: int = Query(12, ge=3, le=24),
    area_min_ha: float = Query(2.0, ge=0.5, le=50),
    sensibilidade: float = Query(0.7, ge=0.2, le=2.0),
):
    try:
        return await run_in_threadpool(
            pipeline.por_car, car_codigo, meses=meses, area_min_ha=area_min_ha,
            limiar_fusao=sensibilidade,
        )
    except Exception as e:  # noqa: BLE001
        _erro(e)


@app.post("/talhoes")
async def talhoes_por_geometria(p: PedidoGeometria):
    try:
        return await run_in_threadpool(
            pipeline.por_geometria, p.geometry, meses=p.meses, area_min_ha=p.area_min_ha,
            limiar_fusao=p.sensibilidade,
        )
    except Exception as e:  # noqa: BLE001
        _erro(e)


@app.get("/", response_class=HTMLResponse)
def pagina_teste():
    return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
