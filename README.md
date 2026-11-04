# Netwatch для Orange Pi Lite

Лёгкий круглосуточный монитор качества интернета для Debian 13 (Trixie). Каждые пять секунд он проверяет домашний шлюз и два внешних IPv4-адреса через Wi-Fi, сохраняет результаты в SQLite и формирует отчёты о потерях и перерывах. Отдельный таймер systemd выполняет ежедневный замер скорости через HTTPS.

- `netwatch.py` — параллельный ping шлюза, Cloudflare и Google.
- `netwatch-report.py` — отчёт за выбранное число часов.
- `netwatch-speed.py` — короткие передачи к Cloudflare в обоих направлениях.
- `*.service`, `*.timer` — запуск после загрузки и расписание.

## Настройки

По умолчанию используются интерфейс `wlan0`, шлюз `192.168.0.1` и внешние адреса `1.1.1.1`, `8.8.8.8`. Если сеть отличается, проверьте `ip -4 route` и измените интерфейс в обоих скриптах сбора, а адреса — в `netwatch.py` и `netwatch-report.py`. Время отчёта задаётся через `ZONE` в скрипте отчёта. По умолчанию таймер запускается в 04:15 по Брюсселю.

## Установка на Debian 13

Команды ниже рассчитаны на новую установку и выполняются из каталога проекта. Если Netwatch уже работает, используйте существующего владельца базы и сохраните текущие параметры службы. Перед заменой файлов сравните их с установленными.

```sh
sudo apt install python3 iputils-ping linux-sysctl-defaults curl ca-certificates tzdata
getent passwd netwatch >/dev/null || sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin netwatch
sudo install -d -m 0755 /usr/local/lib/netwatch
sudo install -d -o netwatch -g netwatch -m 0750 /var/lib/netwatch
sudo install -m 0644 netwatch.py netwatch-report.py netwatch-speed.py /usr/local/lib/netwatch/
sudo install -m 0644 netwatch.service netwatch-speed.service netwatch-speed.timer /etc/systemd/system/
```

Проверьте доступ к ping от имени службы. Если команда сообщает об ошибке прав, проверьте `sysctl net.ipv4.ping_group_range` и применение настроек пакета `linux-sysctl-defaults`.

```sh
sudo -u netwatch /usr/bin/ping -4 -n -I wlan0 -c 1 -W 2 192.168.0.1
sudo systemd-analyze verify /etc/systemd/system/netwatch.service /etc/systemd/system/netwatch-speed.service /etc/systemd/system/netwatch-speed.timer
sudo systemctl daemon-reload
sudo systemctl enable --now netwatch.service
systemctl status netwatch.service --no-pager
```

Замер скорости включается отдельно. Пробный запуск передаёт примерно 12 МБ данных. Если он успешен, можно включить таймер.

```sh
sudo systemctl start netwatch-speed.service &&
sudo systemctl enable --now netwatch-speed.timer
systemctl list-timers --all netwatch-speed.timer
sudo journalctl -u netwatch-speed.service -n 10 --no-pager
```

Если плата выключена в момент замера, он пропускается. Измеренная скорость относится к одной HTTPS-передаче с платы через выбранный интерфейс. Во время теста задержка ping может вырасти.

## Отчёт и хранение

```sh
sudo -u netwatch /usr/bin/python3 /usr/local/lib/netwatch/netwatch-report.py --hours 24
```

Отчёт отделяет запросы без ответа от ошибок выполнения и пропусков сбора. `external_down` означает, что шлюз ответил, а оба внешних узла промолчали. `all_down` означает отсутствие ответа от всех трёх узлов. Это наблюдения с точки зрения Orange Pi. Границы событий оцениваются с точностью до интервала опроса.

База хранится в `/var/lib/netwatch/netwatch.sqlite3`. Таблица `probes` содержит результаты ping, а `speed_tests` — интервалы передач, объём и скорость. История накапливается без автоматического удаления. Для сохранения отчёта добавьте к команде `> ~/netwatch-24h.txt`. При плановом выключении платы используйте `sudo poweroff`.

Для диагностики запуска используйте `sudo journalctl -u netwatch.service -n 20 --no-pager`. Если служба активна, повторите отчёт через 15 секунд и проверьте, что счётчики растут.
