#!/bin/sh
# Build tinyproxy's allow-list from EGRESS_ALLOWED_HOSTS (comma-separated host names), then run it.
set -eu
set -f  # no glob expansion of the host list ("*" must not expand to file names)

: "${EGRESS_ALLOWED_HOSTS:?EGRESS_ALLOWED_HOSTS must list the hosts the tracker may reach}"
filter=/tmp/allowed-hosts
: > "$filter"

for host in $(printf '%s' "$EGRESS_ALLOWED_HOSTS" | tr ',' ' '); do
    case "$host" in
        *[!A-Za-z0-9.-]*)
            echo "EGRESS_ALLOWED_HOSTS: not a host name: $host" >&2
            exit 1
            ;;
    esac
    # Exact "host:443" tunnel target: dots escaped, anchored at both ends (no subdomains,
    # no look-alikes, no other port, no plain HTTP)
    printf '^%s:443$\n' "$(printf '%s' "$host" | sed 's/\./\\./g')" >> "$filter"
done

if [ ! -s "$filter" ]; then
    echo "EGRESS_ALLOWED_HOSTS has no host name" >&2
    exit 1
fi
echo "Egress allowed to: $(tr '\n' ' ' < "$filter")"

exec tinyproxy -d -c /etc/tinyproxy/tinyproxy.conf
