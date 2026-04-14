#!/bin/bash
# Fetch all data (YouTube + blogs + X)
# This is a convenience wrapper; the scheduled task calls python3 directly.
cd /Users/weiwei/Documents/SKILL/follow-builders/scripts
python3 prepare_digest.py --lookback-hours 48 > /tmp/digest_data.json 2>/dev/null

# Show stats
python3 -c "
import json
with open('/tmp/digest_data.json') as f:
    data = json.load(f)
print(json.dumps(data.get('totals', {})))
"
