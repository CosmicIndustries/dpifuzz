#!/usr/bin/env python3
"""dpifuzz — ByeDPI strategy search. Async, memoized, self-terminating.

Replaces ga.py / emit.py / ingest.py / gen.sh.

  doctor   classify every target: reachable, SNI-blocked, or structurally dead
  run      autonomous GA — breeds and measures locally, no human in the loop
  emit     breed one generation, launch-validate, write strategies.txt for the app
  ingest   read ByeByeDPI proxy-test output back into the memo
  report   leaderboard, noise band, verdict
  export   write the domain list to paste into the app

What the 1.7.7 APK taught us, encoded here:
  * its test list is 139 static domains, 19 of them hardcoded rr1---sn-*
    googlevideo edge hostnames that rotate and decay -> excluded, they were
    the entire source of the 399-vs-381 noise
  * {sni} is an app-side template; ciadpi's --fake-sni takes a literal, so it
    is bound at load time
  * -L0 is documented but rejected by byedpi 0.17.3
  * a config that fails to parse never starts, and reads as a score of zero
    rather than an error -> every genome is launch-validated before it ships
"""
from __future__ import annotations
import argparse, asyncio, hashlib, json, os, random, re, shlex, socket, ssl
import statistics, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BYEDPI = "/usr/local/bin/byedpi"
POS = re.compile(r"^-([sdoqfr])(-?\d+)(:\d+(?::\d+)?)?(\+[a-z]+)?$")
FLAGS = ["", "+s", "+s", "+s", "+sm", "+se", "+h"]
MODS = ["-Qr", "-Mh,d,r", "-Mh,d", '-n "google.com"', "-S", "-r1+s", "-r3+s", "-r5"]
TRIGS = ["-At", "-As", "-At,s", "-At,r,s", "-Ar,s"]
METH = ["s", "d", "o", "q", "f"]
FALLBACK_SEED = ["-d1 -s1+s -s3+s -s6+s -s9+s -s12+s -s15+s -s20+s -s30+s -a1",
                 "-o1 -a1 -At,r,s -f-1 -a1", "-T3 -At,s -d1 -s1+s -s3+s -a1", ""]
FALLBACK_HOSTS = "github.com cloudflare.com discord.com youtube.com telegram.org".split()

R = lambda a, b: random.randint(a, b)
pick = random.choice
key = lambda c: hashlib.sha1(c.encode()).hexdigest()[:12]
toks = lambda c: re.findall(r'-n\s+"[^"]*"|\S+', c)


def load_data() -> tuple[list[str], list[str]]:
    """Official strategies + stable domains, both extracted from the APK.
    {sni} is bound to a literal because ciadpi has no per-connection template."""
    sp, dp = HERE / "seeds_official.list", HERE / "domains_builtin.txt"
    seeds = ([l.strip().replace("{sni}", '"google.com"')
              for l in sp.read_text().splitlines() if l.strip()] + [""]
             if sp.exists() else list(FALLBACK_SEED))
    hosts = ([h.strip() for h in dp.read_text().splitlines()
              if h.strip() and not h.strip().startswith("rr")]
             if dp.exists() else list(FALLBACK_HOSTS))
    return seeds, hosts


# ------------------------------------------------------------------- genome
def groups(c: str) -> dict:
    """Globals (-T/-L/-u) plus -A-delimited groups. The first group is the
    default arm; if it is empty, untriggered traffic passes through clean."""
    g, glob = [{"trig": None, "o": []}], []
    for k in toks(c):
        if re.match(r"^-[TLu]", k): glob.append(k)
        elif k.startswith("-A"): g.append({"trig": k, "o": []})
        else: g[-1]["o"].append(k)
    return {"glob": glob, "g": g}


def unparse(x: dict) -> str:
    out = list(x["glob"])
    for v in x["g"]:
        if v["trig"]: out.append(v["trig"])
        out += v["o"]
    return " ".join(out).strip()


