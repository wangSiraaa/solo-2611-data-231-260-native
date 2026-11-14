"""Local capped-search input service. This is not the extraction implementation."""
import argparse
import json
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8231)
    parser.add_argument('--ready-file')
    parser.add_argument('--truth-log', required=True, help='QA-only, outside collector output')
    parser.add_argument('--request-log', required=True)
    args = parser.parse_args()
    source = json.loads(Path(__file__).with_name('scenarios.json').read_text())
    config = json.loads(Path(__file__).with_name('config.json').read_text())
    snapshots = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def reply(self, status, body):
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
        def do_POST(self):
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 65536: raise ValueError('invalid body length')
                body = json.loads(self.rfile.read(length))
            except (ValueError, TypeError):
                return self.reply(400, {'error': 'bad_json'})
            with open(args.request_log, 'a') as out:
                out.write(json.dumps({'path': self.path, 'body': body}) + '\n')
            if self.path == '/open':
                mode = body.get('scenario', 'normal')
                if mode not in source: return self.reply(400, {'error': 'unknown_scenario'})
                sid = 'snapshot-' + str(len(snapshots) + 1)
                rows = json.loads(json.dumps(source[mode]))
                snapshots[sid] = {'mode': mode, 'rows': rows, 'calls': 0, 'live_generation': 0, 'live_rows': json.loads(json.dumps(rows))}
                with open(args.truth_log, 'a') as out:
                    out.write(json.dumps({'snapshot_id': sid, 'scenario': mode, 'rows': rows}, ensure_ascii=False) + '\n')
                return self.reply(200, {'snapshot_id': sid, 'scenario': mode, 'cap': config['cap'], 't_range': config['t_range'], 'bucket_range': config['bucket_range']})
            if self.path == '/search':
                snap = snapshots.get(body.get('snapshot_id'))
                if snap is None: return self.reply(404, {'error': 'unknown_snapshot'})
                snap['calls'] += 1
                if snap['mode'] == 'expired' and snap['calls'] > config['expires_after_searches_for_expired']:
                    return self.reply(410, {'error': 'snapshot_expired', 'snapshot_id': body['snapshot_id']})
                try:
                    tr, br = body['t_range'], body['bucket_range']
                    for values, domain in [(tr, config['t_range']), (br, config['bucket_range'])]:
                        if len(values) != 2 or any(type(x) is not int for x in values) or not domain[0] <= values[0] < values[1] <= domain[1]:
                            raise ValueError('range')
                except (KeyError, TypeError, ValueError):
                    return self.reply(400, {'error': 'invalid_range'})
                snap['live_generation'] += 1
                snap['live_rows'].append({'record_id': 'live-' + str(snap['live_generation']), 't': 0, 'bucket': 0})  # Frozen rows never change.
                rows = sorted((r for r in snap['rows'] if tr[0] <= r['t'] < tr[1] and br[0] <= r['bucket'] < br[1]), key=lambda r: r['record_id'])
                return self.reply(200, {'snapshot_id': body['snapshot_id'], 't_range': tr, 'bucket_range': br, 'count': len(rows), 'cap': config['cap'], 'rows': rows[:config['cap']]})
            return self.reply(404, {'error': 'unknown_endpoint'})
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    ready = {'port': server.server_port, 'base_url': f'http://127.0.0.1:{server.server_port}'}
    if args.ready_file: Path(args.ready_file).write_text(json.dumps(ready))
    print(json.dumps(ready), flush=True)
    server.serve_forever()

if __name__ == '__main__': main()
