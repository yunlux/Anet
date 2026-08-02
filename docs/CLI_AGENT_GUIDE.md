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

When the operator explicitly requests a self-starting Windows node and has
provided a remote control page, use the separate deployment prototype:

```powershell
.\scripts\install_windows_oneclick.ps1 `
  -ControlUrl https://example.invalid/anet/control.json
```

This path creates or reuses one node home after its preflight, imports the
page's default configuration and Peer Cards, registers the current-user `Anet\Supervisor`
scheduled task, and starts the supervisor with an `anet serve` child. For a
machine-wide Windows deployment, run PowerShell as administrator and add
`-Admin`; this uses `%ProgramData%\Anet`, the `SYSTEM` task principal, and an
`AtStartup` trigger. Use explicit `-Port`, `-LocatorContext`, and `-Advertise`
values when this Windows node must coexist with a WSL node. It is not the clean runtime install above. The prototype
page is unsigned and can install a wheel or Git source; see
[`WINDOWS_AUTOSTART.md`](WINDOWS_AUTOSTART.md) before using it.

For direct Windows/WSL connectivity, port numbers only isolate listeners. Bind
both nodes to a non-loopback shared host address (or `0.0.0.0` plus an explicit
advertised address), use different ports, and never publish `127.0.0.1` under a
shared `host:` locator context.

The equivalent explicit POSIX deployment paths are:

```bash
python3 scripts/install_wsl_oneclick.py --control-url <CONTROL_URL>
python3 scripts/install_linux_oneclick.py --control-url <CONTROL_URL>
python3 scripts/install_macos_oneclick.py --control-url <CONTROL_URL>
```

The first two create and enable `anet-supervisor.service` as a systemd user
unit; macOS creates and loads `net.anet.supervisor` as a LaunchAgent. These
paths also create one new node home and are separate from the runtime-only
installers. See [`POSIX_AUTOSTART.md`](POSIX_AUTOSTART.md).
The macOS entry point verifies the LaunchAgent reports `state = running`, and
one-shot `anet control-sync` shares the node-home lock with the supervisor.

For WSL on Windows, `systemd --user` does not itself launch the distribution
after a Windows reboot. After the WSL node is installed, the optional host
bridge can be registered with
`scripts/register_wsl_keepalive.ps1 -Distribution <DISTRO> -LinuxUser <USER>`.

For Android inside Termux, use the separate Termux entry point:

```bash
python3 scripts/install_termux_oneclick.py --control-url <CONTROL_URL>
```

It uses Termux-native packages, `termux-services`/runit, and a
`~/.termux/boot` script. The Termux:Boot add-on is an explicit external
prerequisite; it cannot be installed by the Python runtime installer. See
[`TERMUX_AUTOSTART.md`](TERMUX_AUTOSTART.md).

### WSL

```bash
python3 scripts/install_wsl.py \
  --version 0.12.1 \
  --wheel dist/anet_fabric-0.12.1-py3-none-any.whl \
  --wheel-sha256 6AC09D43E470E9E3A88C8AACCFE47F3971CF78785103012C6FC645A2461CBCD7 \
  --feature mcp
```

The selected CLI is `~/.local/anet/current/venv/bin/anet`.

The platform install entry points above emit an `Anet install preflight` report on stderr
before changing local state. Runtime-only installers use it to report an
existing runtime/Ahub location and retain their versioned-runtime semantics;
the one-click deployment installers also inspect their platform service/task
and process markers and stop before mutation when another same-platform
persistent deployment is found. The target deployment is idempotently reused.
The installer also acquires a target-scoped lock before preflight, so concurrent
invocations cannot both pass the report and create the same runtime or service.
Use `-AllowExisting` on Windows or `--allow-existing` on POSIX/Termux only as
an explicit override. Windows and WSL are separate preflight boundaries and
must not share a node home or identity.

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
anet --home <ABSOLUTE_PRIVATE_NODE_HOME> peer-reachability <PEER_NODE_ID>
```

`doctor` must succeed before network or MCP use. A label, directory name, IP
address, or service name is not identity; compare complete Node IDs.

