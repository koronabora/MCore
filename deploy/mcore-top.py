#!/usr/bin/env python3
"""Read-only Linux console monitor; independent of the running MCore process."""
import argparse
import curses
import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import unicodedata


def clean(value):
    return ''.join(c if not unicodedata.category(c).startswith('C') else ' ' for c in str(value))


def size(value):
    if value is None:
        return '—'
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(value) < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024


def duration(seconds):
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours}:{minutes:02}:{seconds:02}'


def age(timestamp, now):
    if not timestamp:
        return 'нет событий'
    seconds = max(0, int(now-timestamp))
    if seconds < 60:
        return f'{seconds}с назад'
    if seconds < 3600:
        return f'{seconds//60}м назад'
    if seconds < 86400:
        return f'{seconds//3600}ч назад'
    return f'{seconds//86400}д назад'


def service():
    result = subprocess.run(['systemctl', 'show', 'mcore.service', '--no-pager',
                             '--property=MainPID,ActiveState,SubState,InvocationID'],
                            capture_output=True, text=True, timeout=2, check=True)
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def counters(pid, interface):
    values = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
    result = {'clock': time.monotonic(), 'cpu_total': sum(values), 'cpu_idle': sum(values[3:5]), 'pid': pid}
    memory = {line.split(':')[0]: int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()}
    result.update(memory_total=memory['MemTotal'], memory_available=memory['MemAvailable'],
                  swap_total=memory['SwapTotal'], swap_free=memory['SwapFree'],
                  uptime=float(Path('/proc/uptime').read_text().split()[0]), load=os.getloadavg())
    result.update(process_ticks=None, rss=None, process_read=None, process_write=None)
    if pid:
        try:
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            result.update(process_ticks=int(fields[11])+int(fields[12]),
                          process_start=int(fields[19]), rss=int(fields[21])*os.sysconf('SC_PAGE_SIZE'))
            io = dict(line.split(': ') for line in Path(f'/proc/{pid}/io').read_text().splitlines())
            result.update(process_read=int(io['read_bytes']), process_write=int(io['write_bytes']))
        except (OSError, ValueError, IndexError):
            pass  # The service may stop between systemctl and /proc reads.
    result.update(rx=None, tx=None)
    for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
        name, fields = line.split(':', 1)
        if name.strip() == interface:
            fields = fields.split()
            result.update(rx=int(fields[0]), tx=int(fields[8]))
    return result


def rates(old, new):
    result = dict(cpu=None, process_cpu=None, rx=None, tx=None, process_read=None, process_write=None)
    if not old:
        return result
    elapsed = new['clock']-old['clock']
    total = new['cpu_total']-old['cpu_total']
    if total > 0:
        result['cpu'] = max(0, min(100, 100*(1-(new['cpu_idle']-old['cpu_idle'])/total)))
    if elapsed <= 0:
        return result
    same_process = new['pid'] == old['pid'] and new.get('process_start') == old.get('process_start')
    for key in ('rx', 'tx', 'process_read', 'process_write'):
        if key.startswith('process_') and not same_process:
            continue
        if new.get(key) is not None and old.get(key) is not None and new[key] >= old[key]:
            result[key] = (new[key]-old[key])/elapsed
    if same_process and new['process_ticks'] is not None and old['process_ticks'] is not None:
        result['process_cpu'] = max(0, (new['process_ticks']-old['process_ticks'])/os.sysconf('SC_CLK_TCK')/elapsed*100)
    return result


def network_interface():
    for line in Path('/proc/net/route').read_text().splitlines()[1:]:
        fields = line.split()
        if fields[1] == '00000000' and int(fields[3], 16) & 1:
            return fields[0]
    return next((p.name for p in Path('/sys/class/net').iterdir() if p.name != 'lo'), 'lo')


def connections(port):
    count, listening = 0, False
    for path in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            for line in Path(path).read_text().splitlines()[1:]:
                fields = line.split()
                if int(fields[1].rsplit(':', 1)[1], 16) == port:
                    count += fields[3] == '01'
                    listening |= fields[3] == '0A'
        except FileNotFoundError:
            pass
    return {'established': count, 'listening': listening}


def activity(database):
    """No Store constructor, migrations, tokens, or writes; short bounded reads only."""
    db = sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True, timeout=.25)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        deadline = time.monotonic()+.75
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        users = [dict(row) for row in db.execute('''
            SELECT u.id,u.login,COUNT(s.hash) AS sessions,MAX(s.created) AS paired
            FROM users u LEFT JOIN sessions s ON s.user=u.id GROUP BY u.id ORDER BY u.login''')]
        rows = db.execute('''
            SELECT * FROM (
                SELECT *,ROW_NUMBER() OVER(PARTITION BY user ORDER BY updated DESC,priority DESC) AS rank FROM (
                    SELECT user,movie,episode,position,updated,'view' AS kind,'' AS query,2 AS priority FROM episode_history
                    UNION ALL SELECT user,movie,'' AS episode,position,updated,'view','',1 FROM history
                    UNION ALL SELECT user,'','',0,updated,'search',query,0 FROM queries
                )
            ) WHERE rank=1''').fetchall()
        events = {r['user']: dict(r) for r in rows}
        for user in users:
            event = events.get(user['id'], {})
            if (user['paired'] or 0) > (event.get('updated') or 0):
                event = dict(updated=user['paired'], kind='pair')
            user['event'] = event
            if event.get('movie'):
                row = db.execute('SELECT data FROM releases WHERE movie=? LIMIT 1', (event['movie'],)).fetchone()
                try:
                    event['title'] = json.loads(row['data']).get('title', event['movie']) if row else event['movie']
                except (ValueError, TypeError):
                    event['title'] = event['movie']
        users.sort(key=lambda u: u['event'].get('updated', 0), reverse=True)
        return users
    finally:
        db.close()


