# Supervisor Health v1

Every persistent `anet supervisor` process owns one private
`<ANET_HOME>/supervisor-health.json` document. It is updated atomically as the
control-page sync and supervised `anet serve` child move through their
lifecycle. Operators and Agents read the same interface with:

```text
anet --home <ANET_HOME> supervisor-status
```

The command exits `0` only when all of these observations are true:

- `kind` is `anet.supervisor.health` and `schema_version` is `1`;
- the supervisor state and server-child state are both `running`;
- both recorded process IDs still identify live local processes;
- the heartbeat is no older than the configured poll interval plus 30 seconds
  (with a minimum 30-second window).

Missing, malformed, stale, degraded, restarting, or stopped state exits `1`
and still prints a machine-readable JSON observation. A control-sync failure
records `degraded`, the bounded error text, and a consecutive-failure count
even when the supervisor successfully keeps the existing server child alive.
A server-child exit also records degraded state before the restart attempt.

The document records local PIDs, timestamps, the last control sequence, and a
bounded local error. It is private runtime evidence and must not be published
without redaction. It does not contain node private keys or message content.

One-click installers wait for this interface after registering the native
Scheduled Task, systemd unit, LaunchAgent, or runit service. Deployment Receipt
v1 embeds the resulting observation as `supervisor.health`. This proves the
first persistent sync and child start observed during installation; it does
not prove a later reboot. After reboot or logout, run `supervisor-status` again
and combine it with the native service manager's state for release evidence.