def shape(c: str) -> str:
    """Human-readable structure: which methods, how many groups, key deps."""
    x = groups(c)
    ms = sorted({m.group(1) for t in toks(c) if (m := POS.match(t))})
    bits = [f"{len(x['g'])}grp", "".join(ms) or "none"]
    if not x["g"][0]["o"]: bits.append("passthrough-default")
    if any(t.startswith("-t") and t[2:].isdigit() for t in toks(c)): bits.append("ttl-dep")
    if "-n" in toks(c): bits.append("fake-sni")
    return " ".join(bits)


# ---------------------------------------------------------------- mutation
def _pos(x): return [(i, j) for i, v in enumerate(x["g"])
                     for j, k in enumerate(v["o"]) if POS.match(k)]

def op_jitter(x):
    c = _pos(x)
    if not c: return False
    i, j = pick(c); m = POS.match(x["g"][i]["o"][j])
    n = int(m.group(2)) + pick([-4, -3, -2, -1, 1, 2, 3, 4, 6])
    if not m.group(2).startswith("-") and n < 0: n = 0
    fl = pick(FLAGS) if random.random() < .25 else (m.group(4) or "")
    x["g"][i]["o"][j] = f"-{m.group(1)}{n}{m.group(3) or ''}{fl}"; return True

def op_method(x):
    c = _pos(x)
    if not c: return False
    i, j = pick(c); m = POS.match(x["g"][i]["o"][j])
    x["g"][i]["o"][j] = f"-{pick(METH)}{m.group(2)}{m.group(3) or ''}{m.group(4) or ''}"
    return True

def op_add(x):
    i = R(0, len(x["g"]) - 1)
    x["g"][i]["o"].insert(R(0, len(x["g"][i]["o"])),
                          f"-{pick(['s','s','d','o'])}{R(1,40)}{pick(FLAGS)}"); return True

def op_drop(x):
    c = [(i, j) for i, v in enumerate(x["g"]) for j, k in enumerate(v["o"])
         if not k.startswith("-a")]
    if len(c) < 3: return False
    i, j = pick(c); x["g"][i]["o"].pop(j); return True

def op_ttl(x):
    o = x["g"][R(0, len(x["g"]) - 1)]["o"]
    j = next((i for i, k in enumerate(o) if re.match(r"^-t\d", k)), -1)
    if j < 0: o.append(f"-t{R(2,14)}")
    elif random.random() < .25: o.pop(j)
    else: o[j] = f"-t{max(1, int(o[j][2:]) + pick([-3,-2,-1,1,2,3]))}"
    return True

def op_mod(x):
    o = x["g"][R(0, len(x["g"]) - 1)]["o"]; m = pick(MODS)
    o.remove(m) if m in o else o.append(m); return True

def op_addgrp(x):
    if len(x["g"]) > 4: return False
    x["g"].insert(R(1, len(x["g"])), {"trig": pick(TRIGS),
                  "o": [f"-{pick(['s','d','o','f'])}{R(1,20)}{pick(FLAGS)}", "-a1"]})
    return True

def op_dropgrp(x):
    if len(x["g"]) < 2: return False
    x["g"].pop(R(1, len(x["g"]) - 1)); return True

def op_trig(x):
    c = [i for i, v in enumerate(x["g"]) if v["trig"]]
    if not c: return False
    x["g"][pick(c)]["trig"] = pick(TRIGS); return True

def op_glob(x):
    x["glob"] = [k for k in x["glob"] if not re.match(r"^-[TL]", k)]
    if random.random() < .7: x["glob"].append(f"-T{R(2,5)}")
    if random.random() < .5: x["glob"].append(f"-L{R(1,3)}")  # 0 rejected by 0.17.3
    return True

OPS = [op_jitter, op_method, op_add, op_drop, op_ttl, op_mod,
       op_addgrp, op_dropgrp, op_trig, op_glob]


