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

## Status (2026-07-20)

Live at **https://shop.evix.md** (TLS via Cloudflare). All routes 200: home/`/ro`/`/ru`,
category, PDP, search, cart, `sitemap-{ro,ru}.xml` + `robots.txt`; `/api/v1/*` same-origin;
media on `media.evix.md`. Catalog seeded (6 demo products, images are `cdn.example`
placeholders — upload real ones via the admin API → MinIO). Staff user in
`~/apps/evix-store/ADMIN_CREDS.txt`. All services `restart: unless-stopped`.

## Notes / gotchas

- **SSR uses an internal API base** (`API_INTERNAL_BASE=http://app:8000`, set on the
  `front` service) — server-side fetch must NOT hairpin out to the public domain
  (that fails inside the network). Browser islands use the public base.
- **cloudflared after a rebuild:** recreating `app`/`front` gives new IPs and
  cloudflared caches the old ones (`origin unreachable`). `deploy.sh` restarts it;
  do the same after any manual `up --build`.
- **Verifying from the LAN/Tailscale:** the server's own resolver may cache the fresh
  CNAME as AAAA-only and it has no IPv6 route → `curl` gives `000`. Real users are fine
  (A records exist: `dig @1.1.1.1 A shop.evix.md`). To test locally, pin IPv4:
  `curl --resolve shop.evix.md:443:104.21.68.244 https://shop.evix.md/...`.
- **Email** defaults to `console` (order-confirmation logged, not sent). Set
  `EMAIL_BACKEND=smtp` + creds in `.env` when a relay is available.
- **Static IP / Ethernet / DHCP reservation** on the router — see `base_server_msi_info.md`.
