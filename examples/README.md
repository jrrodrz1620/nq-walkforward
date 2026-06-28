# examples/

## sample_options_chain_nq.csv
Cadena de opciones de ejemplo (sintetica) para probar la herramienta
**GEX Levels** del app (`app_mode = "GEX Levels (Estructura+ GEX)"`).

Columnas: `strike, type (call/put), gamma, open_interest`.

Con **Spot = 20000** y **Multiplicador = 100** produce aproximadamente:

| Nivel | Valor |
|---|---|
| Call Wall (resistencia) | 20100 |
| Put Wall (soporte) | 19700 |
| Gamma Flip / Zero Gamma | ~19978 |
| Regimen (spot > flip) | Positivo (rango / reversion) |

Esos tres niveles se pegan en los inputs de la estrategia Pine
`estructura-gex-strategy.pine`. Datos ficticios, solo para demo.