def normalize(x: dict) -> dict:
    """Upstream: 'Позиции следует указывать в порядке возрастания' — split
    positions must ascend. Enforced per group, per flag-class, and ONLY for
    non-negative --split: negative offsets have packet size added, and the
    author's own strategies ship descending negative --fake offsets
    (-f-43 -f-85 -f-165), so those are left alone deliberately."""
    for g in x["g"]:
        idx, vals = [], []
        for i, k in enumerate(g["o"]):
            m = POS.match(k)
            if m and m.group(1) == "s" and not m.group(2).startswith("-"):
                idx.append((i, m.group(4) or "")); vals.append(int(m.group(2)))
        for fl in {f for _, f in idx}:
            sel = [i for i, f in idx if f == fl]
            if len(sel) < 2: continue
            got = sorted(int(POS.match(g["o"][i]).group(2)) for i in sel)
            for i, v in zip(sel, got):
                m = POS.match(g["o"][i])
                g["o"][i] = f"-s{v}{m.group(3) or ''}{m.group(4) or ''}"
    return x


def mutate(cfg: str, n: int) -> str:
    x = groups(cfg)
    for _ in range(n):
        for _ in range(6):
            if pick(OPS)(x): break
    return " ".join(unparse(normalize(x)).split())


def cross(a: str, b: str) -> str:
    A, B = groups(a), groups(b); cut = R(1, max(1, len(A["g"]) - 1))
    return " ".join(unparse(normalize({
        "glob": A["glob"] if random.random() < .5 else B["glob"],
        "g": (A["g"][:cut] + B["g"][cut:])[:5]})).split())


# ---------------------------------------------------------------- plumbing
def free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


