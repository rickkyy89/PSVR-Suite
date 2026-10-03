# Report milestone 1 — 2026-10-03

## Risultato

**PASS sul dispositivo reale:** PC Windows → USB → PSVR rilevato → HID sensori
e controllo aperte → report sensori 64 byte letti correttamente.
Il test senza PSVR e la ripresa dopo replug sono stati eseguiti con gesti
fisici confermati dall'utente. Nessun comando power/VR/feature/output inviato.

## Build

Root: `C:\Users\rickk\Desktop\PSVR`. Windows 11 Pro x64 10.0.26200.
VS 2022 non rilevato: w64devkit x64 2.10.0 portabile, archivio SHA-256
verificato (vedere BUILD.md). GCC 16.2.0, CMake 4.4.3, Ninja.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -Toolchain MinGW -ToolchainRoot "C:\Users\rickk\AppData\Local\Temp\opencode\w64devkit"
```

Prima build: configure riuscito, 8 step compilazione/link riusciti,
**5/5 test passati**, install in `out/diag`. Correzione delle capacità HID:
configure riuscito, 2 step incrementali riusciti, **5/5 test passati**,
pacchetto aggiornato. Nessun errore di compilazione, nessun warning C++ emesso.
`objdump -p`: formato `pei-x86-64`, PE32+, import solo HID.DLL, KERNEL32.dll,
msvcrt.dll e SETUPAPI.dll; HIDAPI/JSON/runtime GCC statici, niente DLL terze.
CfgMgr32 è collegato dal target e HIDAPI carica ulteriori API Windows a runtime.
Build MSVC e Windows 10 ancora non verificate.

## Risultati hardware

| Prova | Log in `logs/` | Esito |
|---|---|---|
| Prima capture | `psvr_diag_20261003T165554.691Z_5160.log` | open 4/5 OK; lettura controllo non supportata, exit 3 |
| Dopo correzione caps, standby | `psvr_diag_20261003T165729.565Z_4000.log` | open OK, 0 report in 2 s, exit 4 ordinato |
| Dopo accensione fisica | `psvr_diag_20261003T170300.238Z_19092.log` | 5002/5002 report validi in 5 s, exit 0 |
| USB scollegato | `psvr_diag_20261003T170411.053Z_4416.log` | 0 USB/HID PSVR, exit 2 in ~40 ms, nessun crash |
| USB ricollegato, visore acceso | `psvr_diag_20261003T170514.661Z_11392.log` | 2001/2001 report validi in 2 s, exit 0 |

I log completi sono locali, esclusi da Git; questo report conserva l'evidenza
utile al commit senza path USB macchina-specifici. I primi errori sono conservati.

### Dispositivo

- VID `054C`, PID `09AF`, USB release `0107`, manufacturer Sony.
- MI_04: prodotto `PS VR Sensor`, usage_page `ff01`, usage `0001`, driver HidUsb.
  Input report Windows **65**, output **0**, feature **0**.
- MI_05: prodotto `PS VR Control`, usage_page `ff00`, usage `0008`, driver HidUsb.
  Input **0**, output **0**, feature **5**.
- MI_08: tre collezioni `PS VR Control2`; enumerate, non aperte.
- Composite driver `usbccgp`; 8 nodi PnP USB PSVR, 5 collezioni HID PSVR.
- MI_00, MI_06, MI_07: problem code **28** (nessun driver installato), non
  necessari alla lettura IMU e non modificati.
- Nessun seriale fornito dai descrittori HID. Non dedotto CUH-ZVR1/ZVR2.

### Comando e output reale, capture 5 secondi

```powershell
.\out\diag\psvr_diag.exe --config .\config.json --duration-ms 5000 --raw-packets 3
$LASTEXITCODE
```

Estratto console/log (timestamp UTC omessi qui):

```text
PSVR detected VID=054c PID=09af (PSVR1 family; exact CUH revision not inferred)
USB interface OK (PnP enumeration; no raw USB claim)
sensor HID capabilities input_bytes=65 output_bytes=0 feature_bytes=0
sensor interface OK interface=4 (read/write handle access confirmed; no output reports sent)
control HID capabilities input_bytes=0 output_bytes=0 feature_bytes=5
control interface OK interface=5 (read/write handle access confirmed; no output reports sent)
control descriptor exposes no input reports; passive control read skipped
capture started duration_ms=5000 read_timeout_ms=100
sensor packet OK length=64 tick0=7956812 tick1=7957312 seq=72
sensor summary reports=5002 valid64=5002 rejected=0 sequence_discontinuities=0 elapsed_s=5.00 report_hz=1000.20 samples_hz_estimate=2000.41
control summary reports=0 (passive timeout is allowed)
MILESTONE 1 PASS: HID 4/5 open and sensor reports received; power/mode/orientation/display untested
HID handles closed (no mode/power changes)
shutdown exit_code=0
```

Primo report raw reale, 64 byte:

```text
00 00 22 00 0a 04 20 00 12 f5 00 ff 7f 00 00 00
4c 69 79 00 f9 ff 02 00 ee ff 51 40 1f 00 d1 0f
40 6b 79 00 02 00 02 00 e3 ff 01 41 2f 00 a1 10
f5 00 00 00 00 03 ff c0 00 12 00 00 00 b8 01 48
```

Timestamp del primo campione 7956812, secondo 7957312: delta **500 µs**.
La stima ~2000 campioni/s è due campioni/report, non una misura di qualità fusion
né refresh display. La capture ha attraversato più rollover della sequenza
8 bit senza discontinuità rilevate; rollover timestamp 24 bit non validato in M1.

### USB assente e replug

```text
USB enumeration present_nodes=24 psvr_nodes=0
HID enumeration total=28 psvr_collections=0
PSVR not detected VID=054c PID=09af (no crash; check USB data cable and processor power)
shutdown exit_code=2
```

Dopo replug:

```text
sensor packet OK length=64 tick0=8146620 tick1=8147121 seq=79
sensor summary reports=2001 valid64=2001 rejected=0 sequence_discontinuities=0 elapsed_s=2.00 report_hz=1000.08 samples_hz_estimate=2000.16
shutdown exit_code=0
```

## Ipotesi, correzione minima, verifica

La prima implementazione presumeva input sul controllo come nel backend
libusb OpenPSVR. HIDAPI 0.15 distingue `hid_error` da **`hid_read_error`**:
usare il primo dava il fuorviante messaggio "Success" dopo errore lettura.
La correzione legge `HidD_GetPreparsedData`/`HidP_GetCaps`, registra lunghezze,
evita input inesistenti e usa l'errore di lettura specifico.
Non è stato silenziato un errore I/O reale: il descriptor verifica input=0.
Una sola correzione mirata dopo lettura dei sorgenti/log, poi build/test.
Zero dati sensori in standby è stato risolto tramite pulsante fisico, non codice M2.

## Riutilizzo e limiti

Riutilizzati identità/interfacce e decoder pacchetto OpenHMD (adattamento C++
con Boost/copyright preservati), HIDAPI Windows reale. OpenPSVR/mSparks hanno
informato l'architettura; nessun loro driver/binario o comando eseguito.

Non provati: comandi power/mode, orientamento calcolato, assi/drift/recenter,
HDMI/display/refresh, lens distortion, SteamVR/giochi. Hot-unplug durante
lettura e Ctrl+C restano test manuali aggiuntivi nella checklist.
M1 diagnostica l'USB **tramite PnP** e accede solo a HID 4/5: non reclama
interfacce audio/H.264/BulkIn né il composite device tramite libusb.

## Prossimo passo (solo nuova sessione)

M2: determinare il trasporto di controllo compatibile Windows per il descriptor
reale input=0/output=0/feature=5, confrontando comandi documentati OpenHMD e
libpsvr. Non presumere che `hid_write` sia supportato o che i comandi a 8 byte
possano essere troncati/adattati a feature=5. Eventuale backend WinUSB/libusb
su MI_05 richiede scelta esplicita e procedura di ripristino; non toccare MI_04
che già funziona con HID. Poi implementare power/VR con verifica stato ed errori.

**Nessuna milestone successiva implementata in questa sessione.**
