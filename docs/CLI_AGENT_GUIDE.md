# Anet CLI guide for autonomous Agents

This guide is the executable path for an Agent that has been authorized to
install and operate Anet. Anet installation, persistent node creation, trust
changes, and process supervision are separate actions. Authorization for one
does not imply authorization for the others.

## CLI or MCP?

Use CLI as Anet's control plane:

- install and upgrade the runtime;
- discover, initialize, diagnose, back up, or recover a node;
- export Cards, pair, revoke, or change trust;
- configure locators, carriers, routing, and services;
- perform sparse, one-shot operations where a persistent MCP session would add
  unnecessary schema and lifecycle overhead.

Use MCP as the data plane for a long-lived Agent's frequent `send`, `sync`,
`probe`, durable claim, and typed task operations. MCP avoids repeated process
startup and shell quoting, while CLI avoids keeping a large tool schema in
context for occasional work.

The recommended architecture is **CLI control plane + narrowly scoped MCP data
plane**. A runtime installation or trust change must never be inferred from an
MCP message.

## Safety contract

An Agent may install the versioned runtime when asked to install Anet. It must
not automatically create a persistent node, copy an existing node home, accept
a peer, revoke a peer, or start a daemon unless that action is also in scope.

Before creating or repairing a persistent node:

1. locate the deployment-owned `ANET_HOME`;
2. check for `identity.json` and `config.json`;
3. if they exist, run `doctor` and use that node rather than initializing;
4. if an expected home is missing, stop and locate the deployment configuration;
5. create a new node only when explicitly authorized.

One persistent runtime owns one private node home and one complete Node ID.
Never copy a node home, `identity.json`, TLS key, or SQLite state between
Windows, WSL, macOS, profiles, containers, or workers.

## 1. Install a pinned runtime

Use the release wheel and its trusted SHA-256. `core` installs the CLI only;
`mcp` installs the CLI and stdio MCP dependency; `full` additionally installs
Ahub server/Relay dependencies. The installations are versioned and
idempotent.

### Windows

```powershell
.\scripts\install_windows.ps1 `
  -Version 0.12.1 `
  -Wheel .\dist\anet_fabric-0.12.1-py3-none-any.whl `
  -WheelSha256 6AC09D43E470E9E3A88C8AACCFE47F3971CF78785103012C6FC645A2461CBCD7 `
  -Feature mcp
```

Read `%LOCALAPPDATA%\Anet\current.json` to discover the selected absolute
`runtime` and `cli` paths. Do not guess a Python installation.

### WSL

```bash
python3 scripts/install_wsl.py \
  --version 0.12.1 \
  --wheel dist/anet_fabric-0.12.1-py3-none-any.whl \
  --wheel-sha256 6AC09D43E470E9E3A88C8AACCFE47F3971CF78785103012C6FC645A2461CBCD7 \
  --feature mcp
```

The selected CLI is `~/.local/anet/current/venv/bin/anet`.

### macOS

```bash
python3 scripts/install_macos.py \
  --version 0.12.1 \
  --wheel dist/anet_fabric-0.12.1-py3-none-any.whl \
  --wheel-sha256 6AC09D43E470E9E3A88C8AACCFE47F3971CF78785103012C6FC645A2461CBCD7 \
  --feature mcp
```

The selected CLI is
`~/Library/Application Support/Anet/current/venv/bin/anet`.

Verify the selected binary before continuing:

```text
anet --version
anet --help
```

Expected version: `Anet 0.12.1`.

For a new Linux host already running Hermes, the self-contained
`skills/install-anet` workflow can install this runtime from one Skill prompt.
See [`HERMES_SKILL_INSTALL.md`](HERMES_SKILL_INSTALL.md).

### Explicitly authorized WSL host bootstrap

When the user asks for the complete persistent WSL environment—not merely an
install—use the Skill's deterministic bootstrap:

```bash
python3 <SKILL_DIR>/scripts/bootstrap_wsl.py \
  --agent-id <STABLE_LOCAL_PROFILE_ID>
```

