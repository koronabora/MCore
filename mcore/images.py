"""Bounded image proxy; resolve and connect to the same public IP to prevent SSRF."""
import asyncio
import ipaddress
import socket
import httpx
from io import BytesIO
from PIL import Image


async def image_bytes(url, poster=False, screenshot=False):
    from .metadata import usable_image_url
    if not usable_image_url(url):
        raise ValueError('Not a media image')
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        for _ in range(4):
            parsed = httpx.URL(url)
            if parsed.scheme not in ('https', 'http') or parsed.port not in (None, 80, 443) or parsed.userinfo:
                raise ValueError('Invalid image URL')
            addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.host, parsed.port or (443 if parsed.scheme=='https' else 80), 0, socket.SOCK_STREAM)
            ips = [ipaddress.ip_address(a[4][0]) for a in addresses]
            if not ips or any(not ip.is_global for ip in ips):
                raise ValueError('Image host must resolve to public addresses')
            target = parsed.copy_with(host=str(ips[0]))
            async with client.stream('GET', target, headers={'Host': parsed.netloc.decode(), 'User-Agent': 'MCore/0.2.0'},
                                     extensions={'sni_hostname': parsed.host}) as response:
                if response.is_redirect:
                    url = str(parsed.join(response.headers['location']))
                    continue
                response.raise_for_status()
                media = response.headers.get('content-type', '').split(';')[0]
                if media not in ('image/jpeg', 'image/png', 'image/webp'):
                    raise ValueError('Unsupported image type')
                chunks, total = [], 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > 4*1024*1024:
                        raise ValueError('Image too large')
                    chunks.append(chunk)
                data = b''.join(chunks)
                if poster:
                    validate_poster(data)
                if screenshot:
                    validate_screenshot(data)
                return data, media
    raise ValueError('Too many image redirects')


def validate_poster(data):
    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            if width < 180 or height < 240 or width > height*1.35 or width*height > 25000000:
                raise ValueError('Not a usable poster')
            image.verify()
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError('Invalid poster') from error


def validate_screenshot(data):
    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            if width < 320 or height < 180 or not 1 <= width/height <= 2.5 or width*height > 25000000:
                raise ValueError('Not a usable video screenshot')
            image.verify()
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError('Invalid screenshot') from error
