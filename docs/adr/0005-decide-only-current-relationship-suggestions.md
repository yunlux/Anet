# Decide only current relationship suggestions

An observer may accept or reject a relationship suggestion only while the
advisor can reproduce its exact deterministic ID from current bounded
evidence. The decision and any accepted relationship change are persisted
atomically because allowing stale-basis decisions or writing audit history
after mutation would make the local worldview impossible to explain reliably;
new evidence instead produces a new suggestion that requires a new decision.
