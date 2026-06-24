#!/usr/bin/env bash
# apecx-mcp host firewall — the SECOND wall layer (deployment policy P1).
#
# The FIRST layer is the loopback binds in docker-compose.server.yml (3.1): every backend publishes
# on 127.0.0.1, so it is already unreachable off-host regardless of this firewall. This script is the
# independent backstop: default-deny inbound, allowing only :443 (the nginx edge — the sole public
# surface) and a restricted SSH. Either layer alone would suffice; the policy requires BOTH, so
# neither may be removed "because the other covers it". Because the backends bind loopback, the
# well-known Docker-bypasses-ufw trap (a 0.0.0.0 publish inserts DNAT that skips ufw) cannot fire here.
#
# Idempotent: ufw rules are declarative, so re-running re-asserts the same state (re-adding a rule is
# a no-op). Run as root on the deploy host. CAUTION: review SSH_CIDR before running on a REMOTE host —
# an over-narrow rule can lock you out (the classic firewall footgun). SSH is added BEFORE enable.
set -euo pipefail

# Restrict SSH to a known admin source if you can (a CIDR or single IP). "any" leaves SSH open to all
# sources (still key-only IF sshd enforces it — see below). Override:  SSH_CIDR=203.0.113.4/32 sudo bash firewall.sh
SSH_CIDR="${SSH_CIDR:-any}"
SSH_PORT="${SSH_PORT:-22}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
command -v ufw >/dev/null 2>&1 || die "ufw not found. Install it (e.g. 'apt-get install ufw')."
[[ "$(id -u)" == "0" ]] || die "run as root (ufw needs it): sudo bash $0"

# Default-deny inbound; allow outbound — the host needs egress (RCSB / PubMed / BV-BRC / Globus /
# the container registry / Ollama model pulls).
ufw default deny incoming
ufw default allow outgoing

# The ONLY public ingress: the nginx edge on 443.
ufw allow 443/tcp comment 'apecx-mcp nginx edge (sole public surface)'

# SSH. NOTE: key-only auth is enforced by sshd (PasswordAuthentication no in sshd_config) — NOT by
# ufw; this rule only restricts the SOURCE. Prefer a specific CIDR over "any".
if [[ "$SSH_CIDR" == "any" ]]; then
  ufw allow "${SSH_PORT}/tcp" comment 'SSH (source-unrestricted — set SSH_CIDR to lock down)'
else
  ufw allow from "$SSH_CIDR" to any port "$SSH_PORT" proto tcp comment 'SSH (restricted source)'
fi

# Enable non-interactively (rules above are already staged, so SSH is not cut).
ufw --force enable
ufw status verbose

echo
echo "Firewall active: inbound default-deny; allowed = 443/tcp (nginx) + SSH (${SSH_CIDR}:${SSH_PORT})."
echo "NOTE: changing SSH_CIDR later does NOT remove the old rule — 'ufw status numbered' + 'ufw delete <n>',"
echo "or 'ufw --force reset' then re-run (reset also drops any rules you added by hand)."
echo "Prove it from OFF-HOST: 'nmap <host>' should show ONLY 443 (+SSH). See deploy/SECURITY_AUDIT_PLAN.md (Phase D)."
