# How this profile works

`README.md` contains nothing but a `<picture>` element pointing at two SVGs in
this repo. GitHub picks `dark_mode.svg` or `light_mode.svg` depending on the
visitor's theme. A scheduled Action regenerates both every day, so the stats on
the card stay current without you touching anything.

```
profile.json  ──┐
                ├──►  generate_svgs.py  ──►  light_mode.svg + dark_mode.svg
GitHub GraphQL ─┘
```

## One-time setup

1. **Create a token.** GitHub → Settings → Developer settings → Personal access
   tokens → **Tokens (classic)** → Generate new token. Tick `repo` and
   `read:user`. Copy it.
2. **Add it as a secret.** This repo → Settings → Secrets and variables →
   Actions → New repository secret. Name it `ACCESS_TOKEN`, paste the token.
3. **Run it once.** Actions tab → *Update profile card* → Run workflow.

Step 1–2 is optional. Without `ACCESS_TOKEN` the workflow falls back to the
built-in `GITHUB_TOKEN`, which still works but only counts **public** activity —
private commits and private repo lines of code won't show up.

## Changing what the card says

Everything editable lives in `profile.json`. Rows are `[label, value]` pairs;
`[]` renders a blank line. Push the change and the Action rebuilds the SVGs.

These placeholders are filled in with live data:

| Placeholder | Meaning |
| --- | --- |
| `{{uptime}}` | How long the account has existed, e.g. `2 years, 2 months, 21 days` |
| `{{account_created}}` | Account creation date |
| `{{repos}}` / `{{contributed}}` | Own non-fork repos / repos contributed to |
| `{{commits}}` | Commit contributions across every active year |
| `{{stars}}` / `{{forks}}` | Stars and forks summed across own repos |
| `{{followers}}` | Follower count |
| `{{loc_add}}` / `{{loc_del}}` / `{{loc_total}}` | Lines added, deleted, and net |

Inline colour markup works in values and in the ASCII art:

| Markup | Result |
| --- | --- |
| `[+text]` | green |
| `[-text]` | red |
| `[*text]` | accent colour |
| `[~text]` | accent colour, blinking |

The canvas measures its own contents, so long values widen the card rather than
getting clipped. The ASCII art is a plain list of strings — swap in your own,
just keep every line the same character width.

## Previewing locally

```bash
pip install -r requirements.txt
ACCESS_TOKEN=<your-pat> python3 generate_svgs.py
```

Without a token it renders from `stats_cache.json` instead of hitting the API,
which is handy for iterating on layout.

## Notes

- `stats_cache.json` is committed on purpose. It stores the newest commit SHA
  seen per repo, so each run only fetches commits added since the last one
  instead of re-walking your whole history.
- The blinking cursor is a CSS animation inside the SVG. It's disabled
  automatically for visitors with `prefers-reduced-motion`.
- Inspired by [Andrew6rant's profile](https://github.com/Andrew6rant/Andrew6rant).
