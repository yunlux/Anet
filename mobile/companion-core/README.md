# Anet Companion Core

This is the Android-independent Kotlin implementation of the
`anet.companion v1` protocol boundary. It intentionally contains no Android
Service, UI, network transport, node identity, or persistent node home.

The module:

- validates the six Companion kinds with exact fields;
- normalizes IDs, tokens, consent, observations and bounded metrics;
- rejects forbidden raw/private sensor fields;
- validates ApprovalRequest/ApprovalDecision binding;
- binds source/target Device Node IDs to the enclosing Packet endpoints;
- reads the same fixtures as the Python implementation from
  `docs/examples/companion-v1`.

It is designed to become a dependency of an Android application. Protocol and
privacy validation must remain independent from notification UI and network
lifecycles.

Build with JDK 21 and Gradle 9.6.1 or a compatible newer patch:

```text
gradle test
```

The repository does not commit a downloaded JDK, Gradle distribution, Android
SDK, private key, or Anet node home. Runtime and test dependencies are pinned
in `gradle.lockfile`; regenerate it only as an explicit dependency update.
