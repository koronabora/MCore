import json
import pytest
from mcore.torrents import byte_range, cache_path


@pytest.mark.parametrize('header,expected', [(None, (0,99,200)), ('bytes=10-19',(10,19,206)),
                                          ('bytes=-10',(90,99,206)), ('bytes=95-',(95,99,206)),
                                          ('bytes=0-999',(0,99,206))])
def test_range(header, expected):
    assert byte_range(header, 100) == expected


@pytest.mark.parametrize('header', ['bytes=100-', 'bytes=50-20', 'bytes=-0', 'bytes=0-1,5-6', 'bytes=-'])
def test_invalid_range(header):
    with pytest.raises(ValueError):
        byte_range(header, 100)


def test_cache_move_and_conflicting_destination(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    old, new = tmp_path/'old', tmp_path/'new'
    config = {'state_dir': str(state), 'cache_dir': str(old)}
    cache_path(config)
    (old/'video').write_bytes(b'original bytes')
    config['cache_dir'] = str(new)
    new.mkdir()
    assert cache_path(config) == old
    assert (old/'video').read_bytes() == b'original bytes'
    new.rmdir()
    assert cache_path(config) == new
    assert (new/'video').read_bytes() == b'original bytes'
    assert not old.exists()
    assert json.loads((state/'cache-location.json').read_text()) == str(new)
