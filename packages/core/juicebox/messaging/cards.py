"""Getting a rendered job card onto a thread.

Three questions, and each has one answer that matters:

**Should this thread get an image at all?** Only when Linq has told us the
thread is on iMessage or RCS. An image over SMS is an MMS -- carrier cost,
recompression, and some carriers strip it -- and ``messaging/uploads.py``
already records the decision not to push media through a carrier. An unknown
protocol falls back to text, deliberately: never having been told is not
evidence of iMessage.

**Has this card been rendered before?** A role proposed to thirty candidates is
one card. ``JobCardAsset`` holds the PNG and Linq's attachment id, keyed by
posting and invalidated by ``card_version`` -- so a renderer change reaches a
posting somebody has already been shown, which a plain cache would not.

**What happens when any of it fails?** The text card goes out on its own,
exactly as it did before there were images. Nothing in here may take down a
turn: a role the candidate never hears about is a far worse outcome than a role
without a picture, so every failure path in this module returns "no media"
rather than raising.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..models import JobCardAsset, JobPosting, utcnow
from ..presentation import palette as palettemod
from ..presentation.cardimage import CARD_VERSION, render_card_png
from ..presentation.logo import fetch_logo, posting_domain
from .linq import RICH_SERVICES, LinqSendError, OutboundMedia, upload_attachment

logger = logging.getLogger(__name__)

VARIANT_CARD = "card"
CONTENT_TYPE = "image/png"


def asset_version() -> str:
    """The cache key. Both halves, because either invalidates a card.

    A renderer change and a palette-rules change both alter the pixels, and one
    combined string is harder to forget than two columns somebody has to
    remember to compare separately.
    """
    return f"{CARD_VERSION}+{palettemod.PALETTE_VERSION}"


def wants_image(thread) -> bool:
    """Whether an image will render on this thread rather than become an MMS."""
    return (thread.last_service or "") in RICH_SERVICES


def card_media(
    db: Session, *, thread, posting: JobPosting, card: dict[str, Any], index: int = 1, total: int = 1
) -> tuple[OutboundMedia, ...]:
    """The card image for this posting, as a sendable media part.

    Empty is the ordinary answer, not a failure: an SMS thread, a logo we
    cannot fetch, a Linq upload that refused. The caller sends the text card
    either way.

    The ordinal ("2 of 3") is baked into the pixels now that the image *is*
    the card on rich threads -- with no text twin there is nowhere else for
    the reply grammar ("apply to 2") to point. That keys the cache by position
    as well as posting, which is bounded: MAX_PROPOSALS_PER_MESSAGE caps a
    batch at three, so a posting renders at most a handful of variants, and a
    solo card keeps the original "card" variant so nothing already cached is
    re-rendered.
    """
    if not wants_image(thread):
        return ()
    try:
        asset = _asset_for(db, thread=thread, posting=posting, card=card, index=index, total=total)
    except Exception as exc:  # noqa: BLE001 - a card is never worth a failed turn
        logger.warning("job card image unavailable for posting %s: %s", posting.id, exc)
        return ()
    if asset is None or not asset.attachment_id:
        return ()
    return (
        OutboundMedia(
            content_type=CONTENT_TYPE,
            attachment_id=asset.attachment_id,
            filename=_filename(posting),
        ),
    )


def _variant(index: int, total: int) -> str:
    """The cache key's position half. Solo cards keep the original name."""
    return VARIANT_CARD if total <= 1 else f"{VARIANT_CARD}-{index}of{total}"


def _asset_for(
    db: Session, *, thread, posting: JobPosting, card: dict[str, Any], index: int = 1, total: int = 1
) -> JobCardAsset | None:
    version = asset_version()
    variant = _variant(index, total)
    row = db.scalar(
        select(JobCardAsset).where(
            JobCardAsset.org_id == thread.org_id,
            JobCardAsset.job_posting_id == posting.id,
            JobCardAsset.variant == variant,
        )
    )
    if (
        row is not None
        and row.card_version == version
        and row.attachment_id
        and row.storage_path
    ):
        return row

    # The employer's own mark, from the fetch that already paints contact
    # photos. `fetch_logo` returns the raw bitmap rather than `photo_for`'s
    # composed 512px gradient tile: the card draws its own white tile, and
    # compositing twice would put a gradient square inside it.
    logo = b""
    domain = posting_domain(posting)
    if domain:
        try:
            logo = fetch_logo(domain) or b""
        except Exception as exc:  # noqa: BLE001 - a missing logo is a plain card
            logger.info("logo fetch missed for %s: %s", domain, exc)
    brand = palettemod.brand_from_logo(logo, domain=domain)

    png = render_card_png(card, brand=brand, logo=logo, index=index, total=total)
    relpath = storage.card_relpath(
        org_id=thread.org_id, posting_id=posting.id, variant=variant
    )
    storage.save_bytes(relpath, png)

    try:
        attachment_id = upload_attachment(
            filename=_filename(posting), content_type=CONTENT_TYPE, data=png
        )
    except LinqSendError as exc:
        # Recorded without an attachment id so the bytes are not re-rendered on
        # every send, but the next one still retries the upload. Storing a
        # half-made id would send a message referencing nothing.
        _remember(
            db,
            row=row,
            thread=thread,
            posting=posting,
            relpath=relpath,
            version=version,
            variant=variant,
        )
        raise exc

    return _remember(
        db,
        row=row,
        thread=thread,
        posting=posting,
        relpath=relpath,
        version=version,
        variant=variant,
        attachment_id=attachment_id,
    )


def _remember(
    db: Session,
    *,
    row: JobCardAsset | None,
    thread,
    posting: JobPosting,
    relpath: str,
    version: str,
    variant: str = VARIANT_CARD,
    attachment_id: str = "",
) -> JobCardAsset:
    if row is None:
        row = JobCardAsset(
            org_id=thread.org_id, job_posting_id=posting.id, variant=variant
        )
        db.add(row)
    row.storage_path = relpath
    row.attachment_id = attachment_id
    # Blank when there is no attachment, so the row is a record of the render
    # and not a cache hit: the next send must retry the upload rather than
    # read `card_version` and conclude it has nothing to do.
    row.card_version = version if attachment_id else ""
    row.rendered_at = utcnow()
    db.flush()
    return row


def _filename(posting: JobPosting) -> str:
    company = (posting.company_normalized or posting.company or "role").strip()
    company = "-".join(company.lower().split()) or "role"
    return f"{company}-role.png"[:255]
