#!/bin/bash
set -e

pip install -r requirements.txt pyinstaller

pyinstaller --noconfirm git-compare.spec

echo "Built: dist/git-compare.app"
