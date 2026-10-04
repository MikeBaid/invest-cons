# Полезные команды

Команды приведены для PowerShell из корня `invest_cons`. Они не должны печатать секреты. Перед внешним refresh убедитесь, что выбран правильный `.env` и нет второго процесса с тем же Telegram token. Детали параметров - в [runbook технического среза](portfolio_v2_runbook.md).

## Подготовка

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
docker compose up -d postgres
docker compose ps
```

`.env` создается локально по `.env.example` и не добавляется в Git. `docker compose up` использует локальные `POSTGRES_*` из `.env`; PostgreSQL по умолчанию доступен на `localhost:5434`.

## Разработка и запуск

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe run_portfolio_v2.py --refresh-once
.\.venv\Scripts\python.exe run_portfolio_v2.py
```

Если тесты дошли до `100%`, но процесс не завершается при записи `.pytest_cache` в ограниченном окружении, используйте `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`. Это не включает интеграционный тест PostgreSQL: он запускается отдельно только при `PORTFOLIO_V2_TEST_DB=1`. Тест **записывает данные в БД из `.env`**; текущая защита проверяет лишь `localhost`, поэтому перед включением нужна отдельная локальная тестовая БД.

`--refresh-once` делает реальные вызовы API и запись в БД. Код `2` означает неполный снимок или операции, требующие разбора; код `0` означает техническую загрузку, не завершенную финансовую сверку. Последняя команда запускает Telegram polling и ежедневную синхронизацию из `config/runtime.yaml`; процесс должен оставаться запущенным в плановое время. Для ручного обновления портфеля используйте `/refresh`; эта команда выполняет синхронизацию и сообщает результат. В `/income` кнопка запускает фоновую синхронизацию: статусное сообщение в чате обновится по завершении, а открытый отчёт будет перестроен. Первая успешная синхронизация при необходимости загрузит операции с 1 января для годового факта выплат. `/income` и `/income_report` показывают суммы в RUB/USD; drill-down идет по IPS до позиции и счета, список позиций разбит на страницы. Диагностика поясняет неполный охват факта, пустые календари дивидендов и ошибки календарей без вывода идентификаторов. Факт переводится по историческому FX даты операции, прогноз — по текущему курсу; отдельный налоговый пересчет не выполняется. Не запускать одновременно два poller-процесса с одним Telegram token. Таймер и кнопка сериализуются общим lock внутри процесса; не запускать параллельно отдельный CLI refresh.

## Диагностика без раскрытия секретов

Пробная read-only привязка последнего снимка с позициями к черновику IPS (путь к старому файлу указывается явно; исключения по умолчанию берутся из `config/ips/portfolio_exclusions.yaml`):

```powershell
.\.venv\Scripts\python.exe -m scripts.preview_portfolio_classification --legacy-mapping C:\path\to\asset_bucket_mapping.json --list-accounts
.\.venv\Scripts\python.exe -m scripts.preview_portfolio_classification --legacy-mapping C:\path\to\asset_bucket_mapping.json
```

Импорт подтверждённых назначений из проверенного preview в локальную версионируемую историю PostgreSQL. Сначала команда только проверяет и считает записи; `--apply` записывает их. Разрешён только локальный PostgreSQL.

```powershell
.\.venv\Scripts\python.exe -m scripts.import_preview_classifications
.\.venv\Scripts\python.exe -m scripts.import_preview_classifications --apply
```

Файлы `reports/classification_preview.json` и `.md` содержат персональные данные и игнорируются Git. Команда сверки не меняет исходный учетный снимок. Бот отдельно применяет `config/ips/portfolio_exclusions.yaml` к инвестиционному NAV, доходности и выплатам. На неполном snapshot доли и целевые суммы в `/portfolio` показываются от наблюдаемого NAV с явной пометкой.

```powershell
git status --short
docker compose ps
docker compose logs --tail 100 postgres
```

Не выводите `.env`, полный broker payload или DSN в чат и диагностические логи. Перед передачей лога проверьте отсутствие персональных и учетных данных. Конкретные команды backup/restore будут добавлены после проверки на отдельной базе; пока процедура описана в [operations.md](operations.md).
