# Security model

- SignalWatch has no inbound listener and opens no firewall port.
- The operating-system account is unprivileged and has no interactive shell.
- The ntfy account is separate from the administrator and has write-only access
  to the `signalwatch` topic.
- Tokens are stored only in `/etc/signalwatch/ntfy.env`, outside the source tree,
  with root ownership and group-read permission for the service account.
- HTTP credentials are never included in exception strings, application logs,
  the SQLite database, tests, or documentation.
- RSS redirects are restricted to configured HTTPS hosts, and remote content is
  bounded before parsing.

At-least-once delivery can rarely produce a duplicate after a crash between
remote acceptance and local acknowledgement. It is designed to avoid silent
loss instead.

Because ntfy runs on the same physical host, this service cannot use it to report
that the entire host or home Internet connection is down. Host-death detection
must eventually use an external heartbeat and a secondary notification path.
