from concurrent.futures import ThreadPoolExecutor
import time
from mcore.store import Store, digest


def test_invitation_is_single_use_even_when_racing(tmp_path):
    store = Store(tmp_path)
    uid = store.create_user('test')
    token = store.invite('test')
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.redeem(token, 'phone'), range(8)))
    successes = [r for r in results if r]
    assert len(successes) == 1
    assert store.authenticate(successes[0]['key']) == uid
    assert store.authenticate('wrong') is None
    with store.connect() as db:
        assert db.execute('SELECT hash FROM sessions').fetchone()['hash'] != successes[0]['key']


def test_expired_invite_and_reinstallation(tmp_path):
    store = Store(tmp_path)
    store.create_user('test')
    old = store.invite('test')
    with store.connect() as db:
        db.execute('UPDATE invites SET expires=? WHERE hash=?', (time.time()-1, digest(old)))
    assert store.redeem(old, 'phone') is None
    first = store.redeem(store.invite('test'), 'phone')
    second = store.redeem(store.invite('test'), 'reinstalled-phone')
    assert first['user_id'] == second['user_id']
    assert first['key'] != second['key']
