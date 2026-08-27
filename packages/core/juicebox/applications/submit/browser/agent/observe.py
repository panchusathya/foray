"""Bounded page observation: screenshot plus a Juicebox-issued control inventory.

Control ids are ours. The planner may only refer to ids from the current
observation; it never invents a selector.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

PAGE_APPLICATION = "application"
PAGE_RECEIPT = "receipt"

_ENUMERATE_JS = """() => {
  const ATTR = 'data-juicebox-id';
  const seen = new Set();
  const nodes = Array.from(document.querySelectorAll(
    'input, textarea, select, button, a[href], [role="button"], [role="checkbox"], [role="radio"]'
  ));
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
    const type = (el.getAttribute('type') || tag || 'text').toLowerCase();
    const role = (el.getAttribute('role') || (type === 'submit' ? 'submit' : tag)).toLowerCase();
    out.push({
      id,
      label: labelFor(el),
      fieldType: type,
      role,
      name: el.getAttribute('name') || '',
      required: !!(el.required || el.getAttribute('aria-required') === 'true'),
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

    @property
    def identity(self) -> str:
        return " ".join(part for part in (self.id, self.name, self.label, self.role, self.field_type) if part)


@dataclass
class Observation:
    url: str = ""
    title: str = ""
    body_text: str = ""
    screenshot: bytes = b""
    controls: tuple[Control, ...] = ()

    def fingerprint(self) -> str:
        material = f"{self.url}|{self.title}|{','.join(c.id + ':' + c.field_type for c in self.controls)}"
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def for_planner(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "controls": [
                {
                    "id": c.id,
                    "label": c.label,
                    "field_type": c.field_type,
                    "role": c.role,
                    "required": c.required,
                    "options": list(c.options)[:12],
                }
                for c in self.controls
            ],
        }


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
        screenshot = page.screenshot(full_page=True) or b""
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
            )
        )
    return Observation(
        url=url,
        title=title,
        body_text=body,
        screenshot=screenshot,
        controls=tuple(controls),
    )


def locator(page, control_id: str):
    return page.locator(f'[data-juicebox-id="{control_id}"]').first
