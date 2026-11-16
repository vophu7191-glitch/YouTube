#!/usr/bin/env bash
set -euo pipefail

export DENO_INSTALL="$HOME/.deno"
curl -fsSL https://deno.land/install.sh | sh
export PATH="$DENO_INSTALL/bin:$PATH"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
deno --version
