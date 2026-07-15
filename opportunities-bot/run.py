"""Entrypoint. Add a scraper here and it joins the schedule."""
import logging, sys
from scrapers import uptree
from pipeline import sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run")

SCRAPERS = [uptree]

def main() -> int:
    all_rows, failed = [], []
    for mod in SCRAPERS:
        name = mod.__name__.split(".")[-1]
        try:
            rows = mod.scrape()
            log.info("%s -> %s rows", name, len(rows))
            all_rows.extend(rows)
        except Exception:
            # One dead scraper must not take the others down.
            log.exception("%s FAILED", name)
            failed.append(name)

    if all_rows:
        sync.run(all_rows)
    if failed:
        log.error("failed scrapers: %s", ", ".join(failed))
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
