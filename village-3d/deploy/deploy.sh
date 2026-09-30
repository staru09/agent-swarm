#!/usr/bin/env bash
# Host village-3d on this machine behind Caddy.
#   sudo deploy/deploy.sh tunnel                       # once: public, behind a Cloudflare Tunnel (Cloudflare does HTTPS)
#   sudo deploy/deploy.sh setup village.example.com   # or, without Cloudflare: Caddy's own HTTPS + password on ports 80/443
#   deploy/deploy.sh publish [--build]                 # every release: pull from GitHub, copy the site to /var/www
#                                                      #   (--build reruns extract.py first)
# setup only: VILLAGE_USER (default "village") and VILLAGE_PASSWORD (default: random, printed once) set the login.
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
WWW=/var/www/village-3d

case "${1:-}" in
setup | tunnel)
	[ "$(id -u)" = 0 ] || { echo "$1 needs sudo" >&2; exit 1; }
	command -v caddy >/dev/null || apt-get install -y caddy
	if [ "$1" = setup ]; then
		domain=${2:?usage: sudo deploy/deploy.sh setup <subdomain.example.com>}
		user=${VILLAGE_USER:-village}
		pass=${VILLAGE_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')}
		hash=$(caddy hash-password --plaintext "$pass")  # bcrypt: [./$A-Za-z0-9], safe inside sed's | delimiters
		sed "s|VILLAGE_DOMAIN|$domain|; s|VILLAGE_USER|$user|; s|VILLAGE_HASH|$hash|" "$HERE/deploy/Caddyfile" > /etc/caddy/Caddyfile
	else
		# plain HTTP on localhost only, for cloudflared; the login block goes (the site is public)
		sed -e 's|^VILLAGE_DOMAIN {|:8080 {\n\tbind 127.0.0.1|' -e '/Login block/,/^\t}$/d' "$HERE/deploy/Caddyfile" > /etc/caddy/Caddyfile
	fi
	caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
	install -d -o "${SUDO_USER:-root}" "$WWW"
	systemctl enable caddy
	systemctl restart caddy
	if [ "$1" = setup ]; then echo "https://$domain  login: $user / $pass"
	else echo 'Caddy serves http://127.0.0.1:8080; point the tunnel public hostname at http://localhost:8080'; fi
	;;
publish)
	git -C "$HERE" pull --ff-only --quiet  # GitHub is the source of truth
	[ "${2:-}" = --build ] && (cd "$HERE" && python3 extract.py)
	[ -f "$HERE/data/index.json" ] || { echo "no data/index.json: run 'deploy/deploy.sh publish --build'" >&2; exit 1; }
	# only the site itself: page, scripts, models, data (not the extractor, docs or deploy kit)
	rsync -a --delete --include=/index.html --include='/*.js' --include='/assets/***' --include='/data/***' --exclude='*' "$HERE/" "$WWW/"
	echo "published $(git -C "$HERE" rev-parse --short HEAD), $(du -sh "$WWW" | cut -f1) to $WWW"
	;;
*)
	sed -n '2,7p' "$0"; exit 1 ;;
esac
