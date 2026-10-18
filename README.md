# RTL_TCP Server Manager

Web interface for configuring, starting, and stopping `rtl_tcp` on a Raspberry Pi 3B. It has no external Python dependencies; Python 3 is sufficient.

## Install on Raspberry Pi

```bash
sudo apt install rtl-sdr python3
# Copy rtl_tcp_manager.py and the assets/ directory to the Pi, then run:
MANAGER_TOKEN=secret python3 rtl_tcp_manager.py
```

Open `http://<pi-ip>:8080` in a browser. The interface is available in English and Czech; English is selected by default.

Environment variables: `MANAGER_HOST` (defaults to `0.0.0.0`), `MANAGER_PORT` (defaults to `8080`), and `MANAGER_TOKEN` (optional API protection token; recommended on untrusted networks).

Settings are saved to `config.json` next to `rtl_tcp_manager.py`.

## Live Telemetry Proxy

By default, the manager runs a small TCP proxy. Clients keep connecting to the configured address and port, while `rtl_tcp` listens only on `127.0.0.1` on a free local port. The proxy passes data through unchanged and displays the current frequency, sample rate, gain, PPM, bias-T, client address, stream rate versus expected rate, a relative IQ level (dBFS, uncalibrated and dependent on AGC) and ADC clipping. Only one client at a time is supported. Disable the proxy with the "Live telemetry proxy" checkbox to run plain `rtl_tcp`.

## Start Automatically on Raspberry Pi

Run the application as a `systemd` service. This example assumes the files are in `/home/pi/rtl_tcp_manager` and the Linux user is `pi`. Adjust the path and username for your installation.

Create the service file:

```bash
sudo nano /etc/systemd/system/rtl-tcp-manager.service
```

Add:

```ini
[Unit]
Description=RTL_TCP server manager
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/rtl_tcp_manager
ExecStart=/usr/bin/python3 /home/pi/rtl_tcp_manager/rtl_tcp_manager.py
Restart=on-failure
RestartSec=5
Environment=MANAGER_TOKEN=secret

[Install]
WantedBy=multi-user.target
```

Replace `secret` with your own token, or remove the `Environment=MANAGER_TOKEN=secret` line if you do not want to use a token. Save the file and enable the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rtl-tcp-manager
```

The service will now start whenever the Raspberry Pi boots. Check its status with `sudo systemctl status rtl-tcp-manager`, view its log with `sudo journalctl -u rtl-tcp-manager -f`, or stop and disable it with `sudo systemctl disable --now rtl-tcp-manager`.

## Česká verze

# RTL_TCP server manager

Webové rozhraní pro nastavení, spuštění a zastavení `rtl_tcp` na Raspberry Pi 3B. Bez závislostí, stačí Python 3.

## Instalace na Raspberry Pi

```bash
sudo apt install rtl-sdr python3
# zkopírovat rtl_tcp_manager.py a složku assets/ na Pi, pak:
MANAGER_TOKEN=tajne python3 rtl_tcp_manager.py
```

Otevřít `http://<ip-pi>:8080`.

Proměnné prostředí: `MANAGER_HOST` (výchozí `0.0.0.0`), `MANAGER_PORT` (`8080`), `MANAGER_TOKEN` (volitelný token pro ochranu API; doporučeno v nedůvěryhodné síti).

Nastavení se ukládá do `config.json` vedle `rtl_tcp_manager.py`.

## Proxy pro živá data

Správce ve výchozím stavu spouští malou TCP proxy. Klienti se dál připojují na nastavenou adresu a port, `rtl_tcp` ale naslouchá jen na `127.0.0.1` na volném lokálním portu. Proxy data předává beze změny a zobrazuje aktuální kmitočet, vzorkovací frekvenci, zesílení, PPM, bias-T, adresu klienta, rychlost proudu oproti očekávané, relativní úroveň IQ (dBFS, nekalibrovaná a závislá na AGC) a přebuzení ADC. Podporován je jen jeden klient najednou. Proxy lze vypnout zaškrtávátkem „Proxy pro živá data“, pak běží čistý `rtl_tcp`.

## Automatické spuštění po startu Raspberry Pi

Aplikaci lze spouštět jako službu `systemd`. Následující příklad předpokládá, že soubory jsou v `/home/pi/rtl_tcp_manager` a uživatel se jmenuje `pi`. Cestu i uživatelské jméno upravte podle své instalace.

Vytvořte soubor služby:

```bash
sudo nano /etc/systemd/system/rtl-tcp-manager.service
```

Vložte do něj:

```ini
[Unit]
Description=RTL_TCP server manager
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/rtl_tcp_manager
ExecStart=/usr/bin/python3 /home/pi/rtl_tcp_manager/rtl_tcp_manager.py
Restart=on-failure
RestartSec=5
Environment=MANAGER_TOKEN=tajne

[Install]
WantedBy=multi-user.target
```

Nahraďte `tajne` vlastním tokenem, případně řádek `Environment=MANAGER_TOKEN=tajne` odstraňte, pokud token nechcete používat. Službu uložte a zapněte:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rtl-tcp-manager
```

Služba se nyní spustí při každém startu Raspberry Pi. Stav ověříte příkazem `sudo systemctl status rtl-tcp-manager`; její log zobrazíte příkazem `sudo journalctl -u rtl-tcp-manager -f`. Pro zastavení použijte `sudo systemctl disable --now rtl-tcp-manager`.

