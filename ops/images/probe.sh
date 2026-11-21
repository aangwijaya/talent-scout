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

report PENDING egress "allowlist proxy arrives in sandbox stage 3"
report PENDING git "agent clone arrives in sandbox stage 4; the main .git is still mounted"
