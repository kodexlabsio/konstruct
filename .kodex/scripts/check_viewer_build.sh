#!/usr/bin/env bash
# Rebuild the viewer and the MCP App views and fail if the pages konstruct ships differ from what
# is staged, so the committed konstruct/outputs/viewer.html and konstruct/outputs/views/ always
# match viewer/src. Run by pre-commit and CI.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
pnpm build > /dev/null
built=(konstruct/outputs/viewer.html konstruct/outputs/views)
if ! git diff --quiet -- "${built[@]}" || [ -n "$(git ls-files --others --exclude-standard -- "${built[@]}")" ]; then
    echo "konstruct/outputs/viewer.html or konstruct/outputs/views/ was rebuilt from viewer/src; stage it (git add ${built[*]})."
    exit 1
fi
