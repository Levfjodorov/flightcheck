#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional


@dataclass
class FlightStatus:
    flight_number: str
    status: str
    scheduled_departure: Optional[str] = None
    estimated_departure: Optional[str] = None
    actual_departure: Optional[str] = None
    terminal: Optional[str] = None
    gate: Optional[str] = None
    source: Optional[str] = None


class FlightProvider:
    def get_status(self, flight_number: str) -> Optional[FlightStatus]:
        raise NotImplementedError


class MockProvider(FlightProvider):
    """Provider for local development/testing.

    Reads static fixtures from data/mock_statuses.json.
    """

    def __init__(self, fixture_path: Path) -> None:
        self.fixture_path = fixture_path

    def get_status(self, flight_number: str) -> Optional[FlightStatus]:
        if not self.fixture_path.exists():
            return None
        with self.fixture_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        row = data.get(flight_number.upper())
        if not row:
            return None
        return FlightStatus(flight_number=flight_number.upper(), source="mock", **row)


class CarrierAPIProvider(FlightProvider):
    """Provider backed by carrier (airline) endpoints.

    Config format example (JSON):
    {
      "AA": {
        "url": "https://api.airline.com/status/{flight_number}",
        "headers": {"Authorization": "Bearer ..."},
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
    """

    def __init__(self, config: dict) -> None:
        self.config = {key.upper(): value for key, value in config.items()}

    @staticmethod
    def _extract(payload: dict, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        cur = payload
        for part in path.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
            if cur is None:
                return None
        return str(cur) if cur is not None else None

    def get_status(self, flight_number: str) -> Optional[FlightStatus]:
        normalized = flight_number.upper()
        airline_code = "".join(ch for ch in normalized if ch.isalpha())
        conf = self.config.get(airline_code)
        if not conf:
            return None

        query = conf.get("query") or {}
        formatted_query = {k: str(v).format(flight_number=normalized) for k, v in query.items()}
        qs = urllib.parse.urlencode(formatted_query)
        base_url = str(conf["url"]).format(flight_number=normalized)
        url = f"{base_url}?{qs}" if qs else base_url

        req = urllib.request.Request(url=url, headers=conf.get("headers") or {})
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))

        fields = conf.get("fields") or {}
        status = self._extract(payload, fields.get("status"))
        if not status:
            return None

        return FlightStatus(
            flight_number=normalized,
            status=status,
            scheduled_departure=self._extract(payload, fields.get("scheduled_departure")),
            estimated_departure=self._extract(payload, fields.get("estimated_departure")),
            actual_departure=self._extract(payload, fields.get("actual_departure")),
            terminal=self._extract(payload, fields.get("terminal")),
            gate=self._extract(payload, fields.get("gate")),
            source=f"carrier:{airline_code}",
        )


class AviationstackProvider(FlightProvider):
    """Provider backed by aviationstack API.

    Requires AVIATIONSTACK_API_KEY in environment.
    """

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def get_status(self, flight_number: str) -> Optional[FlightStatus]:
        params = urllib.parse.urlencode({"access_key": self.api_key, "flight_iata": flight_number.upper()})
        url = f"http://api.aviationstack.com/v1/flights?{params}"
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload.get("data", [])
        if not rows:
            return None

        row = rows[0]
        departure = row.get("departure") or {}
        return FlightStatus(
            flight_number=flight_number.upper(),
            status=row.get("flight_status") or "unknown",
            scheduled_departure=departure.get("scheduled"),
            estimated_departure=departure.get("estimated"),
            actual_departure=departure.get("actual"),
            terminal=departure.get("terminal"),
            gate=departure.get("gate"),
            source="aviationstack",
        )


class ProviderChain(FlightProvider):
    """Tries providers in order and returns the first status found."""

    def __init__(self, providers: list[FlightProvider]) -> None:
        self.providers = providers

    def get_status(self, flight_number: str) -> Optional[FlightStatus]:
        for provider in self.providers:
            try:
                status = provider.get_status(flight_number)
            except Exception:
                continue
            if status is not None:
                return status
        return None


