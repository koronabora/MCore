import hashlib
import secrets
import sqlite3
import time
from pathlib import Path


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, directory):
        self.path = Path(directory) / 'mcore.db'
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, login TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS invites (hash TEXT PRIMARY KEY, user TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, user TEXT NOT NULL, device TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS history (user TEXT, movie TEXT, position INTEGER, updated REAL, PRIMARY KEY(user,movie));
                CREATE TABLE IF NOT EXISTS queries (user TEXT, query TEXT, updated REAL, PRIMARY KEY(user,query));
                CREATE TABLE IF NOT EXISTS releases (id TEXT PRIMARY KEY, movie TEXT, data TEXT, updated REAL);
                CREATE TABLE IF NOT EXISTS torrents (id TEXT PRIMARY KEY, magnet TEXT, added REAL, size INTEGER DEFAULT 0);
            ''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(history)')}
            for name, kind in [('torrent', 'TEXT'), ('file', 'INTEGER')]:
                if name not in columns:
                    db.execute(f'ALTER TABLE history ADD COLUMN {name} {kind}')
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def create_user(self, login):
        if not login.strip() or len(login) > 100:
            raise ValueError('Invalid login')
        uid = secrets.token_hex(16)
        with self.connect() as db:
            db.execute('INSERT INTO users VALUES (?,?)', (uid, login))
        return uid

    def invite(self, login):
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            user = db.execute('SELECT id FROM users WHERE login=?', (login,)).fetchone()
            if not user:
                raise ValueError('Unknown user')
            db.execute('DELETE FROM invites WHERE expires < ?', (time.time(),))
            db.execute('INSERT INTO invites VALUES (?,?,?)', (digest(token), user['id'], time.time()+120))
        return token

    def redeem(self, token, device):
        key = secrets.token_urlsafe(32)
        with self.connect() as db:
            # DELETE RETURNING consumes the invitation atomically, including concurrent requests.
            row = db.execute('DELETE FROM invites WHERE hash=? AND expires>? RETURNING user',
                             (digest(token), time.time())).fetchone()
            if row is None:
                return None
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest(key), row['user'], device, time.time()))
        return {'key': key, 'user_id': row['user']}

    def authenticate(self, key):
        with self.connect() as db:
            row = db.execute('SELECT user FROM sessions WHERE hash=?', (digest(key),)).fetchone()
            return row['user'] if row else None
