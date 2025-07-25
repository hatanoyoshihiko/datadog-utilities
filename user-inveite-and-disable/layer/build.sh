#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
rm -rf python && mkdir python
pip3 install -r requirements.txt -t python
