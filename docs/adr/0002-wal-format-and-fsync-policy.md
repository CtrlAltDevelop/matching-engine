# 2. WAL format, group commit and what a crash can lose

- Status: accepted
- Date: 2026-09-26

## Context

The engine is deterministic (ADR 1), so its state is fully defined by the
sequence of commands it has applied. Persisting that sequence is enough to
rebuild the book: this is event sourcing with the *inputs* as the events.
Two questions follow: how is a record laid out so a damaged file is
detected, and when is a record durable enough to acknowledge?

## Decision

### Record format

```
segment = MAGIC(8) record*
record  = length:u32 | crc:u32 | payload[length]
crc     = CRC32(length || payload)
payload = kind:u8 | seq:u64 | ts:i64 | body     (little-endian, fixed width)
```

- **Length framing** lets a reader skip to the next record without parsing
  the body, and detect a record cut off by end-of-file.
- **The CRC covers the length too.** A bit flip in a length field would
  otherwise re-frame the rest of the file into plausible-looking garbage.
- **Fixed-width binary bodies** (`struct`) rather than JSON or MessagePack:
  about 56 bytes per command, no schema ambiguity, trivially readable from
  another language (ADR 4).
- Enum values are stored as positions in append-only tables; codes are never
  reordered or reused.
- Segments are named by their first sequence number. A checkpoint writes a
  snapshot at `seq` and starts a new segment at `seq + 1`, so old history is
  dropped a whole file at a time.

### Torn writes versus corruption

On recovery:

| What the reader finds | Diagnosis | Action |
|---|---|---|
| Record extends past end-of-file | torn write | truncate from that record |
| Checksum fails and nothing but EOF/zeros follows | torn write | truncate from that record |
| Zero-filled bytes where a header should be | torn write (pre-extended file) | truncate |
| Checksum fails and valid-looking data follows | **corruption** | refuse to start |
| A gap or repeat in sequence numbers | **corruption** | refuse to start |
| A torn record in a segment that is not the newest | **corruption** | refuse to start |

A torn tail is what a crash during an append looks like, and the records in
it were never acknowledged (see below), so dropping them is correct. Damage
*before* valid records means acknowledged history has changed; silently
truncating there would throw away real trades. The engine stops and an
operator decides. `matching-engine verify` reports either case without
touching the files.

### Group commit

Each market's worker drains up to `batch_max` queued requests, appends them
all, then issues **one** `fsync`. Only after it returns are the commands
applied to the engine and their callers answered. While that fsync runs,
new requests queue up and form the next batch, so the batch size grows with
load and no timer is needed.

Measured on the development laptop (Windows 11, SATA SSD; two sessions,
see the README for the machine and how noisy it is):

| fsync every | records/s, session 1 | session 2 |
|---|---|---|
| 1 record | 906 | 741 |
| 64 records | 47,818 | 5,430 |
| 512 records | 171,843 | 80,938 |

An fsync costs about a millisecond here, which caps one-at-a-time durability
near a thousand orders a second; batching amortizes it away.

## Consequences

- **With `ME_FSYNC=1` (the default), a crash loses no acknowledged order.**
  It can lose the commands of the batch whose fsync was in flight — at most
  `batch_max` of them — but none of those callers had an answer yet, so from
  the outside they simply timed out. Clients retry, which is why a real
  deployment adds client order ids for idempotency (roadmap).
- The subprocess crash test (`tests/test_crash.py`) kills a writer with
  `TerminateProcess`/`SIGKILL` mid-load and checks exactly this: every
  acknowledged sequence number is recovered and the rebuilt state hash
  matches an in-memory replay.
- `ME_FSYNC=0` leaves flushing to the OS. A *process* crash still loses
  nothing (the data is in the page cache); a *machine* crash can lose
  whatever the OS had not written back, i.e. acknowledged orders. That is
  acceptable for tests, benchmarks and a disposable demo, never for money.
- Latency per order includes one fsync of wait in the worst case. That is
  the price of durability without replication; a replicated log (acknowledge
  once a quorum has the record in memory) is the standard way to lower it
  and is out of scope here.
- On POSIX the directory is fsynced after creating a segment or renaming a
  snapshot, so the file itself survives a crash. Windows cannot open a
  directory for fsync; NTFS journals metadata instead.
