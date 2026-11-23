# Exchange architecture and limits

## Order entry and ownership

An Optiq session is identified by the Logical Access ID and OE Partition ID
in Logon. The access has an allowlist of executing firms. A firm owns the
orders it enters and may cancel or amend via any session on which it is
authorised. Each order retains its original session for disconnect handling.
An exchange Order ID is unique during the run and is stable across a
successful amendment. Cancel and amend look it up by instrument, side and
firm. When both IDs are present the exchange Order ID takes precedence.
Requests addressed only by Original Client Order ID are rejected.

Each resting order records its instrument, side, maker price, remaining
quantity, executed quantity, priority, firm and original session. A limit
order executes at the best eligible resting price, oldest first at that
price. It may consume multiple makers and leave a resting remainder.
An amendment supplies the new total quantity including earlier executions.
A reduction in total quantity alone preserves priority. A price change or
size increase gets fresh priority and executes immediately when crossing.
The new total must exceed executed quantity. Rejected changes leave the
original live order intact. Side and order type are fixed.

## TCP contract

The supported Optiq envelope starts with five little-endian 16-bit values
in order: inclusive frame length, fixed block length, template ID, schema
ID, and version. The Optiq subset uses schema 0 and version 354.
The gateway can assemble fragmented frames and process coalesced frames.
A frame with invalid length, layout, or an unsupported template closes its
connection. Supported layouts include NewOrder (01), Ack (03), Fill (04),
Kill (05), CancelReplace (06), Reject (07), CancelRequest (12) and Logon
(100 through 102). Optional groups carry their v354 headers and are empty
in the supported workflow. The selected segment is continuous lit limit
trading. Use instrument IDs configured for this run and prices in integer
ticks. [Optiq OEG SBE specification](https://connect.euronext.com/sites/default/files/it-documentation/Optiq%20OEG%20SBE%20Messages%20-%20Interface%20Specification%20-%20Euronext%20Cash%20and%20Derivatives%20Markets%20-%20External%20-%20v5.354.0.pdf)

Every framed request receives one Ack, Kill or Reject carrying its
Client Order ID. Ack Type identifies entry or replace. It includes the
exchange ID, the submitted total quantity, symbol, side, limit price,
priority and the executed-on-entry flags. Fill (04) identifies each side's
order and execution, maker price, execution quantity and leaves after that
fill. Private fills and disconnect Kill notices carry the signed numeric
null Client Order ID. Kill reason 1 identifies requested cancellation and
11 identifies automatic cancellation on disconnect. Sequence numbers describe
the private stream, including unsolicited messages. The request response
precedes each fill produced by that command for its initiator.
Book IN and Trade Time are UTC epoch times in nanoseconds. Trade Time is
recorded once per matching command and shared by both sides of its fills.
Unmeasured optional gateway transport timings use their numeric nulls.

Reject Error Code is local to this exchange. Values 1 through 7 indicate
invalid or unsupported request, unauthorised firm, unknown instrument,
unknown or inaccessible exchange ID, incompatible or invalid amendment,
capacity limit, and invalid inbound sequence, respectively. Reject carries
the request template and its supplied Order ID field. Transport errors may
close the stream without a reply.

## Sequencing, publication and memory

BlueZone owns the order-entry sockets and hands complete SBE frames to
the matcher through bounded queues. The matcher decodes up to four frames
per connection and 32 frames per callback, rotating which connection
it serves first. A detected close is sequenced behind commands already
admitted to the global FIFO. With no earlier work or backpressure, the
matcher decodes the frame, applies the order, and encodes the complete Ack
in one Simplx iteration. BlueZone handles partial TCP writes and frees
each reply slot after sending its final byte.

The matcher plans exact book and output changes before applying them. It
checks live order, price and sweep limits, notice reservations, complete
private reply space, and publication credits. A temporary shortage at the
head pauses matching while output drains. Later requests remain queued.
Intake pauses if the command queue fills. Private writes with no progress for
30 seconds close the connection. Losing the market subscriber after it
becomes ready, or blocking publication for 30 seconds, invalidates the
market view and ends the run.

The matching worker sends immutable update batches through Simplx Events.
The RedZone publisher assigns consecutive market sequence numbers, encodes
the [demo SBE market-data schema](../schema/market-data.xml), and publishes
the encoded slots to the BlueZone socket actor. Each update contains the command number, action,
instrument, order ID, side, price, remaining quantity, and priority.
The subscriber connects before the first order and receives its ready
message before building the book from an empty state. A missing market
sequence invalidates that view.

The Actor allocator holds book records and price and ID indexes across
requests. Simplx Events carry temporary handoff metadata. Bounded
publication and disconnect arrays hold the records referred to by Events.
The writer can reuse handoff slots when the receiving actor releases them.
A bounded command count is sampled by the ordinary worker. Verbose
diagnostics are available only when the explicitly local Unix control
socket is enabled.

## Disconnect and replay

When a session closes, the matcher cancels its remaining live orders at
the disconnect command's place in the global sequence. Other sessions'
orders and completed executions stand. Each live order reserves future
notice capacity. Full fills and requested cancels release the reservation.
Disconnect transfers the reservation to a compact Kill record. The matcher
assigns its private sequence and hands records to the BlueZone actor in
bounded batches. If the handoff is full, ready records remain in reserved
storage. The BlueZone copy remains until client confirmation.

Every configured session has an independent private sequence and ordinary
reply history. The history keeps the newest messages up to its limit
and records each encoded reply before sending. Logon supplies the largest
private sequence already processed. The exchange sends Logon Ack then
retained messages in private sequence order. A missed ordinary reply that
aged out is reported as an inclusive gap. The client records unknown
outcomes and receives remaining messages, including unconfirmed notices.
BlueZone encodes newly constructed replay frames and sends them alongside
the saved, already encoded ordinary replies. A ReplayComplete marker
precedes processing new requests on that connection.

Compute Trade schema 1 version 1 adds template 200 Confirm with a uint32
frontier, 201 ReplayGap with inclusive first and last uint32 sequences, and
202 ReplayComplete with a uint32 frontier. A client can confirm the largest
accounted-for sequence once it has recorded gaps and processed replay.
Confirmations free retained notice capacity. After an interrupted replay,
reconnect with the largest processed sequence and use the sequence numbers
to identify duplicates. The injector emits a JSON report for incomplete
recovery. An exchange restart starts new session state and private history.

## Configured limits

Set limits in the `limits` object of the exchange JSON. Defaults support 32
configured instruments or session identities and up to 16 authorised firms
per session. Socket slots are bounded by twice the configured session count
plus four. Input storage is 4,096 bytes per connection and the supported
frame envelope is 256 bytes. A full queue applies natural TCP backpressure.

| Configuration | Default | Effect |
| --- | --- | --- |
| `live_orders_total` | 1024 | Global live index and book limit |
| `live_orders_per_session` | 64 | Live orders and maximum disconnect removal work per session |
| `active_prices` | 512 | Active price levels across instruments and sides |
| `matching_max_fills` | 16 | Maximum maker fills per order or amendment |
| `command_queue_capacity` | 128 | Waiting commands in the global FIFO |
| `reply_slots_per_session` | 64 | Unsent private frame slots per connection |
| `ordinary_replay_capacity` | 128 | Newest ordinary messages retained per session |
| `notice_slots` | 2048 | Live reservations plus unconfirmed disconnect notices |
| `notice_handoff_slots` | 4 | In-flight notice batches |
| `notice_batch_size` | 32 | Maximum records per notice batch |
| `publication_capacity` | 1024 | Outstanding immutable market updates |

The maximum single-command publication batch must fit the configured
publisher capacity. Orders beyond a logical order, sweep, reply-per-command,
price or notice limit receive a rejection before book mutation. Retries use
the next inbound sequence and a suitable request identifier. Exhausting an
identifier or hitting an internal invariant stops the run. Operating rates
and sustained backlog evidence are reported by the paced workload runner.