class FlightTracker:
    def __init__(self, db_path: Path, provider: FlightProvider) -> None:
        self.db_path = db_path
        self.provider = provider
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                flight_number TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flight_number TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                scheduled_departure TEXT,
                estimated_departure TEXT,
                actual_departure TEXT,
                terminal TEXT,
                gate TEXT,
                source TEXT,
                raw_json TEXT NOT NULL
            )
            """
        )
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(snapshots)").fetchall()
        }
        if "source" not in columns:
            self.conn.execute("ALTER TABLE snapshots ADD COLUMN source TEXT")
        self.conn.commit()

    def add_flight(self, flight_number: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO subscriptions(flight_number, created_at) VALUES (?, ?)",
            (flight_number.upper(), datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def remove_flight(self, flight_number: str) -> None:
        self.conn.execute("DELETE FROM subscriptions WHERE flight_number = ?", (flight_number.upper(),))
        self.conn.commit()

    def list_flights(self) -> list[str]:
        rows = self.conn.execute("SELECT flight_number FROM subscriptions ORDER BY flight_number").fetchall()
        return [r["flight_number"] for r in rows]

    def _last_snapshot(self, flight_number: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE flight_number = ? ORDER BY observed_at DESC LIMIT 1",
            (flight_number.upper(),),
        ).fetchone()

    @staticmethod
    def _changed_fields(old: dict, new: dict) -> dict[str, tuple[object, object]]:
        changes: dict[str, tuple[object, object]] = {}
        for key, value in new.items():
            if old.get(key) != value:
                changes[key] = (old.get(key), value)
        return changes

    def check_once(self) -> list[dict]:
        updates = []
        for flight_number in self.list_flights():
            status = self.provider.get_status(flight_number)
            if status is None:
                updates.append({"flight_number": flight_number, "error": "not_found"})
                continue

            current = asdict(status)
            last = self._last_snapshot(flight_number)
            old = json.loads(last["raw_json"]) if last else {}
            changes = self._changed_fields(old, current) if last else {"initial": (None, "created")}

            self.conn.execute(
                """
                INSERT INTO snapshots(
                    flight_number, observed_at, status, scheduled_departure,
                    estimated_departure, actual_departure, terminal, gate, source, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    flight_number,
                    datetime.now(timezone.utc).isoformat(),
                    status.status,
                    status.scheduled_departure,
                    status.estimated_departure,
                    status.actual_departure,
                    status.terminal,
                    status.gate,
                    status.source,
                    json.dumps(current, ensure_ascii=False),
                ),
            )
            self.conn.commit()

            updates.append(
                {
                    "flight_number": flight_number,
                    "status": current,
                    "changes": changes,
                }
            )
        return updates


def load_carrier_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_provider() -> FlightProvider:
    providers: list[FlightProvider] = []

    carrier_config_path = Path(os.getenv("CARRIER_CONFIG_PATH", "data/carrier_endpoints.json"))
    carrier_config = load_carrier_config(carrier_config_path)
    if carrier_config:
        providers.append(CarrierAPIProvider(carrier_config))

    key = os.getenv("AVIATIONSTACK_API_KEY")
    if key:
        providers.append(AviationstackProvider(key))

    providers.append(MockProvider(Path("data/mock_statuses.json")))
    return ProviderChain(providers)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Flight number change tracker")
    parser.add_argument("--db", default="flight_tracker.db", help="SQLite database path")

    sub = parser.add_subparsers(dest="command", required=True)
    add_cmd = sub.add_parser("add", help="Track new flight number")
    add_cmd.add_argument("flight_number")

    rm_cmd = sub.add_parser("remove", help="Stop tracking flight number")
    rm_cmd.add_argument("flight_number")

    get_cmd = sub.add_parser("get", help="Get live status for one flight without saving")
    get_cmd.add_argument("flight_number")

    serve_cmd = sub.add_parser("serve", help="Run web interface")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8080)

    sub.add_parser("list", help="List tracked flights")
    sub.add_parser("check", help="Fetch statuses and detect changes")
    return parser


