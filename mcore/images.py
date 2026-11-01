"""Bounded image proxy; resolve and connect to the same public IP to prevent SSRF."""
import asyncio
import ipaddress
import socket
import httpx


async def image_bytes(url):
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
            async with client.stream('GET', target, headers={'Host': parsed.netloc.decode(), 'User-Agent': 'MCore/0.1'},
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
                return b''.join(chunks), media
    raise ValueError('Too many image redirects')
