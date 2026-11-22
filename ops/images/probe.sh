#!/bin/sh
# Run inside a sandbox by `ops/run --check-sandbox`. One line per wall: PASS|FAIL|PENDING <name>: <detail>.
# Arguments: host HOME, control plane root, expected memory bytes, expected pids, allowed env names (comma list).
host_home=$1 root=$2 mem=$3 pids=$4 allowed=$5

report() { echo "$1 $2: $3"; }

for path in "$host_home/.ssh" "$host_home/.config/gh" "$host_home/.claude" "$host_home/.claude.json" \
            "$host_home/.local/share/opencode" "$root/ops/env" "$host_home/.gitconfig"; do
  if [ -e "$path" ]; then report FAIL hidden-files "$path is visible"; else report PASS hidden-files "$path"; fi
done

extra=$(env | cut -d= -f1 | while read -r name; do
  case ",$allowed," in *",$name,"*) ;; *) echo "$name" ;; esac
done | tr '\n' ' ')
if [ -n "$extra" ]; then report FAIL env "unexpected variables: $extra"; else report PASS env "only allowed names"; fi

if [ "$(id -u)" = 0 ]; then report FAIL user "running as root"; else report PASS user "uid $(id -u)"; fi
caps=$(awk '/^CapEff/ {print $2}' /proc/self/status)
if [ "$caps" = 0000000000000000 ]; then report PASS capabilities "none"; else report FAIL capabilities "$caps"; fi

got_mem=$(cat /sys/fs/cgroup/memory.max 2>/dev/null)
if [ "$got_mem" = "$mem" ]; then report PASS memory "$got_mem"; else report FAIL memory "expected $mem, got $got_mem"; fi
got_pids=$(cat /sys/fs/cgroup/pids.max 2>/dev/null)
if [ "$got_pids" = "$pids" ]; then report PASS pids "$got_pids"; else report FAIL pids "expected $pids, got $got_pids"; fi

# Egress: listed hosts only, only through the proxy, and no DNS for outside names.
if curl -sS -m 20 -o /dev/null https://api.github.com 2>/dev/null; then report PASS egress "listed host reachable via the proxy"
else report FAIL egress "api.github.com is not reachable through the proxy"; fi
if curl -sS -m 20 -o /dev/null https://example.com 2>/dev/null; then report FAIL egress "example.com (not listed) was reachable"
else report PASS egress "unlisted host refused"; fi
if curl -sS -m 10 --noproxy '*' -o /dev/null https://api.github.com 2>/dev/null; then report FAIL egress "direct connection bypassed the proxy"
else report PASS egress "no route around the proxy"; fi
if getent hosts example.com >/dev/null 2>&1; then report FAIL egress "outside names resolve (DNS leak)"
else report PASS egress "outside names do not resolve"; fi
for repo in client server; do
  if [ -e "$root/talent-scout-$repo/.git" ]; then report FAIL git "the human's $repo repository is visible"
  else report PASS git "the human's $repo repository is invisible"; fi
done
