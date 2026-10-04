# Развёртывание invest_cons на внешнем сервере

Дата проверки цен: 04.10.2026. Инструкция относится к текущему первому релизу: Telegram-бот, один процесс Python с расписанием внутри, PostgreSQL 16 в Docker Compose, источники T-Invest и Freedom. Это не схема с несколькими worker-ами и не managed PostgreSQL.

## 1. Рекомендуемый сервер

### Требования приложения

| Ресурс | Минимум для пробного запуска | Рекомендуется для постоянной работы |
|---|---:|---:|
| ОС | Ubuntu Server 24.04 LTS, x86_64 | Ubuntu Server 24.04 LTS, x86_64 |
| CPU | 2 vCPU | 2 vCPU, shared допустимы |
| RAM | 2 GiB, без гарантии запаса | 4 GiB |
| Диск | 40 GiB SSD/NVMe | 80 GiB SSD/NVMe |
| PostgreSQL | PostgreSQL 16 в контейнере | PostgreSQL 16 в контейнере + отдельные внешние backup-копии |
| Сеть | исходящий HTTPS и T-Invest gRPC/TLS | то же; входящие соединения только SSH |

Нагрузка невысокая: бот обслуживает ограниченный круг пользователей, а синхронизация запускается раз в день или вручную. Рекомендованный запас RAM нужен для Ubuntu, Docker, PostgreSQL и Python одновременно, а не из-за тяжёлых вычислений. 2 GiB можно использовать для временного пилота, но не считать целевой конфигурацией. 80 GiB оставляют место для роста истории операций и локального обслуживания базы; это не заменяет резервные копии.

Серверу нужен исходящий доступ к Telegram Bot API, T-Invest API (включая gRPC/TLS), Freedom Tradernet API и сайту ЦБ для валютных курсов. Входящий веб-порт для Telegram polling не нужен. PostgreSQL нельзя публиковать в интернет.

### Провайдеры и стоимость

