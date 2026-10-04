# invest_cons

Отдельное учетное ядро инвестиционного портфеля. Первый технический срез читает T-Invest и Freedom, сохраняет снимки и операции в PostgreSQL, оценивает портфель в RUB/USD и показывает факт/план выплат через Telegram.

Старая система `tinkoff-portfolio-ai-agent` для работы этого проекта не нужна. Здесь нет торговых токенов, заявок и LLM-рекомендаций.

## Документация

- [Спецификация первого релиза](docs/specification.md) и [контракты данных](docs/data_contracts.md).
- [Проект классификации активов и структуры IPS](docs/ips_classification_design.md) - черновик будущего расширения.
- [Черновой IPS YAML](config/ips/personal_main.draft.yaml) и [план хранения и переноса классификации](docs/classification_mapping_plan.md).
- [Архитектура и структура репозитория](docs/architecture.md), [правила для разработчиков и ИИ-агентов](AGENTS.md).
- [Критерии завершения](docs/definition_of_done.md), [фактический статус](docs/status.md), [протокол тестирования](docs/testing.md).
- [Команды](docs/commands.md), [эксплуатация](docs/operations.md), [архитектурные решения](docs/adr/README.md).

Новая документация различает целевую модель и текущий технический срез. Исполняемый код пока остается в `src/portfolio_v2`; целевой перенос модулей и миграции не считаются выполненными.

## Локальный запуск

1. Создайте виртуальное окружение: `python -m venv .venv`.
2. Установите зависимости: `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`.
3. Создайте `.env` по `.env.example`, задайте read-only ключи обоих брокеров, владельцев и отдельный Telegram bot token. Не используйте token старого бота одновременно.
4. Поднимите отдельный PostgreSQL: `docker compose up -d postgres`. Он слушает `localhost:5434` и хранит данные в собственном volume.
5. Проверьте загрузку: `.\.venv\Scripts\python.exe run_portfolio_v2.py --refresh-once`.
6. Запустите бот: `.\.venv\Scripts\python.exe run_portfolio_v2.py`.

Обязательные настройки и известные ограничения перечислены в [runbook](docs/portfolio_v2_runbook.md). Перед доверием финансовым результатам нужны сверка NAV с обоими брокерами и разбор неоднозначных денежных потоков Freedom. Старые данные, накопленные при проверочном запуске в базе прежнего проекта, сюда автоматически не импортируются.
