import asyncio
import json
from mcore.catalog import Catalog, normalize
from mcore.store import Store
from mcore.metadata import parse_detail


def test_series_seasons_and_exact_search_survive_old_ids(tmp_path):
    async def check():
        store = Store(tmp_path)
        catalog = Catalog(store, {'providers': ['kinozal', 'rutracker'], 'torapi_url': 'http://localhost:1'})
        try:
            one = normalize('kinozal', {'Id': '1', 'Title': 'Большой куш. Бангкок - S2E1-13 - 2026 HDTV 1080p'})
            two = normalize('kinozal', {'Id': '2', 'Title': 'Большой куш. Бангкок - S1E1-10 - 2025 HDTV 720p'})
            assert one['movie'] == two['movie'] and one['title'] == 'Большой куш. Бангкок'
            pack = normalize('kinozal', {'Id': '3', 'Title': 'One Piece. Большой куш (Ван-Пис) One Piece - S1-2E1-16 - 2023-2026 WEBDL'})
            assert pack['season'] == '1-2' and pack['series']
            anime = normalize('rutracker', {'Id': '99', 'Title': 'Большой куш / One Piece [TV] [1-130] [RUS(int)] [1999, DMRip]'})
            assert anime['series'] and anime['movie'] != pack['movie']
            assert normalize('rutracker', {'Id': '98', 'Title': 'Ah! My Goddess / One Piece [Artbook] [JPG]'}) is None
            film = normalize('kinozal', {'Id': '4', 'Title': 'Большой куш Snatch 2000 MVO BDRip'})
            assert film['title'] == 'Большой куш' and 'Original' not in film['resolution']
            film['movie'] = 'legacy-id'; catalog.put(film)
            film = normalize('kinozal', {'Id': '4', 'Title': 'Большой куш Snatch 2000 MVO BDRip'})
            catalog.put(film)
            for item in (one, two, pack):
                item['search_queries'] = ['большой куш']; catalog.put(item)
            assert catalog.movie_releases('legacy-id')[0]['id'] == film['id']
            movies = catalog.movies('Большой куш')
            assert movies[0]['title'] == 'Большой куш' and movies[0]['exact']
            assert all(not m['exact'] for m in movies[1:])
            assert catalog.movies('Snatch')[0]['exact']
            assert len([m for m in movies if m['title'] == 'Большой куш. Бангкок']) == 1
        finally:
            await catalog.close()
    asyncio.run(check())


def test_tracker_details_extract_only_release_metadata():
    ru = parse_detail('rutracker', '''<a id="topic-title">Фильм / Movie [2000, BDRip]</a>
        <div class="post_body"><img class="postImg" src="https://images.example/poster.jpg">
        Режиссер: Режиссёр<br>Описание: Настоящее описание.<hr>Видео: 704x384<br>
        <img class="postImg" src="https://images.example/frame.jpg"></div>
        <div class="post_body">Описание: Чужой комментарий</div>''', 'https://rutracker.org/forum/viewtopic.php?t=1')
    assert ru['description'] == 'Настоящее описание.'
    assert ru['dimensions'] == '704x384'
    assert ru['screenshots'] == ['https://images.example/frame.jpg']
    kz = parse_detail('kinozal', '''<meta property="og:image" content="/i/poster.jpg"><h1>Фильм</h1>
        <div class="mn1_content"><h2>Название: Фильм<br>Режиссер: Автор<br></h2>
        <p><b>О фильме:</b> Описание фильма.</p></div>''', 'https://kinozal.me/details.php?id=1')
    assert kz['poster'] == 'https://kinozal.me/i/poster.jpg'
    assert kz['description'] == 'Описание фильма.' and kz['director'] == 'Автор'
