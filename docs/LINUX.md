# GRFS Server для Linux

Сервер работает на Linux с Python 3.11 или новее, без сторонних зависимостей.
Клиенты с игрой подключаются с Windows к IP Linux-сервера через `GRFS-Client.exe`.

Готовая папка — `dist/linux/` в репозитории. Также можно собрать архив:

```sh
python3 tools/build_linux.py
tar -xzf dist/GRFS-Linux.tar.gz
cd GRFS-Linux
```

## Ручной запуск

Проверочный запуск без дополнительных прав, из корня исходников или папки
Linux-комплекта:

```sh
python3 --version
cp grfs.ini.example grfs.ini
./run-server.sh --onlineconfig-port 8081
```

Параметры передаются серверу напрямую, например `./run-server.sh --bind 127.0.0.1`.
Другой интерпретатор выбирается через `GRFS_PYTHON=/путь/к/python3 ./run-server.sh`.
Настройки, `data/` и `logs/` создаются рядом со скриптом, независимо от текущей папки.
Ctrl+C и SIGTERM корректно останавливают сервер с сохранением данных.

Игра запрашивает конфигурацию на TCP 80; порт 8081 подходит для проверки без игры.
На Linux порт 80 обычно требует права `CAP_NET_BIND_SERVICE`; готовая служба ниже
выдаёт его процессу сервера и запускает сервер на игровых портах по умолчанию.

## Автозапуск через systemd

Для системы с systemd и Python 3.11+ в `/usr/bin/python3`. Выполните из папки
Linux-комплекта; для исходников unit-файл находится в `deploy/`:

```sh
sudo install -d /opt/grfs-emulator /etc/grfs-emulator
sudo cp -a grfs schema run-server.sh /opt/grfs-emulator/
sudo install -m 644 grfs.ini.example /etc/grfs-emulator/grfs.ini
sudo install -m 644 grfs-emulator.service /etc/systemd/system/grfs-emulator.service
sudo systemctl daemon-reload
sudo systemctl enable --now grfs-emulator
```

systemd создаёт отдельного служебного пользователя и каталоги состояния:

- Настройки: `/etc/grfs-emulator/grfs.ini`.
- Аккаунты, друзья, профили и прочие данные: `/var/lib/grfs-emulator/`.
- Логи: `/var/log/grfs-emulator/grfs.log` и `journalctl -u grfs-emulator`.

Остановка и перезапуск: `sudo systemctl stop grfs-emulator`,
`sudo systemctl restart grfs-emulator`. Уже созданный конфиг при обновлении
сохраняйте; повторное копирование шаблона заменит ваши настройки.

## Сеть

Разрешите входящие TCP 80 и UDP 21030–21033 на сервере. Страница состояния
доступна на TCP 8080, этот порт для подключения игры не требуется.
Если сервер за NAT, перенаправьте игровые порты на него и задайте в `advertise`
адрес, доступный игрокам. На Windows-компьютерах укажите IP сервера в лаунчере.

Windows-библиотеки из `dist/python/` для Linux не используются. Linux-комплект
содержит код сервера, обе таблицы из `schema/`, скрипт запуска и unit-файл.
