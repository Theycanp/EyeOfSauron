# Security model

- The collector has no inbound listener. The optional management process binds
  only to `127.0.0.1`, requires a separate bearer token, and opens no firewall
  port; remote access is through an SSH tunnel.
- The operating-system account is unprivileged and has no interactive shell.
- The ntfy account is separate from the administrator and has write-only access
  to the `eos` topic.
- Tokens are stored only in root-owned environment files under
  `/etc/argus`, outside the source tree, with group-read permission for
  the service account. Managed configuration stores environment variable names,
  never secret values.
- HTTP credentials are never included in exception strings, application logs,
  the SQLite database, tests, or documentation.
- RSS redirects are restricted to configured HTTPS hosts, and remote content is
  bounded before parsing.
- Authenticated market and X clients reject redirects so credentials cannot be
  forwarded to another host.
- MQTT command support is deny-by-default. A command needs an allowlisted
  device/action, bounded expiry, required parameters, and an idempotency key.
  It is not a public remote-control endpoint.

At-least-once delivery can rarely produce a duplicate after a crash between
remote acceptance and local acknowledgement. It is designed to avoid silent
loss instead.

Because ntfy runs on the same physical host, this service cannot use it to report
that the entire host or home Internet connection is down. Host-death detection
must eventually use an external heartbeat and a secondary notification path.
