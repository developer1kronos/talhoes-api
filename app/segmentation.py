"""Divide a fazenda em talhões a partir das imagens Sentinel-2.

Ideia (versão "explica pra criança"):
1. Cada pixel vira uma ficha com várias notas: o quão verde ele estava em cada
   data (NDVI) e a cor dele.
2. Onde a ficha muda de repente de um pixel pro vizinho, tem uma "parede"
   (estrada, carreador, cerca, mudança de cultura). Isso é o gradiente.
3. Jogamos "água" nos vales do gradiente (watershed): cada poça vira uma
   regiãozinha.
4. Juntamos regiõezinhas vizinhas quando a parede entre elas é baixa.
5. O que sobrar e for grande o bastante vira talhão; viramos polígono.
"""
from __future__ import annotations

import numpy as np
from rasterio import features
from scipy import ndimage as ndi
from shapely.geometry import shape, mapping
from shapely.ops import transform as shp_transform, unary_union
from skimage import filters, graph, morphology, segmentation

from .imagery import Cena, Grade

AREA_PIXEL_HA = 0.01  # 10 m x 10 m = 100 m² = 0,01 ha


def _ndvi(c: Cena) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        v = (c.nir - c.red) / (c.nir + c.red)
    v[~c.valido] = np.nan
    return v


