# RTL_TCP server manager

Webové rozhraní pro nastavení, spuštění a zastavení `rtl_tcp` na Raspberry Pi 3B. Bez závislostí, stačí Python 3.

## Instalace na Raspberry Pi

```bash
sudo apt install rtl-sdr python3
# zkopírovat app.py a složku assets/ na Pi, pak:
MANAGER_TOKEN=tajne python3 app.py
```

Otevřít `http://<ip-pi>:8080`.

Proměnné prostředí: `MANAGER_HOST` (výchozí `0.0.0.0`), `MANAGER_PORT` (`8080`), `MANAGER_TOKEN` (volitelný token pro ochranu API; doporučeno v nedůvěryhodné síti).

Nastavení se ukládá do `config.json` vedle `app.py`.

