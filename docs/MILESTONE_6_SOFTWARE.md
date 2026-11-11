# M6 — matematica, profili e adapter OpenVR — accettazione ottica pendente

2026-10-04. **M6 IN_PROGRESS: software implementato; accettazione ottica pendente.**
I gate M3–M5 rimangono aperti. Questo lavoro è autonomo e usa solo test
sintetici, non misure delle lenti PSVR.

`driver/distortion_math.hpp` implementa matematica C++17 originale MIT:

```text
x = (u - center.u) * scale_x
y = (v - center.v) * scale_y
r = hypot(x,y)
r' = rgb[channel] * r * (d + c*r + b*r² + a*r³)
```

UV locali per occhio: origine top-left, U verso destra, V verso il basso.
Due profili occhio con centri/scales e coefficienti espliciti; RGB radial
multipliers. Default `Profile` è **identity sintetica**, non calibrazione.

API `psvr::distortion`:
- `Mapper::create(Profile)` valida il profilo e restituisce optional;
- `forward(Eye, Channel, UV)` e `forward_rgb`: source nel quadrato [0,1]²;
  output finito anche fuori quadro, senza clamp delle coordinate compositor;
- `inverse(Eye, Channel, UV)`: 80 iterazioni di bisezione deterministiche,
  rifiuto fuori immagine radiale e source-square, gestione esatta centro e
  tolleranza floating point documentata solo sui bordi.

Validazione numerica: finitezza/limiti espliciti, scale/fattori RGB positivi,
derivata radiale `d + 2*c*r + 3*b*r² + 4*a*r³` positiva sul dominio completo.
Estremi controllati analiticamente tramite radici di `2*c + 6*b*r + 12*a*r²`,
non solo sampling a griglia. Floor anti-singolarità e margine di cancellazione.

`tests/distortion_tests.cpp`: **61.159 check sintetici PASS** nella suite
CTest; agente ha verificato anche build standalone optimized/debug con warnings
error. Identità/centri/bordi/grid, profili asimmetrici, RGB, roundtrip inversa,
fold stretto non rilevabile con pochi campioni, singular/nonfinite/enum invalidi,
dominio e determinismo. Nessuna misura di geometria o accuratezza ottica.

## Loader e configurazione esplicita

`driver/optical_profile.hpp`: schema JSON v1 rigoroso, senza coefficienti
impliciti. Campi mancanti/sconosciuti/duplicati, tipi errati e nonfinite rifiutati.
Limiti 65.536 byte / 16 livelli; nome UTF-8 1..128 caratteri senza controlli.
File read limitato e filesystem path nativo Windows Unicode. Vedere
[schema](OPTICAL_PROFILE_SCHEMA.md).

Settings `driver_psvrpc`:
- `distortionMode="identity"` default: diagnostica, nessun file letto;
- `distortionMode="profile"`: richiede `distortionProfilePath` assoluto UTF-8;
  errore file/schema/math/SDK abortisce Init senza fallback identity;
- con `displayEnabled=false` la configurazione ottica non viene letta;
- mapper/profilo congelati per la vita del componente, nessun hot reload.

Esempio `resources/profiles/identity_unverified.json`: identità non calibrata.
Driver/display restano disabilitati per default. Proiezioni, IPD e monitor sono
separati dal profilo: nessun FOV o parametro fisico viene inventato dal loader.

## Adapter e precisione SDK float

`DisplayComponent` usa `forward_rgb`: input UV display/output → render sampling
UV. Nessuna Y flip/clamp. L'inversa mappa ordine SDK **red/blue/green** a mapper
**red/green/blue** per significato, non cast numerico.

Policy SDK separata in `driver/optics_sdk.hpp`: axis scale [0,25;4]; sul disco
del massimo raggio R, B=2|c|R+3|b|R²+4|a|R³; per ogni RGB q richiede
q(d−B)≥0,1 e q(d+B)≤8. Condizione sufficiente conservativa, non accettazione
ottica: può rifiutare profili matematicamente regolari fuori policy. Evita il
collasso patologico dell'intero dominio in float, non garantisce injectività
float per punti arbitrariamente vicini.

L'inversa SDK propaga l'incertezza half-ULP delle coordinate float effettive
tramite bound Jacobian/scales, recuperando bordi solo entro tale incertezza.
`Mapper::inverse()` generale resta strict. Assume round-to-nearest.
`ComputeInverseDistortion` false permette stima runtime, non oscuramento certo.
Forward invalido ritorna coordinate finite fuori texture (-1,-1), mai identity
fallback; il trattamento compositor resta da verificare fisicamente.

## Validatore offline e pattern

Package `out/optics/psvr_optics.exe`, JSON di esempio e licenze:

```powershell
.\out\optics\psvr_optics.exe --validate .\out\optics\identity_unverified.json --json
```

Exit 0 schema/math/SDK validi, 1 argomenti invalidi, 3 profilo invalido/illeggibile,
4 matematica valida ma policy SDK fallita. `optical_acceptance_verified=false`
sempre. Nessun USB, query monitor o chiamata SteamVR.

Pattern in `out/patterns/` e risorse driver `resources/diagnostics/`:
- `stereo_grid.svg`: griglia quadrata 60 px, righelli, croci, cerchi, bordi;
- `stereo_rgb.svg`: strisce RGB e linee bianche;
- `stereo_geometry.svg`: cerchi concentrici e edge/off-center marks.

SVG deterministici 1920x1080 SBS (960x1080 per occhio), etichette L/R e
TOP/BOTTOM, `DIAGNOSTIC - UNCALIBRATED`. Generatore Python stdlib con
output-dir esplicita, preflight collisioni e force per i soli nomi generati.
Nessun avvio GUI/fullscreen. SVG da solo non prova compositor/UV/timing.
Packaging pattern richiede Python 3.11; il driver compila anche senza pattern.

## Verifica finale e gate

Build MinGW x64 OK, **28/28 CTest PASS**:
- 1.574 check loader, limiti/schema/errori/file Unicode;
- 36.834 check adapter SDK, bordi/angoli/allRGB/allEyes;
  614 coordinate float rifiutate dalla strict inverse recuperate entro incertezza;
- 61.159 check math strict ancora PASS;
- 28 test Python pattern XML/geometry/determinismo/CLI/collisioni;
- CLI offline e regressioni M3–M5/mock runtime/package PASS.

Lavoro e review parallelizzati con subagenti. Review finale senza ulteriori
must-fix nei moduli ottici esaminati dopo i fix numerici.
Restano profilo fisicamente misurato, rendering reale e accettazione lenti/
scala/colore. IPD utente non è la separazione fissa lenti. M6 non completa;
nessun coefficiente candidato upstream assunto corretto.
[Coda manuale](MANUAL_VALIDATION_QUEUE.md).
