from mcore.catalog import normalize
from mcore.metadata import parse_detail
from mcore.archive import parse
from mcore.catalog import Catalog
from mcore.store import Store
import asyncio


def test_highlighted_cyrillic_preserves_spaces_and_word_breaks():
    title = 'National Geographic "<span>Военные</span><wbr> <span>самолеты</span>: <span>Война</span> <span>в</span> <span>воздухе</span>"'
    data = parse_detail('rutracker', '<a id="topic-title">'+title+'</a><div class="post_body">Описание: Тест</div>', 'https://rutracker.org/')
    assert data['detail_title'] == 'National Geographic "Военные самолеты: Война в воздухе"'
    rows, _ = parse('rutracker', '<tr><td><a class="torTopic" href="viewtopic.php?t=1">'+title+'</a></td></tr>', '/forum/viewforum.php?f=7')
    assert rows[0]['Title'] == data['detail_title']


def test_documentary_aliases_and_series_in_detail():
    director = parse_detail('kinozal', '<div class="mn1_content">Режиссер: Бернард Джордж О фильме: Длинное описание</div>', 'https://kinozal.me/')
    assert director['director'] == 'Бернард Джордж'
    names = [
        'National Geographic. Неизвестная планета Земля (1 S1-10 серии из 10) / One Strange Rock (Даррен Аронофски / Darren Aronofsky) [2018, Документальный, DVB]',
        'Неизвестная планета Земля / One Strange Rock / S1E1-10 of 10 (Nick Shoolingin-Jordan) [2018, WEB-DL 1080p]',
        'Неизвестная планета земля / Один необычный камень (1 S1-10 серия из 10) / One Strange Rock (Даррен Аронофски / Darren Aronofsky) [2018, WEBRip 720p]',
    ]
    releases = [normalize('rutracker', {'Id': str(i), 'Title': name}) for i, name in enumerate(names)]
    assert len({r['movie'] for r in releases}) == 1
    data = parse_detail('rutracker', '<div class="post_body">Описание: Тест<br>1 Сезон: Эпизоды 1, 2</div>', 'https://rutracker.org/')
    assert data['detail_series']
    first = normalize('rutracker', {'Id': '8', 'Title': 'Загадки планеты Земля / What on Earth? [2015, HDTV]', 'series': data['detail_series']})
    second = normalize('rutracker', {'Id': '9', 'Title': 'Загадки планеты Земля / What on Earth? / S8E1-20 (Director) [2022, HDTV]'})
    assert first['movie'] == second['movie']


def test_late_external_id_does_not_split_existing_series(tmp_path):
    async def check():
        catalog=Catalog(Store(tmp_path), {'providers':['rutracker'], 'torapi_url':'http://localhost:1'})
        try:
            one=normalize('rutracker',{'Id':'1','Title':'Загадки планеты Земля / What on Earth? / S1E1 [2015, HDTV]'})
            two=normalize('rutracker',{'Id':'2','Title':'Загадки планеты Земля / What on Earth? / S8E1 [2022, HDTV]'})
            catalog.put(one); catalog.put(two)
            one['external_ids']={'imdb':'tt4543056'}; catalog.put(one)
            assert len(catalog.movies('Загадки планеты Земля'))==1
            assert len(catalog.movie_releases(two['movie']))==2
        finally:
            await catalog.close()
    asyncio.run(check())
