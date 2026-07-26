# Contributing results

The useful thing about this tool isn't the search — it's the corpus. A strategy
that beats Russian TSPU tells you nothing about Iranian or Turkish DPI, so what
actually helps people is **results tagged with the network they came from**.

If you run it, please share what you find.

## Submitting

```bash
./dpifuzz.py export                       # set the app's domain list to match
./dpifuzz.py run                          # or emit/ingest with the Android app
./dpifuzz.py share --country RU --asn AS12389 --isp "Rostelecom" --medium fiber
```

That writes `results/RU-AS12389-2026-07.json`. Open a PR adding it, or paste it
into an issue with the **Result submission** template. Either is fine.

Find your ASN at <https://bgp.tools> — it takes one click and results are close
to useless without it.

## What the file contains

Strategy strings, their scores, a hash of the domain list, and the network
metadata you passed on the command line. That is all.

It does **not** contain your IP address, any hostname you actually visited, or
any timestamp finer than the month. `share` prints this before writing, and the
file is plain JSON — read it before you post it.

If your ISP is small enough that country + ASN + medium identifies you
personally, think about whether you want that public. Omitting `--isp` and
`--notes` still leaves a useful record.

## Why results are never averaged across networks

`merge` keys everything by `country/ASN` and refuses to blend across them.
Two reasons, both learned the hard way:

**DPI is deployment-specific.** The whole point of a strategy is that it
defeats one particular middlebox implementation. Averaging a TSPU result with a
Turkish one produces a number describing no real network.

**Denominators must match.** The `fingerprint.domains_sha256` field is a hash of
the sorted domain list. If yours differs, `merge` says so and keeps the results
separate. This exists because ByeByeDPI's built-in test scores against 139
domains, 19 of which are hardcoded `rr1---sn-*.googlevideo.com` edge hostnames
that rotate and decay — so a 417-denominator score and a 120-denominator score
are not the same measurement, and averaging them silently destroys both.

## What makes a submission good

- **`n >= 3` per strategy.** One observation cannot rank anything. `share`
  defaults to `--min-obs 2` and higher is better.
- **Include the empty config.** If passthrough already scores 1.0, that is the
  single most valuable line in your file — it says your network isn't filtering,
  and it stops anyone reading meaning into the rest.
- **Report failures.** A network where nothing worked is a real result. Say so
  in `--notes`.
- **Don't hand-edit strategy strings.** Mobile keyboards mangle them, and a
  config that fails to parse never starts — it scores zero and looks exactly
  like a strategy that ran badly. `emit` launch-validates every candidate
  against the real binary for this reason.

## Using other people's results

```bash
./dpifuzz.py merge 'results/*.json'
./dpifuzz.py corpus --network RU
```

Then seed a local run from whatever scored well on a network resembling yours.
Treat it as a starting population, not an answer — TTL in particular is
path-dependent and will not transfer.
