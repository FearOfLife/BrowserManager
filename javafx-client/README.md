# BrowserManager JavaFX client

JavaFX-клиент использует Python backend из `backend_server.py`.

## Запуск backend отдельно

```powershell
.\scripts\run-backend.ps1 -Port 8765
```

Проверка:

```powershell
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/api/health').read().decode())"
```

## Запуск JavaFX UI

Нужен JavaFX SDK. Укажите путь через `JAVAFX_HOME` или параметр `-JavaFxHome`.

```powershell
$env:JAVAFX_HOME = "C:\javafx-sdk-25"
.\scripts\run-javafx.ps1
```

Скрипт компилирует JavaFX-клиент в `build/javafx-client` и запускает класс
`browsermanager.BrowserManagerFx`. Если backend уже работает на `127.0.0.1:8765`,
клиент подключится к нему. Если backend не работает, клиент сам запустит:

```powershell
python backend_server.py --host 127.0.0.1 --port 8765
```

## API

- `GET /api/health`
- `GET /api/profiles`
- `POST /api/profiles`
- `POST /api/profiles/update`
- `POST /api/profiles/start`
- `POST /api/profiles/stop`
- `POST /api/profiles/duplicate`
- `POST /api/profiles/delete`
- `POST /api/fingerprint/randomize`
- `GET /api/proxies`
- `POST /api/proxies`
- `POST /api/proxy/random-assign`
- `GET /api/logs?since=0`
- `POST /api/shutdown`