async def spawn(cfg: str, port: int):
    """Start byedpi and wait for the listener. Returns None if it won't run."""
    try:
        pr = await asyncio.create_subprocess_exec(
            BYEDPI, "-i", "127.0.0.1", "-p", str(port), "-u", "1", *shlex.split(cfg),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    except Exception:
        return None
    for _ in range(50):
        if pr.returncode is not None: return None
        try:
            _, w = await asyncio.open_connection("127.0.0.1", port); w.close(); return pr
        except OSError:
            await asyncio.sleep(.05)
    try: pr.kill()
    except Exception: pass
    return None


async def reap(pr):
    try:
        pr.terminate(); await asyncio.wait_for(pr.wait(), 3)
    except Exception:
        try: pr.kill()
        except Exception: pass


async def validates(cfg: str) -> bool:
    """A genome that won't parse never starts, and would score zero rather than
    erroring — indistinguishable from a genuinely bad strategy. So prove it runs."""
    p = free_port(); pr = await spawn(cfg, p)
    if pr is None: return False
    await reap(pr); return True


CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE


async def handshake(host: str, proxy: int | None, timeout: float) -> bool:
    """TLS handshake, optionally through local SOCKS5. SNI-based DPI kills the
    handshake, so this is sharper and far cheaper than a full HTTP fetch."""
    try:
        if proxy is None:
            _, w = await asyncio.wait_for(asyncio.open_connection(
                host, 443, ssl=CTX, server_hostname=host), timeout)
            w.close(); return True
        r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", proxy), timeout)
        try:
            w.write(b"\x05\x01\x00"); await w.drain()
            if await asyncio.wait_for(r.readexactly(2), timeout) != b"\x05\x00": return False
            hb = host.encode()
            w.write(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + (443).to_bytes(2, "big"))
            await w.drain()
            hdr = await asyncio.wait_for(r.readexactly(4), timeout)
            if hdr[1] != 0: return False
            ln = {1: 4, 4: 16}.get(hdr[3])
            if ln is None: ln = (await asyncio.wait_for(r.readexactly(1), timeout))[0]
            await asyncio.wait_for(r.readexactly(ln + 2), timeout)
            await asyncio.wait_for(w.start_tls(CTX, server_hostname=host), timeout)
            return True
        finally:
            w.close()
            try: await w.wait_closed()
            except Exception: pass
    except Exception:
        return False


# --------------------------------------------------------------- evaluator
class Evaluator:
    def __init__(self, hosts, timeout=5.0, proxies=3, probes=10, jitter=.12):
        self.hosts, self.timeout, self.jitter = hosts, timeout, jitter
        self.psem = asyncio.Semaphore(proxies)   # concurrent byedpi processes
        self.qsem = asyncio.Semaphore(probes)    # concurrent handshakes

    async def baseline(self, reps=2) -> list[str]:
        """Keep only hosts reachable with NO proxy, every rep. This is what stops
        dead and rotating hosts being charged against every genome equally."""
        async def ok(h):
            for _ in range(reps):
                async with self.qsem:
                    if not await handshake(h, None, self.timeout): return None
            return h
        return [h for h in await asyncio.gather(*(ok(h) for h in self.hosts)) if h]

    async def measure(self, cfg: str, hosts: list[str]) -> float:
        """One observation. Fresh process every time: --auto caches its per-IP
        group choice for 28h, so a reused proxy leaks decisions between genomes."""
        async with self.psem:
            port = free_port(); pr = await spawn(cfg, port)
            if pr is None: return 0.0
            try:
                async def one(h):
                    async with self.qsem:
                        await asyncio.sleep(random.uniform(0, self.jitter))
                        return await handshake(h, port, self.timeout)
                hits = sum(await asyncio.gather(*(one(h) for h in hosts)))
                return hits / max(1, len(hosts))
            finally:
                await reap(pr)


class Memo:
    """Per-genome async memo. Concurrent callers for the same config share one
    measurement instead of racing, and `ensure` tops a genome up to N
    observations rather than remeasuring from zero. Survives restarts."""

    def __init__(self, path: Path):
        self.path, self.d, self._locks = path, {}, {}
        if path.exists():
            try: self.d = json.loads(path.read_text())
            except Exception: self.d = {}

    def scores(self, cfg): return self.d.get(key(cfg), {}).get("s", [])
    def mean(self, cfg):
        s = self.scores(cfg); return statistics.mean(s) if s else None

    def flush(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d)); os.replace(tmp, self.path)

    def add(self, cfg: str, score: float):
        self.d.setdefault(key(cfg), {"c": cfg, "s": []})["s"].append(round(score, 4))

    async def ensure(self, cfg, n, ev, hosts) -> float:
        k = key(cfg)
        async with self._locks.setdefault(k, asyncio.Lock()):
            rec = self.d.setdefault(k, {"c": cfg, "s": []})
            while len(rec["s"]) < n:
                rec["s"].append(round(await ev.measure(cfg, hosts), 4)); self.flush()
            return statistics.mean(rec["s"])

    def noise(self) -> float:
        v = [statistics.variance(r["s"]) for r in self.d.values() if len(r["s"]) > 1]
        return 2 * (statistics.mean(v) ** .5) if v else 0.0

    def ranked(self):
        return sorted(((statistics.mean(r["s"]), len(r["s"]), r["c"])
                       for r in self.d.values() if r["s"]), reverse=True)


# ------------------------------------------------------------------ breeding
async def breed(parents: list[str], n: int, elite: int) -> list[str]:
    """Elites verbatim and first — they get re-scored every round, which is what
    drags a lucky observation back toward its true mean. Everything else is
    launch-validated concurrently; survivors only."""
    out, seen = list(parents[:elite]), set(parents[:elite])
    cand, guard = [], 0
    while len(cand) + len(out) < n * 3 and guard < 4000:
        guard += 1
        p = pick(parents)
        c = (cross(p, pick(parents)) if random.random() < .35 and len(parents) > 1
             else mutate(p, R(1, 3)))
        if c and c not in seen and len(c) > 5:
            seen.add(c); cand.append(c)
    good = await asyncio.gather(*(validates(c) for c in cand))
    for c, ok in zip(cand, good):
        if ok and len(out) < n: out.append(c)
    return out


