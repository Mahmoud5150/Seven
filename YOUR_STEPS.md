# What only Mah can do (in order)

1. **Push the code:** `git push -u origin spike-readers`, then merge into `main` on GitHub.
2. **Send the Salla and Zid partner emails** (ready to copy in `docs/partner_emails.md`): policy, API use, app-store fees, review time.
3. **WhatsApp:** create a Meta Business account and a WhatsApp Business account, add a phone number not used in the WhatsApp app, create a permanent System User token, note the Phone Number ID. Submit the two templates in `docs/whatsapp_templates.md`.
4. **Email:** pick an SMTP service (e.g. Amazon SES, Brevo), verify your sending domain (SPF/DKIM).
5. **Server:** rent a small VPS (2 vCPU / 4 GB) **located in Saudi Arabia** (Salla hides VAT from foreign IPs, which would cause false alerts), point a domain or subdomain at it (e.g. `seven.albahrani.dev`), install Docker, clone the repo, `cp .env.example .env`, fill it in, `docker compose up -d --build`.
6. **Legal:** a short opinion from a Saudi lawyer on reading public store data (plan: Risks).
7. **Beta:** recruit ~15 merchants. Using Artist Hub's contacts needs your CEO's approval first. Add them at `/admin?key=<ADMIN_TOKEN>` and send each their dashboard link.
8. **Shopify:** Shopify blocks unsigned bots (8+ hours and counting). Once the domain is live, ask Claude to add Web Bot Auth signing (needs the domain to host our public key). If signed limits are still too low, apply for more capacity via Shopify's bot form.
9. Delete the `_to_delete/` folder (old broken database files).
