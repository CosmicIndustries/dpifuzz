#!/usr/bin/env python3
"""dpifuzz TUI — live view of the search.

    ./tui.py            monitor + drive the GA
    ./tui.py --demo     no byedpi calls, for trying the interface

Stdlib curses over asyncio. Everything the CLI does, plus you can watch a
generation being measured instead of staring at a log.
"""
from __future__ import annotations
import argparse, asyncio, curses, json, random, statistics, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dpifuzz as D

HERE = Path(__file__).resolve().parent


class State:
    def __init__(self):
        self.gen = 0
        self.phase = "idle"          # idle | doctor | measuring | breeding | done
        self.done = self.total = 0
        self.rows: list[tuple] = []  # (mean, n, cfg)
        self.log: list[str] = []
        self.hosts: list[str] = []
        self.admissible = 0
        self.noise = 0.0
        self.verdict: str | None = None
        self.running = True
        self.paused = False
        self.sel = 0
        self.scroll = 0
        self.detail = False

    def say(self, msg: str):
        self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")
        del self.log[:-200]


def clip(s: str, w: int) -> str:
    return s if len(s) <= w else s[: max(0, w - 1)] + "…"


class UI:
    def __init__(self, scr):
        self.scr = scr
        curses.curs_set(0)
        scr.nodelay(True)
        self.color = curses.has_colors()
        if self.color:
            curses.start_color(); curses.use_default_colors()
            for i, fg in enumerate((curses.COLOR_CYAN, curses.COLOR_GREEN,
                                    curses.COLOR_YELLOW, curses.COLOR_RED,
                                    curses.COLOR_MAGENTA, curses.COLOR_BLUE), 1):
                curses.init_pair(i, fg, -1)

    def c(self, n, bold=False):
        a = curses.color_pair(n) if self.color else 0
        return a | (curses.A_BOLD if bold else 0)

    def put(self, y, x, s, attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try: self.scr.addnstr(y, x, s, w - x - 1, attr)
            except curses.error: pass

    # ------------------------------------------------------------------ draw
    def draw(self, st: State):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if h < 12 or w < 60:
            self.put(0, 0, "terminal too small", self.c(4)); self.scr.refresh(); return

        logh = min(6, max(3, h // 5))
        body_top, body_bot = 4, h - logh - 3

        # header
        best = st.rows[0][0] if st.rows else 0.0
        med = statistics.median([r[0] for r in st.rows]) if st.rows else 0.0
        self.put(0, 0, " dpifuzz ", self.c(1, True))
        self.put(0, 10, f"gen {st.gen}   memo {len(st.rows)}   "
                        f"noise ±{st.noise:.4f}   admissible {st.admissible}",
                 self.c(6))
        pstr = st.phase + (" (paused)" if st.paused else "")
        self.put(1, 1, f"best {best:.4f}   median {med:.4f}   {pstr}")
        if st.total:
            bw = max(10, w - 40)
            fill = int(bw * st.done / st.total)
            self.put(2, 1, "[" + "█" * fill + "·" * (bw - fill) +
                     f"] {st.done}/{st.total}", self.c(3))
        elif st.verdict:
            self.put(2, 1, clip(st.verdict.replace("\n", " "), w - 3), self.c(3, True))
        self.put(3, 0, "─" * (w - 1), self.c(6))

        # population
        rows = st.rows
        view = body_bot - body_top
        if st.sel >= len(rows): st.sel = max(0, len(rows) - 1)
        if st.sel < st.scroll: st.scroll = st.sel
        if st.sel >= st.scroll + view: st.scroll = st.sel - view + 1
        top = rows[0][0] if rows else 0

        for i in range(view):
            idx = st.scroll + i
            if idx >= len(rows): break
            mean, n, cfg = rows[idx]
            y = body_top + i
            sel = idx == st.sel
            tied = rows and (top - mean) <= st.noise
            barw = 14
            fill = int(barw * mean)
            col = 2 if mean >= .95 else 3 if mean >= .7 else 4
            self.put(y, 0, ">" if sel else " ", self.c(1, True))
            self.put(y, 2, f"{mean:.4f}", self.c(col, sel))
            self.put(y, 9, "▌" * fill + "·" * (barw - fill), self.c(col))
            self.put(y, 9 + barw + 1, f"n={n:<3}", self.c(6))
            self.put(y, 9 + barw + 6, f"{'=' if tied else ' '}")
            sh = D.shape(cfg) if cfg else "passthrough"
            self.put(y, 9 + barw + 8, clip(sh, 30), self.c(5))
            x = 9 + barw + 39
            self.put(y, x, clip(cfg or "<empty>", w - x - 1),
                     curses.A_BOLD if sel else 0)

        if not rows:
            self.put(body_top + 1, 4, "no observations yet — press r to run",
                     self.c(6))

        # detail overlay
        if st.detail and rows:
            self.detail_box(st, rows[st.sel])

        # log
        ly = h - logh - 2
        self.put(ly, 0, "─" * (w - 1), self.c(6))
        for i, line in enumerate(st.log[-(logh - 1):]):
            self.put(ly + 1 + i, 1, clip(line, w - 2), self.c(6))

        keys = (" r run   p pause   e emit   d doctor   s share   "
                "↑↓ select   ⏎ detail   q quit ")
        self.put(h - 1, 0, clip(keys, w - 1), curses.A_REVERSE)
        self.scr.refresh()

    def detail_box(self, st: State, row):
        h, w = self.scr.getmaxyx()
        mean, n, cfg = row
        bw, bh = min(w - 6, 96), min(h - 6, 14)
        y0, x0 = (h - bh) // 2, (w - bw) // 2
        for y in range(y0, y0 + bh):
            self.put(y, x0, " " * bw, curses.A_REVERSE)
        self.put(y0, x0 + 2, f" score {mean:.4f}  n={n} ", curses.A_REVERSE | curses.A_BOLD)
        toks, line, ly = (cfg or "<empty>").split(), "", y0 + 2
        for t in toks:
            if len(line) + len(t) + 1 > bw - 6:
                self.put(ly, x0 + 3, line, curses.A_REVERSE); ly += 1; line = ""
            line += (" " if line else "") + t
        if line: self.put(ly, x0 + 3, line, curses.A_REVERSE); ly += 1
        self.put(ly + 1, x0 + 3, f"shape: {D.shape(cfg) if cfg else 'passthrough'}",
                 curses.A_REVERSE)
        self.put(y0 + bh - 1, x0 + 2, " ⏎/esc close   c copy to clipboard file ",
                 curses.A_REVERSE)


# --------------------------------------------------------------- the search
async def measure_all(st: State, memo, pool, ev, obs, demo):
    """Measure a generation, updating progress as each genome resolves."""
    st.phase, st.done, st.total = "measuring", 0, len(pool)

    async def one(cfg):
        while st.paused: await asyncio.sleep(.2)
        if demo:
            await asyncio.sleep(random.uniform(.15, .5))
            v = min(1.0, max(0.0, random.gauss(.9 - .3 * ("-r" in cfg), .06)))
            memo.add(cfg, v); memo.flush(); r = v
        else:
            r = await memo.ensure(cfg, obs, ev, st.hosts)
        st.done += 1
        refresh_rows(st, memo)
        return r

    res = await asyncio.gather(*(one(c) for c in pool))
    st.total = st.done = 0
    return res


def refresh_rows(st: State, memo):
    st.rows = [(statistics.mean(r["s"]), len(r["s"]), r["c"])
               for r in memo.d.values() if r["s"]]
    st.rows.sort(key=lambda r: -r[0])
    st.noise = memo.noise()


async def run_search(st: State, a):
    memo = D.Memo(HERE / "memo.json")
    refresh_rows(st, memo)
    seeds, hosts = D.load_data()
    ev = D.Evaluator(hosts, a.timeout, a.proxies, a.probes)

    if a.demo:
        st.hosts = hosts[:20]; st.admissible = len(st.hosts)
        st.say("demo mode — scores are synthetic, no byedpi is being run")
    else:
        st.phase = "doctor"; st.say("checking which targets are reachable…")
        st.hosts = await ev.baseline()
        st.admissible = len(st.hosts)
        dead = len(hosts) - len(st.hosts)
        st.say(f"{len(st.hosts)}/{len(hosts)} admissible"
               + (f", {dead} excluded as unreachable" if dead else ""))
        if len(st.hosts) < 5:
            st.say("too few reachable targets — check the uplink, not the configs")
            st.phase = "idle"; return

    pool, flat = list(seeds), 0
    for gen in range(1, a.gens + 1):
        if not st.running: break
        st.gen = gen
        res = await measure_all(st, memo, pool, ev, a.obs, a.demo)
        scored = sorted(zip(res, pool), reverse=True)
        med = statistics.median([s for s, _ in scored])
        st.say(f"gen {gen}: best {scored[0][0]:.4f}  median {med:.4f}  "
               f"noise ±{st.noise:.4f}")
        st.verdict = D.verdict(memo, scored[0][0], med)
        if st.verdict:
            flat += 1
            if flat >= a.patience:
                st.say("converged — stopping"); break
        else:
            flat = 0
        st.phase = "breeding"
        parents = [c for _, c in scored[: max(4, len(scored) // 2)]]
        pool = await D.breed(parents, a.pop, a.elite) if not a.demo else \
               [D.mutate(random.choice(parents), 2) for _ in range(a.pop)]
    st.phase = "done"
    st.say("search finished — q to quit, r to run again")


# -------------------------------------------------------------- input + main
async def input_loop(st: State, ui: UI, a):
    task: asyncio.Task | None = None
    while st.running:
        ch = ui.scr.getch()
        if ch != -1:
            if st.detail:
                if ch in (10, 13, 27, ord("q")): st.detail = False
                elif ch == ord("c") and st.rows:
                    p = HERE / "selected.txt"
                    p.write_text(st.rows[st.sel][2] + "\n")
                    st.say(f"wrote selected strategy to {p.name}")
            elif ch in (ord("q"), 27):
                st.running = False
            elif ch == ord("p"):
                st.paused = not st.paused
                st.say("paused" if st.paused else "resumed")
            elif ch == ord("r"):
                if task and not task.done():
                    st.say("already running — p to pause")
                else:
                    task = asyncio.create_task(run_search(st, a))
            elif ch == ord("d"):
                st.say("doctor: use ./dpifuzz.py doctor for the full report")
            elif ch == ord("e"):
                if a.demo:
                    st.say("emit is disabled in demo mode")
                else:
                    memo = D.Memo(HERE / "memo.json")
                    r = memo.ranked()
                    parents = [c for _, _, c in r[:12] if c.strip()] or \
                              [s for s in D.load_data()[0] if s.strip()]
                    out = await D.breed(parents, 40, 6)
                    (HERE / "strategies.txt").write_text("\n".join(out) + "\n")
                    st.say(f"emitted {len(out)} validated strategies -> strategies.txt")
            elif ch == ord("s"):
                st.say("share: ./dpifuzz.py share --country XX --asn ASxxxx")
            elif ch in (curses.KEY_UP, ord("k")): st.sel = max(0, st.sel - 1)
            elif ch in (curses.KEY_DOWN, ord("j")): st.sel += 1
            elif ch == curses.KEY_NPAGE: st.sel += 10
            elif ch == curses.KEY_PPAGE: st.sel = max(0, st.sel - 10)
            elif ch in (10, 13): st.detail = not st.detail
            elif ch == curses.KEY_RESIZE: ui.scr.clear()
        ui.draw(st)
        await asyncio.sleep(.04)
    if task and not task.done():
        task.cancel()


async def amain(scr, a):
    st = State()
    ui = UI(scr)
    memo = D.Memo(HERE / "memo.json")
    refresh_rows(st, memo)
    st.say("ready — press r to start the search, q to quit")
    if a.auto:
        asyncio.create_task(run_search(st, a))
    await input_loop(st, ui, a)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--demo", action="store_true",
                   help="synthetic scores, no byedpi — for trying the interface")
    p.add_argument("--auto", action="store_true", help="start searching immediately")
    p.add_argument("--binary", default=D.BYEDPI)
    p.add_argument("--gens", type=int, default=25)
    p.add_argument("--pop", type=int, default=12)
    p.add_argument("--elite", type=int, default=3)
    p.add_argument("--obs", type=int, default=3)
    p.add_argument("--patience", type=int, default=3)
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--proxies", type=int, default=3)
    p.add_argument("--probes", type=int, default=10)
    a = p.parse_args()
    D.BYEDPI = a.binary
    curses.wrapper(lambda scr: asyncio.run(amain(scr, a)))


if __name__ == "__main__":
    main()
