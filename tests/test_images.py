import asyncio
import socket
import pytest
from mcore.images import image_bytes


@pytest.mark.parametrize('address', ['127.0.0.1', '10.16.16.31', '169.254.169.254', '::1', '::ffff:127.0.0.1'])
def test_poster_source_cannot_reach_private_network(monkeypatch, address):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))])
    with pytest.raises(ValueError, match='public'):
        asyncio.run(image_bytes('https://untrusted-poster.example/image.jpg'))
