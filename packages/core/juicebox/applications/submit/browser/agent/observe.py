"""Bounded page observation: screenshot plus a Juicebox-issued control inventory.

Control ids are ours. The planner may only refer to ids from the current
observation; it never invents a selector.
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

PAGE_APPLICATION = "application"
PAGE_RECEIPT = "receipt"

#: Planner payload bounds. The screenshot is visual context, not the source of
#: truth (the control inventory is), so it can be small; and a large wizard
#: page must not grow the text payload without limit. Oversized payloads came
#: back as empty model responses on the bigger Workday wizard steps while the
#: same planner answered 40 calls in a row on the small ones (live, Intapp
#: 2026-08-27) -- size, not provider.
_SCREENSHOT_MAX_DIM = 1024
_PLANNER_MAX_CONTROLS = 60
_PLANNER_LABEL_CHARS = 120
_PLANNER_VALUE_CHARS = 60
_PLANNER_OPTION_CHARS = 60
_PLANNER_MAX_OPTIONS = 12

_ENUMERATE_JS = """() => {
  const ATTR = 'data-juicebox-id';
  const seen = new Set();
  // role=option / promptOption: Workday has no native selects -- every
  // dropdown is a button that opens a listbox of option rows, and a list the
  // inventory cannot see is a question the planner cannot answer (live,
  // Intapp 2026-08-27: the source multiselect opened and the run died).
  const nodes = Array.from(document.querySelectorAll(
    'input, textarea, select, button, a[href], [role="button"], [role="checkbox"], [role="radio"], ' +
    '[role="option"], [data-automation-id="promptOption"]'
  ));
  function isOption(el) {
    return el.getAttribute('role') === 'option'
      || (el.getAttribute('data-automation-id') || '') === 'promptOption';
  }
  function labelFor(el) {
    const wrap = el.closest('label');
    if (wrap && wrap.innerText) return wrap.innerText.trim().slice(0, 160);
    if (el.id) {
      const byFor = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (byFor) return (byFor.innerText || '').trim().slice(0, 160);
    }
    return (el.getAttribute('aria-label') || el.getAttribute('placeholder')
      || el.getAttribute('name') || el.innerText || '').trim().slice(0, 160);
  }
  function visible(el) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  }
  const out = [];
  for (const el of nodes) {
    if (!visible(el)) continue;
    if (el.type === 'hidden') continue;
    let id = el.getAttribute(ATTR);
    if (!id) {
      id = 'c' + String(out.length + 1).padStart(3, '0');
      el.setAttribute(ATTR, id);
    }
    if (seen.has(id)) continue;
    seen.add(id);
    const tag = (el.tagName || '').toLowerCase();
    const type = isOption(el) ? 'option' : (el.getAttribute('type') || tag || 'text').toLowerCase();
    const role = isOption(el) ? 'option'
      : (el.getAttribute('role') || (type === 'submit' ? 'submit' : tag)).toLowerCase();
    out.push({
      id,
      label: labelFor(el),
      fieldType: type,
      role,
      name: el.getAttribute('name') || '',
      required: !!(el.required || el.getAttribute('aria-required') === 'true'),
      value: tag === 'input' && (type === 'password' || type === 'file')
        ? (el.value ? '<set>' : '')
        : String(el.value || '').slice(0, 80),
      checked: !!el.checked,
      options: tag === 'select'
        ? Array.from(el.options || []).map(o => (o.text || o.value || '').trim()).filter(Boolean).slice(0, 30)
        : [],
    });
    if (out.length >= 80) break;
  }
  return out;
}"""


@dataclass(frozen=True)
class Control:
    id: str
    label: str = ""
    field_type: str = "text"
    role: str = ""
    name: str = ""
    required: bool = False
    options: tuple[str, ...] = ()
    # Current state, so the planner can see what is already done. Without it
    # a live run re-filled the Name field eight times and submitted with the
    # required choice questions untouched (Forerunner, 2026-08-27). Password
    # and file values are never carried — only whether one is set.
    value: str = ""
    checked: bool = False

    @property
    def identity(self) -> str:
        return " ".join(part for part in (self.id, self.name, self.label, self.role, self.field_type) if part)


_ERRORS_JS = """() => {
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('[role="alert"], [data-automation-id*="rror"]')) {
    const s = window.getComputedStyle(el);
    const box = el.getBoundingClientRect();
    if (s.display === 'none' || s.visibility === 'hidden' || box.width === 0) continue;
    const text = (el.innerText || '').trim().slice(0, 200);
    if (!text || seen.has(text)) continue;
    seen.add(text);
    out.push(text);
    if (out.length >= 8) break;
  }
  return out;
}"""


@dataclass
class Observation:
    url: str = ""
    title: str = ""
    body_text: str = ""
    screenshot: bytes = b""
    controls: tuple[Control, ...] = ()
    # The page's own visible validation messages. Without them the planner
    # clicked Save and Continue thirty times into a step Workday was refusing
    # to advance, blind to the error banner saying which fields were the
    # reason (live, Intapp 2026-08-27).
    errors: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        material = f"{self.url}|{self.title}|{','.join(c.id + ':' + c.field_type for c in self.controls)}"
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def for_planner(self) -> dict:
        controls = self.controls
        if len(controls) > _PLANNER_MAX_CONTROLS:
            # Fields first: dropping the tail of a long page must not drop the
            # inputs still to fill in favour of yet more chrome links.
            chrome = {"button", "submit", "reset", "image", "a", "link"}
            fields = [c for c in controls if c.field_type not in chrome]
            rest = [c for c in controls if c.field_type in chrome]
            controls = tuple((fields + rest)[:_PLANNER_MAX_CONTROLS])
        return {
            "url": self.url,
            "title": self.title,
            "errors": [e[:200] for e in self.errors[:8]],
            "controls": [
                {
                    "id": c.id,
                    "label": c.label[:_PLANNER_LABEL_CHARS],
                    "field_type": c.field_type,
                    "role": c.role,
                    "required": c.required,
                    "current_value": c.value[:_PLANNER_VALUE_CHARS],
                    "checked": c.checked,
                    "options": [
                        o[:_PLANNER_OPTION_CHARS] for o in c.options[:_PLANNER_MAX_OPTIONS]
                    ],
                }
                for c in controls
            ],
        }


def _downscale(png: bytes) -> bytes:
    """Bound the planner screenshot to _SCREENSHOT_MAX_DIM on its long side.

    Best effort: no Pillow, or a bytes blob Pillow cannot read, means the
    original image -- a full-size screenshot degrades the call, a raised
    exception would kill the observation.
    """
    if not png:
        return png
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png)) as img:
            width, height = img.size
            longest = max(width, height)
            if longest <= _SCREENSHOT_MAX_DIM:
                return png
            scale = _SCREENSHOT_MAX_DIM / longest
            resized = img.convert("RGB").resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                Image.LANCZOS,
            )
            out = io.BytesIO()
            resized.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("screenshot downscale failed, sending original: %s", str(exc)[:200])
        return png


def observe(page) -> Observation:
    """Read the current page. Never raises; a dead page is an empty observation."""
    url = title = body = ""
    screenshot = b""
    raw: list = []
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001
        url = ""
    try:
        title = page.title() or ""
    except Exception:  # noqa: BLE001
        title = ""
    try:
        body = (page.inner_text("body") or "")[:4000]
    except Exception:  # noqa: BLE001
        body = ""
    try:
        raw = page.evaluate(_ENUMERATE_JS) or []
    except Exception:  # noqa: BLE001
        raw = []
    try:
        errors = tuple(str(e) for e in (page.evaluate(_ERRORS_JS) or []) if str(e).strip())
    except Exception:  # noqa: BLE001
        errors = ()
    try:
        # Viewport, not full_page: a long signed-in wizard produced a
        # screenshot so tall the planner call returned an empty response every
        # time (live, Intapp 2026-08-27), while a short page worked. The
        # control inventory below is enumerated from the whole DOM regardless
        # of what is scrolled into view, so completeness does not depend on
        # the image -- it is visual context, and a bounded viewport is enough.
        screenshot = _downscale(page.screenshot(full_page=False) or b"")
    except Exception:  # noqa: BLE001
        screenshot = b""
    controls = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "").strip()
        if not cid:
            continue
        controls.append(
            Control(
                id=cid,
                label=str(item.get("label") or "").strip(),
                field_type=str(item.get("fieldType") or "text").strip().lower() or "text",
                role=str(item.get("role") or "").strip().lower(),
                name=str(item.get("name") or "").strip(),
                required=bool(item.get("required")),
                options=tuple(str(o) for o in (item.get("options") or []) if o),
                value=str(item.get("value") or ""),
                checked=bool(item.get("checked")),
            )
        )
    return Observation(
        url=url,
        title=title,
        body_text=body,
        screenshot=screenshot,
        controls=tuple(controls),
        errors=errors,
    )


def locator(page, control_id: str):
    return page.locator(f'[data-juicebox-id="{control_id}"]').first
