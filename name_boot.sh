#!/usr/bin/env bash
# 名字字形庫的自我擴張迴圈：每一輪用現有樣本去對齊 tesseract 的字串，
# 把「只差一兩個未知字」的那些補進樣本庫；涵蓋率不再成長就收工，
# 最後把所有名字裁圖重讀一遍寫成對照表。
cd /d/tryClaudeSteam || exit 1
PY="/c/Users/Willy/AppData/Local/Programs/Python/Python312/python.exe"
export PYTHONIOENCODING=utf-8

covered() {
  "$PY" -c "
import sys, collections
sys.path.insert(0, r'D:\tryClaudeSteam')
import dex
print(len({str(l) for l, _ in dex.load_glyphs('name')}))"
}

prev=$(covered)
echo "起始涵蓋 $prev 種字元"
for round in 1 2 3 4 5 6; do
  n=$((400 * round))
  "$PY" dex.py names extend --limit "$n" 2>&1 | tail -2
  now=$(covered)
  echo "第 $round 輪：涵蓋 $now 種字元"
  if [ "$now" -le "$prev" ] && [ "$round" -ge 3 ]; then
    echo "不再成長，收工"
    break
  fi
  prev=$now
done
"$PY" dex.py check 2>&1 | tail -3
"$PY" dex.py names read 2>&1 | tail -2
"$PY" dex.py export 2>&1 | tail -2
