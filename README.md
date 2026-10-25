# Overlay Studio — установщик для Windows

1. Распакуйте архив в корень репозитория `overlay` (рядом с `index.html` и `studio.html`), закоммитьте и запушьте.
2. GitHub → вкладка **Actions** → **Build Windows installer** → **Run workflow**.
3. Через 5–10 минут откройте завершённый запуск и скачайте артефакт `overlay-studio-installer` (внутри `.exe`).
4. Чтобы получить готовую страницу релиза со ссылкой для скачивания, создайте тег: `git tag v0.1.0 && git push origin v0.1.0`.

Лаунчер открывает `https://multivarka01.github.io/overlay/studio.html`, поэтому студия обновляется сама при каждом пуше сайта.
Если адрес сайта изменится, поменяйте `URL_` в `launcher/dist/index.html`.
