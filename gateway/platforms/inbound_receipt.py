"""Process-local consumption receipts for ACK-gated relay input.

Admission into a task or pending slot is RAM ownership. Only successful handler consumption
may settle the connector buffer; cancellation before consumption leaves it replayable.
"""

from gateway.platforms.event import MessageEvent


def begin_inbound(event: MessageEvent) -> object:
    event._inbound_deferred = False
    event._inbound_owner = object()
    for receipt in getattr(event, "_inbound_receipts", ()):
        receipt._inbound_owner = event._inbound_owner
    return event._inbound_owner


def defer_inbound(event: MessageEvent) -> None:
    event._inbound_deferred = True
    for receipt in getattr(event, "_inbound_receipts", ()):
        receipt._inbound_owner = None


def finish_inbound(event: MessageEvent, *, consumed: bool, expected_owner=None) -> None:
    if getattr(event, "_inbound_deferred", False):
        return
    owner = expected_owner if expected_owner is not None else getattr(event, "_inbound_owner", None)
    for receipt in getattr(event, "_inbound_receipts", ()):
        if (not receipt.done()
                and getattr(receipt, "_inbound_owner", None) is owner):
            receipt.set_result(consumed)


def discard_inbound(event: MessageEvent) -> None:
    for receipt in getattr(event, "_inbound_receipts", ()):
        if not receipt.done():
            receipt.set_result(False)


def merge_inbound_receipts(target: MessageEvent, incoming: MessageEvent) -> None:
    if not getattr(incoming, "_inbound_receipts", None):
        return
    if not hasattr(target, "_inbound_receipts"):
        target._inbound_receipts = []
    for receipt in incoming._inbound_receipts:
        if receipt not in target._inbound_receipts:
            target._inbound_receipts.append(receipt)
            receipt._inbound_owner = (
                None if getattr(target, "_inbound_deferred", False) else getattr(target, "_inbound_owner", None)
            )
