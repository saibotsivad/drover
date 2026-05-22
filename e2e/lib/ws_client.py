#!/usr/bin/env python3
"""Minimal WebSocket client for the e2e bash tests.

Wraps the ``websockets`` library to provide a small CLI:

  python3 ws_client.py URL
      [-H "Header: Value"]...
      [--max-messages N]   # default unlimited; 0 = handshake only
      [--timeout SECONDS]  # default 10s total budget

Output contract (consumed by ``e2e/lib/ws.sh``):

* stdout: one JSON message per line (server -> client text frames).
* stderr:
    HANDSHAKE_STATUS: <code>   always emitted (101 on success,
                               otherwise the HTTP status from the
                               upgrade response, 0 on connect failure).
    CLOSE_CODE: <code>         emitted if the server sent a close frame.
* exit status: always 0; the bash helpers read HANDSHAKE_STATUS to decide
  whether the connection succeeded.

Reading stops at the first of: max-messages reached, --timeout elapsed,
server-initiated close.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import warnings

# ``websockets`` < 16 raises ``InvalidStatusCode``; >= 13 raises
# ``InvalidStatus``.  Importing both lets the client work against either,
# and the catch_warnings block hides the deprecation noise the older
# alias prints on import.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import websockets
    from websockets.exceptions import (
        ConnectionClosed,
        InvalidHandshake,
        InvalidStatus,
    )
    try:
        from websockets.exceptions import InvalidStatusCode  # legacy name
    except ImportError:  # websockets >= some-future-major dropped it
        InvalidStatusCode = ()  # type: ignore[assignment,misc]


def _parse_headers(values: list[str]) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    for raw in values:
        if ":" not in raw:
            raise SystemExit(f"invalid header (missing ':'): {raw!r}")
        name, _, value = raw.partition(":")
        headers.append((name.strip(), value.strip()))
    return headers


def _emit_status(code: int) -> None:
    print(f"HANDSHAKE_STATUS: {code}", file=sys.stderr, flush=True)


def _handshake_status_from_exc(exc: BaseException) -> int | None:
    """Pull the HTTP upgrade response status out of a handshake failure.

    Newer ``websockets`` releases raise ``InvalidStatus`` carrying the
    full response object; older ones raise ``InvalidStatusCode`` with a
    bare ``status_code`` attribute.  Both are flattened to the int status.
    """
    if isinstance(exc, InvalidStatus):
        resp = getattr(exc, "response", None)
        if resp is not None and getattr(resp, "status_code", None) is not None:
            return int(resp.status_code)
    if InvalidStatusCode and isinstance(exc, InvalidStatusCode):
        return int(getattr(exc, "status_code", 0))
    return None


async def _run(args: argparse.Namespace) -> None:
    extra_headers = _parse_headers(args.header)

    # ``websockets`` 13+ uses ``additional_headers``; older releases use
    # ``extra_headers``.  Pass whichever the runtime accepts.
    connect_kwargs: dict = {"open_timeout": args.timeout}
    try:
        import inspect

        sig = inspect.signature(websockets.connect)
        if "additional_headers" in sig.parameters:
            connect_kwargs["additional_headers"] = extra_headers
        else:
            connect_kwargs["extra_headers"] = extra_headers
    except Exception:
        connect_kwargs["extra_headers"] = extra_headers

    handshake_excs: tuple[type[BaseException], ...] = (InvalidStatus, InvalidHandshake)
    if InvalidStatusCode:
        handshake_excs = (*handshake_excs, InvalidStatusCode)
    try:
        ws = await websockets.connect(args.url, **connect_kwargs)
    except handshake_excs as exc:
        status = _handshake_status_from_exc(exc)
        if status is None:
            _emit_status(0)
            print(f"ERROR: handshake failed: {exc}", file=sys.stderr)
        else:
            _emit_status(status)
        return
    except (OSError, asyncio.TimeoutError) as exc:
        _emit_status(0)
        print(f"ERROR: connect failed: {exc}", file=sys.stderr)
        return

    _emit_status(101)

    if args.max_messages == 0:
        await ws.close()
        return

    read_remaining = args.max_messages if args.max_messages is not None else -1
    deadline = asyncio.get_event_loop().time() + args.timeout

    try:
        while read_remaining != 0:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if isinstance(msg, bytes):
                # We only expect JSON text frames; treat binary as opaque.
                print(msg.hex(), flush=True)
            else:
                print(msg, flush=True)
            if read_remaining > 0:
                read_remaining -= 1
    except ConnectionClosed as exc:
        print(f"CLOSE_CODE: {exc.code}", file=sys.stderr, flush=True)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        help="Extra HTTP header on the upgrade request (Name: Value)",
    )
    p.add_argument(
        "--max-messages",
        type=int,
        default=None,
        help="Stop after N messages (0 = handshake only, omit = unlimited)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Total time budget in seconds (default 10)",
    )
    args = p.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
