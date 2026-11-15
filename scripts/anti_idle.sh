#!/bin/bash
# Oracle Always Free anti-idle — runs brief CPU burst to prevent VM reclaim
# ~10 seconds of compute every call
for i in {1..4}; do
    dd if=/dev/zero bs=16M count=5 of=/dev/null 2>/dev/null
done
