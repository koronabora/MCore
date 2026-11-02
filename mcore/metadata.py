"""Public tracker detail metadata, fetched only on MCore through FlareSolverr."""
import asyncio
import re
from html import unescape
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import httpx


def clean_description(value):
    """Normalize tracker HTML, including escaped markup; discard indexer uploader blurbs."""
    value = str(value or '')
    for _ in range(3):
        decoded = unescape(value)
        if decoded == value:
            break
        value = decoded
    soup = BeautifulSoup(value, 'html.parser')
    for tag in soup.select('script,style'):
        tag.decompose()
    for tag in soup.select('br,hr'):
        tag.replace_with('\n')
    for tag in soup.select('p,div,li'):
        tag.append('\n')
    text = soup.get_text('', strip=False)
    if re.search(r'(?i)\buploader\s*:', text):
        synopsis = re.search(r'(?is)(?:Описание|О фильме|О сериале|Сюжет)\s*:\s*(.+)', text)
        if not synopsis:
            return ''
        text = synopsis[1]
        text = re.split(r'(?i)\buploader\s*:', text)[0]
    return re.sub(r'\n\s*\n+', '\n\n', re.sub(r'[^\S\n]+', ' ', text)).strip()[:12000]


def usable_image_url(url):
    return not re.search(r'(?i)captcha|/smiles/|/buttons/|rating\.|\.gif(?:\?|$)', url)


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
        if src and usable_image_url(src):
            pictures.append(urljoin(url, src))
    if not poster and pictures:
        poster = pictures[0]
    video = field('Видео')
    dimensions = re.search(r'\b(\d{3,4})\s*[xх×]\s*(\d{3,4})\b', video)
    title = soup.select_one('#topic-title' if provider == 'rutracker' else 'h1')
    identifiers = {}
    for link in root.select('a[href]'):
        href = link.get('href', '')
        imdb = re.search(r'imdb\.com/title/(tt\d+)', href)
        kp = re.search(r'kinopoisk\.ru/(?:film|series)/(\d+)', href)
        if imdb:
            identifiers['imdb'] = imdb[1]
        if kp:
            identifiers['kinopoisk'] = kp[1]
    if provider == 'kinozal':
        for img in root.select('a[href] img'):
            src = img.get('src', '')
            if src.startswith(('http://', 'https://')) and src not in pictures:
                pictures.append(src)
    director = re.split(r'\s+(?:О фильме|О сериале|Описание|В ролях|Акт[её]ры|Жанр|Страна|Год выпуска)\s*:', field('Режисс[её]р'), maxsplit=1, flags=re.I)[0]
    magnet = next((a['href'] for a in soup.select('a[href^="magnet:?"]') if re.search(r'xt=urn:btih:(?:[a-fA-F0-9]{40}|[A-Z2-7]{32})(?:&|$)', a['href'])), '')
    return {'description': clean_description(description), 'director': director[:500],
            'magnet': magnet,
            'poster': poster, 'screenshots': [p for p in pictures if p != poster][:3], 'audio': field('Аудио')[:1500],
            'external_ids': identifiers,
            'dimensions': dimensions[0] if dimensions else '',
            'detail_series': bool(re.search(r'(?i)(?:\bS\d{1,3}E\d|\b(?:сезон|season)\s*\d|\b\d+\s*сезон|список серий)', text)),
            'detail_title': re.sub(r'\s+', ' ', title.get_text('', strip=False)).strip() if title else ''}


class Metadata:
    def __init__(self, config):
        self.url = config.get('flaresolverr_url', 'http://127.0.0.1:8191/v1')
        self.sites = config.get('detail_sites', {'kinozal': 'https://kinozal.me', 'rutracker': 'https://rutracker.org'})
        self.client = httpx.AsyncClient(timeout=75, trust_env=False)
        self.limit = asyncio.Semaphore(4)

    async def detail(self, provider, rid):
        if provider not in self.sites or not str(rid).isdigit():
            raise ValueError('Invalid tracker detail')
        path = ('/details.php?id=' if provider == 'kinozal' else '/forum/viewtopic.php?t=') + str(rid)
        return parse_detail(provider, await self.html(provider, path), self.sites[provider]+path)

    async def html(self, provider, path):
        url = self.sites[provider] + path
        async with self.limit:
            response = await self.client.post(self.url, json={'cmd': 'request.get', 'url': url, 'maxTimeout': 60000})
            response.raise_for_status()
            data = response.json()
        if data.get('status') != 'ok':
            raise ValueError('Tracker detail unavailable')
        return data['solution']['response']

    async def screenshots(self, provider, rid):
        if provider != 'kinozal' or not str(rid).isdigit():
            return []
        html = await self.html(provider, '/get_srv_details.php?id='+str(rid)+'&pagesd=2')
        soup = BeautifulSoup(html, 'html.parser')
        pictures = []
        for img in soup.select('img[src]'):
            parent = img.find_parent('a')
            full = parent.get('href', '') if parent else ''
            src = full if re.search(r'(?i)\.(?:jpg|jpeg|png|webp)(?:\?|$)', full) else img['src']
            if src.startswith(('http://', 'https://', '//')) and usable_image_url(src):
                pictures.append(urljoin(self.sites[provider], src))
        return list(dict.fromkeys(pictures))[:3]
