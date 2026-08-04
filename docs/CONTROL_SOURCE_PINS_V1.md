# Control Source Publisher Pins v1

Anet control pages can compose configuration, software declarations, and Peer
Cards from nested `pages` and `kv` sources. A persistent deployment keeps its
publisher trust roots only in the local `remote-control.json` `trusted_keys`
map. Remote documents cannot add, remove, or rotate those local keys or change
the local `root_key_id`.

The legacy source form remains valid:

```json
{
  "pages": ["https://community.example/anet/page.json"]
}
```

When local `trusted_keys` is non-empty, that child must be signed by one of the
locally trusted keys. To bind a community source to one exact publisher rather
than any trusted publisher, use:

```json
{
  "pages": [
    {
      "url": "https://actor-a.example/anet/page.json",
      "key_id": "actor-a"
    }
  ],
  "kv": [
    {
      "url": "https://actor-b.example/anet/nodes.json",
      "key_id": "actor-b"
    }
  ]
}
```

`actor-a` and `actor-b` must be approved either by the node's local
`trusted_keys` or by a signed root-page delegation described below. An unknown
or malformed key ID is rejected before fetching the source. After fetching,
the child page must carry a valid `_anet_control` signature whose `key_id`
exactly matches the source entry. A different key is rejected even when that
different key is otherwise approved. Source objects accept only `url` and
`key_id`, so unsupported policy-looking fields cannot be silently ignored.
Relative URLs resolve against the declaring page as before.

## Root-scoped publisher delegation

An operator may pin only the deployment root key locally and let that signed
root page curate the public keys for community-maintained child sources:

```json
{
  "control_publishers": {
    "actor-a": "<BASE64URL_ED25519_PUBLIC_KEY>",
    "actor-b": "<BASE64URL_ED25519_PUBLIC_KEY>"
  },
  "pages": [
    {
      "url": "https://actor-a.example/anet/page.json",
      "key_id": "actor-a"
    }
  ],
  "kv": [
    {
      "url": "https://actor-b.example/anet/nodes.json",
      "key_id": "actor-b"
    }
  ]
}
```

The root page must itself be signed by the exact locally pinned `root_key_id`.
Its signature and expiry cover the canonical `control_publishers` map, and a
delegation change at an already applied sequence is rejected. A delegated key
cannot sign the root page, shadow a local key ID, reuse a local public key, or
identify multiple publishers. A nested page cannot delegate again.

Delegation is ephemeral and source-scoped. The public keys are used only while
verifying this fetched root composition, and only a child object that names the
matching `key_id` activates one. They are not written to local `trusted_keys`,
do not appear in the Deployment Receipt's locally enrolled `control.key_ids`,
and do not change PeerBook trust, Node ID authorization, capabilities,
relationship state, or reputation. The root and child pages each retain their
own signature and expiry checks.

For a fresh deployment, enroll all approved publishers in the one-click
command. Windows accepts
`-ControlTrustedKey "actor-a=<KEY>","actor-b=<KEY>"`; WSL, Linux, macOS, and
Termux accept repeated `--control-trusted-key actor-a=<KEY>` options. The
legacy single `ControlKeyId`/`ControlPublicKey` pair remains compatible. The
installer rejects one ID mapped to conflicting keys and one public key mapped
to multiple publisher IDs, then writes the complete local map before running
`control-verify`. The first/legacy publisher is also written as
`root_key_id`. The root page must match that key exactly; additional community
keys have no root authority and become effective only when a nested source
names them.

`control-verify`, `control-sync`, and the persisted private control state expose
`source_publishers` records containing each composed URL, whether it was
signed, and its verified key ID. They also expose `delegated_publisher_ids`
without returning the delegated public keys. These records contain deployment
URLs and are private evidence. Derived attribution does not change the digest
of an existing legacy string source after an upgrade. An explicit source pin
or root delegation is part of the composed policy digest, so adding or changing
one requires a new signed sequence.

Publisher pins express operator-selected provenance, not a universal
reputation score, automatic trust expansion, quorum, endorsement, or
authorization grant. Peer Cards from those pages still undergo their own Node
ID/signature validation, and the local operator remains responsible for which
publisher keys may influence the node's remote-control policy.