This is a deployment layer over the clean installer. It installs the `full`
runtime, validates and reuses the one registered host-local Ahub or creates it
only when none exists, creates/reuses one private node per Agent, explicitly
pairs only bootstrap-managed local nodes, writes a least-privilege MCP config,
and manages systemd user services. It stops on incomplete Ahub state, an
unknown Ahub unit, a missing registered node, or a Node ID mismatch.

The bootstrap registry is `~/.config/anet/bootstrap.json`; node state is under
`~/.local/state/anet/nodes/`. These locations are deployment metadata, not a
license to scan other homes. See
[`HERMES_SKILL_INSTALL.md`](HERMES_SKILL_INSTALL.md) for the one-sentence
prompt and complete boundary.

## 2. Bind an existing node

Prefer an explicit home on every command:

```text
anet --home <ABSOLUTE_PRIVATE_NODE_HOME> doctor
anet --home <ABSOLUTE_PRIVATE_NODE_HOME> status
anet --home <ABSOLUTE_PRIVATE_NODE_HOME> peer-list
```

`doctor` must succeed before network or MCP use. A label, directory name, IP
address, or service name is not identity; compare complete Node IDs.

## 3. Create a node only when authorized

For a genuinely new persistent runtime:

```text
anet --home <NEW_EMPTY_PRIVATE_HOME> init \
  --label <OPERATOR_CHOSEN_LABEL> \
  --host 127.0.0.1 \
  --port <UNUSED_PORT>
```

Loopback is same-host only. Do not advertise it to a physical peer. WSL and
Windows remain distinct nodes even if mirrored networking gives them the same
IP; use distinct ports and Node IDs.

After initialization:

```text
anet --home <NEW_HOME> doctor
anet --home <NEW_HOME> card --out <PUBLIC_CARD_FILE>
```

The exported Card is public signed material. The node home remains private.

## 4. Establish trust explicitly

For asynchronous pairing, use the challenge-bound flow:

```text
anet --home <HOME_A> pair-offer --out <OFFER_FILE> --ttl 3600
anet --home <HOME_B> pair-accept <OFFER_FILE> --out <RESPONSE_FILE>
anet --home <HOME_A> pair-complete <OFFER_FILE> <RESPONSE_FILE>
```

`pair-accept`, `pair-complete`, `peer-add`, and `peer-revoke` change trust.
They require explicit operator policy; receiving a Card or task is not consent
to trust it.

For a human-style camera or image exchange, use the signed QR friend flow:

```text
anet --home <A_HOME> friend-qr --out <A_INVITE.png> --ttl 600
anet --home <B_HOME> friend-scan <A_INVITE.png> --out <B_RESPONSE.png>
anet --home <A_HOME> friend-scan <B_RESPONSE.png>
anet --home <A_HOME> relation-list
anet --home <A_HOME> relation-list --model
```

`friend-scan` remains an explicit trust-changing action. The code is signed,
time-bounded, and challenge-bound; it contains no private key. A successful
exchange adds each verified Node Actor to the local `friend` circle, but does
not prove the concrete Subject behind that Actor or grant tool/task/file
capabilities. See [`QR_FRIENDS.md`](QR_FRIENDS.md).

The full model keeps Actor facts, Subject hypotheses, relationship estimates,
contextual trust, relationship events, and content-free interaction evidence
separate. Once a trusted peer exchanges an application Packet, the runtime
idempotently records only its Packet reference, direction, coarse
message/task/skill/artifact facets, outcome, and time. It does not copy the
payload. A first verified interaction may move `public` to `known`; traffic
volume never creates `collab`, `friend`, `close`, or `family`, never changes a
contextual trust estimate, and never grants a capability. `relation-link`,
`relation-circle`, and `relation-trust` update only this observer's local social
model. They never grant Anet trust or execution capability. Do not place raw
private content in their `--evidence` references.

Actors are typed action sources, not presumed people or Agents. Node Actors use
their complete `an1...` ID and cryptographic proof. Platform Adapters may
create opaque account, device, or session Actor IDs with a scoped
`platform-observed`, `bridge-attested`, or operator proof. A bridge-attested
Actor never inherits the bridge Node's relationship, trust, or capabilities.
For a stopped Discord-enabled runtime, replay any already durable source events
after relationship-state repair with:

