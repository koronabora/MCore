from io import BytesIO
import pytest
from PIL import Image
from mcore.metadata import clean_description, parse_detail
from mcore.images import validate_screenshot


@pytest.mark.parametrize('value', [
    'Uploader:\n Aids\n <br>Дикая Аляска [2012, BDRip 1080p]',
    '<b>Uploader:</b> Aids&lt;br&gt;Дикая Аляска',
    'Uploader: Aids&amp;lt;br&amp;gt;Дикая Аляска',
])
def test_indexer_uploader_blurb_is_not_synopsis(value):
    assert clean_description(value) == ''


def test_encoded_markup_and_uploader_do_not_leak_into_synopsis():
    assert clean_description('Uploader: Aids<br>Описание: Жизнь &lt;b&gt;Аляски&lt;/b&gt;.&lt;br&gt;Природа.') == 'Жизнь Аляски.\nПрирода.'
    assert clean_description('Обычное описание без разметки.') == 'Обычное описание без разметки.'
    assert clean_description('<script>bad()</script><p>История</p>') == 'История'


def test_tracker_description_and_captcha_url():
    result = parse_detail('rutracker', '<div class="post_body">Описание: &lt;br&gt;Жизнь Аляски.<br><img class="postImg" src="/captcha.php?id=1"></div>', 'https://rutracker.org/forum/x')
    assert '<' not in result['description']
    assert result['poster'] == '' and result['screenshots'] == []


@pytest.mark.parametrize('size,valid', [((180,60),False), ((300,120),False), ((640,360),True), ((720,576),True), ((1920,800),True)])
def test_captcha_sized_screenshots_are_rejected(size,valid):
    data=BytesIO(); Image.new('RGB',size).save(data,format='PNG')
    if valid:
        validate_screenshot(data.getvalue())
    else:
        with pytest.raises(ValueError): validate_screenshot(data.getvalue())
