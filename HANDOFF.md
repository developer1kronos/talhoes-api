# Handoff — API de Talhões (CAR → talhões delimitados)

Resumo do que foi construído para continuar o trabalho no Claude Code.

## Objetivo

Uma API que recebe o **código do CAR** (Cadastro Ambiental Rural) ou um **polígono** de fazenda e devolve os **talhões delimitados** em GeoJSON, com área em hectares e classe.

Não existe API pública pronta que faça isso de ponta a ponta. Por isso juntamos duas fontes gratuitas:
- **Perímetro do imóvel:** GeoServer público do SICAR.
- **Divisão em talhões:** segmentação própria sobre imagens Sentinel-2.

## Onde está

| O quê | Onde |
|---|---|
| Repositório | https://github.com/developer1kronos/talhoes-api (branch `main`) |
| App no ar | https://talhoes-api.onrender.com (página de teste com mapa) |
| Swagger | https://talhoes-api.onrender.com/docs |
| Hospedagem | Render, plano free, configurado por `render.yaml` (Blueprint) |

**Deploy:** o serviço foi criado pelo link público do repo, então **não tem auto-deploy**. Depois de cada push é preciso ir no dashboard do Render e usar **Manual Deploy → Deploy latest commit**. Para ter auto-deploy, conecte o repo pela conta do GitHub em Settings → Build & Deploy.

**Plano free:** o servidor dorme quando fica parado, e a primeira requisição leva uns 40 a 50 s a mais.

## Estrutura

```
talhoes-api/
├── app/
│   ├── main.py          # FastAPI: rotas, validação, mapeamento de erros
│   ├── car.py           # Busca o perímetro no SICAR (WFS)
│   ├── imagery.py       # Busca e lê cenas Sentinel-2 (STAC Earth Search + COG)
│   ├── segmentation.py  # Algoritmo de talhões (watershed + fusão por RAG)
│   ├── pipeline.py      # Orquestra: perímetro -> imagens -> segmentação -> metadata
│   └── static/index.html  # Página de teste (Leaflet), funciona no celular
├── requirements.txt
├── render.yaml
└── README.md
```

## Endpoints

| Método | Rota | Entrada |
|---|---|---|
| GET | `/talhoes?car=SP-3543402-...` | código do CAR |
| POST | `/talhoes` | `{"geometry": <Polygon/MultiPolygon GeoJSON EPSG:4326>, ...}` |
| GET | `/` | página de teste |

**Parâmetros opcionais (nos dois endpoints):**
- `sensibilidade`: 0.2–2.0, padrão 0.7. É o limiar de fusão: menor gera mais talhões, maior gera menos.
- `area_min_ha`: 0.5–50, padrão 2.
- `meses`: 3–24, padrão 12. É a janela de imagens.

**Resposta:** uma `FeatureCollection`.
- Cada feature traz em `properties`: `id` (`T01`… para talhões, `A01`… para as outras áreas), `classe` (`talhão`, `vegetação perene`, `água` ou `indefinido`), `area_ha`, `ndvi_por_data` e `nitidez_borda`.
- O bloco `metadata` traz: `imovel` (dados do SICAR mais o `perimetro`), `area_total_ha`, `qtd_talhoes`, `qtd_outras_areas`, `cenas` (data, id, nuvem e % de pixels válidos), as fontes, os `parametros` usados e `tempo_s`.

**Erros:**
- 422: código CAR inválido ou área grande demais (limite de ~4M pixels, cerca de 40.000 ha).
- 404: CAR não encontrado.
- 409: nenhuma imagem limpa no período.
- 502: falha numa fonte externa.

## Como funciona (pipeline)

