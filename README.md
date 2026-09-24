# Grok X Farm

Full Grok/X farming suite — everything from registration to monetized API gateway.

```
farm.py reg --count 5        → accounts registered (tmail/gptmail/luckmail/fce/gmail/outlook)
farm.py import               → SSO imported into grok2api gateway + convert to Build
farm.py keys                 → client API key issued
farm.py parse "query"        → live X search via grok-chat-fast pool
farm.py doctor               → health: gateway, pool, quotas, geo, config
```

## Structure

| Path | What |
|------|------|
| `farm.py` | Unified CLI orchestrator (reg / import / keys / parse / crawl / doctor) |
| `reg_loop.py` | Continuous registration loop with browser cleanup watchdog |
| `autoreg/` | **grok-auto** — core registration engine |
| `autoreg/grok_auto.py` | Main registrar: email → signup → Turnstile → SSO → Build token |
| `autoreg/turnstile_farm.py` | Turnstile solving farm (free browser solver, paid fallbacks) |
| `autoreg/turnstile_solver_local.py` | Local free Turnstile solver (patchright, headed) |
| `autoreg/email_service.py` | 8 email providers: tmail, gptmail, mailtm, luckmail, mailnest, fce, gmail, outlook |
| `autoreg/auto_replenish.py` | Auto pool replenishment |
| `autoreg/device_mint.py` / `device_consent.py` | OAuth device-flow token minting |
| `autoreg/token_daemon.py` | Background token refresh daemon |
| `autoreg/sso_to_cpa.py` | SSO → CPA conversion |
| `autoreg/luckmail/` | LuckMail SDK (vendored) |
| `grok-register/` | **grok-register** — GUI registrar + proxy infrastructure |
| `grok-register/grok_register_ttk.py` | Tkinter GUI registrar |
| `grok-register/proxy_pool_v3.py` | Proxy pool with health checks + rotation |
| `grok-register/mail_service.py` | Cloudflare temp-mail service |
| `grok-register/outlook_mailbox_pool.py` | Outlook mailbox pool |
| `parser/` | X/Twitter parser farm (live search via gateway pool) |
| `dashboard/panel.py` | Web dashboard |
| `gateway_keepalive.py` | Gateway keep-alive pinger |
| `deploy_grok_gateway.sh` | EU VPS gateway deployment |
| `docker-compose.grok.yml` | Docker stack: grok2api + new-api |
| `setup_newapi_grok.py` | new-api bootstrap: root init, consumer token |
| `config/farm.config.example.json` | Full config reference |

## Config

```bash
cp config/farm.config.example.json farm.config.json
# edit: email provider, captcha mode, proxy, gateway URL
```

Secrets via env vars (win over config): `G2A_KEY`, `G2A_ADMIN_PASS`, `YESCAPTCHA_KEY`, `GROK_PROXY`, `EMAIL_PROVIDER`, `REG_DIR`.

## Captcha

Default `free_browser` mode — patchright headed Chrome solves Turnstile for $0. Paid fallback chain (yescaptcha → capsolver → nopecha → 2captcha) kicks in after N failures.

## Proxy

xAI geo-blocks RU egress — `single`/`pool` mode requires US/EU exit. Health check via ip-api.com.

## Verify

```bash
python tests/test_parser_offline.py
python tests/test_panel_offline.py
```

Secrets never committed: `SECRETS.local.txt`, `g2a_key.txt`, `keys/`, `auths/`, cookies, sessions — all gitignored.

## License

MIT
