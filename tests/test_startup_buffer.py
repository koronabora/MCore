from types import SimpleNamespace
from mcore.torrents import Torrents


def buffer_fixture():
    mib=1024**2
    pieces=set(range(10)) | set(range(92,100))
    state=SimpleNamespace(progress=.18, download_payload_rate=mib, num_seeds=2, num_peers=3,
                          has_metadata=True, errc=SimpleNamespace(value=lambda:0))
    handle=SimpleNamespace(status=lambda:state, have_piece=pieces.__contains__,set_piece_deadline=lambda *args:None)
    info=SimpleNamespace(piece_length=lambda:mib,piece_size=lambda p:mib)
    torrents=SimpleNamespace(handles={'t':handle},last_used={},errors={},files=lambda key:[],
                             file=lambda key,index:(handle,info,None,100*mib,0),media_info={('t',0):{'duration_ms':3600000}})
    return torrents,pieces


def test_start_requires_percentage_minutes_and_container():
    torrents,pieces=buffer_fixture()
    assert Torrents.status(torrents,'t')['ready']
    assert not Torrents.status(torrents,'t',seconds=600)['ready']
    assert not Torrents.status(torrents,'t',percent=20)['ready']
    pieces.remove(99)
    result=Torrents.status(torrents,'t')
    assert not result['ready'] and not result['index_ready']
    pieces.add(99)
    torrents.media_info={}
    assert not Torrents.status(torrents,'t')['ready']


def test_cached_file_starts_immediately_and_resume_is_clamped_to_remaining():
    torrents,pieces=buffer_fixture()
    pieces.update(range(100))
    result=Torrents.status(torrents,'t',seconds=900,position=3590000,percent=100)
    assert result['ready'] and result['index_ready']
    assert 0<result['buffer_bytes']<1024**2
