# Pcnaid Energy

Static website for Pcnaid Energy Services (Texas PUC Broker BR220207).

## Pages

- `/` (`index.html`): Main landing page featuring value proposition, energy procurement process, deregulation map, customer testimonials, credentials, and contact info.
- `/compare` (`compare.html`): Rate comparison and shopping widget page featuring the live MyServiceCloud rate engine. Cloudflare Pages natively serves `/compare` directly from `compare.html` via extensionless routing without a `_redirects` file (avoiding redirect loops).
- `/404.html`: 404 error page. Cloudflare Pages serves `404.html` natively for unknown paths.

## Energy Widget

- **Location**: The live rate engine widget lives exclusively on the `/compare` page (`compare.html`).
- **Mounting**: The widget mounts within `<main>` inside `#widgetMount`. Modals, dropdowns, and overlays are scoped and constrained within `<main>` between the sticky header and the footer.
- **Header & Footer**: Both `/` and `/compare` share identical header navigation (desktop and mobile drawer with toggle) and footer blocks. All rate-shopping CTAs across the site route directly to `/compare`.
- **Styling Notes**: The drawer CTA reuses the header button styles.
- **Default ZIP / Utility**: The rate engine opens with default ZIP 10118 / ConEd (NY) originating from the vendor API geolocation lookup (`getPricesByGeo` in `EnergyWidget/app.a284fb4734e1dbc1f531.1726825162760.js:1`) when no initial ZIP parameter is passed. The vendor embed provides no page-side script `data-*` attribute or config setting to override this default without client input.

## Engineering Rules

- **CRITICAL**: Never paste fenced code blocks (e.g. ` ``` ` or ` ```html `) into HTML files. Markdown fences break HTML parsing, disrupt layout containers, and disable client-side navigation scripts.

## Visual gate

The `Energy visual gate` workflow runs on every pull request, every push to `main`, and manual dispatches. Playwright serves the repository with `python3 -m http.server` inside the pinned official Playwright container, captures every route, viewport, and state in `ui-states.json`, and compares each viewport image with a committed baseline at a maximum differing-pixel ratio of 0.001. The rate widget uses an intercepted fixed fixture, animations are disabled, and the time-based footer year is masked. Captured images and Playwright diff, actual, and expected images are uploaded as workflow artifacts.

After the pixel comparison, two independent gates inspect every captured PNG. GitHub Models runs `openai/gpt-4.1` once per image, and Claude runs the repository's `.claude/agents/design-audit.md` agent over the full image set. Either a failed per-control check, an audit request error, or a Claude MUST finding fails the workflow. Production deploys happen only through this workflow, from a successful push to `main` after the pixel and both vision gates pass.

The workflow installs JavaScript dependencies with `npm ci`; a committed `package-lock.json` is therefore required.

### Refreshing baselines

1. Run the workflow manually with the boolean `update_baselines` input enabled.
2. Download the `baselines` artifact from that workflow run.
3. Inspect every generated image. The baseline diff is the review; accepting a baseline accepts that rendered UI.
4. Copy the reviewed PNGs into `tests/visual/__screenshots__/`, then commit them in a pull request with the related UI change.

For the first-run bootstrap before manual dispatch is available, a run with no committed baselines captures in update mode, uploads candidate PNGs as the `baselines` artifact for review, and then fails; CI never commits baselines.

### `ui-states.json`

- `viewports` maps a viewport name to `width`, `height`, `deviceScaleFactor`, `isMobile`, and `hasTouch` Playwright context values.
- `mask` lists selectors for animated or time-based regions that must not affect screenshots.
- `routes` is a list of route objects. Each route has a filesystem-safe `id`, a served `path`, and `states` grouped by viewport name.
- Each state has a unique `name`, an `actions` list, and a `pairs` list. Supported actions are `click`, `hover`, `focus`, and `fill`; every action has a `selector`, `fill` also has a `value`, and `waitFor` can name the selector that must become visible after the action.
- A state may set `clip` to a selector when a deliberately clipped capture is required. States without `clip` capture the viewport.
- Each pair names sibling controls and records their real selectors so both vision passes can compare text colour, font family, font size, and alignment.

Captured and baseline filenames use `<route-id>-<viewport>-<state>.png`.

### Required secrets

- `CLAUDE_CODE_OAUTH_TOKEN`: authenticates the Claude design audit.
- `CLOUDFLARE_API_TOKEN`: authorizes the production Cloudflare Pages deployment.
- `CLOUDFLARE_ACCOUNT_ID`: selects the Cloudflare account that owns the `energy` Pages project.

`GITHUB_TOKEN` is provided by GitHub Actions and is scoped in the workflow for pull-request comments, artifact access, and GitHub Models inference.
