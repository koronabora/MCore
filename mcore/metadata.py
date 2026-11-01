"""Public tracker detail metadata, fetched only on MCore through FlareSolverr."""
import asyncio
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import httpx


def parse_detail(provider, html, url):
    soup = BeautifulSoup(html, 'html.parser')
    root = soup.select_one('.post_body' if provider == 'rutracker' else '.mn1_content')
    if root is None:
        raise ValueError('Tracker detail not found')
    for tag in root.select('script,style'):
        tag.decompose()
    for tag in root.select('br,hr'):
        tag.replace_with('\n')
    text = root.get_text(' ', strip=False)
    text = re.sub(r'[^\S\n]+', ' ', text)

    def field(label):
        match = re.search(r'(?:^|\n)\s*(?:' + label + r')\s*:\s*([^\n]+)', text, re.I)
        return match[1].strip() if match else ''

    description = ''
    if provider == 'kinozal':
        block = next((p for p in root.select('p') if re.match(r'\s*О (?:фильме|сериале|мультфильме|передаче)\s*:', p.get_text(' '))), None)
        if block:
            description = re.sub(r'^.*?:\s*', '', block.get_text(' ', strip=True), count=1)
    if not description:
        description = field('Описание|О фильме|Сюжет')
    image = soup.select_one('meta[property="og:image"]') if provider == 'kinozal' else None
    poster = urljoin(url, image.get('content', '')) if image else ''
    pictures = []
    for img in root.select('img.postImg, img.postImgAligned'):
        src = img.get('src') or img.get('data-src', '')
        if src and not any(x in src for x in ('/buttons/', 'rating.', '.gif', '/smiles/')):
            pictures.append(urljoin(url, src))
    if not poster and pictures:
        poster = pictures[0]
    video = field('Видео')
    dimensions = re.search(r'\b(\d{3,4})\s*[xх×]\s*(\d{3,4})\b', video)
    title = soup.select_one('#topic-title' if provider == 'rutracker' else 'h1')
    return {'description': description[:12000], 'director': field('Режисс[её]р')[:500],
            'poster': poster, 'screenshots': pictures[1:4], 'audio': field('Аудио')[:1500],
            'dimensions': dimensions[0] if dimensions else '',
            'detail_title': title.get_text('', strip=True) if title else ''}


class Metadata:
    def __init__(self, config):
        self.url = config.get('flaresolverr_url', 'http://127.0.0.1:8191/v1')
        self.sites = config.get('detail_sites', {'kinozal': 'https://kinozal.me', 'rutracker': 'https://rutracker.org'})
        self.client = httpx.AsyncClient(timeout=75, trust_env=False)
        self.limit = asyncio.Semaphore(2)

    async def detail(self, provider, rid):
        if provider not in self.sites or not str(rid).isdigit():
            raise ValueError('Invalid tracker detail')
        url = self.sites[provider] + ('/details.php?id=' if provider == 'kinozal' else '/forum/viewtopic.php?t=') + str(rid)
        async with self.limit:
            response = await self.client.post(self.url, json={'cmd': 'request.get', 'url': url, 'maxTimeout': 60000})
            response.raise_for_status()
            data = response.json()
        if data.get('status') != 'ok':
            raise ValueError('Tracker detail unavailable')
        return parse_detail(provider, data['solution']['response'], url)
