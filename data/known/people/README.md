# known_people.json

Enrolled person identities for the person identity matcher (Phase V2-032).

## Schema

Each entry is a dict with:

- `name` (str): Human-readable name for the enrolled person
- `tag` (str): Primary identifier — the first `identity_markers` entry used for exact tag matching
- `identity_markers` (list[str]): Secondary features used for Jaccard fallback matching

## Example

```json
{
  "name": "Red Jacket Person",
  "tag": "red-jacket",
  "identity_markers": ["red-jacket", "beard", "tall"]
}
```

## Privacy

This file is **gitignored**. The repository contains an empty placeholder `[]`
only. Real enrollments live outside version control.
