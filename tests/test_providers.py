import asyncio
import httpx
from mcore.catalog import Catalog, normalize
from mcore.store import Store


def test_disabled_cached_sources_never_return_or_make_requests(tmp_path):
    async def check():
        catalog = Catalog(Store(tmp_path), {'torapi_url': 'http://adapter',
                                            'providers': ['rutracker', 'kinozal']})
        calls = []

        def respond(request):
            calls.append(request.url.path)
            return httpx.Response(200, json=[])

        await catalog.client.aclose()
        catalog.client = httpx.AsyncClient(base_url='http://adapter', transport=httpx.MockTransport(respond))
        try:
            for provider in ('rutor', 'nonameclub', 'kinozal', 'rutracker', 'local'):
                item = normalize(provider, {'Name': 'Shared movie (2024)', 'Id': '1'})
                catalog.put(item)
            mid = item['movie']
            disabled = normalize('rutor', {'Name': 'Disabled only (2023)', 'Id': '2'})
            catalog.put(disabled)
            assert catalog.release('rutor:1') is None
            assert await catalog.detail('nonameclub:1') is None
            await catalog.provider_search('rutor', 'Shared')
            assert calls == []
            assert await catalog.movie(disabled['movie']) is None
            assert len(catalog.movies()) == 1
            movie = await catalog.movie(mid)
            assert {r['provider'] for r in movie['releases']} == {'rutracker', 'kinozal', 'local'}
            assert calls and all(path.endswith(('/kinozal', '/rutracker')) for path in calls)
            # Old rows remain stored; account history and cached payload need no destructive migration.
            with catalog.store.connect() as db:
                assert db.execute('SELECT COUNT(*) FROM releases').fetchone()[0] == 6
        finally:
            await catalog.client.aclose()

    asyncio.run(check())
