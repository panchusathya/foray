# Juicebox intake chat widget

Embeddable web chat for candidate intake. Produces a single JavaScript bundle
for marketing sites and career pages.

## Build

```bash
npm install
npm run build
```

Output: `dist/juicebox-intake-widget.js`

## Embed

Recruiter mints a token from the authenticated API (`GET /v1/intake/widget-token`),
then embeds:

```html
<div id="juicebox-intake-widget"></div>
<script
  src="https://your-site.example/juicebox-intake-widget.js"
  data-token="org_abc.<signature>"
  data-base-url="/"
  data-target="juicebox-intake-widget"
></script>
```

Or initialize programmatically:

```html
<div id="chat"></div>
<script src="/juicebox-intake-widget.js"></script>
<script>
  JuiceboxIntakeWidget.init({
    token: 'org_abc.<signature>',
    baseUrl: '/',
    mount: '#chat',
  });
</script>
```

The widget mints a fresh `client_message_id` per send, persists it in
`sessionStorage`, and reuses it on retry so duplicate posts are idempotent.
