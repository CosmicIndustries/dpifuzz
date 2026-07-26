"""Self-contained: runs dpisim in-process, fuzzes against it, prints results."""
import asyncio, sys
sys.path.insert(0, '/home/radxa/dpiga')
import dpifuzz as D
import dpisim

BLOCK = ["en.wikipedia.org", "duckduckgo.com", "pypi.org"]
PORT = 8443
TESTS = [("passthrough (none)", ""), ("-s1", "-s1"), ("-s1+s", "-s1+s"),
         ("-s2+s", "-s2+s"), ("-s4+s", "-s4+s"), ("-d1", "-d1"),
         ("-d1+s", "-d1+s"), ("-o1+s", "-o1+s"),
         ("-d1 -s1+s -s3+s", "-d1 -s1+s -s3+s"),
         ("-At,s -d1 -s1+s", "-At,s -d1 -s1+s")]


async def via(cfg, host):
    port = D.free_port()
    pr = await D.spawn(cfg, port)
    if pr is None:
        return "spawn-fail"
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 5)
        w.write(b"\x05\x01\x00"); await w.drain(); await r.readexactly(2)
        hb = b"127.0.0.1"
        w.write(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + PORT.to_bytes(2, "big"))
        await w.drain()
        h = await r.readexactly(4)
        if h[1] != 0:
            return "socks-refused"
        ln = {1: 4, 4: 16}.get(h[3]) or (await r.readexactly(1))[0]
        await r.readexactly(ln + 2)
        await asyncio.wait_for(w.start_tls(D.CTX, server_hostname=host), 10)
        w.close()
        return "OK"
    except Exception as e:
        return type(e).__name__
    finally:
        await D.reap(pr)


async def main():
    srv = await asyncio.start_server(
        lambda r, w: dpisim.handle(r, w, BLOCK, 0.05, False), "127.0.0.1", PORT)
    print(f"dpisim on 127.0.0.1:{PORT}, blocking {', '.join(BLOCK)}\n", flush=True)
    lines = []
    async with srv:
        for name, cfg in TESTS:
            b = await via(cfg, "en.wikipedia.org")
            a = await via(cfg, "github.com")
            tag = "  <-- BYPASS" if (b == "OK" and a == "OK") else \
                  ("  (collateral damage)" if a != "OK" else "")
            lines.append(f"  {name:<20} blocked:{b:<22} allowed:{a:<22}{tag}")
            print(lines[-1], flush=True)
    print(f"\nsim stats: {dpisim.STATS}", flush=True)
    open("/tmp/simout.txt", "w").write("\n".join(lines) + "\n")


asyncio.run(main())
