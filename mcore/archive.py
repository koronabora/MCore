"""Public paginated video listings; authenticated torrent retrieval remains in Jackett."""
import re
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup


def parse(provider, html, path):
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup.select('wbr'):
        tag.unwrap()
    soup.smooth()
    rows, links = [], set()
    query = parse_qs(urlparse(path).query)
    if provider == 'kinozal':
        if not soup.select('td.nam'):
            raise ValueError('Kinozal archive listing unavailable')
        for tr in soup.select('tr:has(td.nam)'):
            a = tr.select_one('td.nam a[href^="/details.php?id="]')
            cells = tr.select('td')
            if a and len(cells) >= 6:
                rows.append({'Id': parse_qs(urlparse(a['href']).query)['id'][0], 'Title': re.sub(r'\s+', ' ', a.get_text()).strip(),
                             'Size': cells[3].get_text(' ', strip=True), 'Seeds': cells[4].get_text(), 'Peers': cells[5].get_text()})
        category = query.get('c', ['1002'])[0]
        page = int(query.get('page', ['0'])[0])
        pages = []
        for a in soup.select('a[href*="page="]'):
            values = parse_qs(urlparse(a['href']).query)
            if values.get('c', [category])[0] == category and values.get('page', [''])[0].isdigit():
                following = int(values['page'][0])
                if following > page:
                    pages.append(following)
        if pages:
            links.add(f'/browse.php?c={category}&page={min(pages)}')
    else:
        root = soup.select_one('#c-2')
        is_index = path.startswith('/forum/index.php')
        if is_index:
            containers = soup.select('#c-2, #c-18, #c-20')
            if not containers:
                raise ValueError('RuTracker categories unavailable')
            forums = [a for c in containers for a in c.select('.forumlink a[href], .subforums a[href]')]
        else:
            if not soup.select('.forumlink, .torTopic, #forum-title, .nav'):
                raise ValueError('RuTracker archive unavailable')
            forums = soup.select('.forumlink a[href]')
        for a in forums:
            if re.search(r'(?i)помощ|обсуж|^звуковые дорожки|^авторский перевод|^закадровый перевод|заявк|предложен|подборки ссылок|акт[её]р|режисс[её]р|саундтрек|озвуч', a.get_text()):
                continue
            match = re.search(r'viewforum\.php\?f=(\d+)', a['href'])
            if match:
                links.add('/forum/viewforum.php?f='+match[1])
        for a in soup.select('a.torTopic[href]'):
            match = re.search(r'\bt=(\d+)', a['href'])
            tr = a.find_parent('tr')
            if not match or tr is None:
                continue
            def text(selector):
                node = tr.select_one(selector)
                return node.get_text('', strip=True) if node else ''
            rows.append({'Id': match[1], 'Title': re.sub(r'\s+', ' ', a.get_text()).strip(), 'Size': text('a.f-dl'),
                         'Seeds': text('.seedmed'), 'Peers': text('.leechmed')})
        if not is_index:
            forum = query['f'][0]; start = int(query.get('start', ['0'])[0]); pages = []
            for a in soup.select('a[href*="viewforum.php"][href*="start="]'):
                values = parse_qs(urlparse(a['href']).query)
                if values.get('f', [''])[0] == forum and values.get('start', [''])[0].isdigit() and int(values['start'][0]) > start:
                    pages.append(int(values['start'][0]))
            if pages:
                links.add(f'/forum/viewforum.php?f={forum}&start={min(pages)}')
    return rows, sorted(links)


async def page(metadata, provider, path):
    valid = r'/browse\.php\?c=(?:1001|1002|1003|1006)&page=\d+' if provider == 'kinozal' else r'/forum/(?:index\.php|viewforum\.php\?f=\d+(?:&start=\d+)?)'
    if not re.fullmatch(valid, path):
        raise ValueError('Invalid archive cursor')
    html = await metadata.html(provider, path)
    return parse(provider, html, path)
