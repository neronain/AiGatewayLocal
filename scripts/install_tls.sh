#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Put the LiteGate behind HTTPS, as part of installing it rather than as a
# thing someone remembers to do later.
#
#   sudo ./scripts/install_tls.sh                          # private CA, auto names
#   sudo ./scripts/install_tls.sh gateway.uni.ac.th        # add a name
#   sudo ./scripts/install_tls.sh --cert /path/full.pem --key /path/key.pem
#   sudo ./scripts/install_tls.sh --force gw.local 10.0.0.5   # ออกใหม่เมื่อชื่อเปลี่ยน
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

OWN_CERT="" OWN_KEY="" HSTS="" FORCE="" NAMES=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --cert) OWN_CERT="${2:?--cert needs a path}"; shift 2 ;;
        --key)  OWN_KEY="${2:?--key needs a path}"; shift 2 ;;
        --hsts) HSTS="yes"; shift ;;
        --force) FORCE="yes"; shift ;;
        --no-hsts) HSTS="no"; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) NAMES+=("$1"); shift ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -n "$OWN_CERT" && -z "$OWN_KEY" ]] && die "--cert also needs --key"
[[ -z "$OWN_CERT" && -n "$OWN_KEY" ]] && die "--key also needs --cert"

# ตรวจไฟล์ตั้งแต่ตอนนี้ ก่อนจะไปลง nginx หรือแตะอะไรในระบบ
#
# เจอจริง: คนก๊อปตัวอย่างจาก README ทั้งบรรทัด — `--cert full.pem --key key.pem` —
# ซึ่งเป็นชื่อสมมติ แล้วได้ `install: cannot stat 'full.pem'` โผล่มากลางการติดตั้ง
# ตอนที่ apt ลง nginx ไปแล้ว · ข้อความนั้นไม่ได้บอกว่าเขาต้องเอาไฟล์จริงมาจากไหน
for pair in "cert:$OWN_CERT" "key:$OWN_KEY"; do
    label="${pair%%:*}" file="${pair#*:}"
    [[ -z "$file" ]] && continue
    [[ -e "$file" ]] || die "--${label} ชี้ไปที่ '$file' ซึ่งไม่มีไฟล์นั้นอยู่
    ถ้ายังไม่มีใบรับรองของตัวเอง ให้รันโดยไม่ต้องใส่ --cert/--key
    สคริปต์จะออกใบให้เองจาก CA ส่วนตัว ซึ่งพอสำหรับวงแลน"
    [[ -r "$file" ]] || die "--${label}: อ่าน '$file' ไม่ได้ (ลองใส่ path เต็ม)"
    [[ -s "$file" ]] || die "--${label}: '$file' เป็นไฟล์ว่าง"
done
if [[ -n "$OWN_CERT" ]]; then
    openssl x509 -in "$OWN_CERT" -noout >/dev/null 2>&1 \
        || die "--cert: '$OWN_CERT' ไม่ใช่ใบรับรอง PEM ที่อ่านได้"
    openssl pkey -in "$OWN_KEY" -noout >/dev/null 2>&1 \
        || die "--key: '$OWN_KEY' ไม่ใช่ private key PEM ที่อ่านได้"
    # คู่ที่ไม่แมตช์กันทำให้ nginx ไม่ยอมสตาร์ต และข้อความของ nginx อ่านยากกว่านี้มาก
    cert_mod="$(openssl x509 -in "$OWN_CERT" -noout -modulus 2>/dev/null | openssl md5)"
    key_mod="$(openssl pkey -in "$OWN_KEY" -noout -pubout 2>/dev/null | openssl md5)"
    cert_pub="$(openssl x509 -in "$OWN_CERT" -noout -pubkey 2>/dev/null | openssl md5)"
    [[ "$cert_pub" == "$key_mod" ]] \
        || die "--cert กับ --key ไม่ใช่คู่กัน (public key ไม่ตรง) — nginx จะไม่ยอมสตาร์ต"
fi

