# FlooGoo FMA120 Firmware Upgrade - Wissensbasis

## Gerät
- **Modell:** Flairmesh FlooGoo FMA120 USB Bluetooth Dongle
- **Chip:** Qualcomm QCC3086
- **USB:** VID=0x0A12, PID=0x4007, Serial=FMA120E9B12AM6
- **Aktuelle Firmware:** 1.1.7.2G (am 18.07.2026 erfolgreich nativ auf macOS geflasht und per `BC:VR` verifiziert; G = Variante)
- **Neueste Firmware:** 1.1.7.2G; speziell für Sony WH-1000XM6: 48-kHz-Wiedergabe bei aktivem Mikrofon (XM6-Firmware 3.1.5)
- **Firmware-Download:** `https://www.flairmesh.com/support/FMA120_1.1.7.2.zip`
- **Firmware-Paket:** `/Users/robin/Desktop/FMA120_1.1.7.2.zip`; enthält `DFU/FMA120_1_1_7_2G.bin` (G-Variante, 2 MB)
- **Mac-Port:** `/dev/cu.usbmodemFMA120E9B12AM6` (921600 Baud, 8N1)

## USB-Struktur des Dongles
- **Interface 0:** HID, 1x Interrupt IN (EP 0x81, 64B) - Consumer Control / Telephony
- **Interface 1:** HID, 1x Interrupt OUT (EP 0x01, 64B) + 1x Interrupt IN (EP 0x82, 64B) - Vendor HID (Usage Page 0xFF00)
- **Interface 2-4:** Audio (USB Speaker/Mic, Isochronous)
- **Interface 5-6:** CDC (Serial Port, Bulk)

## HID Report Descriptor (Interface 1, Usage Page 0xFF00)
### Collection 1 (Usage 0x01) - GAIA/Control
| Report ID | Typ     | Bytes | Zweck               |
|-----------|---------|-------|---------------------|
| 1         | Output  | 62    | Befehle (klein)     |
| 5         | Output  | 254   | Befehle (gross)     |
| 2         | Input   | 16    | Antworten (klein)   |
| 6         | Input   | 12    | Antworten (klein)   |
| 3         | Feature | 62    | Bidirektional       |
| 4         | Feature | 62    | Bidirektional       |

### Collection 2 (Usage 0x03) - Daten/DFU
| Report ID | Typ     | Bytes | Zweck               |
|-----------|---------|-------|---------------------|
| 7         | Output  | 446   | Firmware-Daten      |
| 8         | Input   | 446   | Daten-Antworten     |
| 9         | Input   | 11    | Status              |

### Collection 3 (Usage 0x03)
| Report ID | Typ     | Bytes |
|-----------|---------|-------|
| 32 (0x20) | Feature | 255   |

## Serielles Protokoll (BAI)
- Befehle: `BC:XX\r\n` (z.B. `BC:VR\r\n` für Version)
- Antworten: `XX=...\r\n` (z.B. `VR=1.0.8G\r\n`)
- **Wichtig:** Dongle muss frisch eingesteckt sein, antwortet nicht nach unsauberem Disconnect
- `BC:PD=01` = Reset
- `BC:LU` = UUID128 lesen -> ER=03 (nicht unterstützt auf FMA120)
- `BC:FD` = Factory Default (löst Disconnect aus! ST=08)

## Native Firmware-Aktualisierung auf macOS/Linux
- Dieses Projekt wurde eigens gebaut, um den FMA120 ohne Windows zu aktualisieren.
- `hid_dfu.py` implementiert das Qualcomm VM Upgrade Protocol nativ in Python; keine proprietäre Windows-DLL/VM nötig.
- Installierte Firmware per BAI lesen: seriell `BC:VR\r\n` senden; erwartet `VR=1.1.7.2G`.
- `hid_dfu.py --version` meldet nur die Qualcomm-VM-Host-Version (hier `1.1.2`), nicht die installierte Produktfirmware.
- Firmware prüfen: `venv/bin/python hid_dfu.py --info <firmware.bin>`.
- Flashen: `venv/bin/python hid_dfu.py <firmware.bin>`; passende G-Variante verwenden.
- FlooCast vor direktem HID-Zugriff schließen, da die laufende App HID/seriellen Port exklusiv halten kann.
- Reboot-Fix: Nach `TRANSFER_COMPLETE_RES` HID-Handle bis zum echten `OSError`-Disconnect offen halten (bis 30 s). Zu frühes Schließen lässt Status auf `PreReboot`; Commit dann Fehler `0x001d`.

### Historischer Windows-Weg
Der offizielle DFU-Button war in `main.py:1104` hinter `if platform.system().lower().startswith('win'):` versteckt.
Die `HidDfu`-Klasse in `FlooDfuThread.py` ist ein Wrapper um eine proprietäre Windows-DLL.

