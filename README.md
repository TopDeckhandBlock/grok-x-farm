# Grok X Farm

Full Grok/X farming suite — account pool, grok2api gateway, X parser farm.

## Components

| Dir | What |
|-----|------|
| `farm.py` | Main farm orchestrator |
| `autoreg/` | Account auto-registration |
| `gateway/` | grok2api OpenAI-compatible gateway over account pool |
| `parser/` | X/Twitter parser farm (free extraction via account rotation) |
| `dashboard/` | Web dashboard |
| `workflow/` | Automation workflows |
| `skill/` | Agent skill pack |
| `tests/` | Test suite |
| `deploy_grok_gateway.sh` | Gateway deployment script |
| `docker-compose.grok.yml` | Docker stack (grok2api + new-api) |
| `setup_newapi_grok.py` | new-api setup: root init, consumer token |
| `roast_verify.py` | Live farm self-check: 7 checks (login/pool/models/keys/X-parse/fxtwitter/tool-calls) |

## Quick Start

```bash
# gateway
docker compose -f docker-compose.grok.yml up -d
python setup_newapi_grok.py

# verify farm
python tests/roast_verify.py
```

Secrets stay local: `SECRETS.local.txt`, `farm.config.json`, cookies and session files are gitignored by design.

## License

MIT
