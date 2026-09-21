# Vendored browser libraries

Butters Chat renders assistant Markdown in the browser. Both libraries below
are committed here rather than installed at deploy time, because the Butters
installer ships the checkout tree as-is and Chat must keep working on a host
with no Internet access. Nothing here is loaded from a CDN at runtime: the
Content-Security-Policy is `script-src 'self'`, and these files are served
from Butters' own origin through the explicit allow-list in
`butters/web/app.py`.

They are committed unmodified. Do not edit them; replace a whole file with a
new pinned release and update the digest below in the same change.

| File | Package | Version | License |
| --- | --- | --- | --- |
| `markdown-it.umd.min.js` | [markdown-it](https://github.com/markdown-it/markdown-it) | 15.0.2 | MIT (`MARKDOWN-IT-LICENSE.txt`) |
| `purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.15 | Apache-2.0 OR MPL-2.0 (`DOMPURIFY-LICENSE.txt`) |

## Provenance

Retrieved 2026-09-21 from the npm registry via jsDelivr:

```
https://cdn.jsdelivr.net/npm/markdown-it@15.0.2/dist/browser/markdown-it.umd.min.js
https://cdn.jsdelivr.net/npm/dompurify@3.4.15/dist/purify.min.js
https://cdn.jsdelivr.net/npm/markdown-it@15.0.2/LICENSE
https://cdn.jsdelivr.net/npm/dompurify@3.4.15/LICENSE
```

SHA-256 of the two executable bundles, re-verified by
`test_chat_markdown_rendering.py` on every test run so a corrupted or swapped
file fails loudly rather than silently changing what sanitizes model output:

```
635972b985228e8af9f0143647c68616b7a3bb09f6946e7e4a52e43dcf5e7be5  markdown-it.umd.min.js
f263b05369e050fa175d4ecb9c9358eb4253602d510297adfb31df48b2f1c4d5  purify.min.js
```

## Why these two

Assistant output is untrusted. It passes through two independent layers:

1. **markdown-it** parses it with `html: false`, so raw HTML in the model's
   text is escaped into visible characters and never becomes markup. The
   `image` rule is disabled, so a model cannot cause the browser to fetch a
   third-party resource. markdown-it also ships GFM tables, which is the
   construct the live responses use most.
2. **DOMPurify** then sanitizes the parser's output against an explicit tag
   and attribute allow-list and returns a DOM fragment, not a string.

Neither layer is trusted to be sufficient alone. `purify.min.js` is the
current release; DOMPurify is the component with a history of bypasses being
found and fixed, so it is the one to keep current.

## Upgrading

1. Download the new pinned version and its LICENSE from the same URLs.
2. Replace the files here and update the version and digest tables above.
3. Update `VENDOR_DIGESTS` in
   `butters/tests/test_chat_markdown_rendering.py`.
4. Re-read the DOMPurify release notes for changes to default behaviour
   before assuming the existing configuration still means what it meant.
