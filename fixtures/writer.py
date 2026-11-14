"""Real filesystem rotation producer. Truth data is reserved for independent QA."""
import argparse
import base64
import json
import os
import shutil
import time
from pathlib import Path

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--scenario', choices=['rename', 'copytruncate', 'partial'], required=True)
    p.add_argument('--log-dir', required=True)
    p.add_argument('--truth-file', required=True)
    p.add_argument('--stop-file', required=True)
    p.add_argument('--interval', type=float, default=.35)
    p.add_argument('--start-delay', type=float, default=.5)
    args = p.parse_args(); directory = Path(args.log_dir); directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()): raise SystemExit('Use an empty log directory.')
    truth = Path(args.truth_file); stop = Path(args.stop_file)
    if truth.resolve().is_relative_to(directory.resolve()) or stop.resolve().is_relative_to(directory.resolve()):
        raise SystemExit('truth-file and stop-file must be outside log-dir')
    if stop.exists(): raise SystemExit('stop-file already exists')
    values = json.loads(Path(__file__).with_name('scenarios.json').read_text())[args.scenario]
    truth.write_text(''); active = directory / 'active.log'; generation = 0
    def raw(index): return (json.dumps({'id': index, 'message': values[index]}, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    def append(handle, data, label):
        start = handle.tell(); handle.write(data); handle.flush(); os.fsync(handle.fileno())
        with truth.open('a') as out:
            out.write(json.dumps({'event': label, 'generation': generation, 'dev': os.fstat(handle.fileno()).st_dev, 'inode': os.fstat(handle.fileno()).st_ino, 'start': start, 'end': handle.tell(), 'bytes_base64': base64.b64encode(data).decode()}) + '\n')
        print(label, flush=True); time.sleep(args.interval)
    with active.open('wb', buffering=0) as old:
        print('ready', flush=True); time.sleep(args.start_delay)
        if args.scenario == 'rename':
            append(old, raw(0), 'old-before-rename')
            line = raw(1); split = line.index('中'.encode()) + 1
            append(old, line[:split], 'old-partial-before-rename')
            active.rename(directory / 'active.log.1')
            with active.open('wb', buffering=0) as new:
                generation = 1; append(new, raw(2), 'new-active')
                generation = 0; append(old, line[split:], 'old-finish-after-rename'); append(old, raw(3), 'old-late-append')
                generation = 1; append(new, raw(4), 'new-active-late')
        elif args.scenario == 'copytruncate':
            append(old, raw(0), 'generation-0'); append(old, raw(1), 'generation-0-second')
            shutil.copyfile(active, directory / 'active.log.1')
            old.truncate(0); old.seek(0); generation = 1
            append(old, raw(2), 'generation-1'); append(old, raw(3), 'generation-1-second')
        else:
            line = raw(0); split = line.index('中'.encode()) + 1
            append(old, line[:split], 'partial-first'); append(old, line[split:-1], 'partial-no-LF'); append(old, line[-1:], 'partial-final-LF')
            append(old, b'{bad json}\n', 'invalid-json-complete-line')
            append(old, b'{"id":1,"message":"valid after bad"}\n', 'valid-after-bad')
    stop.write_text('producer closed all handles\n'); print('complete', flush=True)

if __name__ == '__main__': main()