`peer-reachability` queries the configured Ahub carriers for the signed,
short-lived reachability overlay of an already pinned peer. It verifies the
descriptor keys against the PeerBook entry, returns dynamic candidates before
the static Card fallback, and never edits `peers.json`. A running node performs
the same validation during Ahub synchronization; its direct sync, health probe,
and dialer probe can then use the dynamic candidates until they expire.

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
For a person or external Agent without an Anet Node, an Agent may explicitly
create one local, opaque `act_<namespace>_<32-hex>` observation instead of
inventing a global identity:

```text
anet --home <HOME> relation-observe-actor act_local_<32-hex> \
  --kind human.local --confidence 35 \
  --evidence "operator:relationship-bootstrap"
```

This is a local `operator-attested` fact, not proof that the source is human,
AI, a particular person, or a particular controller. Never place raw account
identifiers, names, addresses, or message content in the opaque ID, label, or
evidence reference. It creates a `public` Subject hypothesis only; it cannot
pin a peer, set a circle, grant trust, or authorize any action. Use a signed
Peer Card rather than this command for an `an1...` Node.

To stop treating one such local external source as active, require an exact
confirmation and retain its Subject/relationship history for explicit review:

```text
anet --home <HOME> relation-actor-revoke act_local_<32-hex> \
  --confirm act_local_<32-hex> --reason "operator:source-retired"
```

This only changes the local Actor state to `revoked`; it does not remove a
Subject, change a circle or trust estimate, modify PeerBook, or grant/revoke
authorization. Do not use it for an `an1...` Node; use `peer-revoke` there.

To end the observer's relationship with a local Subject while retaining the
Subject hypothesis, linked Actors, evidence history, contextual trust, and
claims for later review, use an exact confirmation:

```text
anet --home <HOME> relation-end <SUBJECT_REF> --confirm <SUBJECT_REF> \
  --reason "operator:relationship-ended"
```

The command is idempotent and changes no PeerBook or authorization state. It
sets only the local relationship state to `ended`; a later explicit
`relation-circle` reopens it as `active`.

For an inactive relationship that should remain available for later local
review, use the same confirmation pattern with `relation-pause` instead:

```text
anet --home <HOME> relation-pause <SUBJECT_REF> --confirm <SUBJECT_REF> \
  --reason "operator:relationship-inactive"
```

This sets only the local relationship state to `dormant`. It retains the
Subject, linked Actors, contextual trust, claims and history, but excludes the
relationship from advisor suggestions and active-circle counts. An explicit
`relation-circle` is required to reactivate it; the command has
`authorization_effect: none`. It cannot be used to turn an `ended`
relationship back into `dormant`; use an explicit `relation-circle` to reopen.

For a stopped Discord-enabled runtime, replay any already durable source events
after relationship-state repair with:

```text
anet --home <HOME> discord-social-project --limit 1000
```

This command is idempotent and projects only pseudonymous, content-free
metadata. It does not create PeerBook trust or authorization.

Before making an observer-local relationship estimate, request the derived
advice instead of inventing a global score:

```text
anet --home <HOME> relation-suggest
anet --home <HOME> relation-suggest --subject <SUBJECT_REF>
```

The command is read-only. Each result includes explicit `accept` and `reject`
decision command arguments plus the transparent `proposed_mutation`. Use the
decision command when the evidence and local policy justify the proposed circle
or narrow trust context; do not bypass its audit history by executing the
mutation directly. The default advisor can suggest only `known -> collab` and
`task.delivery` review. It cannot infer friendship, intimacy, family, Subject
sameness, PeerBook trust, or capability.

Prefer the auditable decision seam over directly executing the suggested
mutation command:

```text
anet --home <HOME> relation-decide <SUGGESTION_ID> accepted \
  --reason "agent:bounded-collaboration-confirmed"
anet --home <HOME> relation-decide <SUGGESTION_ID> rejected \
  --reason "agent:insufficient-social-context"
anet --home <HOME> relation-decision-list --subject <SUBJECT_REF>
```

The runtime recomputes the current suggestions before deciding. If the evidence
basis changed, the old ID is stale and the Agent must review the newly derived
suggestion. Do not retry by guessing an ID. Acceptance atomically applies only
the proposed circle or narrow contextual-trust estimate; rejection changes no
relationship. Both are immutable observer-local history with
`authorization_effect: none`. Use a bounded rationale code or content-free
reference, never raw conversation, task output, filenames, or secrets.

For a human-visible or Agent-consumed replay of the local social model, read
the privacy-bounded activity feed:

