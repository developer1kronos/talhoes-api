"""Baixa recortes Sentinel-2 L2A (10 m, gratuito) do catálogo Earth Search (AWS).

Nenhuma chave de API é necessária: os arquivos são COGs públicos e o rasterio
lê só o pedacinho que cobre a fazenda.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import rasterio
from pystac_client import Client
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from shapely.geometry import mapping
from shapely.ops import transform as shp_transform

STAC_URL = "https://earth-search.aws.element84.com/v1"
COLLECTION = "sentinel-2-l2a"
RES = 10.0  # metros

# Configuração para ler COGs por HTTP sem listar diretórios
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "3")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "1")
if os.path.exists("/root/.ccr/ca-bundle.crt"):  # ambiente com proxy (não afeta sua máquina)
    os.environ.setdefault("CURL_CA_BUNDLE", "/root/.ccr/ca-bundle.crt")

# SCL (Scene Classification): 4 vegetação, 5 solo exposto, 6 água, 7 não classificado
SCL_VALIDOS = {4, 5, 6, 7}


@dataclass
class Grade:
    crs: CRS
    transform: rasterio.Affine
    width: int
    height: int
    to_utm: Transformer = field(repr=False)
    to_wgs: Transformer = field(repr=False)


@dataclass
class Cena:
    data: str
    item_id: str
    nuvem_cena: float
    validos_aoi: float
    red: np.ndarray
    green: np.ndarray
    blue: np.ndarray
    nir: np.ndarray
    valido: np.ndarray


def utm_crs(lon: float, lat: float) -> CRS:
    zona = int((lon + 180) // 6) + 1
    return CRS.from_epsg((32700 if lat < 0 else 32600) + zona)


def montar_grade(geom_wgs, buffer_m: float = 60) -> Grade:
    c = geom_wgs.centroid
    crs = utm_crs(c.x, c.y)
    to_utm = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    g = shp_transform(to_utm.transform, geom_wgs)
    minx, miny, maxx, maxy = g.buffer(buffer_m).bounds
    minx, miny = np.floor(minx / RES) * RES, np.floor(miny / RES) * RES
    maxx, maxy = np.ceil(maxx / RES) * RES, np.ceil(maxy / RES) * RES
    w, h = int((maxx - minx) / RES), int((maxy - miny) / RES)
    return Grade(crs, from_origin(minx, maxy, RES, RES), w, h, to_utm, to_wgs)


def _ler(href: str, grade: Grade, resampling=Resampling.bilinear) -> np.ndarray:
    with rasterio.open(href) as src:
        with WarpedVRT(
            src,
            crs=grade.crs,
            transform=grade.transform,
            width=grade.width,
            height=grade.height,
            resampling=resampling,
        ) as vrt:
            return vrt.read(1)


def buscar_cenas(geom_wgs, meses: int = 12, max_nuvem: float = 20, max_cenas: int = 5,
                 intervalo_min_dias: int = 35) -> list:
    """Escolhe as cenas mais limpas, espalhadas no tempo.

    Várias datas ajudam: dois talhões vizinhos podem estar iguais num mês e
    bem diferentes em outro (plantio/colheita em épocas diferentes).
    """
    fim = date.today()
    ini = fim - timedelta(days=30 * meses)
    cat = Client.open(STAC_URL)
    busca = cat.search(
        collections=[COLLECTION],
        intersects=mapping(geom_wgs),
        datetime=f"{ini.isoformat()}/{fim.isoformat()}",
        query={"eo:cloud_cover": {"lt": max_nuvem}},
        max_items=200,
    )
    itens = sorted(busca.items(), key=lambda i: i.properties.get("eo:cloud_cover", 100))
    escolhidos = []
    for it in itens:
        d = it.datetime.date()
        if all(abs((d - e.datetime.date()).days) >= intervalo_min_dias for e in escolhidos):
            escolhidos.append(it)
        if len(escolhidos) >= max_cenas * 2:  # pega folga; descartamos as sujas sobre a AOI
            break
    return escolhidos


def carregar(geom_wgs, grade: Grade, itens, max_cenas: int = 5, min_validos: float = 0.9) -> list[Cena]:
    cenas: list[Cena] = []
    for it in itens:
        a = it.assets
        try:
            scl = _ler(a["scl"].href, grade, Resampling.nearest)
        except Exception:
            continue
        valido = np.isin(scl, list(SCL_VALIDOS))
        frac = float(valido.mean())
        if frac < min_validos:
            continue
        bandas = {}
        for nome in ("red", "green", "blue", "nir"):
            arr = _ler(a[nome].href, grade).astype("float32")
            # Baseline >= 04.00 soma +1000 nos números. O Earth Search já desconta
            # isso nos arquivos (earthsearch:boa_offset_applied = True), então só
            # aplicamos o offset quando ele NÃO foi aplicado ainda.
            rb = a[nome].extra_fields.get("raster:bands", [{}])[0]
            scale = rb.get("scale", 1e-4) or 1e-4
            ja_aplicado = it.properties.get("earthsearch:boa_offset_applied", False)
            off = 0 if ja_aplicado else (rb.get("offset", 0) or 0)
            bandas[nome] = np.clip(arr * scale + off, 0, 1)
        cenas.append(
            Cena(
                data=it.datetime.date().isoformat(),
                item_id=it.id,
                nuvem_cena=float(it.properties.get("eo:cloud_cover", -1)),
                validos_aoi=frac,
                valido=valido,
                **bandas,
            )
        )
        if len(cenas) >= max_cenas:
            break
    cenas.sort(key=lambda c: c.data)
    return cenas