def verdict(memo: Memo, top: float, med: float) -> str | None:
    nb = memo.noise()
    empty = memo.mean("")
    if empty is not None and empty > .98:
        return ("The empty config scores {:.3f}. Nothing here is being blocked, so "
                "no genome can beat any other — this network does not discriminate. "
                "Ship the simplest passthrough config.".format(empty))
    if top - med <= nb:
        return ("Spread has collapsed inside the noise band (±{:.4f}). Nothing "
                "distinguishes the survivors; take the shortest one.".format(nb))
    return None


def show(memo: Memo, n=12):
    r = memo.ranked()
    if not r:
        print("no observations yet"); return
    nb, top = memo.noise(), r[0][0]
    tied = sum(1 for s, _, _ in r if top - s <= nb)
    print(f"\n{'score':>7} {'n':>3}  {'shape':<34} strategy")
    print("-" * 108)
    for s, k, c in r[:n]:
        mark = "=" if top - s <= nb else " "
        print(f"{s:7.4f} {k:>3}{mark} {shape(c):<34} {(c or '<empty>')[:52]}")
    print(f"\nnoise band ±{nb:.4f} — {tied} strategy(ies) statistically tied at the top")
    v = verdict(memo, top, statistics.median([s for s, _, _ in r]))
    if v: print("\n" + v)


# ---------------------------------------------------------------- commands
async def cmd_doctor(a, hosts):
    ev = Evaluator(hosts, a.timeout, a.proxies, a.probes)
    good = await ev.baseline()
    dead = [h for h in hosts if h not in good]
    print(f"reachable with no proxy: {len(good)}/{len(hosts)}")
    if dead:
        print(f"unreachable ({len(dead)}): {' '.join(dead[:12])}"
              f"{' ...' if len(dead) > 12 else ''}")
        print("  ^ not DPI failures. Excluded so they cannot be charged "
              "against every genome equally.")
    if len(good) < 5:
        print("\nToo few reachable targets — the uplink is the problem, not the configs.")
    elif not dead:
        print("\nEvery target resolves and completes a TLS handshake untouched. "
              "If nothing is blocked, no strategy can outscore the empty one; "
              "run `run` to confirm, then stop tuning.")


