# Deployment Continuity Gate v1

The continuity gate proves a narrow post-restart claim for one persistent Anet
node. It is a two-phase, cross-platform interface shared by Windows, WSL,
Linux, macOS, and Termux.

Before restarting the native supervisor, WSL distribution, or device:

```text
anet --home <ANET_HOME> continuity-prepare --out <PRIVATE_CHALLENGE>
```

The command stops closed unless Supervisor Health v1 is currently healthy. It
writes a private, expiring challenge containing the Node ID, node-home
fingerprint, supervisor instance and boot-session identifiers, and SHA-256
hashes of `identity.json`, `tls-key.pem`, and `tls-cert.pem`. The complete
challenge is signed by the current node identity, so field tampering is
rejected before it can become restart evidence. The default TTL is 24 hours;
accepted TTLs range from 60 seconds to seven days.

After a service-manager restart:

```text
anet --home <ANET_HOME> continuity-verify <PRIVATE_CHALLENGE>
```

After an actual Windows/macOS/Linux/Android reboot or WSL distribution restart:

```text
anet --home <ANET_HOME> continuity-verify <PRIVATE_CHALLENGE> \
  --require-boot-change
```

Verification succeeds only when:

- the challenge is valid, unexpired, and belongs to this exact node home;
- the Node ID and all three protected identity/TLS files are unchanged;
- the TLS certificate belongs to the Node ID and matches its private key;
- Supervisor Health v1 is currently healthy;
- the supervisor instance differs from the prepared instance;
- the current supervisor completed a control sync after preparation;
- and, when requested, the operating-system boot-session identifier changed.

One successful verification atomically creates
`<ANET_HOME>/continuity/receipts/<CHALLENGE_ID>.json`. That local marker makes
the challenge one-time even if its original file was copied before use. Both
the challenge and receipt are private evidence containing Node ID, paths,
process/session identifiers, timestamps, and hashes; redact them before any
external sharing.

This gate does not restart the machine or service. An operator or authorized
platform Adapter performs that action between the two commands. It also does
not prove listener ownership, peer reachability, route recovery, queued-work
preservation, PeerBook/revocation/prekey stability, or bidirectional delivery.
Those remain separate Gate 7 and physical-device checks.
