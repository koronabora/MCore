import argparse
import hashlib
import ssl
from urllib.parse import urlencode
from .config import load
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='Local-only MCore account administration')
    parser.add_argument('action', choices=['create', 'invite', 'revoke'])
    parser.add_argument('login')
    args = parser.parse_args()
    config = load()
    store = Store(config['state_dir'])
    if args.action == 'create':
        print(store.create_user(args.login))
    elif args.action == 'invite':
        with open(config['tls_cert']) as f:
            fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(f.read())).hexdigest()
        print('mapp://pair?' + urlencode({'server': config['public_url'], 'token': store.invite(args.login), 'pin': fingerprint}))
    else:
        with store.connect() as db:
            user = db.execute('SELECT id FROM users WHERE login=?', (args.login,)).fetchone()
            if not user:
                raise SystemExit('Unknown user')
            db.execute('DELETE FROM sessions WHERE user=?', (user['id'],))
            db.execute('DELETE FROM invites WHERE user=?', (user['id'],))
        print('All sessions and invitations revoked')


if __name__ == '__main__':
    main()
