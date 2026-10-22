# API de Talhões (CAR + Sentinel-2)

Recebe o código do CAR (ou um polígono) e devolve os talhões da fazenda em GeoJSON.

## Rodar

```bash
python -m venv .venv && source .venv/bin/activate   # opcional
pip install -r requirements.txt
uvicorn app.main:app --reload
```

- `http://localhost:8000` → página de teste com mapa (busca por CAR ou desenha uma área)
- `http://localhost:8000/docs` → Swagger

## Endpoints

| Método | Rota | Entrada |
|---|---|---|
| GET | `/talhoes?car=SP-3543402-...` | código do CAR |
| POST | `/talhoes` | `{"geometry": <Polygon GeoJSON>}` |

Parâmetros opcionais: `sensibilidade` (0.2–2.0, padrão 0.7; menor divide mais),
`area_min_ha` (padrão 2), `meses` (janela de imagens, padrão 12).

Resposta: `FeatureCollection` com `id`, `classe` (`talhão`, `vegetação perene`, `água`),
`area_ha`, `ndvi_por_data` e `nitidez_borda` em cada talhão, mais um bloco `metadata`
com o imóvel do SICAR e as cenas usadas.

## Como funciona (passo a passo)

1. **Perímetro** — `app/car.py` consulta o GeoServer público do SICAR
   (`geoserver.car.gov.br`, camada `sicar_imoveis_<uf>`). O servidor só aceita
   cifras TLS antigas, por isso o contexto SSL com `SECLEVEL=1`.
2. **Imagens** — `app/imagery.py` busca no catálogo STAC Earth Search (AWS) as cenas
   Sentinel-2 L2A mais limpas dos últimos 12 meses, espaçadas ≥ 35 dias, e lê só o
   recorte da fazenda (COG). Descarta cenas com < 90% de pixels limpos na área.
3. **Segmentação** — `app/segmentation.py`:
   NDVI de cada data + cores → gradiente (onde o "jeitão" muda de repente) →
   watershed (superpixels) → fusão de vizinhos parecidos sem divisa forte →
   absorção de pedaços < área mínima → polígonos suavizados e recortados pelo CAR.
4. **Classe** — região verde o ano todo vira "vegetação perene"; NDVI < 0,05 vira "água".

## Limites conhecidos

- Resolução de 10 m: carreadores estreitos às vezes não separam talhões.
- Estradas largas podem ficar coladas no talhão vizinho.
- Cultura perene (laranja, café, eucalipto) pode cair como "vegetação perene".
- Para produção: cachear resultado por CAR, rodar em fila (Celery/RQ) e trocar a
  segmentação por um modelo treinado (ex.: Fields of The World) ou provedor pago.
