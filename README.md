---
title: ThumbPack
emoji: 🎬
colorFrom: purple
colorTo: pink
sdk: docker
app_port: 7860
---

# ThumbPack — Thumbnail Packaging Suite

Give ThumbPack your video topic, and get back a complete click-ready
package in seconds:

- **5 viral title options**, each with a predicted **CTR score (1–10)**
- The **best-title pick** for your video
- **10–15 SEO tags** ready to paste
- A **generated thumbnail image** (16:9) composed with space for your
  big text

Built for YouTube and Facebook creators. Free plan: 5 generations a
day. Pro plan: 200 a day.

## How it runs

- One small Python service (standard library only) serves the web app,
  user accounts and per-day quotas, and generates each package by
  calling Google Gemini directly — titles/scores/tags from the model,
  the thumbnail image from Pollinations.
- The Gemini API key is provided to the Space as a secret
  (`GEMINI_API_KEY`); it is never in the code.
- Payments (Lemon Squeezy) upgrade paying customers to Pro
  automatically via a signed webhook.
