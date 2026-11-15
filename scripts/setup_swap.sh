#!/bin/bash
# scripts/setup_swap.sh — One-time setup: 4GB swap file to prevent OOM lockups.
#
# The Oracle Free Tier A1.Flex VM ships without swap. When physical RAM (6GB
# on our shape) is exhausted by Whisper + bot + dashboard + pipeline, the
# kernel starts OOM-killing critical processes (SSH, systemd-resolved, etc),
# and the VM appears "Running" but becomes unreachable.
#
# A 4GB swap file gives the kernel headroom: memory pressure goes to disk
# instead of killing processes. Slower but recoverable.
#
# Idempotent — safe to re-run.
set -e

SWAP_FILE="/swapfile"
SWAP_SIZE_GB=4

if swapon --show | grep -q "$SWAP_FILE"; then
    echo "✅ Swap already active:"
    swapon --show
    free -h
    exit 0
fi

echo "Creating ${SWAP_SIZE_GB}GB swap file at $SWAP_FILE..."
sudo fallocate -l "${SWAP_SIZE_GB}G" "$SWAP_FILE" || {
    echo "fallocate failed, falling back to dd"
    sudo dd if=/dev/zero of="$SWAP_FILE" bs=1M count=$((SWAP_SIZE_GB * 1024)) status=progress
}
sudo chmod 600 "$SWAP_FILE"
sudo mkswap "$SWAP_FILE"
sudo swapon "$SWAP_FILE"

# Persist across reboots
if ! grep -q "^$SWAP_FILE" /etc/fstab; then
    echo "$SWAP_FILE  none  swap  sw  0  0" | sudo tee -a /etc/fstab
fi

# Tune: don't swap aggressively unless really needed
sudo sysctl -w vm.swappiness=10
sudo sysctl -w vm.vfs_cache_pressure=50
if ! grep -q "^vm.swappiness" /etc/sysctl.conf; then
    echo "vm.swappiness=10" | sudo tee -a /etc/sysctl.conf
    echo "vm.vfs_cache_pressure=50" | sudo tee -a /etc/sysctl.conf
fi

echo "✅ Swap setup complete:"
swapon --show
free -h
