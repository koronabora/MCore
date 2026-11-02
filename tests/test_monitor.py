import importlib.util
import json
from pathlib import Path
import sqlite3
import time

from mcore.store import Store

spec = importlib.util.spec_from_file_location('mcore_top', Path(__file__).parents[1]/'deploy'/'mcore-top.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def test_activity_is_read_only_while_server_writes(tmp_path):
    store = Store(tmp_path)
    user = store.create_user('tester')
    empty = store.create_user('new')
    with store.connect() as db:
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)', ('secret-session-hash', user, 'device', 1))
        db.execute('INSERT INTO history(user,movie,position,updated) VALUES (?,?,?,?)', (user, 'movie', 15000, 5))
        db.execute('INSERT INTO episode_history VALUES (?,?,?,?,?,?,?,?,?)', (user, 'movie', 's001e0002', 15000, 60000, 0, 't', 1, 5))
        db.execute('INSERT INTO releases VALUES (?,?,?,?)', ('release', 'movie', json.dumps({'title': 'Test movie'}), 1))
    writer = store.connect()
    try:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('INSERT INTO queries VALUES (?,?,?)', (user, 'uncommitted search', 6))
        snapshot = monitor.activity(store.path)
        assert snapshot[0]['event']['episode'] == 's001e0002'
        assert snapshot[0]['event']['title'] == 'Test movie'
        assert snapshot[0]['sessions'] == 1
        assert snapshot[1]['id'] == empty and snapshot[1]['event'] == {}
        assert 'secret-session-hash' not in json.dumps(snapshot)
        writer.commit()
        assert monitor.activity(store.path)[0]['event']['query'] == 'uncommitted search'
    finally:
        writer.close()
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1
        assert db.execute('SELECT position FROM history').fetchone()[0] == 15000


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path/'missing.db'
    try:
        monitor.activity(path)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError('Missing database should be reported')
    assert not path.exists()


def test_disk_scan_measures_allocated_space_and_finishes(tmp_path):
    cache = tmp_path/'cache'
    cache.mkdir()
    with (cache/'sparse-video').open('wb') as file:
        file.seek(32*1024*1024)
        file.write(b'end')
    scan = monitor.DiskScan({'cache': cache}, 60)
    try:
        scan.refresh()
        scan.thread.join(timeout=5)
        result = scan.snapshot()
        assert not result['running'] and not result['error']
        assert 0 < result['bytes']['cache'] < 32*1024*1024
        started = scan.last_started
        scan.refresh()
        assert scan.last_started == started
    finally:
        scan.close()


def test_terminal_control_characters_are_not_rendered():
    assert '\x1b' not in monitor.clean('name\x1b[2J\nnext')
    assert '\n' not in monitor.clean('name\nnext')
    assert monitor.clean('Тестировщик') == 'Тестировщик'


def test_torrent_panel_hides_stale_rates_and_distinguishes_gb(tmp_path):
    path = tmp_path/'torrent-stats.json'
    row = dict(name='Video', state='Скачивание', percent=25, uploaded=12_500_000_000,
               download_rate=1024, upload_rate=2048, metadata_only=False)
    path.write_text(json.dumps(dict(schema=1, pid=42, updated=100, torrents=[row])))
    current = monitor.torrent_activity(path, 42, 105)
    assert current['available'] and not current['stale']
    panel = '\n'.join(monitor.torrent_lines(dict(time=105, torrents=current)))
    assert '25.0%' in panel and '12.500' in panel and '2.0 KiB' in panel
    for pid, now in ((43, 105), (42, 116)):
        stale = monitor.torrent_activity(path, pid, now)
        assert stale['stale']
        panel = '\n'.join(monitor.torrent_lines(dict(time=now, torrents=stale)))
        assert '2.0 KiB' not in panel and '1.0 KiB' not in panel
        assert 'устарел' in panel


def test_missing_telemetry_does_not_break_monitor(tmp_path):
    path = tmp_path/'missing.json'
    data = monitor.torrent_activity(path, 42, time.time())
    assert not data['available'] and data['message']
    path.write_text('{incomplete')
    assert not monitor.torrent_activity(path, 42, time.time())['available']
