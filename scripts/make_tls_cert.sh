#!/usr/bin/env bash
# Issue a TLS certificate for a LiteGate that has no public DNS name.
#
# Most TLS guidance assumes a public hostname and Let's Encrypt. A gateway sits
# on a campus or office network and is reached at 192.168.x.y or an internal
# hostname, and no public CA will ever issue for either. The alternative people
# reach for - `openssl req -x509` in one line - produces a certificate with no
# subjectAltName, which every browser and every HTTP client made in the last
# decade rejects outright. The usual next step is --insecure everywhere, which
# is worse than plain HTTP because it looks encrypted.
#
# So this creates a small certificate authority of your own, once, and issues a
# server certificate from it with the right names. Install the CA on the
# machines that will call the gateway and TLS simply works: real verification,
# no --insecure, no warnings.
#
#   ./scripts/make_tls_cert.sh gateway.local 192.168.1.10
#   ./scripts/make_tls_cert.sh --out /etc/ssl gateway.uni.ac.th 10.0.0.5
#
# Every argument is a name the gateway will be reached by. Anything that parses
# as an IP address becomes an IP SAN, everything else a DNS SAN. Include every
# name you will actually use - a certificate for the hostname does not cover the
# IP, and clients differ in which one they send.
set -euo pipefail

OUT_DIR="./certs"
DAYS=825          # ~27 months: the longest Apple and Chrome will accept
CA_DAYS=3650      # the CA outlives the certificates it signs

usage() {
    cat <<'USAGE'
Usage: make_tls_cert.sh [--out DIR] [--days N] NAME [NAME...]

  NAME      A hostname or IP address the gateway is reached at. Repeatable.
  --out     Where to write (default ./certs)
  --days    Server certificate lifetime (default 825)

Writes:
  ca.crt        Install this on client machines (see the notes it prints)
  ca.key        Keep this. Anyone holding it can issue certificates you trust.
  litegate.crt  The server certificate, chained
  litegate.key  The server private key

Re-running reuses an existing CA, so certificates issued later stay trusted and
clients do not need to install anything again.
USAGE
}

names=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --out)   OUT_DIR="$2"; shift 2 ;;
        --days)  DAYS="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*)      echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
        *)       names+=("$1"); shift ;;
    esac
done

if [[ ${#names[@]} -eq 0 ]]; then
    echo "ERROR: give at least one hostname or IP the gateway is reached at." >&2
    echo >&2
    usage >&2
    exit 2
fi

command -v openssl >/dev/null || { echo "ERROR: openssl is not installed." >&2; exit 1; }

mkdir -p "$OUT_DIR"
cd "$OUT_DIR"

# Split the names into DNS and IP entries. A certificate listing an IP under
# DNS: is not merely untidy - clients that connect by IP will reject it.
san=""
dns_count=0
ip_count=0
primary=""
for name in "${names[@]}"; do
    [[ -z "$primary" ]] && primary="$name"
    if [[ "$name" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ || "$name" == *:* ]]; then
        ip_count=$((ip_count + 1))
        san+="IP:${name},"
    else
        dns_count=$((dns_count + 1))
        san+="DNS:${name},"
    fi
done
san="${san%,}"

# --- the CA ---------------------------------------------------------------
# Reused if present, so certificates issued next month are trusted by clients
# that installed the CA today. Re-creating it every run would mean reinstalling
# on every machine every time, which is how people end up back at --insecure.
if [[ -f ca.key && -f ca.crt ]]; then
    echo "Using the existing CA in $(pwd) - clients that already trust it stay working."
else
    echo "Creating a certificate authority (once)..."
    openssl genrsa -out ca.key 4096 2>/dev/null
    chmod 600 ca.key
    openssl req -x509 -new -nodes -key ca.key -sha256 -days "$CA_DAYS" -out ca.crt \
        -subj "/CN=LiteGate Local CA/O=LiteGate" 2>/dev/null
fi

# --- the server certificate ----------------------------------------------
echo "Issuing a certificate for: ${names[*]}"
openssl genrsa -out litegate.key 2048 2>/dev/null
chmod 600 litegate.key

cat > .csr.cnf <<EOF
[req]
distinguished_name = dn
req_extensions     = ext
prompt             = no
[dn]
CN = ${primary}
O  = LiteGate
[ext]
subjectAltName = ${san}
EOF

openssl req -new -key litegate.key -out .litegate.csr -config .csr.cnf 2>/dev/null

cat > .ext.cnf <<EOF
subjectAltName         = ${san}
basicConstraints       = CA:FALSE
keyUsage               = digitalSignature, keyEncipherment
extendedKeyUsage       = serverAuth
subjectKeyIdentifier   = hash
EOF

openssl x509 -req -in .litegate.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out litegate.crt -days "$DAYS" -sha256 -extfile .ext.cnf 2>/dev/null

rm -f .csr.cnf .ext.cnf .litegate.csr
chmod 644 ca.crt litegate.crt

echo
echo "Written to $(pwd):"
echo "  litegate.crt  litegate.key   -> the gateway (or its reverse proxy)"
echo "  ca.crt                        -> every machine that calls the gateway"
echo "  ca.key                        -> keep this safe; it can issue certificates you trust"
echo
echo "Names on this certificate: ${dns_count} DNS, ${ip_count} IP"
openssl x509 -in litegate.crt -noout -ext subjectAltName | tail -n +2 | sed 's/^/  /'
echo
cat <<'NOTES'
Point the reverse proxy at it (deploy/nginx/litegate.conf expects these paths):

  sudo install -m 644 litegate.crt /etc/ssl/certs/litegate.crt
  sudo install -m 600 litegate.key /etc/ssl/private/litegate.key
  sudo nginx -t && sudo systemctl reload nginx

Then trust the CA on the machines that call the gateway. Until you do, they are
right to refuse the connection:

  Ubuntu/Debian  sudo cp ca.crt /usr/local/share/ca-certificates/litegate.crt
                 sudo update-ca-certificates
  RHEL/Rocky     sudo cp ca.crt /etc/pki/ca-trust/source/anchors/litegate.crt
                 sudo update-ca-trust
  macOS          sudo security add-trusted-cert -d -r trustRoot \
                   -k /Library/Keychains/System.keychain ca.crt
  Windows        certutil -addstore -f Root ca.crt        (as Administrator)
  Python         export SSL_CERT_FILE=/path/to/ca.crt     (also REQUESTS_CA_BUNDLE)
  Node           export NODE_EXTRA_CA_CERTS=/path/to/ca.crt

Firefox keeps its own store: Settings -> Privacy & Security -> Certificates ->
View Certificates -> Authorities -> Import.

Verify from a client that trusts the CA - no --insecure, that is the point:

  curl https://<name>/healthz
NOTES
