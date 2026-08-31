# Browser Compatibility Issues

## crypto.randomUUID() — Older Browsers

`crypto.randomUUID()` is not available in:
- Older Safari (pre-15.4)
- Older iOS (pre-15.4)
- Some Android browser versions
- Older desktop browsers

**Symptom:** `TypeError: crypto.randomUUID is not a function`

**Fix:** Use a `generateUUID()` polyfill:

```tsx
function generateUUID(): string {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}
```

### Three call patterns — correct fix strategy

| Location | Pattern | Fix |
|----------|---------|-----|
| `useEffect` (client-only) | `const id = generateUUID()` in useEffect | ✅ already correct |
| Render body / Link href | `href={\`/game/${generateUUID()}\`}` | ❌ causes hydration mismatch — see below |
| useState initializer | `const [id] = useState(crypto.randomUUID())` | ❌ broken during SSR — use empty string + useEffect |

**Critical — the "direct in render" pattern is WRONG for hrefs:** Calling `generateUUID()` directly in a Link href during render causes a Next.js hydration error: `Prop 'href' did not match. Server: "/game/UUID1" Client: "/game/UUID2"`. The server renders one UUID, the client generates another.

**Correct pattern for hrefs / render-time UUIDs:** Use `useState` + `useEffect` to generate client-only:

```tsx
// index.tsx — CORRECT pattern
const [freeUUID, setFreeUUID] = useState('')
const [challengeUUID, setChallengeUUID] = useState('')

useEffect(() => {
  setFreeUUID(generateUUID())
  setChallengeUUID(generateUUID())
}, [])

// In JSX:
<Link href={`/game/${freeUUID}?mode=free`}>Start Free Mode</Link>
<Link href={`/game/${challengeUUID}?mode=challenge`}>Start Challenge Mode</Link>
```

The old "direct in href" fix was applied in 2026-05-07 but was itself wrong — it was fixing a secure-context error while introducing a hydration mismatch. The useState+useEffect pattern solves both problems: no secure-context issue (runs only in browser) and no hydration mismatch (initial state is empty string during SSR).

### Files affected

- `src/pages/index.tsx` — Link href (render-time UUID generation)
- `src/pages/game.tsx` — useEffect redirect

### Testing

Test on: Safari 14.1, iOS 14.x, older Chrome/Firefox. BrowserStack or similar.

---

## globals.css Self-Import Corruption

**Symptom:** Next.js build fails with `Syntax error: Unknown word` at line 1 of globals.css.

**Cause:** `globals.css` contained only `import '../styles/globals.css'` — a self-referential import that corrupts the file.

**Fix:**
```bash
cp ~/staging_apps/lemonade-stand/src/styles/globals.css \
   ~/ralph/projects/lemonade-stand/src/styles/globals.css
```

**Prevention:** Never edit `globals.css` directly. If build errors appear out of nowhere on a CSS file, check for self-import corruption.

---

## Next.js Cross-Origin Dev Warning

**Symptom:** `⚠ Cross origin request detected from <lan-host> to /_next/* resource.`

**Fix (if needed):** Add to `next.config.js`:
```js
experimental: {
  allowedDevOrigins: ['<lan-host>']
}
```

Usually harmless in development — the warning says a future major version will require explicit config.