def run_web_interface(provider: FlightProvider, tracker: FlightTracker, host: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self._render_index()
                return
            if parsed.path == "/flight":
                params = urllib.parse.parse_qs(parsed.query)
                flight_number = (params.get("number") or [""])[0].strip()
                self._render_flight_lookup(flight_number)
                return
            self.send_error(404, "Not Found")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length).decode("utf-8")
            params = urllib.parse.parse_qs(payload)
            action = params.get("action", [""])[0]
            flight_number = (params.get("flight_number") or [""])[0].strip().upper()

            if action == "add" and flight_number:
                tracker.add_flight(flight_number)
            elif action == "remove" and flight_number:
                tracker.remove_flight(flight_number)
            elif action == "check":
                tracker.check_once()
            self._redirect("/")

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

        def _send_html(self, content: str) -> None:
            encoded = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _render_index(self) -> None:
            flights = tracker.list_flights()
            rows = []
            for flight in flights:
                rows.append(
                    f"""<li><b>{html.escape(flight)}</b>
                    <form method="post" style="display:inline;margin-left:8px;">
                      <input type="hidden" name="action" value="remove" />
                      <input type="hidden" name="flight_number" value="{html.escape(flight)}" />
                      <button type="submit">Удалить</button>
                    </form>
                    <a href="/flight?number={urllib.parse.quote(flight)}" style="margin-left:8px;">Проверить сейчас</a>
                    </li>"""
                )
            flights_html = "\n".join(rows) if rows else "<li>Нет отслеживаемых рейсов</li>"

            page = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <title>Flight Tracker Web</title>
</head>
<body>
  <h1>Flight Tracker</h1>
  <form method="post">
    <input type="hidden" name="action" value="add" />
    <label>Flight number: <input name="flight_number" placeholder="AA100" required /></label>
    <button type="submit">Добавить</button>
  </form>
  <form method="post" style="margin-top:12px;">
    <input type="hidden" name="action" value="check" />
    <button type="submit">Обновить все рейсы</button>
  </form>
  <h2>Отслеживаемые рейсы</h2>
  <ul>{flights_html}</ul>
</body>
</html>
"""
            self._send_html(page)

        def _render_flight_lookup(self, flight_number: str) -> None:
            status = provider.get_status(flight_number) if flight_number else None
            if not status:
                body = "<p>Рейс не найден.</p>"
            else:
                body = f"<pre>{html.escape(json.dumps(asdict(status), ensure_ascii=False, indent=2))}</pre>"
            page = f"""<!doctype html>
<html lang="ru">
<head><meta charset="utf-8" /><title>Проверка рейса</title></head>
<body>
  <p><a href="/">← Назад</a></p>
  <h1>Проверка рейса: {html.escape(flight_number)}</h1>
  {body}
</body>
</html>"""
            self._send_html(page)

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Web interface started on http://{host}:{port}")
    server.serve_forever()


def main() -> None:
    parser = create_parser()
    args = parser.parse_args()

    provider = build_provider()
    tracker = FlightTracker(Path(args.db), provider)

    if args.command == "add":
        tracker.add_flight(args.flight_number)
        print(f"Added: {args.flight_number.upper()}")
    elif args.command == "remove":
        tracker.remove_flight(args.flight_number)
        print(f"Removed: {args.flight_number.upper()}")
    elif args.command == "list":
        flights = tracker.list_flights()
        print("\n".join(flights) if flights else "No flights tracked yet")
    elif args.command == "get":
        status = provider.get_status(args.flight_number)
        if not status:
            print("Flight not found")
            return
        print(json.dumps(asdict(status), ensure_ascii=False, indent=2))
    elif args.command == "serve":
        run_web_interface(provider, tracker, host=args.host, port=args.port)
    elif args.command == "check":
        updates = tracker.check_once()
        if not updates:
            print("No tracked flights")
            return
        for item in updates:
            if item.get("error"):
                print(f"{item['flight_number']}: not found")
                continue
            print(f"{item['flight_number']} -> {item['status']['status']} ({item['status'].get('source')})")
            if item["changes"]:
                print("  Changes:")
                for field, (old, new) in item["changes"].items():
                    print(f"    - {field}: {old} -> {new}")
            else:
                print("  No changes")


if __name__ == "__main__":
    main()