```text
anet --home <HOME> discord-social-project --limit 1000
```

This command is idempotent and projects only pseudonymous, content-free
metadata. It does not create PeerBook trust or authorization.

When Actor-to-Subject explanations change, preserve lineage instead of editing
or deleting a `subj_` record:

```text
anet --home <HOME> subject-supersede <SUBJECT> \
  --confidence 80 --evidence "claim:revision"
anet --home <HOME> subject-merge <SUBJECT_A> <SUBJECT_B> \
  --confidence 75 --evidence "claim:same-subject"
anet --home <HOME> subject-split <SUBJECT> \
  --group <ACTOR_A> --group <ACTOR_B> \
  --confidence 75 --evidence "claim:separate-controllers"
```

Merge and split replacements start as `known`. Use `--inherit <SUBJECT>` or
`--inherit-group <N>` only when evidence justifies transferring one existing
relationship to exactly one replacement. Never copy one relationship or
contextual trust estimate to multiple replacements.

For a relationship-only statement jointly signed by two Actors:

```text
anet --home <A_HOME> relation-propose <B_NODE_ID> friend \
  --label research-partner --out <PROPOSAL.json>
anet --home <B_HOME> relation-accept <PROPOSAL.json> --out <CLAIM.json>
anet --home <A_HOME> relation-import <CLAIM.json>
anet --home <A_HOME> relation-claim-list
```

This flow exchanges no Subject references and does not edit PeerBook trust.
Labels are public to recipients of the claim. Acceptance projects only social
circle evidence; it never grants contextual trust or capabilities. See
[`RELATIONSHIP_CLAIMS_V1.md`](RELATIONSHIP_CLAIMS_V1.md).

## 5. Run and communicate

The long-running network process is separate from one-shot CLI and MCP calls:

```text
anet --home <HOME> serve
```

Queue a structured message:

```text
anet --home <HOME> send <COMPLETE_PINNED_NODE_ID> \
  --kind agent.message \
  --json-body '{"text":"hello"}' \
  --qos normal
```

Synchronize and verify delivery:

```text
anet --home <HOME> sync
anet --home <HOME> probe <COMPLETE_PINNED_NODE_ID> \
  --timeout 15 \
  --qos control
```

A successful queue operation is local durability, not remote delivery.
`probe` or a recipient acknowledgement supplies end-to-end evidence.

## 6. Consume durable work

For unattended Agents, prefer leased consumers over raw inbox reads:

```text
anet --home <HOME> consumer-open <GROUP> \
  --start latest \
  --kind-prefix agent.task. \
  --trusted-only

anet --home <HOME> consumer-claim <GROUP> \
  --owner <STABLE_LOCAL_OWNER> \
  --limit 1 \
  --lease-seconds 300
```

ACK only after durable completion:

```text
anet --home <HOME> consumer-settle <GROUP> <CLAIM_TOKEN> \
  --owner <STABLE_LOCAL_OWNER> \
  --action ack
```

On retryable failure:

```text
anet --home <HOME> consumer-settle <GROUP> <CLAIM_TOKEN> \
  --owner <STABLE_LOCAL_OWNER> \
  --action nack \
  --retry-seconds 30 \
  --error <BOUNDED_NON_SECRET_REASON>
```

Authenticated provenance does not authorize shell commands, file changes, API
calls, or other side effects. Validate every payload under local policy.

## 7. Machine-readable behavior

Normal CLI results are JSON. Agents should use exit status plus parsed fields,
not human-oriented string matching. Keep stdout available for the result and
send diagnostics to the calling runtime’s log. Never print node private files,
claim tokens, bot tokens, approval execution tokens, or environment secrets.

Use `anet <command> --help` as the installed-version parameter reference. This
guide defines the workflow and safety boundary; the CLI help is authoritative
for exact flags.

## Recovery rule

If `doctor` fails, a home is missing, a Node ID changes, or a protected file
appears copied, stop. Do not run `init` as a repair operation. Follow
`openwiki/operations/onboarding-and-recovery.md` and restore only from the
deployment owner’s verified backup.
