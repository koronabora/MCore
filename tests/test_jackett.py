import asyncio
import httpx
import pytest
from mcore.jackett import Jackett
from mcore.catalog import Catalog, normalize
from mcore.store import Store


def test_jackett_filters_music_and_keeps_credentials_server_side(tmp_path):
    async def check():
        key = tmp_path / 'key'
        key.write_text('secret-key')
        adapter = Jackett({'url': 'http://127.0.0.1:9117', 'api_key_file': str(key)})
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path.endswith('/results'):
                assert request.url.params['apikey'] == 'secret-key'
                assert request.url.params['Category'] == '2000,5000'
                return httpx.Response(200, json={'Indexers': [{'Error': None}], 'Results': [
                    {'Title': 'Film (2024) 1080p', 'Details': 'https://kinozal.me/details.php?id=42',
                     'Category': [2000], 'Size': 1024**3, 'Seeders': 10, 'Peers': 2,
                     'Link': 'http://127.0.0.1:9117/download?apikey=secret-key'},
                    {'Title': 'Soundtrack', 'Category': [3000]}]})
            return httpx.Response(302, headers={'Location': 'magnet:?xt=urn:btih:' + 'a'*40})

        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            rows = await adapter.search('kinozal', 'Film')
            assert len(rows) == 1 and rows[0]['Id'] == '42'
            item = normalize('kinozal', rows[0]) | {'download': rows[0]['download']}
            assert 'secret-key' not in str(Catalog.public(item))
            assert (await adapter.download(rows[0]['download'])).startswith('magnet:?')
            count = len(requests)
            with pytest.raises(ValueError, match='through Jackett'):
                await adapter.download('http://169.254.169.254/private')
            assert len(requests) == count
        finally:
            await adapter.client.aclose()
    asyncio.run(check())


def test_tracker_query_keeps_translated_results_in_cached_search(tmp_path):
    async def check():
        catalog = Catalog(Store(tmp_path), {'providers': ['rutracker'], 'torapi_url': 'http://adapter'})
        try:
            item = normalize('rutracker', {'Id': '1', 'Title': 'Interstellar (2014)'})
            item['search_queries'] = ['интерстеллар']
            catalog.put(item)
            catalog.put(normalize('rutracker', {'Id': '1', 'Title': 'Interstellar (2014)'}))
            assert len(catalog.movies('Интерстеллар')) == 1
            assert 'search_queries' not in catalog.movies('Интерстеллар')[0]['releases'][0]
        finally:
            await catalog.close()
    asyncio.run(check())
