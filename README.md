# Розумний вибір

Прототип рекомендаційного застосунку для магістерської роботи. За відповідями анкети й характеристиками житла він оцінює доцільність 19 систем розумного дому, пояснює статус кожної та допомагає скласти попередню рекомендацію із черговістю реалізації.

## Запуск демо у Windows

Потрібен Python 3.12. Відкрийте PowerShell у корені розпакованого проєкту або клону репозиторію та виконайте:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:APP_MODE = 'local'
$env:SMART_HOME_ALLOW_SQLITE_RUNTIME = '1'
$env:DATABASE_URL = 'sqlite:///storage/reviewer_demo.sqlite'
New-Item -ItemType Directory -Path storage -Force | Out-Null
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1
```

Відкрийте `http://localhost:8501`, виберіть **Демонстраційний** у полі **Режим аналізу** й натисніть **Заповнити без плану**. Далі підтвердьте дані про житло та пройдіть анкету. SQLite зберігає результати в локальній папці `storage/`. Демонстраційна модель імітує відповіді LLM; після встановлення залежностей цей режим не звертається до зовнішнього API. Аналіз реальних планів через OpenAI потребує окремого налаштування.

Для швидкої перевірки правил без браузера й бази:

```powershell
.\.venv\Scripts\python.exe evaluate_rules.py --test
```

[Розгортання у Streamlit Community Cloud](DEPLOYMENT_FREE.md)