### HidDfu DLL API
```python
myDll = HidDfu(app_path)  # lädt DLL aus app_path
retval, count = myDll.hidDfuConnect(vid=0x0A12, pid=0x4007, usage=1, usagePage=0xFF00)
retval = myDll.hidDfuUpgradeBin(fileName="path.bin")
progress = myDll.hidDfuGetProgress()  # 0-100
retval = myDll.hidDfuGetResult()
myDll.hidDfuDisconnect()
```

## Gescheiterte Ansätze

### 1. GAIA-Protokoll über HID (hidapi)
- GAIA V1 (SPP-style mit SOF=0xFF) auf Report ID 1, 5 -> keine Antwort
- GAIA V2 (BLE-style) auf Report ID 1, 5, 7 -> keine Antwort
- Mit/ohne Längen-Prefix (1 Byte, 2 Byte LE, 2 Byte BE) -> keine Antwort
- VM_UPGRADE_CONNECT (0x0640), GET_API_VERSION (0x0300) -> keine Antwort
- Feature Reports 3, 4, 0x20 lesen -> "read error"
- SET Feature Report 3 -> "sent OK", aber GET danach -> "read error"
- Usage 0x3 Interface separat öffnen -> "open failed" (gleicher HID-Pfad)

### 2. Raw USB via pyusb (mit sudo, Kernel-Driver detached)
- Interrupt OUT (EP 0x01) -> Write OK, aber Interrupt IN (EP 0x82) -> Timeout
- Auch EP 0x81 (Interface 0) -> Timeout
- SET_REPORT Feature Report 3 (CLEAR_STATUS=0x04) -> OK auf beiden Interfaces
- GET_REPORT Feature Report 2 (Status) -> Pipe Error auf beiden Interfaces
- GET_REPORT Feature Report 3, 4 -> Pipe Error
- USB Control Transfers für DFU DETACH (0x00), DNLOAD (0x01) -> Pipe Error

### 3. CSR DFU-Protokoll (fwupd dfu-csr Style)
- Report ID 1 SET_FEATURE mit UPGRADE-Command -> schreibbar, aber keine Status-Antwort
- Report ID 2 GET_FEATURE (Status) -> Pipe Error
- Report ID 3 SET_FEATURE (CLEAR_STATUS) -> OK, Report 3 SET (RESET) -> OK
- **Fazit:** CSR DFU ist für ältere BlueCore-Chips, QCC3086 nutzt VM Upgrade Protokoll

### 4. GoFlooGoo iOS App (OTA über Bluetooth)
- App installiert auf iPhone
- UUID128 `F455A208-597D-11EC-BF63-0242AC130002` (Standard-Flairmesh-UUID)
- **Problem:** FMA120 ist BLE-Central (verbindet sich zu Kopfhörern), nicht BLE-Peripheral
- Dongle wird nicht als OTA-Ziel erkannt, App zeigt "busy pairing"
- OTA-Methode ist für FMA100/FMB100 Module konzipiert, nicht für USB-Dongles

### 5. Wine
- Nicht getestet, weil Wine auf macOS keinen direkten USB-HID-Zugriff hat

## Noch nicht probiert
- **Reverse Engineering der HidDfu.dll** (aus Microsoft Store FlooCast-Paket extrahieren, mit Ghidra analysieren)
- **USB-Traffic-Capture auf Windows** (Wireshark + USBPcap während DFU auf einem Windows-PC)
- **Flairmesh kontaktieren** (support@flairmesh.com) - User möchte das nicht

## Referenzen
- FlooCast Repo: https://github.com/Flairmesh/FlooCast
- FMA120 Firmware-Seite: https://www.flairmesh.com/Dongle/FMA120.html
- BAI Referenz: ~/Downloads/FlairmeshBAI.pdf
- OTA Guide: ~/Downloads/FlairmeshOTA.pdf
- Flairmesh OTA App (iOS): GoFlooGoo (App Store)
- Qualcomm VM Upgrade Protocol: https://github.com/qiu-yongheng/GAIAControl
- fwupd CSR DFU Plugin: https://github.com/fwupd/fwupd/blob/main/plugins/dfu-csr/fu-dfu-csr-device.c
- AIAIAI HidDfuTool Gist: https://gist.github.com/snoopen/7bce2ede571bc1b33319dbf561db1713

## Setup
```bash
cd /Users/robin/Projects/floocast
source venv/bin/activate
python main.py  # oder ./start.sh
```
- wxPython 4.2.5 (statt 4.2.3, weil Python 3.14 kein Wheel für 4.2.3 hat)
- Zusätzlich installiert: numpy, sounddevice (fehlen in requirements.txt)
