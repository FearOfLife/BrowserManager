# BrowserManager

Локальный менеджер браузерных профилей на Python с интерфейсом в тёмном классическом стиле.

## Возможности

- отдельные профили Chromium с собственными директориями данных;
- запуск профиля через HTTP или SOCKS5 proxy;
- сохранение и импорт/экспорт cookies для каждого профиля;
- настройка fingerprint: User-Agent, platform, locale, timezone, screen/viewport, CPU/RAM, touch, WebGL vendor/renderer, canvas noise, WebRTC protection;
- быстрый рандом fingerprint и запуск проверки IP.

## Установка

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Если установлен Google Chrome или Microsoft Edge, приложение попробует найти его автоматически. Также можно указать путь к `chrome.exe` или `msedge.exe` в поле "Путь браузера".

## Запуск

```powershell
python browser_manager.py
```

Данные профилей лежат в `data/profiles`, список профилей в `data/profiles.json`, cookies профиля в `data/profiles/<id>/cookies.json`.

## Форматы proxy

Можно заполнить поля отдельно или вставить строку и нажать "Разобрать строку":

```text
http://host:port
http://login:password@host:port
socks5://host:port
socks5://login:password@host:port
```

## Что можно уточнить дальше

- какие именно сервисы fingerprint нужно закрывать в первую очередь;
- нужен ли массовый импорт proxy/профилей из CSV/TXT;
- нужен ли API/локальный порт управления профилями;
- нужен ли запуск нескольких профилей одновременно с таблицей статусов.
