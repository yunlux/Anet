# Physical-node onboarding handoff

This runbook connects one operator-approved physical device to an existing
Anet deployment without copying identities or publishing deployment metadata.
All values below are placeholders supplied by the operators at execution time.

## Preconditions

- The new device is physically distinct and runs a supported platform.
- Its operator explicitly authorizes installation and node initialization.
- The Anet wheel and SHA-256 are transferred through an approved channel.
- Every existing and new runtime has its own private node home and port.
- Complete Node IDs are compared through a second authenticated channel.

Do not place hostnames, usernames, LAN addresses, service names, profile names,
complete Node IDs, Peer Cards, packet IDs, or evidence paths in this document
or in a public issue.

## Prepare the new device

Review the macOS bootstrap before use:

```bash
./scripts/bootstrap-macos.sh <WHEEL> --sha256 <VERIFIED_SHA256> \
  --advertise <LAN_ADDRESS> --lan-zone <OPAQUE_LAN_ZONE> \
  --port <UNUSED_PORT> --label <OPERATOR_LABEL>
```

The bootstrap verifies the wheel, installs an isolated runtime, initializes one
new private node home, and exports a public signed Card. It does not copy an
existing identity, add peers, open a public listener, or start a daemon.

If initialization is not explicitly authorized, stop after installing the
platform runtime with `scripts/install_macos.py`.

## Pair explicitly

1. Run `doctor` on each intended home and privately record redacted identity
   fingerprints.
2. Create a time-limited `pair-offer` on one existing node.
3. Transfer the offer through an authenticated channel.
4. Accept it locally on the new device and return the bound response.
5. Complete the original offer and verify the reciprocal PeerBooks.
6. Configure scoped locators with `locator-config`, then redistribute the newly
   signed Cards. Never hand-edit `config.json` or `card.json`.

Labels and addresses are metadata, not identity. A shared LAN or mirrored
network does not permit shared homes, keys, databases, or ports.

## Acceptance gates

- distinct physical devices, node homes, Node IDs, and listener ports;
- `doctor` succeeds on every intended node;
- bidirectional direct probes include destination receipts;
- restart preserves identity, trust, revocations, and queued state;
- carrier fallback, when in scope, is tested with direct listening disabled;
- final `pending`, `rejections`, and `untrusted` counts match the declared
  baseline.

Keep raw evidence private. Publish only the platform types, Anet version, test
result, redacted fingerprints, and the strongest completed verification gate.
Follow
[`openwiki/testing/verification.md`](../openwiki/testing/verification.md) for
the complete staged procedure.
