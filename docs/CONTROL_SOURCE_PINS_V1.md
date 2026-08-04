# Control Source Publisher Pins v1

Anet control pages can compose configuration, software declarations, and Peer
Cards from nested `pages` and `kv` sources. A persistent deployment keeps its
publisher trust roots only in the local `remote-control.json` `trusted_keys`
map. Remote documents cannot add or rotate those keys.

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

`actor-a` and `actor-b` must already exist in the node's local
`trusted_keys`. An unknown or malformed key ID is rejected before fetching the
source. After fetching, the child page must carry a valid `_anet_control`
signature whose `key_id` exactly matches the source entry. A different key is
rejected even when that different key is also locally trusted. Source objects
accept only `url` and `key_id`, so unsupported policy-looking fields cannot be
silently ignored. Relative URLs resolve against the declaring page as before.

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
signed, and its verified key ID. These records contain deployment URLs and are
private evidence. Derived attribution does not change the digest of an existing
legacy string source after an upgrade. An explicit source pin is part of the
composed policy digest, so adding or changing one requires a new signed
sequence.

Publisher pins express operator-selected provenance, not a universal
reputation score, automatic trust expansion, quorum, endorsement, or
authorization grant. Peer Cards from those pages still undergo their own Node
ID/signature validation, and the local operator remains responsible for which
publisher keys may influence the node's remote-control policy.
