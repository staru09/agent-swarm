#!/usr/bin/env bash
# Host village-3d on this machine behind Caddy (automatic HTTPS + login).
#   sudo deploy/deploy.sh setup village.example.com   # once per domain/password: Caddy config, login, start
#   deploy/deploy.sh publish [--build]                 # every release: copy the site to /var/www (--build reruns extract.py first)
# VILLAGE_USER (default "village") and VILLAGE_PASSWORD (default: random, printed once) set the login.
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
WWW=/var/www/village-3d

case "${1:-}" in
setup)
	domain=${2:?usage: sudo deploy/deploy.sh setup <subdomain.example.com>}
	[ "$(id -u)" = 0 ] || { echo 'setup needs sudo' >&2; exit 1; }
	command -v caddy >/dev/null || apt-get install -y caddy
	user=${VILLAGE_USER:-village}
	pass=${VILLAGE_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')}
	hash=$(caddy hash-password --plaintext "$pass")  # bcrypt: [./$A-Za-z0-9], safe inside sed's | delimiters
	sed "s|VILLAGE_DOMAIN|$domain|; s|VILLAGE_USER|$user|; s|VILLAGE_HASH|$hash|" "$HERE/deploy/Caddyfile" > /etc/caddy/Caddyfile
	caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
	install -d -o "${SUDO_USER:-root}" "$WWW"
	systemctl enable caddy
	systemctl restart caddy
	echo "https://$domain  login: $user / $pass"
	;;
publish)
	[ "${2:-}" = --build ] && (cd "$HERE" && python3 extract.py)
	[ -f "$HERE/data/index.json" ] || { echo "no data/index.json: run 'deploy/deploy.sh publish --build'" >&2; exit 1; }
	# only the site itself: page, scripts, models, data (not the extractor, docs or deploy kit)
	rsync -a --delete --include=/index.html --include='/*.js' --include='/assets/***' --include='/data/***' --exclude='*' "$HERE/" "$WWW/"
	echo "published $(du -sh "$WWW" | cut -f1) to $WWW"
	;;
*)
	sed -n '2,6p' "$0"; exit 1 ;;
esac