### 1. Perímetro (`car.py`)
- Consulta WFS em `https://geoserver.car.gov.br/geoserver/sicar/ows`, camada `sicar:sicar_imoveis_<uf>`, filtrando com `CQL_FILTER=cod_imovel='...'` e `srsName=EPSG:4326`.
- Campos disponíveis na camada: `cod_imovel`, `status_imovel`, `dat_criacao`, `area`, `condicao`, `uf`, `municipio`, `cod_municipio_ibge`, `m_fiscal`, `tipo_imovel` e `geo_area_imovel`.
- **Atenção ao TLS:** o servidor do CAR só aceita cifras antigas, e o Python 3.10+ dá `SSLV3_ALERT_HANDSHAKE_FAILURE`. A solução é um `SSLContext` com `set_ciphers("DEFAULT:@SECLEVEL=1")` e `OP_LEGACY_SERVER_CONNECT`, usado só nessa chamada.
- O código é normalizado (maiúsculas, sem pontos e espaços) e validado com a regex `^[A-Z]{2}-\d{7}-[0-9A-F]{32}$`.

### 2. Imagens (`imagery.py`)
- Catálogo STAC `https://earth-search.aws.element84.com/v1`, coleção `sentinel-2-l2a`. Tudo público, sem chave.
- A grade é montada em UTM (zona calculada pelo centróide), com 10 m de resolução e 60 m de buffer em volta do imóvel.
- **Escolha das cenas:** busca cenas com nuvem abaixo de 20% nos últimos 12 meses e ordena da menos para a mais nublada. Escolhe as que ficam a pelo menos 35 dias umas das outras (até 10 candidatas). Depois descarta as que têm menos de 90% de pixels válidos dentro da área, usando a banda SCL (classes 4, 5, 6 e 7 contam como válidas). Ficam no máximo 5 cenas.
- Lê só o recorte necessário dos COGs, com `rasterio` + `WarpedVRT`.
- **Bug corrigido de offset:** o Earth Search já aplica o offset de reflectância do baseline ≥ 04.00, sinalizado pela propriedade `earthsearch:boa_offset_applied = True`. Aplicar o `offset: -0.1` de novo zerava as bandas. Agora o offset só é aplicado quando essa flag não está presente. Escala: `1e-4`.

### 3. Segmentação (`segmentation.py`)
- **Features por pixel:** para cada data, NDVI (peso 2), red (1), nir (1) e green (0.5). A normalização é robusta: mediana e desvio calculados **só dentro do imóvel**, e pixels com nuvem recebem o valor neutro (a mediana).
  - Bug antigo: a normalização por IQR global explodia (valores na casa de 1e5) e quebrava a fusão.
- **Gradiente:** Sobel em cada camada depois de um gaussiano (σ=0.8). Combina com RMS e normaliza pelo percentil 99.
- **Superpixels:** gaussiano (σ=1) sobre o gradiente, depois `h_minima(…, 0.015)` como marcadores, depois `watershed` com máscara do imóvel.
- **Fusão hierárquica (RAG):**
  - Custo de cada aresta = RMS da diferença das médias de features entre as duas regiões + `ALFA_BORDA (0.6)` × gradiente médio na fronteira.
  - Os nós guardam `soma` e `n` para recalcular médias depois de cada fusão.
  - Limiar padrão: 0.7.
  - O nó 0 (fora do imóvel) é removido do grafo.
- **Pequenas regiões:** regiões abaixo de `area_min_ha` são absorvidas pelo vizinho com média mais parecida (até 6 iterações).
- **Vetorização:** `rasterio.features.shapes`, depois buffer +6/−6 m para suavizar, `simplify(4)` e recorte pelo perímetro do CAR. Regiões abaixo de `area_min_ha × 0.5` são descartadas.
- **Classificação** (a partir do NDVI por data):
  - Média abaixo de 0.05 → `água`.
  - Média acima de 0.7, mínimo acima de 0.6 e amplitude abaixo de 0.25 → `vegetação perene` (mata, APP ou cultura perene).
  - Todo o resto → `talhão`.
- **IDs:** talhões ordenados por área decrescente (`T01`…). As demais áreas recebem `A01`….

