"""Structured extraction: the current page as data shaped by a JSON Schema, without a model.

    browser.extract  schema: {type: object, properties: {order_id: {type: string}, total: {type: number},
                              items: {type: array, items: {type: object, properties: {name: …, qty: …}}}},
                              required: [order_id, total]}

A fixed, read-only script (:data:`STRUCTURE_JS`) collects what a page states explicitly:

- **pairs**: a label and its value: ``<label>`` + its control, ``<dt>``/``<dd>``, a table row whose
  first cell is a header, ``aria-label`` on an output, and ``Label: value`` lines of text;
- **tables**: header cells and rows;
- **lists**: the items of ``<ul>``/``<ol>`` with the heading or label before them.

:func:`extract` maps schema properties to those by their *terms* (the property name — snake_case
or camelCase — or its ``title``, stemmed, with the same synonyms as trajectory search), converts
values to the declared type, and reports what it could not do. It never guesses: a property that
two different values match equally is *ambiguous*, a value that is not a number for
``type: number`` is a problem, and a missing ``required`` property fails the action.

Never read: password, card and one-time-code fields (the script skips them), hidden inputs.
The result still goes through the executor's redaction like any action output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from highhx.trajectories.search import terms

MAX_PROPERTIES = 100
MAX_ROWS = 500
SUPPORTED = ("string", "number", "integer", "boolean", "array", "object")

STRUCTURE_JS = r"""
(() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 500);
  const secret = el => {
    const t = (el.getAttribute('type') || '').toLowerCase();
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    return t === 'password' || t === 'hidden' || /cc-|one-time-code|password/.test(ac);
  };
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const value = el => {
    if (secret(el)) return null;
    if (el.type === 'checkbox' || el.type === 'radio') return el.checked ? 'true' : 'false';
    if (el.tagName === 'SELECT') return clean(el.selectedOptions[0] ? el.selectedOptions[0].textContent : '');
    if ('value' in el && el.tagName !== 'BUTTON' && el.tagName !== 'LI') return clean(el.value);
    return clean(el.innerText || el.textContent);
  };
  const pairs = [];
  const add = (label, v, source) => { label = clean(label).replace(/[:\uFF1A]\s*$/, ''); if (label && v !== null && v !== undefined) pairs.push([label, v, source]); };
  document.querySelectorAll('label').forEach(l => {
    const c = l.control || (l.htmlFor && document.getElementById(l.htmlFor));
    if (c && visible(c)) add(l.innerText, value(c), 'label');
  });
  document.querySelectorAll('input[aria-label],textarea[aria-label],select[aria-label],output[aria-label],[role=status][aria-label]').forEach(el => {
    if (visible(el)) add(el.getAttribute('aria-label'), value(el), 'aria');
  });
  document.querySelectorAll('dl').forEach(dl => {
    let label = null;
    for (const el of dl.children) {
      if (el.tagName === 'DT') label = el.innerText;
      else if (el.tagName === 'DD' && label !== null) { add(label, clean(el.innerText), 'dl'); label = null; }
    }
  });
  const tables = [];
  document.querySelectorAll('table').forEach(t => {
    if (!visible(t)) return;
    const rows = Array.from(t.rows);
    const head = t.tHead && t.tHead.rows.length ? t.tHead.rows[0] : (rows.length && Array.from(rows[0].cells).every(c => c.tagName === 'TH') ? rows[0] : null);
    const body = rows.filter(r => r !== head && !(t.tHead && t.tHead.contains(r)));
    if (head) {
      tables.push({caption: clean(t.caption ? t.caption.innerText : (t.getAttribute('aria-label') || '')),
        headers: Array.from(head.cells).map(c => clean(c.innerText)),
        rows: body.slice(0, 500).map(r => Array.from(r.cells).map(c => clean(c.innerText)))});
    }
    body.forEach(r => { const c = r.cells; if (c.length === 2 && c[0].tagName === 'TH') add(c[0].innerText, clean(c[1].innerText), 'table-row'); });
  });
  const lists = [];
  document.querySelectorAll('ul,ol').forEach(list => {
    if (!visible(list) || list.closest('nav')) return;
    let label = list.getAttribute('aria-label') || '';
    for (let p = list.previousElementSibling; !label && p; p = p.previousElementSibling) {
      if (/^H[1-6]$/.test(p.tagName) || p.tagName === 'P' || p.tagName === 'LABEL') label = p.innerText;
    }
    const items = Array.from(list.children).filter(li => li.tagName === 'LI').map(li => clean(li.innerText)).filter(Boolean);
    if (items.length) lists.push({label: clean(label), items: items.slice(0, 500)});
  });
  const text = (document.body ? document.body.innerText : '').split('\n');
  for (const line of text.slice(0, 5000)) {
    const m = /^\s*([^:\uFF1A]{1,60})[:\uFF1A]\s+(.{1,300})$/.exec(line);
    if (m) add(m[1], clean(m[2]), 'text');
  }
  return {url: location.href, title: document.title, pairs, tables, lists};
})()
"""


def _name_terms(name: str, spec: dict[str, Any]) -> frozenset[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ").replace("-", " ")
    words = set(terms(spaced))
    title = spec.get("title")
    if isinstance(title, str) and title.strip():
        words |= set(terms(title))
    return frozenset(words)


def check_schema(schema: Any, path: str = "schema") -> list[str]:
    """Problems with a schema this extractor cannot honour (it refuses rather than ignores)."""
    if not isinstance(schema, dict):
        return [f"{path}: must be an object"]
    kind = schema.get("type")
    if kind not in SUPPORTED:
        return [f"{path}: type must be one of {', '.join(SUPPORTED)}"]
    problems: list[str] = []
    if kind == "object":
        props = schema.get("properties")
        if not isinstance(props, dict) or not props:
            return [f"{path}: an object needs properties"]
        if len(props) > MAX_PROPERTIES:
            problems.append(f"{path}: at most {MAX_PROPERTIES} properties")
        for name, sub in props.items():
            problems += check_schema(sub, f"{path}.{name}")
        required = schema.get("required") or []
        if not isinstance(required, list) or any(r not in props for r in required):
            problems.append(f"{path}: required must list properties of this object")
    elif kind == "array":
        items = schema.get("items")
        if not isinstance(items, dict):
            return [f"{path}: an array needs items"]
        if items.get("type") == "array":
            problems.append(f"{path}: arrays of arrays are not supported")
        else:
            problems += check_schema(items, f"{path}[]")
    return problems


_NUMBER = re.compile(r"[-+]?\d[\d,  ']*(?:\.\d+)?|[-+]?\.\d+")
_TRUE = {"true", "yes", "on", "checked", "enabled", "1", "✓"}
_FALSE = {"false", "no", "off", "unchecked", "disabled", "0", "✗"}


def convert(value: str, kind: str) -> tuple[Any, str]:
    """``value`` as ``kind``, or ``(None, problem)``."""
    text = value.strip()
    if kind == "string":
        return text, ""
    if kind in ("number", "integer"):
        found = _NUMBER.findall(text)
        if len(found) != 1:
            return None, f"{text[:40]!r} is not a single number"
        number = float(re.sub(r"[,  ']", "", found[0]))
        if kind == "integer":
            if not number.is_integer():
                return None, f"{text[:40]!r} is not a whole number"
            return int(number), ""
        return number, ""
    if kind == "boolean":
        if text.lower() in _TRUE:
            return True, ""
        if text.lower() in _FALSE:
            return False, ""
        return None, f"{text[:40]!r} is not yes/no"
    return None, f"cannot convert to {kind}"


@dataclass
class Extraction:
    data: dict[str, Any]
    problems: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    """Where each value came from (label, dl, table-row, aria, text, table, list)."""

    @property
    def ok(self) -> bool:
        return not self.missing and not self.problems


def _best(
    want: frozenset[str], candidates: list[tuple[frozenset[str], Any, str]]
) -> tuple[list[tuple[Any, str]], float]:
    """Candidates whose label covers the wanted terms best (all wanted terms, fewest extra)."""
    if not want:
        return [], 0.0
    scored: list[tuple[float, Any, str]] = []
    for label, value, source in candidates:
        if not label:
            continue
        shared = len(want & label)
        if shared < len(want):
            continue  # every term of the property must be in the label
        scored.append((shared / len(want | label), value, source))
    if not scored:
        return [], 0.0
    top = max(s for s, _, _ in scored)
    return [(v, src) for s, v, src in scored if s == top], top


def _object_from_row(
    item: dict[str, Any], headers: list[frozenset[str]], row: list[str]
) -> tuple[dict[str, Any], list[str]]:
    out: dict[str, Any] = {}
    problems: list[str] = []
    for name, sub in item["properties"].items():
        want = _name_terms(name, sub)
        matches = [i for i, h in enumerate(headers) if want and want <= h]
        if not matches or matches[0] >= len(row):
            continue
        value, problem = convert(row[matches[0]], str(sub.get("type")))
        if problem:
            problems.append(f"{name}: {problem}")
        else:
            out[name] = value
    return out, problems


def extract(schema: dict[str, Any], page: dict[str, Any]) -> Extraction:
    """Map ``page`` (what :data:`STRUCTURE_JS` returned) onto ``schema`` (an object schema)."""
    result = Extraction({})
    pairs = [(frozenset(terms(str(p[0]))), str(p[1]), str(p[2]) if len(p) > 2 else "") for p in page.get("pairs") or []]
    tables = [t for t in page.get("tables") or [] if t.get("headers")]
    lists = page.get("lists") or []
    for name, sub in schema["properties"].items():
        kind = str(sub.get("type"))
        want = _name_terms(name, sub)
        if kind == "array" and sub["items"].get("type") == "object":
            columns = [_name_terms(n, s) for n, s in sub["items"]["properties"].items()]
            best = None
            for table in tables:
                headers = [frozenset(terms(h)) for h in table["headers"]]
                covered = sum(1 for c in columns if c and any(c <= h for h in headers))
                caption = frozenset(terms(str(table.get("caption") or "")))
                score = (covered, len(want & caption))
                if covered and (best is None or score > best[0]):
                    best = (score, headers, table)
            if best is None:
                continue
            rows: list[dict[str, Any]] = []
            for row in best[2]["rows"][:MAX_ROWS]:
                obj, problems = _object_from_row(sub["items"], best[1], row)
                result.problems += [f"{name}[{len(rows)}].{p}" for p in problems]
                if obj:
                    rows.append(obj)
            result.data[name], result.sources[name] = rows, "table"
            continue
        if kind == "array":
            item_kind = str(sub["items"].get("type"))
            found = [(frozenset(terms(str(lst.get("label") or ""))), lst["items"], "list") for lst in lists]
            for table in tables:  # a column whose header names the property
                for index, header in enumerate(table["headers"]):
                    column = [r[index] for r in table["rows"] if index < len(r)]
                    found.append((frozenset(terms(header)), column, "table"))
            matches, _ = _best(want, found)
            distinct = {tuple(v) for v, _ in matches}
            if len(distinct) > 1:
                result.problems.append(f"{name}: ambiguous ({len(distinct)} lists match)")
                continue
            if matches:
                values = []
                for raw in matches[0][0][:MAX_ROWS]:
                    value, problem = convert(str(raw), item_kind)
                    if problem:
                        result.problems.append(f"{name}: {problem}")
                    else:
                        values.append(value)
                result.data[name], result.sources[name] = values, matches[0][1]
            continue
        if kind == "object":
            nested = extract(sub, page)
            result.problems += [f"{name}.{p}" for p in nested.problems]
            result.missing += [f"{name}.{m}" for m in nested.missing]
            if nested.data:
                result.data[name] = nested.data
            continue
        matches, _ = _best(want, pairs)
        distinct_values = {v.strip() for v, _ in matches}
        if len(distinct_values) > 1:
            result.problems.append(
                f"{name}: ambiguous ({', '.join(sorted(repr(v[:30]) for v in distinct_values)[:3])})"
            )
            continue
        if not matches:
            continue
        value, problem = convert(matches[0][0], kind)
        if problem:
            result.problems.append(f"{name}: {problem}")
            continue
        result.data[name], result.sources[name] = value, matches[0][1]
    for name in schema.get("required") or []:
        if name not in result.data and not any(
            p.startswith(f"{name}:") or p.startswith(f"{name}.") for p in result.problems
        ):
            result.missing.append(name)
    return result