**Рекомендация: DigitalOcean Basic Droplet, 2 vCPU / 4 GiB / 80 GiB за $24 в месяц.** Это подходящий размер с понятным фиксированным месячным пределом, диском 80 GiB и 4,000 GiB включённого transfer. Текущая цена указана в [официальном прайсинге DigitalOcean](https://www.digitalocean.com/pricing/droplets). Еженедельный Droplet Backup для Basic-планов стоит ещё 20% цены сервера: ориентировочно $4.80/мес., то есть **около $28.80/мес. до налогов и дополнительных услуг**; см. [тарифы резервных копий DigitalOcean](https://docs.digitalocean.com/products/backups/details/pricing/). Резервная копия диска полезна для восстановления VM, но не заменяет логический backup PostgreSQL.

**Более дешёвая альтернатива: Hetzner CX23** — 2 vCPU / 4 GB / 40 GB NVMe, опубликованная цена $6.49/мес. без IPv4 и VAT для Германии/Финляндии после изменения тарифов 15.06.2026; подробности в [официальной таблице цен Hetzner](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/). Перед выбором нужно добавить стоимость IPv4/backup и проверить доступность регистрации, оплаты и сетевой доступности нужного региона.

DigitalOcean стоит выбирать ради более простого первого развертывания и большего диска; Hetzner — если минимизация регулярных расходов важнее и пользователь готов самостоятельно проверить доступность аккаунта, IPv4 и процедуры восстановления. Тарифы не гарантируют, что конкретная площадка доступна для регистрации/оплаты из страны пользователя или что с неё доступны брокерские API. До переноса данных надо проверить это на временном сервере. Не размещать данные до проверки региона и правовых/договорных ограничений.

Цены динамические и приведены для сравнения, не как оферта. Не включены налоги, платный IP/volume, резервное копирование PostgreSQL во внешнее хранилище и возможные комиссии за оплату. Для DigitalOcean разумно заложить бюджет около **$30–35/мес.** с недельной системной копией и небольшим запасом на отдельные услуги. Отдельное хранилище backup может увеличить сумму.

## 2. Что уже умеет проект и что нужно учесть

- `run_portfolio_v2.py` запускает либо один refresh (`--refresh-once`), либо Telegram polling и встроенное ежедневное расписание. Для постоянной работы нужен ровно один systemd-процесс бота.
- В `config/scheduling.yaml` настраивается ежедневный запуск (сейчас 06:00 `Europe/Moscow`). Отдельный cron для refresh не нужен.
- PostgreSQL 16 запускается через Compose. Схема `portfolio_v2` пока создаётся idempotent DDL из кода при старте, а не версионированными миграциями. Перед первым запуском новой версии на боевой БД требуется backup.
- История старого проекта в эту БД автоматически не попадёт. Переносить нужно backup именно базы `invest_cons`, а не базу старого бота.
- На Telegram token может работать только один polling-процесс. Локальный бот надо остановить перед запуском серверного.
- `/refresh` и встроенное расписание используют внутрипроцессную блокировку. Не запускать `--refresh-once` одновременно с постоянным ботом.
- Отдельные read-only доступы обоих брокеров обязательны для работы приложения. Не передавать ключи и dump базы в чат, тикет или Git.
- Каждый Telegram ID из `PORTFOLIO_V2_TELEGRAM_IDS` сейчас видит все подключённые счета. До реализации изоляции по владельцам добавлять только личные доверенные ID; не публиковать бота для других пользователей.

## 3. Подготовка, которую может выполнить Codex

До создания VPS Codex может:

1. Проверить чистую установку зависимостей на Linux и работу SDK T-Invest на выбранной архитектуре x86_64.
2. Подготовить deployment-файлы: systemd unit, пример production env без секретов, команды диагностики и скрипт проверки конфигурации.
3. Подготовить инструкцию миграции и проверку восстановления backup в отдельную пустую БД.
4. Проверить настройки Compose, тесты, импорты и режим `--refresh-once`; подготовить Git commit/tag для релиза.
5. После того как пользователь развернёт временный сервер и предоставит безопасный доступ согласованным способом, проверить доступность API и пройти deploy вместе с пользователем.

Codex не должен получать приватный SSH-ключ, пароль панели провайдера, токены бота/брокеров или незашифрованный dump с историей портфеля. Удалённые административные действия выполняются владельцем сервера либо через явно настроенный и ограниченный механизм доступа.

## 4. Что требует участия владельца

- Выбрать провайдера и регион, создать аккаунт/оплатить услуги, убедиться, что регистрация и выбранный способ оплаты доступны.
- Создать SSH-ключ на своём компьютере и добавить публичную часть в панель провайдера. Приватный ключ никому не отправлять.
- Выбрать регион после проверки исходящего доступа к Telegram, T-Invest и Freedom. Учитывать требования к хранению финансовых и персональных данных.
- Ввести серверные секреты напрямую на сервере: брокерские read-only ключи, Telegram token, разрешённые Telegram user ID, пароль БД и owner mapping.
- Остановить старый локальный бот в момент переключения Telegram polling; проверить новый бот и отчёты по фактическим счетам.
- Утвердить срок хранения и второе место хранения backup, бюджет, процедуру восстановления и ответственного за контроль резервирования.

## 5. Порядок развёртывания

### Шаг 1. Подготовить локальный релиз и backup

1. Убедиться, что `.env` и backup-файлы исключены из Git (`.gitignore`), тесты проходят, версия приложения записана.
2. Снять backup текущей PostgreSQL `invest_cons`. На Windows установить клиентские утилиты PostgreSQL 16. Пароль не добавлять в командную строку: `pg_dump` должен запросить его интерактивно либо получить из защищённого `.pgpass`.

```powershell
pg_dump --format=custom --no-owner --no-privileges `
  --host=127.0.0.1 --port=5434 --username=invest_cons `
  --dbname=invest_cons --file=invest_cons-predeploy.dump
```

3. Проверить, что файл существует и имеет ненулевой размер. Передать его на сервер только по SSH (`scp`); не загружать в общедоступные облачные диски.
4. Зафиксировать локально дату последнего полного snapshot и количество строк ключевых таблиц. Это контрольные значения для сравнения после восстановления.

### Шаг 2. Создать VPS и закрыть периметр

1. При создании Droplet выбрать Ubuntu Server 24.04 LTS x86_64 и добавить публичный SSH-ключ. Пароль root по SSH не включать. Приватный ключ хранить только у себя.
2. В DigitalOcean Control Panel открыть **Networking → Firewalls**, создать firewall и прикрепить его к этому Droplet. Удалить широкое правило SSH из `All IPv4/All IPv6` и добавить входящее правило `SSH`, TCP 22, источник — ваш текущий публичный IP с `/32` (IPv4) и/или точный IPv6 CIDR. Узнать внешний IPv4 из локального Git Bash можно командой `curl -4 https://api.ipify.org; echo`. Если IP динамический и вы не знаете новый заранее, не ставьте временный узкий CIDR, который может внезапно перестать совпадать: оставьте TCP/22 с любых адресов, но используйте ключевую аутентификацию и позже ограничьте его через VPN/static IP. Удалить остальные входящие правила, если они не нужны; особенно не открывать 5432/5434, 80, 443. Исходящий трафик оставить разрешённым. У DigitalOcean правила нескольких firewall складываются, поэтому проверьте, что к Droplet не прикреплён другой firewall с более широким SSH-разрешением. См. [официальное руководство по Cloud Firewalls](https://docs.digitalocean.com/products/networking/firewalls/how-to/create/).
3. В открытой root SSH-сессии сначала обновить систему:

```bash
apt update
apt full-upgrade -y
```

Если предложено обновление ядра, перезагрузить сервер командой `reboot`, затем подключиться снова. root-логин пока не отключать.

4. Создать отдельную администраторскую учётную запись. Пароль, который задаётся `adduser`, нужен для `sudo`; он не должен использоваться для SSH. Если при создании Droplet выбрали ключ, DigitalOcean обычно отключает SSH password authentication.

```bash
adduser deployadmin
usermod -aG sudo deployadmin
install -d -m 700 -o deployadmin -g deployadmin /home/deployadmin/.ssh
cp /root/.ssh/authorized_keys /home/deployadmin/.ssh/authorized_keys
chown deployadmin:deployadmin /home/deployadmin/.ssh/authorized_keys
chmod 600 /home/deployadmin/.ssh/authorized_keys
```

Не закрывая root-сессию, открыть **второе локальное окно Git Bash** и проверить вход (указать тот же приватный ключ, что сработал для root):

```bash
ssh -i /путь/к/приватному_ключу deployadmin@АДРЕС_DROPLET
sudo -v
sudo whoami
```

Последняя команда должна вывести `root`. Если не получилось, продолжать из старой root-сессии и исправить доступ. Не включать UFW и не отключать root до успешной проверки новой сессии.

5. Настроить UFW на сервере. Сначала разрешить OpenSSH и только потом включать firewall:

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw status verbose
sudo ufw enable
sudo ufw status numbered
```

Оставить обе SSH-сессии открытыми. Из второго локального окна открыть ещё одну новую сессию под `deployadmin` и снова проверить `sudo -v`. Если доступ не работает, отключить UFW из ещё открытой сессии командой `sudo ufw disable` и исправить правила. Правило Cloud Firewall и UFW — два независимых фильтра, SSH должен быть разрешён в обоих.

6. Только после проверки нового администратора запретить SSH root и пароли. Создать файл настроек:

```bash
sudo tee /etc/ssh/sshd_config.d/99-invest-cons.conf >/dev/null <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
sudo sshd -t
sudo sshd -T | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication) '
```

Проверить, что эффективные параметры — `permitrootlogin no`, `passwordauthentication no`, `kbdinteractiveauthentication no`, `pubkeyauthentication yes`. Если значения другие, не перезагружать SSH: сначала разобраться с существующими drop-in файлами. Если значения верны, выполнить `sudo systemctl reload ssh`, не закрывать текущую сессию и проверить новым соединением вход `deployadmin` по ключу.

7. Включить security updates и проверить timezone:

```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
cat /etc/apt/apt.conf.d/20auto-upgrades
systemctl status apt-daily-upgrade.timer --no-pager
timedatectl set-timezone UTC
timedatectl
```

В диалоге unattended upgrades ответить `Yes`; в конфигурации `20auto-upgrades` оба параметра обновления списков и установки обновлений должны быть `"1"`. Ubuntu Server обычно уже применяет security updates ежедневно; повторная настройка делает это явным. Оставить автоматическую перезагрузку выключенной по умолчанию, чтобы сервер не перезагружался неожиданно; периодически проверять `/var/run/reboot-required`. Системное время рекомендуем оставить UTC, а расписание портфельной загрузки независимо задаётся `Europe/Moscow` в конфиге приложения.

8. Установить Git, Python 3.12 и Docker Engine. На Ubuntu 24.04 Python 3.12 доступен из системного репозитория. Сначала базовые инструменты:

```bash
sudo apt update
sudo apt install -y ca-certificates curl git python3.12 python3.12-venv python3-pip
```

Затем добавить официальный apt-репозиторий Docker и установить Engine вместе с Compose plugin:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run hello-world
sudo docker compose version
```

Команды приведены по [официальной инструкции Docker для Ubuntu](https://docs.docker.com/engine/install/ubuntu/). Не добавлять `deployadmin` или пользователя приложения в группу `docker`: доступ к Docker фактически даёт root-права. Администрировать контейнеры через `sudo docker ...`; сам Python-бот работает от отдельного непривилегированного пользователя. Автоматические security updates на Ubuntu описаны в [документации Ubuntu](https://ubuntu.com/server/docs/how-to/software/automatic-updates/).

### Шаг 3. Разместить код и секреты

#### 3.1. Создать private Git-репозиторий и отправить проверенный код

Рекомендуемый вариант — private repository `invest-cons` на GitHub (или уже используемый вами Git-сервер). Не публиковать репозиторий: в нём есть конфигурация и код, работающий с финансовыми данными. На GitHub создать пустой private repository без README, `.gitignore` и лицензии, чтобы не создавать конфликтующую начальную историю.

В текущей локальной копии первый коммит ещё не создан, Git remote не настроен, а имя/email автора коммитов не заданы. Сначала просмотреть список файлов и убедиться, что секретные/генерируемые файлы игнорируются:

```bash
cd /c/Users/baidakovma/Documents/invest_cons
git status --short
git check-ignore -v .env invest_cons-predeploy.dump data/certs/tinvest_grpc_roots_bundle.pem
```

Все три пути должны быть исключены правилами `.gitignore`. В частности, **не добавлять** `.env`, `invest_cons-predeploy.dump`, любые другие дампы, содержимое `data/certs`, `.venv` или локальные логи. Задать локальную identity Git своим именем и адресом, который вы хотите видеть в истории (значения не нужно присылать мне):

```bash
git config user.name "ВАШЕ ИМЯ"
git config user.email "ВАШ EMAIL"
```

После просмотра `git status` добавить файлы и проверить staged-состав до коммита:

```bash
git add -A
git diff --cached --name-only
git diff --cached --check
git status --short
```

В staged-списке не должно быть `.env`, `*.dump`, `*.backup`, `data/certs/*`, `.venv`, логов с данными счетов или иных секретов. Если что-то попало, убрать только этот файл из индекса командой `git restore --staged -- ПУТЬ` и добавить подходящее правило в `.gitignore`. Затем создать первый коммит и привязать пустой remote:

```bash
git commit -m "Initial invest-cons release"
git branch -M main
git remote add origin git@github.com:ВАШ_GITHUB_LOGIN/invest-cons.git
git push -u origin main
git tag -a v0.1.0 -m "Initial server release"
git push origin v0.1.0
```

Для `git push` с компьютера нужна ваша **локальная** SSH-аутентификация в GitHub. Приватный ключ не копировать на сервер и не присылать в чат. При первом SSH-подключении проверить fingerprint GitHub по его официальной документации, затем тестировать доступ командой `ssh -T git@github.com`. Подтверждение успешной аутентификации описано в [GitHub Docs](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/testing-your-ssh-connection).

> Если вы пока не готовы делать первый коммит самостоятельно, остановитесь после проверки staged-списка. Сначала нужно принять его как полный состав проекта: сейчас это новая репозитория без истории.

#### 3.2. Создать service user и отдельный read-only deploy key

На сервере код будет клонироваться из private repository от непривилегированного пользователя приложения. Deploy key ограничивается одним репозиторием и настраивается только для чтения; GitHub описывает этот механизм в [документации deploy keys](https://docs.github.com/en/rest/deploy-keys/deploy-keys). Не использовать ключ SSH-администратора сервера и не включать deploy key право записи.

Создать пользователя приложения и отдельное постоянное хранилище его SSH-ключа вне каталога кода:

```bash
sudo adduser --system --group --home /var/lib/invest-cons --no-create-home --shell /usr/sbin/nologin invest-cons
sudo install -d -o invest-cons -g invest-cons -m 0700 /var/lib/invest-cons/.ssh
sudo -u invest-cons ssh-keygen -t ed25519 -C "invest-cons-deploy@server" -f /var/lib/invest-cons/.ssh/id_ed25519 -N ""
sudo chmod 0600 /var/lib/invest-cons/.ssh/id_ed25519
sudo chmod 0644 /var/lib/invest-cons/.ssh/id_ed25519.pub
```

Показать и скопировать **только публичный** ключ:

```bash
sudo cat /var/lib/invest-cons/.ssh/id_ed25519.pub
```

В настройках private repository на GitHub открыть **Settings → Deploy keys → Add deploy key**, вставить public key, задать понятный title и оставить **Allow write access выключенным**. Приватный файл `id_ed25519` не выводить и не копировать.

Перед первым подключением получить GitHub host key и **сверить его fingerprint** с опубликованным GitHub fingerprint. Сам по себе `ssh-keyscan` не подтверждает подлинность ключа; сравнение обязательно:

```bash
sudo ssh-keyscan github.com | sudo tee -a /var/lib/invest-cons/.ssh/known_hosts >/dev/null
sudo chown invest-cons:invest-cons /var/lib/invest-cons/.ssh/known_hosts
sudo chmod 0644 /var/lib/invest-cons/.ssh/known_hosts
sudo ssh-keygen -lf /var/lib/invest-cons/.ssh/known_hosts
```

Сравнить fingerprint из вывода с актуальным списком [GitHub SSH key fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints). Если совпадения нет — удалить полученный `known_hosts`, не продолжать и выяснить причину. После сравнения настроить SSH-клиент для `invest-cons`:

```bash
sudo tee /var/lib/invest-cons/.ssh/config >/dev/null <<'EOF'
Host github.com
    HostName github.com
    User git
    IdentityFile /var/lib/invest-cons/.ssh/id_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking yes
EOF
sudo chown invest-cons:invest-cons /var/lib/invest-cons/.ssh/config
sudo chmod 0600 /var/lib/invest-cons/.ssh/config
```

Подставить фактический SSH URL private repository, проверить, что GitHub принимает именно deploy key, затем клонировать точный release tag:

```bash
sudo -u invest-cons env HOME=/var/lib/invest-cons ssh -T git@github.com
sudo install -d -o invest-cons -g invest-cons -m 0750 /opt/invest_cons
sudo -u invest-cons env HOME=/var/lib/invest-cons git clone --branch v0.1.0 --depth 1 git@github.com:ВАШ_GITHUB_LOGIN/invest-cons.git /opt/invest_cons
sudo -u invest-cons git -C /opt/invest_cons status --short --branch
sudo -u invest-cons git -C /opt/invest_cons describe --tags --exact-match
```

При первом SSH-подключении GitHub может вывести приветствие, что shell access не предоставляется; для теста SSH это ожидаемо. Команда клонирования должна завершиться успешно и показать тег `v0.1.0`. Каталог `/opt/invest_cons` должен содержать только версионируемые файлы проекта, без `.env`, дампа базы и пользовательских TLS bundle.

#### 3.3. Создать venv и установить зависимости

В `requirements.txt` есть дополнительный индекс T-Invest. Сначала проверить доступность DNS/HTTPS сервера и установку пакетов; если установка T-Invest падает, не удалять дополнительный индекс и не подменять пакет случайной версией. Передавать управление дальше только после успешного `pip check`.

```bash
sudo -u invest-cons python3.12 -m venv /opt/invest_cons/.venv
sudo -u invest-cons /opt/invest_cons/.venv/bin/pip install --upgrade pip
sudo -u invest-cons /opt/invest_cons/.venv/bin/pip install -r /opt/invest_cons/requirements.txt
sudo -u invest-cons /opt/invest_cons/.venv/bin/pip check
```

Если ошибка сообщает об отсутствии компилятора для зависимости, можно установить `build-essential` и `python3.12-dev`, затем повторить установку. Не устанавливать пакеты глобально системным `pip`.

#### 3.4. Разместить TLS-сертификаты, только если они нужны

Не копировать Windows `data/certs` целиком: `tinvest_grpc_roots_bundle.pem` — локально генерируемый объединённый bundle и не должен попадать в Git. Если локальный запуск требует `TINVEST_GRPC_EXTRA_ROOT_CERTS`, определить именно исходные проверенные CA-сертификаты и передать только их. Проверьте локально, какой исходный CA используется в `.env`, не присылая его содержимое в чат.

С локального компьютера по SSH, только если дополнительный корень действительно нужен:

```bash
scp -i KEY_PATH /путь/к/исходному-root-ca.pem deployadmin@64.225.103.14:/tmp/verified-root-ca.pem
```

На сервере установить его с правом чтения для группы приложения:

```bash
sudo install -d -o root -g invest-cons -m 0750 /etc/invest-cons/certs
sudo install -o root -g invest-cons -m 0640 /tmp/verified-root-ca.pem /etc/invest-cons/certs/verified-root-ca.pem
sudo rm -f /tmp/verified-root-ca.pem
```

Тогда в env задать `TINVEST_GRPC_EXTRA_ROOT_CERTS=/etc/invest-cons/certs/verified-root-ca.pem` и `TINVEST_GRPC_ROOTS_BUNDLE_PATH=/var/lib/invest-cons/tinvest_grpc_roots_bundle.pem`. Если дополнительных корней в локальной конфигурации нет, обе переменные оставить незаданными. Не устанавливать CA в системное хранилище ради приложения: проект формирует отдельный gRPC bundle.

#### 3.5. Создать защищённый environment file

Systemd прочитает env-файл до запуска от имени `invest-cons`. Создать каталог и файл с точными правами, затем редактировать его на сервере. Не класть секреты в `.env` проекта, Git, команды shell, историю команд или Telegram:

```bash
sudo install -d -o root -g invest-cons -m 0750 /etc/invest-cons
sudo install -o root -g invest-cons -m 0640 /dev/null /etc/invest-cons/invest-cons.env
sudo nano /etc/invest-cons/invest-cons.env
```

В файл внести реальные значения (в примере placeholders, их не оставлять). Для базы сгенерировать случайный пароль, состоящий только из hex-символов; пример команды запустить в отдельном терминале и вставить результат в оба поля:

```bash
openssl rand -hex 32
```

```dotenv
INVEST_TOKEN_READ=<read-only токены через запятую, в порядке меток>
INVEST_CLIENT_LABELS=Mike,Marina
FREEDOM_TRADERNET_PUBLIC_KEY=<ключ Freedom>
FREEDOM_TRADERNET_PRIVATE_KEY=<секрет Freedom>
FREEDOM_TRADERNET_DOMAIN=freedom24.com
FREEDOM_CLIENT_LABEL=Freedom
PORTFOLIO_V2_TELEGRAM_TOKEN=<token нового Telegram-бота>
PORTFOLIO_V2_TELEGRAM_IDS=<числовые Telegram user ID через запятую>
PORTFOLIO_V2_OWNER_MAP={"Mike":"owner_mike","Marina":"owner_marina","Freedom":"owner_mike"}
POSTGRES_DB=invest_cons
POSTGRES_USER=invest_cons
POSTGRES_PASSWORD=<сгенерированный hex-пароль>
POSTGRES_PORT=5434
DATABASE_URL=postgresql+asyncpg://invest_cons:<тот-же-hex-пароль>@127.0.0.1:5434/invest_cons
```

`INVEST_TOKEN_READ` и `INVEST_CLIENT_LABELS` должны соответствовать по порядку; брокерские токены должны быть read-only. Ключи и labels должны совпадать с именами в `PORTFOLIO_V2_OWNER_MAP`. `PORTFOLIO_V2_TELEGRAM_IDS` — только доверенные личные ID: сейчас каждый из них видит все подключённые счета. Не использовать токен старого бота, если тот ещё где-либо запущен.

Если нужны сертификаты, добавить две переменные из предыдущего подраздела. Все пути должны быть абсолютными Linux-путями. Проверить владельца/права, не выводя значения секретов:

```bash
sudo chown root:invest-cons /etc/invest-cons/invest-cons.env
sudo chmod 0640 /etc/invest-cons/invest-cons.env
sudo -u invest-cons test -r /etc/invest-cons/invest-cons.env && echo 'Environment file readable by service group'
sudo stat -c '%a %U:%G %n' /etc/invest-cons /etc/invest-cons/invest-cons.env
```

### Шаг 4. Запустить PostgreSQL и перенести данные

1. Compose должен брать переменные из защищённого env-файла. Не хранить production `.env` в Git. После изменения `compose.yaml` порт БД публикуется только на `127.0.0.1:5434`.

```bash
cd /opt/invest_cons
sudo docker compose --env-file /etc/invest-cons/invest-cons.env up -d postgres
sudo docker compose --env-file /etc/invest-cons/invest-cons.env ps
sudo docker compose --env-file /etc/invest-cons/invest-cons.env exec postgres pg_isready -U invest_cons -d invest_cons
```

2. Передать backup в `/tmp` через `scp` с локального компьютера. Используйте серверный администраторский ключ, с которым уже работает SSH, и проверьте, что файл на сервере имеет ожидаемый размер. Это отдельная передача данных по SSH; **код приложения архивом не передаётся**. Затем скопировать dump в контейнер и восстановить в созданную Compose пустую БД:

```bash
sudo docker compose --env-file /etc/invest-cons/invest-cons.env cp /tmp/invest_cons-predeploy.dump postgres:/tmp/invest_cons.dump
sudo docker compose --env-file /etc/invest-cons/invest-cons.env exec -T postgres pg_restore -U invest_cons -d invest_cons --clean --if-exists --no-owner --no-privileges /tmp/invest_cons.dump
```

3. Удалить временную копию из контейнера и с `/tmp` после проверки. Сверить контрольные количества строк. Не запускать `pg_restore` поверх уже используемой рабочей БД.
4. Проверить соединение приложения к `127.0.0.1:5434`. `DATABASE_URL` использует `asyncpg` scheme `postgresql+asyncpg`; стандартные `pg_dump`/`pg_restore` используют обычное PostgreSQL-соединение и приведены отдельно.

### Шаг 5. Установить systemd unit

Создать `/etc/systemd/system/invest-cons.service`:

```ini
[Unit]
Description=Invest Cons Telegram bot and daily portfolio refresh
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=invest-cons
Group=invest-cons
WorkingDirectory=/opt/invest_cons
EnvironmentFile=/etc/invest-cons/invest-cons.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/invest_cons/.venv/bin/python /opt/invest_cons/run_portfolio_v2.py
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths=/opt/invest_cons
StateDirectory=invest-cons

[Install]
WantedBy=multi-user.target
```

Проверить, что `invest-cons` может читать код, venv и env-файл. Бот не должен писать runtime-файлы внутрь проекта; если это изменится, разрешить запись только в отдельный `/var/lib/invest-cons`, а не отключать системные ограничения целиком.

```bash
sudo systemctl daemon-reload
sudo systemctl enable invest-cons
```

### Шаг 6. Предпусковая проверка и переключение

1. Пока старый polling работает локально, не запускать серверный бот. Сначала проверить конфигурацию и доступность Postgres.
2. Остановить локальный экземпляр старого нового бота. Убедиться, что он не перезапускается IDE, планировщиком Windows или службой.
3. Установить подготовленный oneshot unit и выполнить однократную синхронизацию. Он читает тот же защищённый env-файл через systemd и запускается до постоянно работающего бота:

```bash
sudo cp /opt/invest_cons/invest-cons-refresh.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start invest-cons-refresh
sudo systemctl show invest-cons-refresh -p Result -p ExecMainStatus --no-pager
sudo journalctl -u invest-cons-refresh -n 200 --no-pager
```

Код возврата 2 означает неполный snapshot или проблему синхронизации и требует разбора до включения постоянного бота. Не считать пустой/частичный результат успешным переносом. Не запускать этот unit одновременно с `invest-cons.service`.

4. Запустить сервис и проверить журнал:

```bash
sudo systemctl start invest-cons
sudo systemctl status invest-cons --no-pager
sudo journalctl -u invest-cons -n 200 --no-pager
```

5. В Telegram проверить `/start`, `/portfolio`, `/performance`, `/income` и ручной `/refresh`; убедиться, что видны ожидаемые владельцы, счета и позиции, а ежедневный запуск расписания указан в локальном часовом поясе. Убедиться, что в логах нет секретов.
6. После нескольких успешных циклов проверить повторный вход в Telegram, перезапуск VM и автоматический старт сервиса.

## 6. Backup, обновление и эксплуатация

### Резервирование

Системный Droplet backup — только быстрый образ VM. Ежедневно создавать логический custom-format `pg_dump` PostgreSQL, шифровать его до отправки и хранить копию вне VPS (другой провайдер/регион или контролируемое хранилище). Начальная политика: 7 ежедневных и 4 еженедельных копии, затем скорректировать по размеру и бюджету. `.env` не включать в обычную копию рядом с БД; секреты хранить отдельно в менеджере паролей.

Раз в квартал восстанавливать копию в отдельную временную БД и сравнивать контрольные счётчики. Записывать timestamp, commit приложения, версию PostgreSQL и результат восстановления. Не удалять старые копии, пока новая не проверена. До появления автоматизированного backup job не считать резервирование настроенным только потому, что включены backups у провайдера.

### Обновление приложения

Релиз выпускается с компьютера разработчика как проверенный annotated tag, а сервер получает его через read-only deploy key. Не коммитить production `.env`, дампы или сертификаты.

На локальном компьютере после тестов:

```bash
git status --short
git tag -a v0.1.1 -m "Release v0.1.1"
git push origin main
git push origin v0.1.1
```

На сервере сначала сделать PostgreSQL backup, сохранить текущий commit как точку возврата, получить тег и проверить его commit SHA. Путь к `.venv` остаётся неизменным; после checkout обновить зависимости и проверить тесты до повторного запуска сервиса:

```bash
cd /opt/invest_cons
sudo sh -c 'docker compose --env-file /etc/invest-cons/invest-cons.env exec -T postgres pg_dump -U invest_cons -d invest_cons --format=custom --no-owner --no-privileges > /var/lib/invest-cons/pre-update.dump'
sudo chown invest-cons:invest-cons /var/lib/invest-cons/pre-update.dump
sudo chmod 0600 /var/lib/invest-cons/pre-update.dump
sudo -u invest-cons git -C /opt/invest_cons rev-parse HEAD
sudo -u invest-cons env HOME=/var/lib/invest-cons git -C /opt/invest_cons fetch --tags --prune origin
sudo -u invest-cons git -C /opt/invest_cons rev-parse 'v0.1.1^{commit}'
sudo systemctl stop invest-cons
sudo -u invest-cons env HOME=/var/lib/invest-cons git -C /opt/invest_cons checkout --detach v0.1.1
sudo -u invest-cons /opt/invest_cons/.venv/bin/pip install -r /opt/invest_cons/requirements.txt
sudo -u invest-cons /opt/invest_cons/.venv/bin/pip check
sudo -u invest-cons /opt/invest_cons/.venv/bin/python -m pytest -q /opt/invest_cons/tests
sudo systemctl start invest-cons
sudo systemctl status invest-cons --no-pager
sudo journalctl -u invest-cons -n 200 --no-pager
```

Заменить `v0.1.1` на фактический новый тег. Записать вывод `rev-parse HEAD` до обновления как `<PREVIOUS_COMMIT>`. Если проверка не прошла, остановить сервис, вернуть `<PREVIOUS_COMMIT>` командой `sudo -u invest-cons env HOME=/var/lib/invest-cons git -C /opt/invest_cons checkout --detach <PREVIOUS_COMMIT>`, затем восстановить нужные зависимости и запустить предыдущую версию. `pre-update.dump` удалять только после успешной проверки обновления и после того, как backup сохранён вне сервера.

Если новая версия изменила схему БД, одного возврата кода недостаточно: до обновления нужна проверенная процедура миграции/восстановления. Сейчас миграции не версионированы, поэтому DDL-изменения требуют отдельного плана и свежей проверенной резервной копии. Не выполнять автоматический `git pull` или `git reset` как замену release-процедуре.

### Еженедельная проверка

```bash
systemctl is-active invest-cons
sudo docker compose --env-file /etc/invest-cons/invest-cons.env ps
df -h
free -h
journalctl -u invest-cons --since '7 days ago' --no-pager
```

Проверять также время последнего полного snapshot, статусы брокеров/операций и дату последнего проверенного backup. `systemctl active` само по себе не означает, что брокерские данные успешно обновляются.

## 7. Этапы и ответственность

| Этап | Codex | Владелец проекта |
|---|---|---|
| Проверка кода, тестов и подготовка deploy-файлов | Выполняет в репозитории | Принимает изменения |
| Регистрация у облачного провайдера, выбор тарифа/региона, оплата | Не выполняет без доступов/подтверждения владельца | Выполняет |
| Создание SSH-ключа и управление приватным ключом | Может дать команды, приватный ключ не получает | Выполняет |
| Создание VPS и первичная настройка доступа | Может дать пошаговые команды; удалённый доступ отдельно согласуется | Создаёт/утверждает |
| Ввод API/Telegram/DB секретов | Не запрашивает секреты в чате | Вводит на сервере |
| Backup, передача, восстановление данных | Готовит команды и помогает проверить процесс | Запускает/контролирует защищённую передачу |
| Запуск сервиса и техническая диагностика | Может анализировать обезличенный журнал/ошибки | Запускает и подтверждает Telegram и сверку портфеля |
| Регламент backup и стоимость | Предлагает параметры | Утверждает бюджет, срок хранения и место хранения |

### Критерии завершения переноса

- На VPS включён автозапуск одного systemd-сервиса, и второй процесс с тем же Telegram token остановлен.
- Порт PostgreSQL недоступен извне; firewall не открывает ненужные порты.
- Перенесённая история сверена по контрольным количествам, а первый серверный snapshot и оба брокера проверены.
- Бот отвечает на основные команды, `/refresh` и ежедневное расписание завершились с понятным статусом.
- Есть внешний зашифрованный backup и подтверждённое тестовое восстановление.
- Секреты не находятся в Git, журнале, backup рядом с БД или переписке.
