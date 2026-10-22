"""Junta as peças: perímetro -> imagens -> talhões."""
from __future__ import annotations

import time

import numpy as np
from shapely.geometry import mapping, shape

from . import car, imagery, segmentation


class SemImagem(Exception):
    pass


def processar(geom_wgs, meses: int = 12, area_min_ha: float = 2.0, max_cenas: int = 5,
              limiar_fusao: float = 0.7, info_imovel: dict | None = None,
              devolver_cenas: bool = False):
    t0 = time.time()
    grade = imagery.montar_grade(geom_wgs)
    if grade.width * grade.height > 4_000_000:
        raise ValueError("Área grande demais para o modo de teste (limite ~40.000 ha).")
    itens = imagery.buscar_cenas(geom_wgs, meses=meses, max_cenas=max_cenas)
    cenas = imagery.carregar(geom_wgs, grade, itens, max_cenas=max_cenas)
    if not cenas:
        raise SemImagem("Nenhuma imagem Sentinel-2 limpa cobre a área no período.")
    fc = segmentacao = segmentation.segmentar(
        cenas, grade, geom_wgs, area_min_ha=area_min_ha, limiar_fusao=limiar_fusao
    )
    props = fc["features"]
    fc["metadata"] = {
        "imovel": info_imovel,
        "area_total_ha": round(sum(f["properties"]["area_ha"] for f in props), 2),
        "qtd_talhoes": sum(1 for f in props if f["properties"]["classe"] == "talhão"),
        "qtd_outras_areas": sum(1 for f in props if f["properties"]["classe"] != "talhão"),
        "cenas": [
            {"data": c.data, "id": c.item_id, "nuvem_cena_pct": round(c.nuvem_cena, 2),
             "pixels_validos_pct": round(100 * c.validos_aoi, 1)}
            for c in cenas
        ],
        "fonte_imagem": "Sentinel-2 L2A (ESA/Copernicus) via Earth Search AWS, 10 m",
        "fonte_perimetro": "SICAR (geoserver.car.gov.br)" if info_imovel else "geometria enviada",
        "parametros": {"meses": meses, "area_min_ha": area_min_ha, "limiar_fusao": limiar_fusao},
        "tempo_s": round(time.time() - t0, 1),
    }
    if devolver_cenas:
        return fc, cenas, grade
    return fc


def por_car(codigo: str, **kw):
    im = car.buscar_imovel(codigo)
    info = {k: im["properties"].get(k) for k in
            ("cod_imovel", "municipio", "uf", "area", "status_imovel", "condicao", "tipo_imovel")}
    info["perimetro"] = mapping(im["geometry"])
    return processar(im["geometry"], info_imovel=info, **kw)


def por_geometria(geojson_geom: dict, **kw):
    return processar(shape(geojson_geom), **kw)


def rgb_png(cena, grade, caminho: str, ganho: float = 3.2):
    """Gera um PNG em cor natural da cena (para usar de fundo no mapa)."""
    from PIL import Image

    rgb = np.stack([cena.red, cena.green, cena.blue], axis=-1)
    rgb = np.clip((rgb * ganho) ** (1 / 1.25), 0, 1)
    Image.fromarray((rgb * 255).astype("uint8")).save(caminho)
    # Cantos em lon/lat para sobrepor no mapa
    t = grade.transform
    x0, y0 = t.c, t.f
    x1, y1 = x0 + grade.width * t.a, y0 + grade.height * t.e
    lon0, lat0 = grade.to_wgs.transform(x0, y0)
    lon1, lat1 = grade.to_wgs.transform(x1, y1)
    return [[lat1, lon0], [lat0, lon1]]
