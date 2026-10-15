# Page-view metrics: what we count and what we do not claim

This Worker counts **page views**: atomic per-day, per-probe aggregate
counters (`views:<probeId>:<yyyy-mm-dd>`). Nothing else is recorded: no IP,
no user-agent, no referrer, no cookies, no fingerprints, no per-visitor
anything. Health checks, asset paths, 404s and controlled test traffic
(`?fixture_test=1`) are excluded.

## Uncertainty you must preserve when reporting

- **Bots:** these counts include bots, crawlers, prefetchers and our own
  manual checks unless excluded above. There is no bot filtering because
  filtering would require storing the very request metadata we refuse to
  keep. Any number from these counters is an upper bound on human reach.
- **Reach:** a page view is not a person. Never report these as "unique
  visitors", "real visitors", "users" or "demand".
- **Synthetic commitments:** signups from fixtures, rehearsals or our own
  tests are feasibility evidence only. Never report them as demand.

## Labels

Call them **page views**, never unique real visitors. The changelog and any
report using these numbers must carry the bot/reach caveat above.
