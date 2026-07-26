# dpifuzz

Strategy search for [ByeDPI](https://github.com/hufrea/byedpi) / [ByeByeDPI](https://github.com/romanvht/ByeByeDPI).
Async, memoized, and it tells you when to stop.

> **CLI reference:** [CosmicIndustries/ByeByeDPIRelease](https://github.com/CosmicIndustries/ByeByeDPIRelease)
> — corrected English documentation of every ByeDPI argument. Read it first if
> you are hand-writing strategies; several widely-copied English references get
> `-A` and `-K` wrong.

---

## Why

ByeDPI has a large parameter space and no way to tell whether a strategy is
actually helping. The obvious approach — try lots of configs, keep the best —
fails in three specific ways, and this tool exists to handle all three.

**1. Dead targets are charged against every strategy equally.**
ByeByeDPI's built-in test uses 139 static domains, **19 of which are hardcoded
`rr1---sn-*.googlevideo.com` edge hostnames**. Those are issued per playback
session and rotate constantly, so they decay over time and fail regardless of
configuration. On an uplink with no filtering, every strategy scores a perfect
120/120 on real domains, and the entire visible leaderboard — 399 vs 398 vs
381 out of 417 — is ranking configs by which rotting YouTube hostnames
happened to answer that minute.

`doctor` measures every target with no proxy first and drops whatever doesn't
hold, so the noise is removed at the source rather than averaged over.

**2. Broken configs look like bad configs.**
A strategy that fails to parse never starts. The proxy simply isn't there, and
it scores zero — indistinguishable from a strategy that ran and performed
badly. Every genome is launch-tested against the real binary before it can
reach a scoreboard.

**3. A single observation can't rank anything.**
Elites are re-queued into every generation, so a lucky result regresses toward
its true mean instead of being crowned. Pooled standard deviation gives a noise
band, and anything inside it is reported as tied rather than ranked.

---

## Use

```bash
./dpifuzz.py doctor          # which targets are real, which are structurally dead
./dpifuzz.py run             # autonomous GA — breeds and measures locally
./dpifuzz.py emit -n 40      # breed a generation for the app's Proxy test
./dpifuzz.py ingest          # paste app results back, Ctrl-D
./dpifuzz.py report          # leaderboard, noise band, verdict
./dpifuzz.py export          # domain list + the app settings to change
```

Python 3.11+ (uses `StreamWriter.start_tls`). No dependencies. Needs a `byedpi`
or `ciadpi` binary — point at it with `--binary` if it isn't at
`/usr/local/bin/byedpi`.

Long runs outlive most terminal sessions:

```bash
nohup ./dpifuzz.py run > run.log 2>&1 &
```

### Loop with the Android app

The app evaluates fitness better than any local harness — 120 domains × 5 reps,
on the device and network that actually matter. It just can't generate
candidates; its 60 strategies are a fixed list compiled into the APK.

```
./dpifuzz.py emit -n 40   →  paste into Proxy test  →  copy log  →  ./dpifuzz.py ingest
```

Before the first ingest, run `export` and set the app's domain list to match.
Otherwise you are blending a 120-domain denominator against a 417-domain one
and the numbers are not comparable.

In the app's Proxy test settings, **turn off "Add the nearest GoogleVideo for
testing"** and set queries-per-domain to 5.

---

## How it measures

Fitness is the fraction of admissible targets completing a TLS handshake
through the proxy. SNI-based DPI kills the handshake, so this is a sharper and
far cheaper signal than a full HTTP fetch.

A fresh `byedpi` process is spawned for **every observation**. `--auto` caches
its per-IP group choice for 28 hours by default, so a reused proxy would carry
decisions between genomes and silently invalidate every comparison after the
first.

Memoization is per-genome behind an `asyncio.Lock`: concurrent evaluations of
the same config share one measurement instead of racing, and `ensure` tops a
genome up to N observations rather than remeasuring from zero. `memo.json`
survives restarts.

## Mutation

Operators act on parsed structure, not text — split positions, method swaps,
TTL, trigger groups, globals, and group-level crossover.

Constraints learned the hard way, encoded so the search can't waste generations
rediscovering them:

- `--split` positions must ascend, enforced per group and per flag class.
  Negative `--fake` offsets are left alone: packet size is added to them, and
  the ByeDPI author ships descending ones (`-f-43 -f-85 -f-165`) deliberately.
- `-L0` is documented as valid but rejected by byedpi 0.17.3
  (`invalid value: -L 0`), so it is excluded from mutation.
- `{sni}` is an app-side template bound at load time; `ciadpi --fake-sni` takes
  a literal. ByeDPI has its own randomization (`?` letter, `#` digit,
  `*` either) which is the better choice for a real strategy.

## Verdicts

`run` stops on its own. If the empty config scores above 0.98 it says so
plainly — nothing is being blocked, no genome can beat any other, ship the
simplest passthrough config. If spread collapses inside the noise band for
several generations it reports convergence rather than presenting a meaningless
ranking.

A flat leaderboard is a real result. On a network that isn't filtering you,
the honest answer is that no strategy matters, and more generations won't
change it.

---

## Share results

Results are only meaningful next to the network they came from, so the corpus is
keyed by country and ASN and never averaged across them.

```bash
./dpifuzz.py share --country RU --asn AS12389 --isp "Rostelecom" --medium fiber
./dpifuzz.py merge 'results/*.json'
./dpifuzz.py corpus --network RU
```

`share` writes `results/<CC>-<ASN>-<YYYY-MM>.json` containing strategy strings,
scores, a hash of the domain list, and the network metadata you passed — no IP,
no hostnames you visited, no timestamp finer than the month.

The domain-list hash is what keeps the corpus honest: if two runs measured
different targets, `merge` says so and keeps them apart instead of averaging a
120-domain score against a 417-domain one.

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Results where
*nothing* worked, or where passthrough already scored 1.0, are as useful as
wins.

---

## Files

| | |
|---|---|
| `dpifuzz.py` | everything |
| `seeds_official.list` | 60 strategies extracted from ByeByeDPI 1.7.7 |
| `domains_builtin.txt` | 120 stable domains, rotting googlevideo hosts removed |
| `ghpush.py` | tokenless-in-shell GitHub pusher |
| `memo.json` | every observation, created on first run |

Licence: GPL-3.0, matching ByeByeDPI.
