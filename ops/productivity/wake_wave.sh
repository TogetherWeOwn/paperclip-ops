#!/bin/bash
# Gated wake wave: <=10 in-focus unblocked cards -> todo per 10 min, only while load < 50. Stops when nothing is left or after 6h.
cd /home/ubuntu/paperclip-enterprise-company/ops/productivity
for i in $(seq 1 36); do
  out=$(python3 cleanup_blocked.py --phase wake --limit 10 --apply 2>&1); echo "$(date -u +%FT%TZ) $(echo "$out" | tail -2 | tr '\n' ' ')" >> wake_wave.log
  echo "$out" | grep -q "^0 woken" && exit 0
  sleep 600
done