class DiskScan:
    """Slow disk walks run off-screen, at most once per minute, at low priority."""
    def __init__(self, paths, interval):
        self.paths, self.interval = paths, interval
        self.result = {'bytes': {}, 'updated': 0, 'error': '', 'running': False}
        self.lock = threading.Lock()
        self.thread = None
        self.process = None
        self.stopped = False
        self.last_started = 0

    def refresh(self):
        if self.stopped or (self.thread and self.thread.is_alive()) or time.monotonic()-self.last_started < self.interval:
            return
        self.last_started = time.monotonic()
        with self.lock:
            self.result['running'] = True
        self.thread = threading.Thread(target=self.scan, daemon=True)
        self.thread.start()

    def scan(self):
        found = {name: path for name, path in self.paths.items() if path.exists()}
        result = {'bytes': {name: 0 for name in self.paths if name not in found}, 'updated': time.time(), 'error': '', 'running': False}
        try:
            if found:
                with self.lock:
                    if self.stopped:
                        return
                    self.process = subprocess.Popen(['nice', '-n', '19', 'du', '-s', '-B1', '--', *map(str, found.values())],
                                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    process = self.process
                try:
                    out, err = process.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise RuntimeError('Подсчёт каталогов превысил 20с')
                for name, path in found.items():
                    for line in out.splitlines():
                        amount, _, entry = line.partition('\t')
                        if entry == str(path):
                            result['bytes'][name] = int(amount)
                if process.returncode:
                    result['error'] = clean(err.strip())[:150] or 'Подсчёт прерван'
        except (OSError, ValueError, RuntimeError) as error:
            result['error'] = clean(error)
        with self.lock:
            self.result = result
            self.process = None

    def snapshot(self):
        with self.lock:
            return dict(self.result, bytes=dict(self.result['bytes']))

    def close(self):
        with self.lock:
            self.stopped = True
            if self.process and self.process.poll() is None:
                self.process.terminate()
        if self.thread:
            self.thread.join(timeout=2)


class Monitor:
    def __init__(self, config, interface, disk_interval):
        self.config, self.interface = config, interface
        self.database = Path(config['state_dir'])/'mcore.db'
        self.disk = DiskScan({'cache': Path(config['cache_dir']), 'state': Path(config['state_dir']),
                              'backups': Path('/var/backups/mcore')}, disk_interval)
        self.previous = None

    def sample(self):
        result = {'time': time.time(), 'interface': self.interface, 'errors': []}
        try:
            result['service'] = service()
        except (OSError, subprocess.SubprocessError) as error:
            result['service'] = {}
            result['errors'].append('systemctl: '+clean(error))
        current = counters(int(result['service'].get('MainPID', 0)), self.interface)
        result.update(system=current, rates=rates(self.previous, current), connections=connections(int(self.config['port'])))
        self.previous = current
        try:
            result['users'] = activity(self.database)
        except (OSError, sqlite3.Error) as error:
            result['users'] = []
            result['errors'].append('База: '+clean(error))
        result['volumes'] = []
        for path in dict.fromkeys(('/', self.config['cache_dir'], self.config['state_dir'])):
            try:
                usage = shutil.disk_usage(path)
                result['volumes'].append(dict(path=path, total=usage.total, used=usage.used, free=usage.free))
            except OSError as error:
                result['errors'].append(clean(error))
        self.disk.refresh()
        result['disk'] = self.disk.snapshot()
        result['cache_limit'] = self.config['cache_bytes']
        return result


def percent(value):
    return '—' if value is None else f'{value:.1f}%'


def lines(snapshot, offset=0):
    s, r, svc = snapshot['system'], snapshot['rates'], snapshot['service']
    now = snapshot['time']
    yield 'MCORE TOP   '+datetime.datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S')
    yield f"MCore: {svc.get('ActiveState', '?')}/{svc.get('SubState', '?')}  PID {s['pid']}  CPU {percent(r['process_cpu'])} (100%=1 ядро)  RSS {size(s['rss'])}"
    conn = snapshot['connections']
    yield f"API: {'слушает' if conn['listening'] else 'НЕ слушает'}  TCP-соединений: {conn['established']}  Аптайм ОС: {duration(s['uptime'])}"
    yield f"CPU ОС {percent(r['cpu'])}  Load {' / '.join(f'{v:.2f}' for v in s['load'])}  RAM {size(s['memory_total']-s['memory_available'])}/{size(s['memory_total'])}  Swap {size(s['swap_total']-s['swap_free'])}/{size(s['swap_total'])}"
    yield f"Сеть ОС [{snapshot['interface']}]: ↓ {size(r['rx'])}/с  ↑ {size(r['tx'])}/с  Диск MCore: чтение {size(r['process_read'])}/с  запись {size(r['process_write'])}/с"
    yield ''
    yield 'ДИСКИ (строки одного раздела не суммировать)'
    for volume in snapshot['volumes']:
        yield f"{volume['path']}: занято {size(volume['used'])}/{size(volume['total'])} ({100*volume['used']/volume['total']:.1f}%)  свободно {size(volume['free'])}"
    disk = snapshot['disk']
    yield f"Каталоги, физически: видео {size(disk['bytes'].get('cache'))}  данные {size(disk['bytes'].get('state'))}  бэкапы {size(disk['bytes'].get('backups'))}"
    yield f"Лимит видеокеша: {size(snapshot['cache_limit'])} (логический)  Подсчёт: {'в работе' if disk['running'] else age(disk['updated'], now)}"
    yield ''
    yield f"ПОЛЬЗОВАТЕЛИ: {len(snapshot['users'])}  (↑/↓ — прокрутка)"
    yield 'События — отметки приложения, не точный онлайн. Ключи = сохранённые входы.'
    for user in snapshot['users'][offset:]:
        event = user['event']
        kind = event.get('kind')
        if kind == 'view':
            detail = f"Просмотр: {event.get('title', '?')} {event.get('episode', '')}  позиция {duration(event['position']/1000)}"
        elif kind == 'search':
            detail = 'Поиск: '+event['query']
        elif kind == 'pair':
            detail = 'Подключено устройство'
        else:
            detail = 'Нет сохранённой активности'
        yield clean(f"{user['login']}  | ключей {user['sessions']} | {age(event.get('updated'), now)} | {detail}")
    if disk['error']:
        yield 'Диск: '+disk['error']
    for error in snapshot['errors']:
        yield 'Ошибка: '+error


def interactive(screen, monitor, interval):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(200)
    offset, paused, due, snapshot = 0, False, 0, None
    while True:
        if not paused and time.monotonic() >= due:
            snapshot = monitor.sample()
            due = time.monotonic()+interval
        if snapshot:
            screen.erase()
            height, width = screen.getmaxyx()
            content = list(lines(snapshot, offset))
            for row, text in enumerate(content[:max(0, height-1)]):
                try:
                    screen.addnstr(row, 0, clean(text), max(0, width-1), curses.A_BOLD if row == 0 else 0)
                except curses.error:
                    pass
            footer = ('ПАУЗА  ' if paused else '')+'q: выход  пробел: пауза  r: обновить  ↑/↓: пользователи'
            try:
                screen.addnstr(height-1, 0, footer, max(0, width-1), curses.A_REVERSE)
            except curses.error:
                pass
            screen.refresh()
        key = screen.getch()
        if key in (ord('q'), ord('Q'), 27):
            return
        if key == ord(' '):
            paused = not paused
        elif key in (ord('r'), ord('R')):
            paused, due = False, 0
        elif key == curses.KEY_DOWN and snapshot:
            offset = min(max(0, len(snapshot['users'])-1), offset+1)
        elif key == curses.KEY_UP:
            offset = max(0, offset-1)


def main():
    parser = argparse.ArgumentParser(description='MCore: консольный монитор без перезапуска сервиса')
    parser.add_argument('--config', default=os.environ.get('MCORE_CONFIG', '/etc/mcore/config.toml'))
    parser.add_argument('--interval', type=float, default=2, help='обновление экрана, секунды (минимум 1)')
    parser.add_argument('--disk-interval', type=float, default=60, help='подсчёт каталогов, секунды (минимум 60)')
    parser.add_argument('--interface', help='сетевой интерфейс; по умолчанию основной маршрут')
    parser.add_argument('--once', action='store_true', help='один текстовый снимок')
    parser.add_argument('--json', action='store_true', help='один снимок JSON')
    args = parser.parse_args()
    if args.interval < 1 or args.disk_interval < 60:
        parser.error('interval >= 1, disk-interval >= 60')
    try:
        with open(args.config, 'rb') as file:
            config = tomllib.load(file)
        monitor = Monitor(config, args.interface or network_interface(), args.disk_interval)
        try:
            if args.once or args.json or not sys.stdout.isatty() or not sys.stdin.isatty():
                monitor.previous = counters(int(service().get('MainPID', 0)), monitor.interface)
                time.sleep(.2)
                snapshot = monitor.sample()
                if monitor.disk.thread:
                    monitor.disk.thread.join(timeout=21)
                snapshot['disk'] = monitor.disk.snapshot()
                print(json.dumps(snapshot, ensure_ascii=False, indent=2) if args.json else '\n'.join(lines(snapshot)))
            else:
                curses.wrapper(interactive, monitor, args.interval)
        finally:
            monitor.disk.close()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, curses.error) as error:
        raise SystemExit('mcore-top: '+clean(error))


if __name__ == '__main__':
    main()