def _empilhar(cenas: list[Cena], dentro: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Retorna (features HxWxC normalizadas, ndvi HxWxT)."""
    ndvis = np.stack([_ndvi(c) for c in cenas], axis=-1)
    camadas = []
    for i, c in enumerate(cenas):
        for arr, peso in ((ndvis[..., i], 2.0), (c.red, 1.0), (c.nir, 1.0), (c.green, 0.5)):
            a = arr.astype("float32").copy()
            a[~c.valido] = np.nan
            camadas.append((a, peso))
    norm = []
    for a, peso in camadas:
        ref = a[dentro] if dentro is not None else a
        mu, sd = np.nanmedian(ref), np.nanstd(ref)
        sd = max(float(sd), 1e-3)
        a = np.where(np.isnan(a), mu, a)  # nuvem/sem dado -> valor "neutro"
        norm.append((a - mu) / sd * peso)
    F = np.stack(norm, axis=-1).astype("float32")
    return F, ndvis


def _gradiente(F: np.ndarray) -> np.ndarray:
    g2 = np.zeros(F.shape[:2], dtype="float32")
    for k in range(F.shape[-1]):
        b = filters.gaussian(F[..., k], sigma=0.8, preserve_range=True)
        g2 += filters.sobel(b) ** 2
    g = np.sqrt(g2 / F.shape[-1])
    return g / (np.percentile(g, 99) + 1e-6)


ALFA_BORDA = 0.6  # quanto a "parede" (estrada/carreador) pesa vs. a diferença de conteúdo


def _custo(g, a, b, borda):
    """Custo de fundir duas regiões: diferença do 'jeitão' médio (NDVI por data,
    cores) + um pouco da força da divisa entre elas. Custo baixo = mesmo talhão."""
    ma = g.nodes[a]["soma"] / g.nodes[a]["n"]
    mb = g.nodes[b]["soma"] / g.nodes[b]["n"]
    dif = float(np.sqrt(np.mean((ma - mb) ** 2)))
    return dif + ALFA_BORDA * borda


def _peso_fusao(g, src, dst, n):
    vazio = {"borda": 0.0, "count": 0}
    a, b = g[src].get(n, vazio), g[dst].get(n, vazio)
    cont = a["count"] + b["count"]
    borda = (a["count"] * a["borda"] + b["count"] * b["borda"]) / cont
    return {"count": cont, "borda": borda, "weight": _custo(g, dst, n, borda)}


def _fundir_nos(g, src, dst):
    g.nodes[dst]["soma"] = g.nodes[dst]["soma"] + g.nodes[src]["soma"]
    g.nodes[dst]["n"] += g.nodes[src]["n"]


def segmentar(cenas: list[Cena], grade: Grade, geom_wgs, area_min_ha: float = 2.0,
              limiar_fusao: float = 0.7) -> dict:
    # Máscara do imóvel (CAR) na grade UTM
    geom_utm = shp_transform(grade.to_utm.transform, geom_wgs)
    dentro = features.rasterize(
        [(mapping(geom_utm), 1)], out_shape=(grade.height, grade.width),
        transform=grade.transform, fill=0, dtype="uint8",
    ).astype(bool)

    F, ndvis = _empilhar(cenas, dentro)
    grad = _gradiente(F)

    # 1) Superpixels por watershed a partir dos "vales" do gradiente
    suave = filters.gaussian(grad, sigma=1.0)
    minimos = morphology.h_minima(suave, 0.015)
    marcadores, _ = ndi.label(minimos)
    rotulos = segmentation.watershed(suave, marcadores, mask=dentro)

    # 2) Funde vizinhos parecidos e sem "parede" forte entre eles
    rag = graph.rag_boundary(rotulos, grad)
    if 0 in rag:
        rag.remove_node(0)  # fora do imóvel
    ids = np.array(sorted(rag.nodes))
    somas = np.stack([ndi.sum(F[..., k], rotulos, index=ids) for k in range(F.shape[-1])], -1)
    contagens = ndi.sum(np.ones_like(rotulos), rotulos, index=ids)
    for i, nid in enumerate(ids):
        rag.nodes[nid]["soma"] = somas[i]
        rag.nodes[nid]["n"] = int(contagens[i])
    for a, b, d in rag.edges(data=True):
        d["borda"] = d["weight"]
        d["weight"] = _custo(rag, a, b, d["borda"])
    rotulos = graph.merge_hierarchical(
        rotulos, rag, thresh=limiar_fusao, rag_copy=False, in_place_merge=True,
        merge_func=_fundir_nos, weight_func=_peso_fusao,
    )
    rotulos = np.where(dentro, rotulos + 1, 0)

    # 3) Regiões pequenas demais são absorvidas pelo vizinho mais parecido
    min_px = int(area_min_ha / AREA_PIXEL_HA)
    rotulos = _absorver_pequenas(rotulos, F, min_px)
    rotulos, _, _ = segmentation.relabel_sequential(rotulos)

    # 4) Vetoriza
    talhoes = []
    for geom, val in features.shapes(rotulos.astype("int32"), mask=rotulos > 0,
                                     transform=grade.transform):
        talhoes.append((int(val), shape(geom)))
    por_rotulo: dict[int, list] = {}
    for val, g in talhoes:
        por_rotulo.setdefault(val, []).append(g)

    feats = []
    datas = [c.data for c in cenas]
    for val, partes in por_rotulo.items():
        g = unary_union(partes)
        # Suaviza o "serrilhado" de pixel e recorta pelo perímetro do CAR
        g = g.buffer(6, join_style=1).buffer(-6, join_style=1).simplify(4)
        g = g.intersection(geom_utm)
        if g.is_empty:
            continue
        area_ha = g.area / 10_000
        if area_ha < area_min_ha * 0.5:
            continue
        m = rotulos == val
        ndvi_datas = [None if np.isnan(x) else round(float(x), 3)
                      for x in np.nanmean(ndvis[m], axis=0)]
        classe = _classificar(ndvi_datas)
        feats.append({
            "type": "Feature",
            "geometry": mapping(shp_transform(grade.to_wgs.transform, g)),
            "properties": {
                "area_ha": round(area_ha, 2),
                "classe": classe,
                "ndvi_por_data": dict(zip(datas, ndvi_datas)),
                "nitidez_borda": round(float(_nitidez(m, grad)), 2),
            },
        })

    feats.sort(key=lambda f: -f["properties"]["area_ha"])
    n = 0
    for f in feats:
        if f["properties"]["classe"] == "talhão":
            n += 1
            f["properties"]["id"] = f"T{n:02d}"
        else:
            f["properties"]["id"] = None
    k = 0
    for f in feats:
        if f["properties"]["id"] is None:
            k += 1
            f["properties"]["id"] = f"A{k:02d}"
    return {"type": "FeatureCollection", "features": feats}


def _absorver_pequenas(rot: np.ndarray, F: np.ndarray, min_px: int, max_iter: int = 6) -> np.ndarray:
    rot = rot.copy()
    for _ in range(max_iter):
        ids, cont = np.unique(rot[rot > 0], return_counts=True)
        pequenas = ids[cont < min_px]
        if len(pequenas) == 0:
            break
        medias = {i: F[rot == i].mean(axis=0) for i in ids}
        mudou = False
        for i in pequenas:
            m = rot == i
            borda = ndi.binary_dilation(m) & ~m
            viz = rot[borda]
            viz = np.unique(viz[viz > 0])
            if len(viz) == 0:
                continue
            alvo = min(viz, key=lambda v: np.linalg.norm(medias[i] - medias[v]))
            rot[m] = alvo
            mudou = True
        if not mudou:
            break
    return rot


def _nitidez(m: np.ndarray, grad: np.ndarray) -> float:
    """Média do gradiente na borda da região (0–1+). Mais alto = divisa mais clara."""
    borda = m & ~ndi.binary_erosion(m)
    return float(grad[borda].mean()) if borda.any() else 0.0


def _classificar(ndvi_datas: list) -> str:
    v = np.array([x for x in ndvi_datas if x is not None])
    if v.size == 0:
        return "indefinido"
    if v.mean() < 0.05:
        return "água"
    # Mata nativa / APP: verde o ano todo e quase sem variação
    if v.mean() > 0.7 and v.min() > 0.6 and (v.max() - v.min()) < 0.25:
        return "vegetação perene"
    return "talhão"
