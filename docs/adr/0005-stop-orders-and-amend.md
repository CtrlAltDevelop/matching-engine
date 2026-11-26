# 5. Stop orders trigger on last trade price; amend keeps priority only when shrinking

- Status: accepted
- Date: 2026-11-26

## Context

Stop orders need a trigger price, and the engine must stay deterministic: the
same WAL has to rebuild the same book, including which parked stops fired.
The engine has no clock and no external price feed, so the only price it can
trigger on is one it produced itself.

## Decision

- **Trigger:** the last trade price. A buy stop fires at `last >= stop`, a
  sell stop at `last <= stop`. Nothing fires before the first trade.
- **Parking:** a stop (`NewOrder.stop_price != 0`) is held off the book in
  arrival order. It is cancellable but not visible in depth or L3.
- **Firing:** after each command, every stop the last price has reached is
  submitted in arrival order as an ordinary limit or market order, with an
  `OrderTriggered` event instead of a second `OrderAccepted`. Fired stops can
  trade and fire further stops; the loop ends because each pass removes one.
  Everything happens under the causing command's `seq`/`ts`.
- **A stop already through the market** fires immediately after being parked.
- **No post-only stops:** a stop is always a taker when it fires.
- **WAL:** a stop order is record kind 4 (the kind-1 layout plus `stop_price`);
  kind 1 is unchanged, so existing logs still replay. Replace is kind 3.
  Snapshots gain `last_trade_price` and `stops`, both defaulted, so older
  snapshots still load.
- **Amend (`ReplaceOrder`):** same price and a smaller or equal size keeps queue
  position; any other change re-queues at the back and matches if it crosses.
  The amended order is a plain GTC limit with the default self-trade policy.

## Consequences

- The trigger scan is O(parked stops) per command that has stops parked
  (`ponytail:` note in `engine.py`); index by stop price if that grows.
- Triggering on the last trade means one trade can sweep many stops in a
  cascade, which is expected stop behaviour but worth knowing.
- A mark-price or best-bid/ask trigger would need a price input in the WAL.