# ── names ──────────────────────────────────────────────────────────────────
# A certificate covering the hostname does not cover the IP, and operators
# reach a LAN box by whichever they happened to write down. Cover both, or the
# first person to use the other one gets a warning page and assumes it broke.
detect_names() {
    local found=() fqdn ip
    found+=("$(hostname)")
    fqdn="$(hostname -f 2>/dev/null || true)"
    [[ -n "$fqdn" && "$fqdn" != "$(hostname)" ]] && found+=("$fqdn")
    while read -r ip; do
        [[ -n "$ip" ]] && found+=("$ip")
    done < <(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9.]+$' || true)
    # เครื่องในวงแลนถูกเรียกด้วย localhost อยู่เสมอ ทั้งจากสคริปต์บนเครื่องเองและตอนทดสอบ
    # ใบที่ไม่ครอบสองชื่อนี้ทำให้ curl บนเครื่องตัวเองยังขึ้นเตือน ซึ่งอ่านเหมือนติดตั้งพลาด
    found+=("localhost" "127.0.0.1")
    printf '%s\n' "${found[@]}" | awk 'NF && !seen[$0]++'
}

if [[ ${#NAMES[@]} -eq 0 ]]; then
    mapfile -t NAMES < <(detect_names)

    # ถามก่อนออกใบ เมื่อรันจากมือคนจริง ๆ
    #
    # คู่มือเขียนตัวอย่างเป็น gateway.example.ac.th ซึ่งทำให้คนที่ลงในวงแลนไม่มีโดเมน
    # ไม่รู้ว่าต้องใส่อะไร · ที่จริงไม่ต้องใส่อะไรเลยก็ได้ — ชื่อเครื่องกับ IP ที่ตรวจเจอ
    # ใช้ได้ทันที · การถามตรงนี้เปลี่ยน "เดาว่าต้องมีโดเมน" เป็น "กด Enter ผ่าน"
    if [[ -z "${TLS_ASSUME_YES:-}" && -t 0 ]]; then
        echo
        log "ใบรับรองนี้จะครอบคลุมชื่อ/ที่อยู่ที่ตรวจเจอบนเครื่องนี้:"
        for n in "${NAMES[@]}"; do echo "     · $n"; done
        echo
        echo "   ถ้าใช้ในวงแลนภายใน กด Enter ผ่านได้เลย — ไม่ต้องมีโดเมน"
        echo "   ถ้ามีชื่อที่จะเรียกเพิ่ม (โดเมน, IP อื่น, ชื่อที่ตั้งใน hosts) พิมพ์ต่อท้ายได้"
        echo
        printf '   ชื่อเพิ่มเติม (คั่นด้วยช่องว่าง, Enter = ไม่เพิ่ม): '
        read -r extra || extra=""
        for n in $extra; do NAMES+=("$n"); done
        mapfile -t NAMES < <(printf '%s\n' "${NAMES[@]}" | awk 'NF && !seen[$0]++')
    fi
fi
[[ ${#NAMES[@]} -gt 0 ]] || die "หาชื่อของเครื่องนี้ไม่ได้ — ใส่มาเองสักชื่อ เช่น: sudo $0 10.0.0.5"
log "Certificate will cover: ${NAMES[*]}"

command -v nginx >/dev/null 2>&1 || {
    log "Installing nginx"
    apt-get update -qq && apt-get install -y -qq nginx
}

# ── certificate ────────────────────────────────────────────────────────────
# ใบที่ยังไม่หมดอายุแต่ไม่ครอบชื่อที่เรากำลังจะประกาศ = หน้าเตือนของเบราว์เซอร์
#
# เจอจริง: สคริปต์เติม localhost/127.0.0.1 ให้อัตโนมัติทีหลัง ใบที่ออกไว้ก่อนหน้านั้น
# จึงไม่ครอบ แต่โค้ดเดิมดูแค่ "หมดอายุหรือยัง" แล้วพิมพ์ว่าเก็บใบเดิมไว้ ตามด้วย URL
# ที่ใบนั้นไม่ครอบ · ผู้ติดตั้งเห็นข้อความว่าสำเร็จ แล้วเปิดเบราว์เซอร์เจอคำเตือน
cert_covers_all() {
    local cert="$1"; shift
    local san name entry wanted
    san="$(openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null || true)"
    [[ -n "$san" ]] || return 1

    # เทียบทีละรายการแบบเต็มชื่อ ไม่ใช่ substring — `DNS:host` ไปตรงกับ
    # `DNS:host.example.local` ได้ถ้าเทียบแบบ substring แล้วจะสรุปว่าครอบทั้งที่ไม่ครอบ
    local -a entries=()
    while IFS= read -r entry; do
        entry="${entry#"${entry%%[![:space:]]*}"}"   # ตัดช่องว่างหน้า
        entry="${entry%"${entry##*[![:space:]]}"}"   # ตัดช่องว่างหลัง
        [[ -n "$entry" ]] && entries+=("$entry")
    done < <(printf '%s\n' "$san" | tr ',' '\n' | grep -E '^\s*(DNS|IP Address):')

    for name in "$@"; do
        if [[ "$name" =~ ^[0-9.]+$ ]]; then wanted="IP Address:${name}"; else wanted="DNS:${name}"; fi
        local hit=1
        for entry in "${entries[@]}"; do
            [[ "$entry" == "$wanted" ]] && { hit=0; break; }
        done
        [[ $hit -eq 0 ]] || return 1
    done
    return 0
}


if [[ -n "$OWN_CERT" ]]; then
    log "Using the certificate you supplied"
    install -m 644 "$OWN_CERT" "$CERT_PATH"
    install -m 600 "$OWN_KEY" "$KEY_PATH"
    # A certificate from a public CA is already trusted everywhere, so the
    # first-visit warning that HSTS makes unbypassable cannot happen.
    [[ -z "$HSTS" ]] && HSTS="yes"
elif [[ -z "$FORCE" && -s "$CERT_PATH" && -s "$KEY_PATH" ]] && \
     openssl x509 -in "$CERT_PATH" -checkend 604800 -noout >/dev/null 2>&1 && \
     cert_covers_all "$CERT_PATH" "${NAMES[@]}"; then
    # ยังไม่หมดอายุ = ไม่ต้องออกใหม่ · แต่ "ยังไม่หมดอายุ" ไม่ได้แปลว่า "ยังใช้ได้"
    # เพิ่มชื่อใหม่ (โฮสต์ที่จะเรียกเพิ่ม, IP ที่เปลี่ยน) แล้วใบเดิมจะไม่ครอบคลุม
    log "Keeping the existing certificate (valid, and covers every name above)"
else
    if [[ -z "$FORCE" && -s "$CERT_PATH" ]] && ! cert_covers_all "$CERT_PATH" "${NAMES[@]}"; then
        log "ใบเดิมยังไม่หมดอายุ แต่ไม่ครอบทุกชื่อข้างบน — ออกใบใหม่ให้"
    fi
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

# HTTP/2 เปลี่ยนวิธีเขียนที่ nginx 1.25.1
#
#   ตั้งแต่ 1.25.1   listen 443 ssl;  แล้วบรรทัดแยก  http2 on;
#   ก่อนหน้านั้น     listen 443 ssl http2;   ไม่มี directive ชื่อ http2
#
# ไฟล์ที่แจกไปเขียนแบบใหม่ · Ubuntu 24.04 มากับ nginx 1.24.0 ซึ่งอ่านแล้วตอบว่า
# `unknown directive "http2"` แล้ว nginx ไม่ยอมสตาร์ต — ขั้นติดตั้ง TLS จึงล้ม
# ทั้งขั้นบนเครื่องที่พบบ่อยที่สุด · เครื่องที่เราทดสอบกันเองมี 1.28 จึงไม่เคยเห็น
nginx_version="$(nginx -v 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
supports_http2_on() {
    local want="1.25.1"
    [[ -z "$nginx_version" ]] && return 1
    [[ "$(printf '%s\n%s\n' "$want" "$nginx_version" | sort -V | head -1)" == "$want" ]]
}
if supports_http2_on; then
    log "nginx ${nginx_version} — ใช้ http2 on;"
else
    log "nginx ${nginx_version:-ไม่ทราบเวอร์ชัน} — ใช้รูปเก่า listen ... http2"
    sed -i -e 's#^\( *\)listen 443 ssl;#\1listen 443 ssl http2;#' \
           -e '/^ *http2 on;$/d' "$tmp"
fi

if [[ "$HSTS" != "yes" ]]; then
    sed -i '/Strict-Transport-Security/d' "$tmp"
fi

# ── ให้บล็อกของเราเป็น default_server บนพอร์ต 80 ────────────────────────────
#
# Ubuntu ลง nginx มาพร้อมไซต์ "default" ที่จอง default_server บน :80 ไว้ แปลว่า
# request ที่ Host ไม่ตรงกับ server_name ของเราสักชื่อ จะไปโผล่หน้าเปล่าของ
# /var/www/html แทนที่จะถูก redirect ขึ้น https · เจอง่ายกว่าที่คิด: ลูกค้าตั้ง
# ชื่อใน DNS เอง เพิ่มการ์ดแลนใบที่สอง หรือ DHCP เปลี่ยน IP หลังเราตรวจชื่อไปแล้ว
#
# ปิดให้เฉพาะตอนที่มันยังเป็นไซต์ default ที่แจกมากับ nginx จริงๆ (ยังชี้
# /var/www/html และ server_name เป็น _) · ถ้าใครแก้ไฟล์นั้นเองไว้ เราไม่ยุ่ง
# เพราะไม่รู้ว่าเขาเอาไปทำอะไรต่อ — แค่บอกให้รู้ว่าจะเกิดอะไรขึ้น
stock_default="/etc/nginx/sites-enabled/default"
default_restore=""
if [[ -e "$stock_default" ]] &&
   grep -qs "var/www/html" "$stock_default" &&
   grep -qsE '^[[:space:]]*server_name[[:space:]]+_;' "$stock_default"; then
    log "ปิดไซต์ default ที่แจกมากับ nginx (ไฟล์ต้นฉบับยังอยู่ที่ sites-available/default)"
    if [[ -L "$stock_default" ]]; then
        rm -f "$stock_default"
        default_restore="link"
    else
        mv "$stock_default" "${stock_default}.disabled-by-litegate"
        default_restore="file"
    fi
elif [[ -e "$stock_default" ]]; then
    warn "มีไซต์ default ของ nginx อยู่และถูกแก้ไขไว้ — ไม่แตะให้"
    warn "ถ้าเข้าด้วยชื่อที่ไม่ได้อยู่ในใบรับรอง แล้ว http ไม่เด้งขึ้น https นี่คือสาเหตุ"
fi

# ยึด default_server ได้ต่อเมื่อไม่มีใครจองไว้ก่อน — จองซ้ำแล้ว nginx ไม่ยอมสตาร์ต
if ! grep -qsE '^[[:space:]]*listen[[:space:]]+(\[::\]:)?80[[:space:]].*default_server' \
        /etc/nginx/nginx.conf /etc/nginx/conf.d/*.conf /etc/nginx/sites-enabled/* 2>/dev/null; then
    sed -i -e 's#^\( *\)listen 80;#\1listen 80 default_server;#' "$tmp"
fi

install -m 644 "$tmp" "$conf"
rm -f "$tmp"
mkdir -p /etc/nginx/sites-enabled
ln -sf "../sites-available/${SITE_NAME}.conf" "/etc/nginx/sites-enabled/${SITE_NAME}.conf"

# Never hand back a box whose nginx will not start. If the render is wrong the
# old config is still the running one, and saying so beats a silent reload.
if ! nginx -t 2>&1 | sed 's/^/    /'; then
    # คืนสภาพก่อนตาย · ไม่งั้นเครื่องจะเหลือ nginx ที่ทั้งไม่มีไซต์ default และ
    # ไม่มีไซต์ของเรา ซึ่งแย่กว่าตอนก่อนรันคำสั่งนี้
    rm -f "/etc/nginx/sites-enabled/${SITE_NAME}.conf"
    [[ "$default_restore" == "link" ]] && ln -sf ../sites-available/default "$stock_default"
    [[ "$default_restore" == "file" ]] && mv "${stock_default}.disabled-by-litegate" "$stock_default"
    die "nginx rejected the config above; nothing was reloaded"
fi
systemctl reload nginx 2>/dev/null || systemctl restart nginx

primary="${NAMES[0]}"

# nginx ส่งต่อไปที่แอปบนพอร์ตนี้ · ถ้ายังไม่มีอะไรฟังอยู่ ทุกอย่างจะขึ้น 502
#
# เจอจริง: ผู้ติดตั้งรัน install_tls.sh ก่อนจะเปิดเกตเวย์ แล้วเปิดเบราว์เซอร์เจอ 502
# ทันที · หน้าจอไม่มีอะไรบอกว่า TLS ทำงานถูกแล้วและสิ่งที่ขาดคือตัวแอป เขาจึงสรุปว่า
# ขั้นตอน TLS พัง แล้วไปรื้อ nginx ซึ่งไม่ได้ผิดอะไรเลย
app_is_up=""
if command -v curl >/dev/null 2>&1 &&
   curl -fsS -m 3 "http://127.0.0.1:${APP_PORT}/healthz" >/dev/null 2>&1; then
    app_is_up="yes"
fi

echo
log "HTTPS is up"
echo "  Console : https://${primary}/console/"
echo "  API     : https://${primary}/v1"
echo "  Plain   : http://${primary}:${APP_PORT}   (scripts, health checks, LAN clients)"
echo
if [[ -n "$OWN_CERT" ]]; then
    echo "  Certificate: the one you supplied."
elif [[ -s "$CA_OUT/ca.crt" ]]; then
    echo "  ใบนี้ออกจาก CA ส่วนตัวของเครื่องนี้ (พอสำหรับวงแลนภายใน ไม่ต้องมีโดเมนจริง)"
    echo "  เครื่องที่จะเรียกเกตเวย์ต้องติดตั้ง CA ก่อน ไม่งั้นจะขึ้นหน้าเตือน:"
    echo
    echo "    ไฟล์ที่ต้องเอาไปคือ  ${CA_OUT}/ca.crt"
    echo
    echo "    Ubuntu/Debian : sudo cp ca.crt /usr/local/share/ca-certificates/litegate.crt && sudo update-ca-certificates"
    echo "    macOS         : sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ca.crt"
    echo "    Windows       : certutil -addstore -f Root ca.crt   (Run as administrator)"
    echo "    Firefox       : Settings → Privacy & Security → Certificates → View Certificates → Authorities → Import"
    echo
    echo "  ยังไม่อยากติดตั้ง CA ก็ใช้พอร์ตธรรมดา http://${primary}:${APP_PORT} ได้ตามเดิม"
fi

if [[ -z "$app_is_up" ]]; then
    echo
    warn "ยังไม่มีอะไรฟังอยู่ที่พอร์ต ${APP_PORT} — nginx ตั้งเสร็จแล้วแต่จะตอบ 502"
    warn "จนกว่าจะเปิดตัวเกตเวย์ · นี่ไม่ใช่ปัญหาของ TLS"
    echo
    echo "  ลองเร็ว ๆ:     ./install.sh --demo"
    echo "  ลงเป็น service: sudo scripts/bootstrap.sh   (ขึ้นเองหลัง reboot)"
fi
if [[ "$HSTS" == "yes" ]]; then
    echo
    warn "HSTS is on. Browsers will refuse plain HTTP for this host for a year,"
    warn "including http://${primary}:${APP_PORT}."
fi
