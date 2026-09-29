# 3. Integer ticks and lots instead of floats or Decimal

- Status: accepted
- Date: 2026-09-26

## Context

Prices and quantities have to compare exactly (is this bid at or above that
ask?), sum exactly (the level's total must equal the sum of its orders) and
serialize exactly (the WAL must replay to the same book).

- `float` fails all three: `0.1 + 0.2 != 0.3`, and two clients can send what
  they both believe is the same price and land on different levels.
- `Decimal` is exact, but every comparison and addition is a method call on
  an object with a context, several times slower than an `int`, and it has no
  fixed-width binary form to put in a log record.

Every real market already defines a *tick size* (smallest price increment)
and a *lot size* (smallest quantity increment). Every legal price is an
integer number of ticks.

## Decision

- Inside the engine, the WAL and snapshots, a price is an `int` number of
  ticks and a quantity an `int` number of lots. Both are packed as signed
  64-bit integers in the WAL.
- Conversion happens once, at the gateway (`market.py`), using `Decimal`.
  It is strict: a price that is not a whole number of ticks, or a quantity
  that is not a whole number of lots, is refused with 422. It is never
  rounded, because silently moving someone's limit price is worse than
  rejecting the order.
- On the wire, prices and quantities are decimal *strings* in both
  directions, so JSON's binary floating point never touches them.

## Consequences

- The matching loop does only integer comparisons and subtractions, which is
  a large part of why the pure-Python engine is usable at all.
- A market's tick and lot size are part of its identity: changing either
  reinterprets every stored order. A real deployment would version the
  market spec and migrate the book; this project treats them as fixed.
- 64 bits is ample: at a tick of 10⁻⁸ it still covers prices up to about
  9·10¹⁰.
