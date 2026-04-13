# Flight Number Change Tracker

CLI-приложение для отслеживания изменений статуса рейса по номеру (`flight number`) с возможностью запрашивать **актуальную информацию у авиаперевозчиков**.

## Что умеет
- добавлять/удалять рейсы из отслеживания;
- сохранять подписки и снапшоты в SQLite;
- определять изменения между проверками (status, время, terminal, gate, source);
- получать "живой" статус конкретного рейса командой `get`;
- проверять и управлять рейсами через web interface (`serve`);
- работать по цепочке источников:
  1. `CarrierAPIProvider` (API авиакомпаний из `data/carrier_endpoints.json`),
  2. `AviationstackProvider` (если задан `AVIATIONSTACK_API_KEY`),
  3. `MockProvider` (локальный fallback для разработки).

## Настройка источников авиаперевозчиков
Файл `data/carrier_endpoints.json` описывает, как дергать API конкретной авиакомпании по её префиксу (например, `AA`, `SU`, `LH`).

Пример:
```json
{
  "AA": {
    "url": "https://carrier.example/api/status/{flight_number}",
    "headers": {"Authorization": "Bearer <token>"},
    "query": {"lang": "en"},
    "fields": {
      "status": "status",
      "scheduled_departure": "departure.scheduled",
      "estimated_departure": "departure.estimated",
      "actual_departure": "departure.actual",
      "terminal": "departure.terminal",
      "gate": "departure.gate"
    }
  }
}
```

Можно переопределить путь к конфигу:
```bash
export CARRIER_CONFIG_PATH=/path/to/carrier_endpoints.json
```

## Использование
```bash
python app.py add AA100
python app.py check
python app.py get AA100
python app.py serve --host 127.0.0.1 --port 8080
python app.py list
python app.py remove AA100
```

После запуска `serve` откройте в браузере:
`http://127.0.0.1:8080`

По умолчанию база создаётся в `flight_tracker.db`. Можно указать путь:
```bash
python app.py --db /tmp/flights.db add SU100
```
