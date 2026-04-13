import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from app import CarrierAPIProvider, FlightTracker, MockProvider, create_parser


class _CarrierHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/aa/status/AA100'):
            body = {
                'status': 'boarding',
                'departure': {
                    'scheduled': '2026-04-13T09:00:00+00:00',
                    'estimated': '2026-04-13T09:12:00+00:00',
                    'actual': None,
                    'terminal': '4',
                    'gate': 'D1',
                },
            }
            payload = json.dumps(body).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


def test_add_list_remove(tmp_path: Path):
    db = tmp_path / 'tracker.db'
    fixture = tmp_path / 'fixture.json'
    fixture.write_text('{"AA100":{"status":"scheduled"}}', encoding='utf-8')

    tracker = FlightTracker(db, MockProvider(fixture))

    tracker.add_flight('aa100')
    assert tracker.list_flights() == ['AA100']

    tracker.remove_flight('AA100')
    assert tracker.list_flights() == []


def test_check_detects_changes(tmp_path: Path):
    db = tmp_path / 'tracker.db'
    fixture = tmp_path / 'fixture.json'
    fixture.write_text('{"AA100":{"status":"scheduled"}}', encoding='utf-8')
    tracker = FlightTracker(db, MockProvider(fixture))
    tracker.add_flight('AA100')

    first = tracker.check_once()
    assert first[0]['changes']

    fixture.write_text('{"AA100":{"status":"active"}}', encoding='utf-8')
    second = tracker.check_once()
    assert 'status' in second[0]['changes']


def test_carrier_provider_returns_live_status():
    server = HTTPServer(('127.0.0.1', 0), _CarrierHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conf = {
            'AA': {
                'url': f'http://{host}:{port}/aa/status/{{flight_number}}',
                'fields': {
                    'status': 'status',
                    'scheduled_departure': 'departure.scheduled',
                    'estimated_departure': 'departure.estimated',
                    'actual_departure': 'departure.actual',
                    'terminal': 'departure.terminal',
                    'gate': 'departure.gate',
                },
            }
        }
        provider = CarrierAPIProvider(conf)
        status = provider.get_status('AA100')
        assert status is not None
        assert status.status == 'boarding'
        assert status.source == 'carrier:AA'
        assert status.gate == 'D1'
    finally:
        server.shutdown()
        server.server_close()


def test_parser_supports_web_interface_command():
    parser = create_parser()
    args = parser.parse_args(['serve', '--host', '0.0.0.0', '--port', '9090'])
    assert args.command == 'serve'
    assert args.host == '0.0.0.0'
    assert args.port == 9090
