"""Live opt-in diagnostic; prints no cookie values or page bodies."""
import json
import re
import httpx

with httpx.Client(base_url='http://127.0.0.1:8191', timeout=110, trust_env=False) as client:
    for url in ('https://kinozal.me/browse.php?s=Interstellar',
                'https://rutracker.org/forum/tracker.php?nm=Interstellar'):
        response = client.post('/v1', json={'cmd': 'request.get', 'url': url, 'maxTimeout': 90000})
        data = response.json()
        solution = data.get('solution', {})
        html = solution.get('response', '')
        title = re.search(r'<title>(.*?)</title>', html, re.I | re.S)
        print(json.dumps({'url': url, 'solver': data.get('status'), 'message': data.get('message'),
                          'http': solution.get('status'), 'title': title[1] if title else None,
                          'bytes': len(html),
                          'login_form': bool(re.search(r'name=[\"\x27]login_username', html)),
                          'release_links': len(re.findall(r'(?:details.php\?id=|viewtopic.php\?t=)', html)),
                          'cookie_names': [c['name'] for c in solution.get('cookies', [])]},
                         ensure_ascii=False), flush=True)
