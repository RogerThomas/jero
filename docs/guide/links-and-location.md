# Links & Location

!!! note "Not built yet — design preview"

    First-class `Location` (RFC 9110) and `Link` (RFC 8288) on responses are **designed
    but not yet implemented**. This page describes the intended shape so you can see where
    it's going; the API below isn't available on the current release. The full design lives
    in `plans/links-and-location.md`.

## The idea

A response often needs to point at *another* route — `Location` on a `201 Created`, a
redirect target, or the status URL on a `202 Accepted` async job. Because the path now
lives **on the class**, jero can reverse-route to a mounted operation from the class
itself rather than making you hand-format URLs.

```python
# Planned API — not available yet.
JSONResponse(
    json=widget,
    status_code=201,
    location=Location.from_operation(WidgetResource.read_one, params=WidgetPath(widget_id=widget.id)),
)
```

`Link` is the same idea plus a required `rel`, and a list of them joins into one `Link`
header.

## Reversal handles

- **`from_operation(Class.operation, params=…)`** — the blessed, fully typed form. The
  method reference *is* the operation, so it carries both the class (→ its `path`) and the
  operation (→ item vs collection shape). The wrong `params` Struct is caught **at
  construction**, not at request time.
- **`from_url(url)`** — a literal URL (relative or absolute), no reversal.
- **`from_ref("ref.operation", params=…)`** — a string escape hatch for genuine circular
  imports between feature modules, where importing the class would form an import cycle.
  Opt in per class with `ref="…"`; prefer `from_operation` everywhere else.

URLs are **relative by default** (`Location: /widgets/widget-id`), with absolute output
opt-in.

This section will be filled in with complete, runnable examples once the feature lands.
