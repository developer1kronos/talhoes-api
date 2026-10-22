"""Busca o perímetro do imóvel no SICAR (GeoServer público do CAR)."""
import os
import re
import ssl

import certifi
import httpx
from shapely.geometry import shape, mapping

WFS_URL = "https://geoserver.car.gov.br/geoserver/sicar/ows"
CAR_RE = re.compile(r"^([A-Z]{2})-\d{7}-[0-9A-F]{32}$")


class CarNotFound(Exception):
    pass


def normalizar_codigo(codigo: str) -> str:
    """Aceita o código com ou sem pontos/espaços e devolve no formato do SICAR."""
    c = codigo.strip().upper().replace(".", "").replace(" ", "")
    if not CAR_RE.match(c):
        raise ValueError(
            "Código CAR inválido. Formato esperado: UF-1234567-32 caracteres hexadecimais "
            "(ex.: SP-3543402-0381059FD0A5487594C09A887A5D2BC8)"
        )
    return c


def _ssl_ctx() -> ssl.SSLContext:
    """O servidor do CAR só aceita cifras antigas; o Python 3.10+ recusa por padrão
    (erro SSLV3_ALERT_HANDSHAKE_FAILURE). Baixamos o nível de segurança só aqui."""
    cafile = os.environ.get("SSL_CERT_FILE")
    if not cafile and os.path.exists("/root/.ccr/ca-bundle.crt"):
        cafile = "/root/.ccr/ca-bundle.crt"
    ctx = ssl.create_default_context(cafile=cafile or certifi.where())
    ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    return ctx


def buscar_imovel(codigo: str, timeout: float = 90) -> dict:
    """Retorna {'geometry': shapely, 'properties': {...}} do imóvel."""
    codigo = normalizar_codigo(codigo)
    uf = codigo[:2].lower()
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeName": f"sicar:sicar_imoveis_{uf}",
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "CQL_FILTER": f"cod_imovel='{codigo}'",
        "count": 1,
    }
    r = httpx.get(WFS_URL, params=params, timeout=timeout, verify=_ssl_ctx())
    r.raise_for_status()
    feats = r.json().get("features", [])
    if not feats:
        raise CarNotFound(f"Imóvel {codigo} não encontrado no SICAR")
    f = feats[0]
    geom = shape(f["geometry"])
    # GeoServer com srsName EPSG:4326 já devolve lon/lat em GeoJSON
    if not geom.is_valid:
        geom = geom.buffer(0)
    return {"geometry": geom, "properties": f["properties"]}


if __name__ == "__main__":
    import sys

    im = buscar_imovel(sys.argv[1])
    print(im["properties"], im["geometry"].bounds)
