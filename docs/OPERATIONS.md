# Operations

## Paths

- Source project: `/home/joker/services/signalwatch`
- Installed code: `/opt/signalwatch`
- Configuration: `/etc/signalwatch/config.toml`
- State database: `/var/lib/signalwatch/state.db`
- Service unit: `/etc/systemd/system/signalwatch.service`
- ntfy credentials: `/etc/signalwatch/ntfy.env`, root-owned and never printed

## Commands

```bash
sudo systemctl status signalwatch
sudo journalctl -u signalwatch -n 100 --no-pager
sudo -u signalwatch env PYTHONPATH=/opt/signalwatch/src \
  /usr/bin/python3 -m signalwatch --config /etc/signalwatch/config.toml status
```

The ntfy topic is `signalwatch`. Subscribe to it on the existing ntfy server as
the existing `joker` account. The service uses a separate write-only ntfy user;
its token cannot subscribe or access other topics.

## Safe restart

```bash
sudo systemctl restart signalwatch
sudo systemctl is-active signalwatch
```

Pending outbox rows and collector baselines survive a restart. Restarting does
not cause the current feed contents to be sent again.

## Backup and restore

Stop the service before a byte-for-byte database backup:

```bash
sudo systemctl stop signalwatch
sudo cp -a /var/lib/signalwatch/state.db /var/lib/signalwatch/state.db.backup
sudo systemctl start signalwatch
```

Configuration and source code contain no credentials. Preserve the private ntfy
credential file separately as part of host secret management.

## Remove or roll back

```bash
sudo systemctl disable --now signalwatch
```

Disabling the unit leaves its code, configuration, and database recoverable.
Do not delete `/var/lib/signalwatch` unless the alert history and feed baseline
are intentionally being discarded.