```text
anet --home <HOME> relation-activity --limit 100
anet --home <HOME> relation-activity --after <NEXT_CURSOR> --wait 30
```

Always resume from the returned `next_cursor`; do not sort by `occurred_ms` or
invent a cursor. Append order is authoritative for incremental reading, while
`occurred_ms` describes when the source interaction reportedly happened. A
cursor is valid only for the same observer-owned node home. The feed contains
no raw evidence references or payloads and grants no authority. For frequent
polling by a long-lived Agent, prefer the capability-gated
`anet_relation_activity` MCP tool; keep it disabled when the Agent does not
need private relationship visibility.

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

Either participant can sign a withdrawal for one stored mutual claim. Importing
that withdrawal only marks the portable claim inactive and records a local
activity fact; it does not automatically alter either observer's Subject,
circle, contextual trust, PeerBook trust, capability, or authorization:

```text
anet --home <B_HOME> relation-claim-withdraw <MREL_CLAIM_ID> --out <WITHDRAWAL.json>
anet --home <A_HOME> relation-claim-withdraw-import <WITHDRAWAL.json>
```

To show a selected content-free part of this node's observer-local social view
to one pinned peer:

```text
anet --home <A_HOME> relation-disclose <B_NODE_ID> --limit 100
anet --home <A_HOME> relation-disclose <B_NODE_ID> \
  --after <RAC_CURSOR>
anet --home <B_HOME> relation-disclosure-list --sender <A_NODE_ID>
anet --home <B_HOME> relation-reported-view <A_NODE_ID>
```

Use `--subject <SUBJECT_REF>` only when the operator intends to expose that
stable local hypothesis reference to the encrypted audience. A received
disclosure remains the sender's reported worldview and is never imported into
the receiver's own relationship model or authorization. See
[`RELATIONSHIP_DISCLOSURES_V1.md`](RELATIONSHIP_DISCLOSURES_V1.md).

Prefer `relation-reported-view` when an Agent or UI needs a compact remote
circle view instead of raw disclosure envelopes. The result is explicitly
`sender-reported` and `partial-unknown`; provenance and missing-coverage
warnings must remain visible. Use `--subject` to select one remote hypothesis
and `--include-activities` only when source-event inspection is needed.

For one human-visible local file that combines this node's model with at most
one separately attributed sender report, use:

```text
anet --home <B_HOME> relation-dashboard > relation-dashboard.json
anet --home <B_HOME> relation-dashboard --reported <A_NODE_ID> \
  > relation-dashboard.json
```

The dashboard file is a local convenience envelope: `local_model` remains B's
private model and `reported_view` remains A's sender-reported view. `/social`
can import it in one step, but neither view is uploaded, merged, or granted
authorization.

When scheduled v2 disclosures expose more than one `rdsr_` series, use
`--series <RDSR_ID>` before treating a segment as ordered. Only
`proven-continuous-segment` establishes cursor continuity; `gap-detected`
requires waiting for or reporting the missing disclosure. To report a visible
gap without requesting or widening disclosure:

```text
anet --home <B_HOME> relation-disclosure-gap-notice <A_NODE_ID> \
  --series <RDSR_ID>
```

On A, inspect the authenticated advisory and retransmit only if the original
schedule remains active:

```text
anet --home <A_HOME> relation-disclosure-gap-notice-list
anet --home <A_HOME> relation-disclosure-gap-retransmit <RGAP_ID>
```

The retransmit command sends the exact archived page to the original audience.
It cannot reconstruct history, expand scope, revive a revoked schedule, or
advance the series.

For continuous future disclosure, create an observer-local schedule. It starts
at the current cursor unless historical replay is explicitly requested:

```text
anet --home <A_HOME> relation-disclosure-schedule-add <B_NODE_ID> \
  --all --interval 300 --lifetime 2592000
anet --home <A_HOME> relation-disclosure-schedule-list
anet --home <A_HOME> relation-disclosure-schedule-run
```

Use `--subject <SUBJECT_REF>` instead of `--all` for one local hypothesis.
Revoke with the complete ID and exact confirmation:

```text
anet --home <A_HOME> relation-disclosure-schedule-revoke <RDSC_ID> \
  --confirm <RDSC_ID> --reason observer-stopped
```

The audience cannot pull, widen, or renew this instruction. Schedule
configuration stays in the CLI control plane; it is intentionally not exposed
as an MCP mutation tool.

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
