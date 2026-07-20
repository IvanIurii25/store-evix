# evix-store — deployment (self-hosted, Cloudflare Tunnel)

Target: the **evix MSI server** (Ubuntu, Docker). Public via **Cloudflare Tunnel**
(edge TLS) on **`shop.evix.md`** (+ **`media.evix.md`** for product images).
Mirrors the `also_search_console` pattern. No host ports are exposed.

## Topology

```
Browser ──HTTPS──> Cloudflare edge ──tunnel──> cloudflared ─┬─ /api/*  -> app:8000  (FastAPI)
  shop.evix.md                                              └─ /*      -> front:3000 (Astro SSR)
  media.evix.md ─────────────────────────────────────────────────────── -> minio:9000 (/evix-media)
```

Storefront is **same-origin**: the browser calls `https://shop.evix.md/api/...`
(no CORS); SSR + islands share one base baked at build (`PUBLIC_API_BASE`).

## First-time setup (on the server)

```bash
mkdir -p ~/apps && cd ~/apps
# NB: the backend GitHub repo is `store-evix`; clone it into `evix-store` so the
# compose paths (../evix-store-front) and scripts line up.
git clone git@github.com:IvanIurii25/store-evix.git evix-store       # backend (+ compose)
git clone git@github.com:IvanIurii25/evix-store-front.git           # frontend (built by compose)

cd ~/apps/evix-store
cp .env.prod.example .env && chmod 600 .env
# fill secrets:  JWT_SECRET=$(openssl rand -hex 32)  POSTGRES_PASSWORD=...  MINIO_ROOT_PASSWORD=...
```

### Cloudflare tunnel (the account cert ~/.cloudflared/cert.pem already exists)

```bash
CF="docker run --rm -v /home/evix/.cloudflared:/home/nonroot/.cloudflared cloudflare/cloudflared:latest"
$CF tunnel create evix-store                       # writes <uuid>.json into ~/.cloudflared
$CF tunnel route dns evix-store shop.evix.md       # DNS CNAME
$CF tunnel route dns evix-store media.evix.md

cp ~/apps/evix-store/deploy/cloudflared.config.example.yml ~/.cloudflared/config-evix-store.yml
# replace REPLACE_WITH_TUNNEL_UUID (x2) with the tunnel UUID from `cloudflared tunnel list`
```

### Launch

```bash
cd ~/apps/evix-store
docker compose -f docker-compose.prod.yml up -d --build           # db/redis/minio + migrate + app + front
docker compose -f docker-compose.prod.yml --profile tunnel up -d  # cloudflared
docker compose -f docker-compose.prod.yml ps
# optional: seed a demo catalog
docker compose -f docker-compose.prod.yml exec app uv run python -m app.scripts.seed   # or scripts/seed.py
# create a staff user for the admin API
docker compose -f docker-compose.prod.yml exec app uv run python scripts/create_staff.py --email you@evix.md --password '***'
```

## Routine ops

- **Update / redeploy:** `bash deploy/deploy.sh` (pulls both repos, rebuilds, migrates, rolls).
- **Recover after power loss:** `bash deploy/recover.sh` (idempotent; the stack also auto-starts via `restart: unless-stopped`).
- **DB backup:** cron `0 3 */3 * * bash ~/apps/evix-store/deploy/pg_backup.sh` → `~/evix-store-backups/` (keeps 5).
- **Restore:** `cat ~/evix-store-backups/evix-store_<ts>.dump | docker compose -f docker-compose.prod.yml exec -T db pg_restore -U evix -d evix_store --clean --if-exists`.

## Notes / follow-ons

- **Email** defaults to `console` (order-confirmation is logged, not sent). Set `EMAIL_BACKEND=smtp` + SMTP creds in `.env` when a relay is available.
- **SSR fetch hop:** SSR currently fetches via `https://shop.evix.md` (through the edge). An internal base (`http://app:8000`) for server-side calls would drop a hop — small follow-on (needs a server/client base split in `api/client.ts`).
- **Static IP / Ethernet / DHCP reservation** on the router — see `base_server_msi_info.md`.
