import json
import os
from types import SimpleNamespace

import pytest
from mcore import telemetry


def handle(**changes):
    values = dict(name='Test movie', has_metadata=True, total_done=250, all_time_upload=12_500_000_000,
                  total_upload=17, progress=.9, download_payload_rate=1024, upload_payload_rate=2048,
                  num_peers=3, num_seeds=1, state=3, paused=False,
                  errc=SimpleNamespace(value=lambda: 0))
    values.update(changes)
    return SimpleNamespace(status=lambda flags: SimpleNamespace(**values),
                           torrent_file=lambda: SimpleNamespace(total_size=lambda: 1000, name=lambda: values['name']))


def manager(handles):
    def forbidden(*args):
        raise AssertionError('Observing telemetry must not request pieces or update last_used')
    return SimpleNamespace(handles=handles, errors={}, metadata_only=set(), status=forbidden,
                           last_used={'t': 123})


def test_full_torrent_progress_cumulative_payload_and_no_scheduler_mutation():
    torrents = manager({'t': handle()})
    result = telemetry.snapshot(torrents)
    row = result['torrents'][0]
    assert result['pid'] == os.getpid()
    assert row['percent'] == 25  # Not libtorrent's wanted-files progress of 90%.
    assert row['uploaded'] == 12_500_000_000  # Not this session's protocol counter.
    assert (row['download_rate'], row['upload_rate']) == (1024, 2048)
    assert torrents.last_used == {'t': 123}


@pytest.mark.parametrize('changes,state', [({'paused': True}, 'Пауза'),
    ({'has_metadata': False, 'state': 2}, 'Получение метаданных'),
    ({'total_done': 1000, 'state': 5}, 'Раздача')])
def test_torrent_states(changes, state):
    row = telemetry.snapshot(manager({'t': handle(**changes)}))['torrents'][0]
    assert row['state'] == state
    if not changes.get('has_metadata', True):
        assert row['percent'] is None


def test_invalid_handle_does_not_hide_others_and_index_is_distinct():
    def invalid(flags):
        raise RuntimeError('invalid handle')
    torrents = manager({'bad': SimpleNamespace(status=invalid), 'index': handle()})
    torrents.metadata_only.add('index')
    rows = {row['id']: row for row in telemetry.snapshot(torrents)['torrents']}
    assert rows['bad']['download_rate'] is None
    assert rows['index']['state'] == 'Только индекс'
    assert rows['index']['metadata_only']


def test_snapshot_permissions_and_atomic_failure(tmp_path):
    path = tmp_path/'torrent-stats.json'
    value = telemetry.snapshot(manager({'t': handle()}))
    telemetry.write_snapshot(path, value)
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == value
    with pytest.raises(TypeError):
        telemetry.write_snapshot(path, {'invalid': object()})
    assert json.loads(path.read_text()) == value
    assert list(tmp_path.iterdir()) == [path]
