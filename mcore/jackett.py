"""Server-only Jackett adapter. API keys and download links never reach MApp."""
from pathlib import Path
import re
import httpx


class Jackett:
    def __init__(self, config):
        self.url = config['url'].rstrip('/')
        self.key = Path(config['api_key_file']).read_text().strip()
        self.indexers = config.get('indexers', {'rutracker': 'rutracker', 'kinozal': 'kinozal-magnet'})
        self.client = httpx.AsyncClient(timeout=150, trust_env=False)

    async def search(self, provider, query):
        response = await self.client.get(self.url + '/api/v2.0/indexers/' + self.indexers[provider] + '/results',
                                         params={'apikey': self.key, 'Query': query, 'Category': '2000,5000'})
        response.raise_for_status()
        data = response.json()
        if any(i.get('Error') for i in data.get('Indexers', [])):
            raise ValueError('Jackett indexer unavailable')
        results = []
        for row in data['Results']:
            if not any(2000 <= int(c) < 3000 or 5000 <= int(c) < 6000 for c in row.get('Category', [])):
                continue
            match = re.search(r'[?&](?:t|id)=(\d+)', row.get('Details') or row.get('Guid') or '')
            if match:
                results.append({'Id': match[1], 'Title': row['Title'], 'Size': self.size(row.get('Size', 0)),
                                'Seeds': row.get('Seeders'), 'Peers': row.get('Peers'),
                                'Magnet': row.get('MagnetUri') or '', 'download': row.get('Link') or '',
                                'description': row.get('Description') or '', 'poster': row.get('Poster') or ''})
        return results

    @staticmethod
    def size(value):
        value = int(value or 0)
        return f'{value / 1024**3:.2f} GiB' if value >= 1024**3 else f'{value / 1024**2:.1f} MiB'

    async def download(self, url):
        origin = httpx.URL(self.url)
        for _ in range(4):
            if url.startswith('magnet:?'):
                return url
            target = httpx.URL(url)
            if (target.scheme, target.host, target.port) != (origin.scheme, origin.host, origin.port):
                raise ValueError('Download must go through Jackett')
            async with self.client.stream('GET', target) as response:
                if response.is_redirect:
                    url = str(target.join(response.headers['location']))
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 16 * 1024**2:
                        raise ValueError('Torrent metadata too large')
                    chunks.append(chunk)
                content = b''.join(chunks)
                if content.startswith(b'magnet:?'):
                    return content.decode().strip()
                return content
        raise ValueError('Too many Jackett redirects')