### 4. Página de teste (`static/index.html`)
- Leaflet 1.9.4 com fundo Esri World Imagery. Tem campo de CAR, sensibilidade e área mínima, botão **Buscar pelo CAR**, modo **Desenhar área** (toques no mapa marcam os cantos e depois manda POST) e **Baixar GeoJSON**.
- Mostra um contador de tempo ("Processando… Ns") e aborta depois de 4 minutos.
- **Bug corrigido no celular:** ao tocar num talhão, aparecia um retângulo (foco ou tap highlight do navegador) cobrindo os vizinhos. Correções: `L.map(..., { preferCanvas: true, renderer: L.canvas({ tolerance: 6 }) })`, CSS sem outline e sem `-webkit-tap-highlight-color`, e seleção que destaca só o contorno real (branco, peso 4) e chama `bringToFront()`. Tocar numa linha da tabela centraliza o talhão.

## Resultados dos testes (Ribeirão Preto/SP)

| CAR | Área | Resultado com padrões |
|---|---|---|
| `SP-3543402-4359E7F681C2440EA2FAB3D514060150` | ~1.028 ha, cana | **Bom:** 24 talhões + 11 áreas de vegetação perene, divisões coerentes com estradas e carreadores |
| `SP-3543402-188EDB67444F4A428CDAC2174BDFDD9D` | ~893 ha, mista (horta, viveiro, faixas) | **Fragmentado demais:** ~89 regiões. Melhora com `sensibilidade` ≈ 1.0–1.2 |
| `SP-3543402-0381059FD0A5487594C09A887A5D2BC8` | ~1.549 ha | Usado só no começo, antes do bug de offset ser corrigido. Não foi revalidado |

**Tempo por consulta:** 20–45 s no Render, mais o tempo de acordar o servidor. O gargalo é a leitura dos COGs, não a segmentação (~1–2 s).

## Limitações conhecidas

- Com 10 m de resolução, carreadores estreitos às vezes não separam talhões.
- Estradas largas podem ficar coladas no talhão vizinho.
- Áreas mistas e pequenas (horta, viveiro) ficam picotadas.
- Culturas perenes (laranja, café, eucalipto) podem cair em `vegetação perene`.
- O fundo Esri pode ser de outra data que as imagens Sentinel, então as linhas parecem não bater com a imagem mesmo estando certas.
- Tudo é síncrono e sem cache: cada consulta baixa as imagens de novo.

## Próximos passos sugeridos

1. **Calibrar com casos reais:** coletar CARs reais e anotar os erros (talhões grudados, picotados, estrada no talhão) para ajustar `limiar_fusao`, `ALFA_BORDA`, `h_minima` e `area_min_ha`.
2. **Fundo Sentinel na página:** botão para trocar o fundo pela imagem que o algoritmo usou. Já existe `pipeline.rgb_png()`, falta expor e usar. Ele usa Pillow, que não está no `requirements.txt`.
3. **Cache por CAR + parâmetros** (Redis ou Postgres) e processamento em fila (Celery/RQ) com polling ou webhook.
4. **Persistência em PostGIS** e endpoint para o usuário ajustar ou salvar os polígonos.
5. **Qualidade da segmentação:** trocar só o `segmentation.py` por um modelo treinado (ex.: Fields of The World) ou por um provedor pago (EOSDA, DigiFarm), mantendo a mesma interface `segmentar(cenas, grade, geom_wgs, ...) -> FeatureCollection`.
6. **Pequenas limpezas:**
   - `pipeline.py` tem `fc = segmentacao = ...`, uma variável sobrando.
   - Adicionar testes com fixture (geometria e cenas em cache).
   - Avaliar Django/DRF se for integrar com o restante do stack.

## Rodar localmente

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# http://localhost:8000  e  http://localhost:8000/docs
curl "localhost:8000/talhoes?car=SP-3543402-4359E7F681C2440EA2FAB3D514060150"
```

Precisa de GDAL (vem no wheel do `rasterio`). No Android/Termux é difícil de compilar, então para testar pelo celular use o deploy no Render.
