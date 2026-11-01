# Пробная интеграция Jackett + FlareSolverr

На сервере установлены Docker и два контейнера с закреплёнными digest образов
в `deploy/tracker-trial.sh`. Автозапуск: Docker `unless-stopped`.

- Jackett: `127.0.0.1:9117`.
- FlareSolverr 3.5.2: `127.0.0.1:8191`.
- Внутренняя Docker-сеть: `mcore-indexers`.
- Настройки и будущие аккаунты: `/var/lib/mcore-indexers/jackett`, вне git.
- Jackett обращается к `http://mcore-flaresolverr:8191`, timeout 90 секунд.
- Обновление Jackett внутри контейнера выключено; обновлять закреплённый образ.

Развёртывание от root после установки `docker.io`:

```sh
sh deploy/tracker-trial.sh
# После первого запуска и создания ServerConfig.json:
python3 deploy/configure-jackett-trial.py
```

## Результат проверки 2026-11-01

`tests/check_flaresolverr.py` дважды проверил поиск Interstellar с IP сервера.
Cloudflare challenge успешно пройден для обоих трекеров:

- Kinozal: HTTP 200, страница «Раздачи», найдены ссылки на карточки.
- RuTracker: HTTP 200, форма входа. Требуется авторизация аккаунтом.

Связь Jackett → FlareSolverr проверена из контейнера Jackett.
Поиск через авторизованные адаптеры Jackett ещё не проверен. MCore пока использует
TorAPI; подключение нового адаптера выполняется после проверки аккаунтов и результатов.

## Вход пользователя

На своём ПК открыть SSH-туннель, оставив терминал работающим:

```sh
ssh -p 64832 -N -L 127.0.0.1:9117:127.0.0.1:9117 kot@navint.space
```

Открыть `http://127.0.0.1:9117` в браузере. Через **Add indexer** добавить
**RuTracker.org** и **Kinozal**, указать свои учётные данные и сохранить.
FlareSolverr уже настроен глобально. Для Kinozal выключить **Strip Cyrillic Letters**
и **Add RUSSIAN to end of all titles**, чтобы сохранить исходные названия.
Выполнить Test и поиск Interstellar. При CAPTCHA следовать форме Jackett;
успешный Cloudflare challenge сам по себе не гарантирует успешный вход.

HTTP идёт внутри зашифрованного SSH-туннеля. Дополнительный проброс портов не нужен.
Cookies и API-ключ не копировать в README или журналы диагностики.
