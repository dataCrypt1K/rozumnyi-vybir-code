# Безкоштовна публікація «Розумного вибору»

Streamlit Community Cloud запускає сайт і надає адресу `*.streamlit.app`. Neon Free зберігає проєкти, чернетки, анкети та результати в PostgreSQL. Для невеликого показу можна почати з $0. Після 12 годин без відвідувань сайт засинає, а відвідувач може розбудити його кнопкою на екрані. Дані лишаються в Neon. [Розгортання Streamlit](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy) · [Режим сну](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app#app-hibernation) · [Neon Free](https://neon.com/pricing)

## Налаштування

1. Створіть приватний репозиторій GitHub і завантажте в його корінь **вміст** архіву, включно з папкою `.streamlit`. У Streamlit Community Cloud підключіть GitHub, надайте доступ до цього репозиторію й натисніть **Create app**. Виберіть гілку `main`, файл `community_app.py`, Python 3.12 в **Advanced settings** і бажану піддоменну адресу. [Підключення GitHub](https://docs.streamlit.io/deploy/streamlit-community-cloud/get-started/connect-your-github-account) · [Параметри застосунку](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
2. У Neon створіть окремий порожній проєкт на тарифі **Free**. У вікні **Connect** вимкніть **Connection pooling**, виберіть роль, яка може створювати таблиці в цій базі, і скопіюйте прямий рядок підключення з `sslmode=require`. Під час першого доступу до даних застосунок створить таблиці міграціями Alembic. [Підключення до Neon](https://neon.com/docs/connect/choose-connection)
3. У Google Auth Platform створіть OAuth-клієнт типу **Web application**. Додайте адресу застосунку з кінцівкою `/oauth2callback` до **Authorized redirect URIs**. Для демонстрації можна додати запрошені Google-акаунти як **Test users**. Ті самі адреси вкажіть у `APP_ALLOWED_EMAILS`. [Вхід через Google у Streamlit](https://docs.streamlit.io/develop/tutorials/authentication/google)
4. У **Advanced settings → Secrets** під час створення застосунку або в **App settings → Secrets** після запуску заповніть наведений шаблон. Для створення `cookie_secret` локально виконайте `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Значення секретів вводьте в панелі Streamlit. [Секрети в Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)

```toml
DATABASE_URL = "<paste-the-direct-neon-connection-string>"
APP_ALLOWED_EMAILS = "first@example.com,second@example.com"

[auth]
redirect_uri = "https://your-subdomain.streamlit.app/oauth2callback"
cookie_secret = "<paste-the-generated-string>"
client_id = "<paste-the-google-client-id>"
client_secret = "<paste-the-google-client-secret>"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

5. У **App settings → Sharing** виберіть **This app is public and searchable**, щоб відвідувачі могли відкрити екран входу за посиланням. Репозиторій лишається приватним, а всі проєкти доступні лише їхнім власникам після входу. [Видимість застосунку](https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app)

## Перед передачею посилання

- Увійдіть двома запрошеними Google-акаунтами в окремих сеансах. Створіть різні тестові чернетки, відкрийте **Мої проєкти** й переконайтеся, що кожен акаунт бачить тільки свою. Перевірте повторний вхід і збереження даних після перезапуску. Незареєстрований відвідувач має бачити лише екран входу.
- Пройдіть анкету до кінця й завантажте звіт у PDF. Без ключа OpenAI працює ручне введення даних і оцінка за правилами. Щоб показати аналіз креслень і пояснення LLM, додайте `OPENAI_API_KEY` у верхній частині Secrets перед `[auth]`. Виклики OpenAI оплачуються окремо за тарифом API.
- Перед самим показом відкрийте адресу сайту й за потреби розбудіть його.
