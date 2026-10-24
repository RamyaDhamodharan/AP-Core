"""Run the idempotent AP vendor-link backfill against the configured database."""

import json

from src.db.session_db import SessionLocal
from src.services.vendor_backfill_services import backfill_vendor_links


def main() -> None:
    db = SessionLocal()
    try:
        counts = backfill_vendor_links(db)
        db.commit()
        print(json.dumps({"status": "completed", **counts}, indent=2))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