async def cmd_run(a, hosts):
    ev, memo = Evaluator(hosts, a.timeout, a.proxies, a.probes), Memo(HERE / "memo.json")
    st = HERE / "state.json"
    cached = json.loads(st.read_text()) if st.exists() else {}
    live = cached.get("hosts") or await ev.baseline()
    print(f"admissible {len(live)}/{len(hosts)} targets")
    if len(live) < 5:
        print("too few reachable targets — check the uplink"); return
    seeds, _ = load_data()
    pool, flat = cached.get("pool") or seeds, 0
    for gen in range(1, a.gens + 1):
        res = await asyncio.gather(*(memo.ensure(c, a.obs, ev, live) for c in pool))
        scored = sorted(zip(res, pool), reverse=True)
        med = statistics.median([s for s, _ in scored])
        print(f"\ngen {gen:>2}  best {scored[0][0]:.4f}  median {med:.4f}  "
              f"noise ±{memo.noise():.4f}  memo {len(memo.d)}")
        print(f"        {(scored[0][1] or '<empty>')[:92]}")
        if verdict(memo, scored[0][0], med):
            flat += 1
            if flat >= a.patience:
                break
        else:
            flat = 0
        parents = [c for _, c in scored[:max(4, len(scored) // 2)]]
        pool = await breed(parents, a.pop, a.elite)
        st.write_text(json.dumps({"pool": pool, "hosts": live, "gen": gen}))
    show(memo)


async def cmd_emit(a, hosts):
    memo = Memo(HERE / "memo.json")
    seeds, _ = load_data()
    r = memo.ranked()
    parents = [c for _, _, c in r[:12] if c.strip()] or [s for s in seeds if s.strip()]
    out = await breed(parents, a.n, a.elite)
    (HERE / "strategies.txt").write_text("\n".join(out) + "\n")
    print("\n".join(out))
    print(f"\n{len(out)} launch-validated strategies -> strategies.txt",
          file=sys.stderr)


FRAC = re.compile(r"\b(\d{1,5})\s*/\s*(\d{1,5})\b")
PCT = re.compile(r"\b(\d{1,3})(?:\.\d+)?\s*%")


def cmd_ingest(a, _hosts):
    """Pair each strategy line with the score line that follows it. Scores are
    normalised to 0..1 so app results sit alongside locally measured ones —
    which only holds if the app is using the same domain list (see `export`)."""
    memo, text = Memo(HERE / "memo.json"), sys.stdin.read()
    pending, n = None, 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line: continue
        if line.startswith("-") and not FRAC.search(line):
            pending = " ".join(line.split()); continue
        m, score = FRAC.search(line), None
        if m and int(m.group(2)): score = int(m.group(1)) / int(m.group(2))
        elif (p := PCT.search(line)): score = int(p.group(1)) / 100
        if score is None or pending is None: continue
        memo.add(pending, score); n += 1; pending = None
    memo.flush()
    print(f"{n} observations recorded, memo holds {len(memo.d)}")
    show(memo)


def cmd_report(a, _hosts): show(Memo(HERE / "memo.json"), a.n)


def cmd_export(a, hosts):
    p = HERE / "domains.txt"; p.write_text("\n".join(hosts) + "\n")
    print(f"{len(hosts)} stable domains -> {p}\n")
    print("In ByeByeDPI -> Proxy test -> gear icon:")
    print("  * turn OFF 'Add the nearest GoogleVideo for testing'")
    print("  * set queries-per-domain to 5")
    print("  * paste this file as the domain list (newlines only, no commas)")
    print(f"  ceiling becomes {len(hosts)} x 5 = {len(hosts)*5}, every point a real domain")


def main():
    global BYEDPI
    ap = argparse.ArgumentParser(prog="dpifuzz", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--binary", default=BYEDPI)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--proxies", type=int, default=3)
    ap.add_argument("--probes", type=int, default=10)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    r = sub.add_parser("run")
    r.add_argument("--gens", type=int, default=25); r.add_argument("--pop", type=int, default=12)
    r.add_argument("--elite", type=int, default=3); r.add_argument("--obs", type=int, default=3)
    r.add_argument("--patience", type=int, default=3)
    e = sub.add_parser("emit")
    e.add_argument("-n", type=int, default=40); e.add_argument("--elite", type=int, default=6)
    sub.add_parser("ingest")
    rp = sub.add_parser("report"); rp.add_argument("-n", type=int, default=12)
    sub.add_parser("export")

    sh = sub.add_parser("share")
    sh.add_argument("--country", help="ISO code, e.g. RU, IR, TR, US")
    sh.add_argument("--asn", help="e.g. AS12389 — look yours up at https://bgp.tools")
    sh.add_argument("--isp"); sh.add_argument("--notes")
    sh.add_argument("--medium", choices=["fiber", "cable", "dsl", "mobile", "wifi", "other"])
    sh.add_argument("--min-obs", type=int, default=2)
    sh.add_argument("-o")

    mg = sub.add_parser("merge"); mg.add_argument("files", nargs="+")
    co = sub.add_parser("corpus")
    co.add_argument("--network", help="filter, e.g. RU or AS12389")
    co.add_argument("-n", type=int, default=8)
    co.add_argument("--consistency", action="store_true",
                    help="which strategies hold up across networks (rank aggregation)")
    co.add_argument("--min-networks", type=int, default=2)

    a = ap.parse_args()

    BYEDPI = a.binary
    _, hosts = load_data()
    fn = {"doctor": cmd_doctor, "run": cmd_run, "emit": cmd_emit,
          "ingest": cmd_ingest, "report": cmd_report, "export": cmd_export,
          "share": cmd_share, "merge": cmd_merge, "corpus": cmd_corpus}[a.cmd]
    try:
        asyncio.run(fn(a, hosts)) if asyncio.iscoroutinefunction(fn) else fn(a, hosts)
    except KeyboardInterrupt:
        print("\nstopped — memo.json keeps every observation", file=sys.stderr)



# ------------------------------------------------------------------ sharing
SCHEMA = 1


def byedpi_version() -> str:
    import subprocess
    for args in (["-v"], ["--version"], ["-h"]):
        try:
            r = subprocess.run([BYEDPI, *args], capture_output=True, text=True, timeout=5)
            m = re.search(r"\d+\.\d+\.\d+(?:\s*\([0-9a-f]+\))?", r.stdout + r.stderr)
            if m: return m.group(0)
        except Exception:
            pass
    return "unknown"


def fingerprint(hosts: list[str]) -> dict:
    """Two runs are only comparable if they measured the same targets the same
    way. This is what stops a 120-domain result being averaged against a
    417-domain one — the mistake that made every early leaderboard meaningless."""
    h = hashlib.sha256("\n".join(sorted(hosts)).encode()).hexdigest()[:16]
    return {"domains_sha256": h, "domain_count": len(hosts),
            "byedpi": byedpi_version(), "schema": SCHEMA}


def cmd_share(a, hosts):
    memo = Memo(HERE / "memo.json")
    rows = [{"cfg": r["c"], "mean": round(statistics.mean(r["s"]), 4),
             "n": len(r["s"]),
             "sd": round(statistics.pstdev(r["s"]), 4) if len(r["s"]) > 1 else None}
            for r in memo.d.values() if len(r["s"]) >= a.min_obs]
    if not rows:
        sys.exit(f"nothing with >= {a.min_obs} observations yet — run `run` first")
    rows.sort(key=lambda x: -x["mean"])
    doc = {"schema": SCHEMA, "created": __import__("datetime").date.today().isoformat(),
           "fingerprint": fingerprint(hosts),
           "network": {"country": a.country, "asn": a.asn, "isp": a.isp,
                       "medium": a.medium, "notes": a.notes},
           "results": rows}
    out = Path(a.o) if a.o else (HERE / "results" /
          f"{a.country or 'XX'}-{(a.asn or 'ASxxxx')}-"
          f"{doc['created'][:7]}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2))
    print(f"{len(rows)} results -> {out}")
    print("\nContains: strategy strings, scores, domain-list hash, and the network\n"
          "metadata you passed. No IP address, no hostnames you visited, no\n"
          "timestamps beyond the month. Read it before sharing if unsure.")
    if not a.asn:
        print("\nNo --asn given. Results are network-specific and close to useless\n"
              "without it — look yours up at https://bgp.tools and re-run.")


def cmd_merge(a, hosts):
    """Import shared results into a corpus, keyed by network. Never averaged
    across networks: a strategy is a claim about one DPI deployment."""
    corpus_p = HERE / "corpus.json"
    corpus = json.loads(corpus_p.read_text()) if corpus_p.exists() else {}
    mine = fingerprint(hosts)["domains_sha256"]
    added = skipped = 0
    for f in a.files:
        for path in sorted(Path(".").glob(f)) or [Path(f)]:
            try: doc = json.loads(Path(path).read_text())
            except Exception as e:
                print(f"  skip {path}: {e}"); continue
            fp = doc.get("fingerprint", {})
            net = doc.get("network", {})
            tag = f"{net.get('country') or 'XX'}/{net.get('asn') or 'ASxxxx'}"
            if fp.get("domains_sha256") != mine:
                print(f"  {path}: different domain list "
                      f"({fp.get('domain_count')} domains, hash "
                      f"{fp.get('domains_sha256')}) — kept separate, not comparable "
                      f"to your local scores")
                skipped += 1
            e = corpus.setdefault(tag, {"network": net, "fingerprints": [], "results": {}})
            if fp.get("domains_sha256") not in e["fingerprints"]:
                e["fingerprints"].append(fp.get("domains_sha256"))
            for r in doc.get("results", []):
                cur = e["results"].get(r["cfg"])
                if not cur or r["n"] > cur["n"]:
                    e["results"][r["cfg"]] = {"mean": r["mean"], "n": r["n"]}
            added += len(doc.get("results", []))
    corpus_p.write_text(json.dumps(corpus, indent=2))
    print(f"{added} results across {len(corpus)} networks -> corpus.json"
          + (f"  ({skipped} file(s) with a different domain list)" if skipped else ""))


def cmd_corpus(a, hosts):
    p = HERE / "corpus.json"
    if not p.exists():
        sys.exit("no corpus.json — run `merge` on some shared result files first")
    corpus = json.loads(p.read_text())

    if a.consistency:
        # Which strategies generalise? Rank within each network, then aggregate
        # ranks — raw scores are not comparable across networks (0.4 on heavy
        # filtering vs 1.0 on an open uplink averages to nothing real), but
        # relative ordering within a network is.
        #
        # Networks with zero spread are dropped: if everything ties, the ranking
        # is arbitrary and would inject pure noise into the aggregate.
        per: dict[str, list[float]] = {}
        used = flat = 0
        for tag, e in corpus.items():
            rows = sorted(e["results"].items(), key=lambda kv: -kv[1]["mean"])
            vals = [r["mean"] for _, r in rows]
            if len(rows) < 2 or max(vals) - min(vals) < 1e-9:
                flat += 1; continue
            used += 1
            for i, (cfg, _) in enumerate(rows):
                per.setdefault(cfg, []).append(1 - i / (len(rows) - 1))
        if not used:
            sys.exit(f"no network in the corpus has any spread ({flat} flat) — "
                     "nothing to compare. Flat corpora mean nobody is being "
                     "filtered, which is itself the answer.")
        rank = sorted(((statistics.median(v), len(v), c) for c, v in per.items()
                       if len(v) >= a.min_networks), reverse=True)
        print(f"cross-network consistency over {used} network(s) with spread"
              + (f"  ({flat} flat, excluded)" if flat else ""))
        print(f"\n{'pctile':>7} {'nets':>5}  {'shape':<30} strategy")
        print("-" * 100)
        for pc, n, c in rank[:a.n]:
            print(f"{pc:7.2f} {n:>5}  {shape(c):<30} {(c or '<empty>')[:44]}")
        if not rank:
            print(f"  nothing measured on >= {a.min_networks} networks yet")
        print("\nPercentile is the median position within each network, 1.0 = best.\n"
              "High percentile across many networks = generalises. High on one\n"
              "network only = tuned to that DPI deployment, and probably to its TTL.")
        return

    want = a.network
    for tag, e in sorted(corpus.items()):
        if want and want.lower() not in tag.lower(): continue
        net = e.get("network", {})
        head = f"{tag}  {net.get('isp') or ''} {net.get('medium') or ''}".strip()
        print(f"\n{head}" + (f"   [{net['notes']}]" if net.get("notes") else ""))
        rows = sorted(e["results"].items(), key=lambda kv: -kv[1]["mean"])[:a.n]
        for cfg, r in rows:
            print(f"  {r['mean']:.4f} n={r['n']:<3} {shape(cfg):<30} {cfg[:46]}")
    print("\nRaw scores are per-network. For what holds up across deployments:"
          "\n  ./dpifuzz.py corpus --consistency")


if __name__ == "__main__":
    main()
