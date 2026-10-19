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
- **Default ZIP / Utility**: The rate engine opens with default ZIP 10118 / ConEd (NY) originating from the vendor API geolocation lookup (`getPricesByGeo` in `EnergyWidget/app.a284fb4734e1dbc1f531.1726825162760.js:1`) when no initial ZIP parameter is passed. The vendor embed provides no page-side script `data-*` attribute or config setting to override this default without client input.

## Engineering Rules

- **CRITICAL**: Never paste fenced code blocks (e.g. ` ``` ` or ` ```html `) into HTML files. Markdown fences break HTML parsing, disrupt layout containers, and disable client-side navigation scripts.
