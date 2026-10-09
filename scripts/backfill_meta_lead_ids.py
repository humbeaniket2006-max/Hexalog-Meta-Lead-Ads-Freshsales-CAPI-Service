"""Backfill Meta leadgen ids for leads whose capture failed.

Dry run by default: fetches each failed lead from Meta and looks the contact up
in Freshsales, but writes nothing. Pass --apply to stamp/create contacts and
record lead_states in the database.

Usage: python -m scripts.backfill_meta_lead_ids [--apply] [--stamp-only] [--limit N]
"""

import argparse
import asyncio

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db.models import DedupeEvent
from app.db.repository import Repository, meta_dedupe_key
from app.db.session import build_engine, build_sessionmaker
from app.services.freshsales_client import DuplicateContactError, FreshsalesClient
from app.services.meta_graph import MetaGraphClient
from app.webhooks.meta_lead_capture import build_freshsales_contact_fields, extract_meta_fields

PREFIX = "meta_lead_capture:"


async def main(apply: bool, limit: int | None, stamp_only: bool) -> None:
    settings = get_settings()
    engine = build_engine(settings)
    sessionmaker = build_sessionmaker(engine)
    counts = {"matched": 0, "created": 0, "no_contact_data": 0, "skipped": 0, "error": 0}
    async with httpx.AsyncClient() as http, sessionmaker() as session:
        repo = Repository(session)
        graph = MetaGraphClient(settings, http)
        freshsales = FreshsalesClient(settings, http)
        result = await session.execute(
            select(DedupeEvent.dedupe_key)
            .where(DedupeEvent.status.in_(("failed", "processing")), DedupeEvent.dedupe_key.like(f"{PREFIX}%"))
            .order_by(DedupeEvent.created_at)
        )
        keys = list(result.scalars())[:limit]
        print(f"{len(keys)} unresolved leads ({'APPLY' if apply else 'DRY RUN'})")
        for key in keys:
            leadgen_id = key.removeprefix(PREFIX)
            try:
                fields = extract_meta_fields(await graph.fetch_lead(leadgen_id))
                email = fields.get("email")
                phone = fields.get("phone_number") or fields.get("phone")
                if not email and not phone:
                    counts["no_contact_data"] += 1
                    print(f"{leadgen_id}: SKIP no email/phone")
                    continue
                contact = await freshsales.find_contact_by_email_or_phone(email=email, phone=phone)
                if contact is not None:
                    record_id, action = str(contact.get("id", "")), "stamp existing contact"
                else:
                    if stamp_only:
                        counts["skipped"] += 1
                        print(f"{leadgen_id}: SKIP no existing contact (--stamp-only)")
                        continue
                    record_id, action = "", "create contact"
                print(f"{leadgen_id}: {action} {record_id}".rstrip())
                if not apply:
                    counts["matched" if contact else "created"] += 1
                    continue
                if contact is not None:
                    await freshsales.update_meta_lead_id(record_id, leadgen_id)
                    counts["matched"] += 1
                else:
                    try:
                        created = await freshsales.create_contact(build_freshsales_contact_fields(fields, settings), leadgen_id)
                        record_id = str(created.get("id", ""))
                    except DuplicateContactError as exc:
                        record_id = exc.object_id
                        await freshsales.update_meta_lead_id(record_id, leadgen_id)
                    counts["created"] += 1
                if not record_id:
                    raise RuntimeError("no record id")
                await repo.save_lead_state(leadgen_id, record_id, record_type="contact")
                await repo.mark_dedupe_success(meta_dedupe_key(leadgen_id))
            except Exception as exc:
                counts["error"] += 1
                detail = ""
                if isinstance(exc, httpx.HTTPStatusError):
                    detail = f" {exc.request.url.host} {exc.response.status_code} {exc.response.text[:300]}"
                print(f"{leadgen_id}: ERROR {type(exc).__name__}{detail}")
                await session.rollback()
    print(counts)
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--stamp-only", action="store_true", help="only stamp existing contacts, never create")
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.limit, args.stamp_only))
