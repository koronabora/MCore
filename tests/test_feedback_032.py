import asyncio
from mcore.catalog import Catalog, normalize
from mcore.store import Store
from mcore.metadata import parse_detail


def test_search_all_words_ignores_hyphens_case_and_punctuation(tmp_path):
    async def check():
        catalog=Catalog(Store(tmp_path), {'providers':['rutracker'], 'torapi_url':'http://localhost:1'})
        try:
            titles=['Человек-паук / Spider-Man (2002)', 'Человек-паук 2 / Spider-Man 2 (2004)',
                    'Новый Человек-Паук / The Amazing Spider-Man (2012)', 'Паук (2002)', 'Человек (2004)']
            for i,title in enumerate(titles): catalog.put(normalize('rutracker',{'Id':str(i),'Title':title}))
            results=catalog.movies('человек паук')
            assert len([m for m in results if m['keyword_match']])==3
            assert all(m['keyword_match'] for m in results[:3])
            assert [m['id'] for m in results]==[m['id'] for m in catalog.movies('ЧЕЛОВЕК—ПАУК')]
            assert catalog.movies('паук человек')[0]['keyword_match']
            assert catalog.movies('челвек паук')
        finally: await catalog.close()
    asyncio.run(check())


def test_rutracker_public_magnet_is_retained():
    magnet='magnet:?xt=urn:btih:'+'a'*40+'&tr=https%3A%2F%2Ftracker.example%2Fannounce'
    data=parse_detail('rutracker','<div class="post_body">Описание: фильм</div><a href="'+magnet+'">magnet</a>','https://rutracker.org/forum/viewtopic.php?t=1')
    assert data['magnet']==magnet
