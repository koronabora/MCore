import socket
import time
import libtorrent as lt
import pytest
from mcore.network import session_settings, validate_torrent


@pytest.mark.parametrize('settings', [
    {'listen_port': 45873}, {'listen_port': 0}, {'listen_port': 65536},
    {'listen_port': True}, {'listen_addresses': []}, {'listen_addresses': ['bad-host']},
    {'enable_dht': 'false'}, {'upload_rate_limit': -1}, {'connections_limit': 0},
    {'outgoing_port_first': 50000},
    {'outgoing_port_first': 51400, 'outgoing_port_last': 51420},
    {'unknown_setting': True},
])
def test_reject_invalid_network_config(settings):
    with pytest.raises(ValueError):
        validate_torrent(settings, 45873)


def test_ipv6_format_and_outgoing_port_count():
    settings = session_settings(validate_torrent({'listen_addresses': ['127.0.0.1', '::1'],
                                'outgoing_port_first': 55000, 'outgoing_port_last': 55009}, 45873))
    assert settings['listen_interfaces'] == '127.0.0.1:51413,[::1]:51413'
    assert settings['num_outgoing_ports'] == 10


def test_real_libtorrent_binds_configured_tcp_and_udp():
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    config = validate_torrent({'listen_port': port, 'listen_addresses': ['127.0.0.1'],
                               'enable_dht': False, 'upload_rate_limit': 65536}, 45873)
    session = lt.session(session_settings(config))
    deadline = time.monotonic() + 5
    while not session.is_listening() and time.monotonic() < deadline:
        time.sleep(.02)
    try:
        assert session.listen_port() == port
        assert session.get_settings()['upload_rate_limit'] == 65536
        with socket.create_connection(('127.0.0.1', port), timeout=2):
            pass
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            with pytest.raises(OSError):
                udp.bind(('127.0.0.1', port))
    finally:
        session.pause()
