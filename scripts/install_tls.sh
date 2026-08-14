#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Put the LiteGate behind HTTPS, as part of installing it rather than as a
# thing someone remembers to do later.
#
#   sudo ./scripts/install_tls.sh                          # private CA, auto names
#   sudo ./scripts/install_tls.sh gateway.uni.ac.th        # add a name
#   sudo ./scripts/install_tls.sh --cert /path/full.pem --key /path/key.pem
#
# HTTPS is not optional in practice. A growing number of clients refuse plain
# HTTP outright - browser APIs gated on a secure context, editor extensions,
# anything that treats http:// as a misconfiguration - so a gateway that only
# speaks HTTP is a gateway half the software on campus cannot talk to.
#
# What this leaves running:
#
#   :443  nginx, TLS            the address to hand out
#   :80   nginx, 301 to :443    so a typed hostname lands somewhere
#   :8080 the app, plain HTTP   for scripts, health checks and LAN clients
#
# The plain port stays. Removing it would break every curl and every monitoring
# probe on the network for no security gain, since anything that matters is
# already reachable over TLS.
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SITE_NAME="litegate"
CERT_PATH="/etc/ssl/certs/${SITE_NAME}.crt"
KEY_PATH="/etc/ssl/private/${SITE_NAME}.key"
CA_OUT="/etc/ssl/${SITE_NAME}-ca"
APP_PORT="${APP_PORT:-8080}"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

OWN_CERT="" OWN_KEY="" HSTS="" NAMES=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --cert) OWN_CERT="${2:?--cert needs a path}"; shift 2 ;;
        --key)  OWN_KEY="${2:?--key needs a path}"; shift 2 ;;
        --hsts) HSTS="yes"; shift ;;
        --no-hsts) HSTS="no"; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) NAMES+=("$1"); shift ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -n "$OWN_CERT" && -z "$OWN_KEY" ]] && die "--cert also needs --key"
[[ -z "$OWN_CERT" && -n "$OWN_KEY" ]] && die "--key also needs --cert"

# ── names ──────────────────────────────────────────────────────────────────
# A certificate covering the hostname does not cover the IP, and operators
# reach a LAN box by whichever they happened to write down. Cover both, or the
# first person to use the other one gets a warning page and assumes it broke.
if [[ ${#NAMES[@]} -eq 0 ]]; then
    NAMES+=("$(hostname)")
    fqdn="$(hostname -f 2>/dev/null || true)"
    [[ -n "$fqdn" && "$fqdn" != "$(hostname)" ]] && NAMES+=("$fqdn")
    while read -r ip; do
        [[ -n "$ip" ]] && NAMES+=("$ip")
    done < <(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9.]+$' || true)
fi
[[ ${#NAMES[@]} -gt 0 ]] || die "could not work out any name for this host; pass one"
log "Certificate will cover: ${NAMES[*]}"

command -v nginx >/dev/null 2>&1 || {
    log "Installing nginx"
    apt-get update -qq && apt-get install -y -qq nginx
}

# ── certificate ────────────────────────────────────────────────────────────
if [[ -n "$OWN_CERT" ]]; then
    log "Using the certificate you supplied"
    install -m 644 "$OWN_CERT" "$CERT_PATH"
    install -m 600 "$OWN_KEY" "$KEY_PATH"
    # A certificate from a public CA is already trusted everywhere, so the
    # first-visit warning that HSTS makes unbypassable cannot happen.
    [[ -z "$HSTS" ]] && HSTS="yes"
elif [[ -s "$CERT_PATH" && -s "$KEY_PATH" ]] && \
     openssl x509 -in "$CERT_PATH" -checkend 604800 -noout >/dev/null 2>&1; then
    log "Keeping the existing certificate (valid for more than a week)"
else
    log "Issuing a certificate from a private CA"
    "$REPO_DIR/scripts/make_tls_cert.sh" --out "$CA_OUT" "${NAMES[@]}" >/dev/null
    install -m 644 "$CA_OUT/${SITE_NAME}.crt" "$CERT_PATH"
    install -m 600 "$CA_OUT/${SITE_NAME}.key" "$KEY_PATH"
fi

# HSTS is a promise that this host is HTTPS-only, and browsers keep it for a
# year. On a private-CA deployment that promise costs more than it buys:
#
#   * Chrome refuses to show the "proceed anyway" link on an HSTS host, so the
#     very first visit - before anyone has installed the CA - becomes a dead
#     end rather than a warning.
#   * The promise covers the host, not the port. Every http:// URL for this
#     machine gets upgraded, including the plain :8080 the operators use, which
#     then fails because nothing is listening for TLS there.
#
# So it stays off unless the certificate is publicly trusted or you insist.
[[ -z "$HSTS" ]] && HSTS="no"

# ── nginx ──────────────────────────────────────────────────────────────────
log "Writing the nginx site"
server_names="${NAMES[*]}"
conf="/etc/nginx/sites-available/${SITE_NAME}.conf"
tmp="$(mktemp)"

sed -e "s#server_name gateway.university.ac.th;#server_name ${server_names};#g" \
    -e "s#server 127.0.0.1:8080;#server 127.0.0.1:${APP_PORT};#" \
    "$REPO_DIR/deploy/nginx/${SITE_NAME}.conf" > "$tmp"

if [[ "$HSTS" != "yes" ]]; then
    sed -i '/Strict-Transport-Security/d' "$tmp"
fi

install -m 644 "$tmp" "$conf"
rm -f "$tmp"
mkdir -p /etc/nginx/sites-enabled
ln -sf "../sites-available/${SITE_NAME}.conf" "/etc/nginx/sites-enabled/${SITE_NAME}.conf"

# Never hand back a box whose nginx will not start. If the render is wrong the
# old config is still the running one, and saying so beats a silent reload.
nginx -t 2>&1 | sed 's/^/    /' || die "nginx rejected the config above; nothing was reloaded"
systemctl reload nginx 2>/dev/null || systemctl restart nginx

primary="${NAMES[0]}"
echo
log "HTTPS is up"
echo "  Console : https://${primary}/console/"
echo "  API     : https://${primary}/v1"
echo "  Plain   : http://${primary}:${APP_PORT}   (scripts, health checks, LAN clients)"
echo
if [[ -n "$OWN_CERT" ]]; then
    echo "  Certificate: the one you supplied."
elif [[ -s "$CA_OUT/ca.crt" ]]; then
    echo "  Install ${CA_OUT}/ca.crt on the machines that will call this gateway,"
    echo "  and TLS verifies properly - no --insecure, no warning page."
fi
if [[ "$HSTS" == "yes" ]]; then
    echo
    warn "HSTS is on. Browsers will refuse plain HTTP for this host for a year,"
    warn "including http://${primary}:${APP_PORT}."
fi
