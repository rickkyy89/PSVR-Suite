# Profilo ottico JSON v1

Schema del loader M6; validazione numerica non è calibrazione ottica.
Esempio completo in `driver/package/resources/profiles/identity_unverified.json`.

```json
{
  "schema_version": 1,
  "name": "Diagnostic identity unverified - NOT optical calibration",
  "uv_convention": "eye-local-top-left",
  "mapping": "display-to-render",
  "eyes": {
    "left": {
      "center": [0.5, 0.5],
      "axis_scale": [1, 1],
      "polynomial": {"a": 0, "b": 0, "c": 0, "d": 1},
      "rgb_scale": {"red": 1, "green": 1, "blue": 1}
    },
    "right": {
      "center": [0.5, 0.5],
      "axis_scale": [1, 1],
      "polynomial": {"a": 0, "b": 0, "c": 0, "d": 1},
      "rgb_scale": {"red": 1, "green": 1, "blue": 1}
    }
  }
}
```

Ogni chiave è obbligatoria, nessuna aggiuntiva/duplicata. Versione intero 1,
nome UTF-8 1..128 caratteri senza C0/C1/DEL. Center in [0,1], scale positive,
coefficienti finiti e tre fattori RGB espliciti. JSON max 65.536 byte / 16 livelli;
raw NUL, suffissi non JSON, stringhe numeriche e NaN/Infinity rifiutati.

`r'=rgb[channel]*r*(d+c*r+b*r²+a*r³)`;
`r=hypot((u-center.u)*scale_x,(v-center.v)*scale_y)`.
UV per singolo occhio, non coordinate SBS: U destra, V basso.
**Display-to-render** significa input display/output, output render sampling.
Non usare inverse per ComputeDistortion o cambiare Y arbitrariamente.

Math generale: scale/RGB 0,001..100, coefficienti |x|≤100 e derivata radiale
positiva sull'intero dominio. Esposizione OpenVR richiede anche la più
conservativa policy SDK del [report M6](MILESTONE_6_SOFTWARE.md).
Un file può passare math e fallire SDK (CLI exit 4, driver Init rifiutata).

Applicazione futura: `distortionMode="profile"`, path assoluta
`distortionProfilePath` negli override `driver_psvrpc`; driver/display e monitor
devono essere già configurati/verificati. Riavvio per modifiche al profilo.
Default `identity`, nessun profilo auto-selezionato. L'esempio non rappresenta
lenti PSVR e nessun campo certifica accettazione hardware.
