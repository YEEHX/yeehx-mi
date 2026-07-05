#!/bin/bash
# 双击导出觅影发布包：干净副本 + 安检 + zip（产物在 release/dist/）
cd "$(dirname "$0")/.."
python3 release/export_release.py
echo ""
read -n1 -p "按任意键关闭"
