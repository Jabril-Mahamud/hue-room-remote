#!/bin/bash
# hue-alarm.sh - Wake-up light alarm for cron
# Launches hue-transition.py in background with nohup.
# Usage: hue-alarm.sh <delay-minutes> [fade-minutes]
#   delay-minutes: how long to stay warm before fading (default: 0 = fade immediately)
#   fade-minutes:  how long the fade takes (default: 30)

set -euo pipefail

DELAY="${1:-0}"
FADE="${2:-30}"
SCRIPT="$HOME/bin/hue-transition.py"
LOG_TAG="hue-alarm"

logger -t "$LOG_TAG" "Alarm triggered: delay=${DELAY}min, fade=${FADE}min"

if [ "$DELAY" -eq 0 ]; then
    # Fade immediately from off to work over FADE minutes
    nohup "$SCRIPT" fade "$FADE" \
        --warm-temp 400 --warm-brightness 1 \
        --work-temp 250 --work-brightness 200 \
        > /tmp/hue-alarm.log 2>&1 &
else
    # Warm now, wait DELAY minutes, then fade to work over FADE minutes
    nohup "$SCRIPT" schedule "$DELAY" --fade-minutes "$FADE" \
        --warm-temp 400 --warm-brightness 1 \
        --work-temp 250 --work-brightness 200 \
        > /tmp/hue-alarm.log 2>&1 &
fi

logger -t "$LOG_TAG" "Launched PID $!"
echo "Alarm started (PID $!)"
