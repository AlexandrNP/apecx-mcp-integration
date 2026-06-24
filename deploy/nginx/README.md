# apecx-mcp nginx edge

`apecx-mcp.conf` is the **only externally-reachable surface** for the apecx-mcp server. The MCP
endpoint is deliberately unauthenticated (a product decision), so this reverse proxy is where the
abuse controls that substitute for authentication live. It forwards **only** `/mcp` to the
loopback-bound server (`127.0.0.1:8001`); everything else is `404`.

## What it enforces (3.3)
- **TLS** — TLSv1.2/1.3, modern ciphers, HTTP→HTTPS redirect (mode B).
- **Body-size cap** — `client_max_body_size 2m` bounds tool-argument payloads.
- **Per-source-IP rate limit** — `limit_req` 5 r/s, burst 10.
- **Per-source-IP concurrency** — `limit_conn` 8 simultaneous streams.
- **Global concurrency ceiling** — `limit_conn` 256 total, backstopping the per-IP cap against a
  many-IP flood (the per-IP limit alone bounds one caller, not aggregate load).
- **Timeouts** — header 15s, body 30s, upstream read 600s (bounds a long tool call), send 60s.
- **Access logging** — method/path/status/size/IP/latency, **never the request body** (see below).
- **Surface hygiene** — `server_tokens off` (no nginx-version leak), HSTS on the TLS server, and
  only `/mcp` is proxied (everything else returns 404).

All limit values are first-pass defaults for a single-purpose host; tune them from real traffic
(the audit's Phase L4 load test, `SECURITY_AUDIT_PLAN.md`).

## Install
1. Copy `apecx-mcp.conf` to nginx's http context: `/etc/nginx/conf.d/apecx-mcp.conf`.
2. Pick a mode (below). `nginx -t` to validate, then `systemctl reload nginx`.
3. `mkdir -p /var/log/nginx` (default exists); ensure the access log is **mode-600**, owned by the
   nginx user, and rotated — it records request paths + source IPs (forensics data, treat as
   sensitive).

## Two ingress modes — pick one

### B. Direct `:443` (default-enabled in the conf)
For a host with a public IP + DNS name. nginx terminates TLS itself.
- Provide a cert at `/etc/nginx/tls/apecx-mcp.crt` + key `…/apecx-mcp.key` (e.g. certbot/Let's Encrypt).
- Set `server_name` to your FQDN.
- The real client IP is the TCP peer — per-IP limits work directly. Nothing else to do.

### A. Behind a TLS-terminating tunnel (cloudflared / ngrok)
For a host with **no public IP** (the current deploy default — see `SERVER_DEPLOYMENT.md`). The
tunnel terminates public TLS and forwards to nginx's **local** `127.0.0.1:8443` listener.
- Uncomment Mode A's `server{}` block; comment out Mode B's two `server{}`s.
- Point the tunnel at `http://127.0.0.1:8443` (NOT directly at `:8001` — that bypasses every limit).

> **Critical — real client IP (resolves the tunnel-vs-rate-limit gap):** with a tunnel in front,
> the TCP peer nginx sees is the **tunnel**, so without `real_ip` config the per-IP rate/conn limits
> collapse into a single global bucket. The conf sets `set_real_ip_from <tunnel source>` +
> `real_ip_header`. **Only ever trust the forwarded-IP header from the tunnel's own source address**
> — `set_real_ip_from` must list exactly the tunnel daemon's address/CIDR (a host-local cloudflared
> is `127.0.0.1`; a remote tunnel needs its egress CIDR). An `X-Forwarded-For`/`CF-Connecting-IP`
> accepted from any other source is attacker-spoofable, which would let one caller forge distinct
> IPs and defeat the rate limit entirely. For Cloudflare use `CF-Connecting-IP` + Cloudflare's
> published IP ranges; for ngrok use `X-Forwarded-For` and trust only ngrok's agent address.

## Why `data_handle`s stay out of the logs (#2)
Workflow results carry a `data_handle` (a `uuid4` capability token — anyone holding it can read the
run's artifacts). It travels in the JSON-RPC **POST body**, and the `apecx_mcp` log format logs only
`$request` (the request line: `POST /mcp HTTP/1.1`) — never `$request_body`. Do not add
`$request_body` to the log format, and keep the access log mode-600: a handle in a world-readable
log would be a data-exposure path.
