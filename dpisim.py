#!/usr/bin/env python3
"""dpisim — a naive SNI-inspecting middlebox, for reproducing COLLATERAL DAMAGE.

Read this before using it, because it does not do the obvious thing.

WHAT IT IS FOR
  Showing which strategies break connections that were working fine. It is a
  proxy with no real TCP/TLS stack — exactly the middlebox class the ByeDPI
  README warns that `oob` and `tlsrec` will confuse. Point dpifuzz at it and
  `-o1+s` / `-d1+s` will reset hosts that are not on the blocklist, while the
  same methods gated behind `-At,s` leave them alone. That is a real and
  useful demonstration.

WHAT IT IS NOT FOR
  Testing bypass. It models first-segment SNI inspection, which is the weakness
  real DPI has, but over loopback the model does not hold: TCP coalesces
  byedpi's separate write() calls, so both arrive in one read() and the SNI is
  still there. Measured: 1 of 21 connections had no SNI in the first segment.
  Segmentation-based evasion cannot be demonstrated over `lo`, because the
  segment boundaries the technique depends on do not survive.

  If you want to test bypass, you need a real network path with a real
  middlebox. There is no shortcut.

    ./dpisim.py --block en.wikipedia.org --block pypi.org -v

Connections are forwarded to whatever host the SNI names. Inspection and
routing are deliberately separate: the inspector gets one read, the router
keeps reading until it can recover a destination — because a real middlebox
always knows where the packet is going, whether or not it could parse it.
"""
from __future__ import annotations
import argparse, asyncio, re, ssl, struct, sys

STATS = {"seen": 0, "blocked": 0, "passed": 0, "no_sni": 0}


def parse_sni(buf: bytes) -> str | None:
    """Minimal ClientHello SNI extraction. Returns None if it isn't there —
    which is the whole point: a split ClientHello has no SNI in segment one."""
    try:
        if len(buf) < 45 or buf[0] != 0x16:
            return None
        # record header 5, handshake header 4, version 2, random 32
        p = 5 + 4 + 2 + 32
        p += 1 + buf[p]                                   # session id
        p += 2 + struct.unpack(">H", buf[p:p + 2])[0]     # cipher suites
        p += 1 + buf[p]                                   # compression
        if p + 2 > len(buf):
            return None
        p += 2                                            # extensions length
        while p + 4 <= len(buf):
            etype, elen = struct.unpack(">HH", buf[p:p + 4])
            p += 4
            if etype == 0x0000:                            # server_name
                q = p + 5
                nlen = struct.unpack(">H", buf[p + 3:p + 5])[0]
                return buf[q:q + nlen].decode("ascii", "replace")
            p += elen
    except Exception:
        return None
    return None


async def pipe(r, w):
    try:
        while (b := await r.read(65536)):
            w.write(b); await w.drain()
    except Exception:
        pass
    finally:
        try: w.close()
        except Exception: pass


async def handle(cr, cw, blocked, deadline, verbose):
    STATS["seen"] += 1
    peer_reset = False
    try:
        # ONE read, with a short deadline. This is the naive-DPI behaviour: it
        # does not wait around reassembling a stream, it inspects and decides.
        try:
            first = await asyncio.wait_for(cr.read(65536), deadline)
        except asyncio.TimeoutError:
            first = b""
        if not first:
            cw.close(); return

        sni = parse_sni(first)
        verdict_sni = sni          # what the INSPECTOR saw in segment one

        if verdict_sni is None:
            STATS["no_sni"] += 1
            if verbose: print(f"  pass   (no SNI in first segment, {len(first)}B)")
        elif any(verdict_sni == b or verdict_sni.endswith("." + b) for b in blocked):
            STATS["blocked"] += 1
            if verbose: print(f"  RESET  {verdict_sni}")
            try:
                import socket as _s
                cw.get_extra_info("socket").setsockopt(
                    _s.SOL_SOCKET, _s.SO_LINGER, struct.pack("ii", 1, 0))
            except Exception:
                pass
            peer_reset = True
            cw.close(); return
        else:
            if verbose: print(f"  pass   {verdict_sni}")

        # Routing is a SEPARATE concern from inspection. A real middlebox is a
        # router: it always knows the destination, whether or not it could parse
        # the payload. So keep reading until the SNI is recoverable — the
        # inspector already had its single look and has been overtaken.
        buf = first
        route_sni = sni
        while route_sni is None and len(buf) < 16384:
            try:
                more = await asyncio.wait_for(cr.read(65536), 1.0)
            except asyncio.TimeoutError:
                break
            if not more:
                break
            buf += more
            route_sni = parse_sni(buf)

        upstream = route_sni
        if upstream is None:
            if verbose: print(f"  drop   (destination unrecoverable, {len(buf)}B)")
            cw.close(); return
        try:
            sr, sw = await asyncio.wait_for(
                asyncio.open_connection(upstream, 443), 8)
        except Exception:
            cw.close(); return
        STATS["passed"] += 1
        sw.write(buf); await sw.drain()
        await asyncio.gather(pipe(sr, cw), pipe(cr, sw))
    except Exception:
        pass
    finally:
        if not peer_reset:
            try: cw.close()
            except Exception: pass


async def amain(a):
    blocked = [b.lower() for b in a.block]
    srv = await asyncio.start_server(
        lambda r, w: handle(r, w, blocked, a.deadline, a.verbose),
        a.ip, a.port)
    print(f"dpisim on {a.ip}:{a.port}   blocking: {', '.join(blocked) or '(nothing)'}")
    print(f"first-read deadline {a.deadline}s — a ClientHello arriving in pieces "
          f"gets through\n")
    try:
        async with srv:
            await srv.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        print(f"\nseen {STATS['seen']}  blocked {STATS['blocked']}  "
              f"passed {STATS['passed']}  no-SNI-in-first-segment {STATS['no_sni']}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ip", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8443)
    p.add_argument("--block", action="append", default=[],
                   help="domain to reset on; repeatable")
    p.add_argument("--deadline", type=float, default=0.05,
                   help="how long it waits for the first segment (default 0.05s)")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    try: asyncio.run(amain(a))
    except KeyboardInterrupt: pass


if __name__ == "__main__":
    main()
