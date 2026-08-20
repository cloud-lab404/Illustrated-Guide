#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
球員圖鑑掃描器 —— 把遊戲內「球員圖鑑」的卡片數值抓成 JSONL，給網頁圖鑑用。

遊戲自己的資料表（designData/*.bytes）是加密的，所以只能從畫面讀。
好在圖鑑的詳情頁右側有「›」可以直接翻下一張，不必回清單捲動：
開一張卡的詳細資訊之後，一路 › 就能掃完整個（已篩選的）清單，
而且切到第 2/3 頁之後翻卡不會跳回第 1 頁——所以一頁掃一趟，共三趟。

    第 1 頁  六項數值（基本／合計）
    第 2 頁  潛力
    第 3 頁  紅區（Hot/Cold Zones）、平均擊球仰角

一趟的節奏是「在模擬器裡自己迴圈」：screencap 存到 /data/local/tmp，
點 ›，睡一下，重複 BATCH 次，然後一次 adb pull 回來。這樣每張卡只花
一次裝置端截圖的時間，不用每張都付 adb 連線的來回成本（快 3 倍）。

座標全部是裝置原生的 900x1600，跟 reroll.py 的 603x1031 畫布無關。

    python dex.py filter   --serial emulator-5554 --type Moment      # 套用篩選，印出張數
    python dex.py sweep    --serial emulator-5554 --type Moment --page 1
    python dex.py calibrate shots/frame.png --page 1                 # 畫出取樣框，人眼驗證
    python dex.py parse     shots/frame.png --page 1                 # 印出單張的解析結果
    python dex.py merge     --type Moment                            # 三趟對齊、找漏
"""

import argparse
import concurrent.futures as _fut
import hashlib
import json
import os
import re
import subprocess
import sys
import time

import cv2
import numpy as np

try:
    import pytesseract
except ImportError:
    pytesseract = None

ADB_EXE = r"C:\LDPlayer\LDPlayer14\adb.exe"
TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
DEV_DIR = "/data/local/tmp/dexshots"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dex_data")
LABELS_PATH = os.path.join(OUT_DIR, "potential_labels.json")

DEVICE_SIZE = (900, 1600)          # 圖鑑是直向的，screencap 出來就是這個尺寸

# ---------------------------------------------------------------- 座標

# 詳情頁三頁共用的卡片抬頭
R_OVR      = (98, 118, 96, 84)      # 左上角 OVR
R_TEAM     = (100, 205, 88, 88)     # 球隊 logo（圖，用模板比對）
R_POS      = (104, 283, 80, 112)    # 守位徽章，最多兩個疊著
R_NAME     = (95, 562, 358, 88)     # 卡面下緣的名字（整條，實際字框再自己找）
R_CARD     = (85, 108, 360, 537)    # 整張卡面（拿來當「這張卡是誰」的指紋）
R_TYPE     = (455, 112, 358, 80)    # 右側面板標題＝卡片類型

# 第 1 頁：培育數值表
STAT_X     = [300, 386, 472, 558, 645, 731]   # 六個欄位的左緣
STAT_W     = 78
R_STAT_HDR = (283, 978, 535, 44)    # 欄位標題那一列（打者／投手不同，用來分類）
Y_BASE     = 1046                   # 「基本」列
Y_TOTAL    = 1338                   # 「合計」列（釘在表格底部，中間的列會捲動）
STAT_H     = 44

# 第 2 頁：潛力（左三右二）
POT_NAME_W, POT_NAME_H = 250, 44
POT_L_X, POT_R_X = 95, 465
POT_Y = [1096, 1191, 1286]
POT_GRADE_DX, POT_GRADE_W, POT_GRADE_H = 285, 62, 58

# 第 3 頁：紅區與仰角
ZONE_X0, ZONE_Y0, ZONE_SIZE = 515, 216, 76
ZONE_STEP_X, ZONE_STEP_Y = 77.5, 85.0
R_ANGLE = (585, 515, 150, 52)

PAGE_NEXT_CARD = (818, 370)         # 「›」下一張卡
PAGE_NEXT_TAB  = (758, 1438)        # 「»」下一頁
PAGE_PREV_CARD = (82, 370)          # 「‹」上一張卡
PAGE_PREV_TAB  = (140, 1438)        # 「«」上一頁
DETAIL_CLOSE   = (450, 1530)        # 詳情頁的 ✕

# 圖鑑清單頁
GRID_SEARCH    = (807, 283)         # 放大鏡＝進階搜尋
GRID_FIRST     = (165, 610)         # 第一張卡（清單頂端）
GRID_DETAIL    = (188, 1470)        # 選中卡片後的「詳細資訊」
R_COUNT        = (40, 385, 430, 60) # 「4/25478」

# 進階搜尋面板（以「卡片類型」標題為錨點算出來，見 find_type_grid）
PANEL_SEARCH   = (450, 1404)        # 「搜尋」
PANEL_CLOSE    = (450, 1545)        # 面板下方的 ✕（球隊全部改用錨點相對座標）
TYPE_COL_PITCH, TYPE_ROW_PITCH = 165.0, 87.4
TYPE_W, TYPE_H = 158, 85

CARD_TYPES = [
    ("Moment", 0, 0), ("Supreme Moment", 0, 1), ("Live", 0, 2), ("Season", 0, 3),
    ("Impact", 1, 0), ("Prime", 1, 1), ("Signature", 1, 2), ("Signature Black", 1, 3),
    ("FA Impact", 2, 0), ("FA Prime", 2, 1), ("FA Signature", 2, 2), ("FA Signature Black", 2, 3),
    ("WBC Prime", 3, 0), ("WBC Signature", 3, 1), ("WBC Signature Black", 3, 2), ("HOF", 3, 3),
]
TYPE_POS = {n: (r, c) for n, r, c in CARD_TYPES}

TEAMS_AL = ["NYY", "BOS", "TB", "TOR", "BAL", "MIN", "DET", "CWS",
            "CLE", "KC", "LAA", "TEX", "SEA", "ATH", "HOU"]
TEAMS_NL = ["PHI", "MIA", "ATL", "NYM", "WSH", "STL", "CHC", "MIL",
            "CIN", "PIT", "LAD", "COL", "SF", "SD", "AZ"]
TEAM_SLOT = {}                       # 球隊代碼 -> (聯盟, 列, 欄)
for _lg, _names in (("AL", TEAMS_AL), ("NL", TEAMS_NL)):
    for _i, _n in enumerate(_names):
        TEAM_SLOT[_n] = (_lg, _i // 4, _i % 4)
TEAM_DX = [82, 247, 412, 578]        # 球隊按鈕相對「OVR」錨點的位移
TEAM_DY = [390, 482, 572, 662]
TEAM_ALL_D = (362, 228)              # 球隊列的「全部」
TEAM_TAB_D = {"AL": (192, 301), "NL": (523, 301)}
TEAM_DIR = os.path.join(OUT_DIR, "teams")

# 掃描目標：非 Live、非 Season
SWEEP_TYPES = [n for n, _, _ in CARD_TYPES if n not in ("Live", "Season")]


def crop(img, box):
    x, y, w, h = box
    return img[y:y + h, x:x + w]


# ---------------------------------------------------------------- 裝置

class Device:
    def __init__(self, serial):
        self.serial = serial

    def _run(self, args, binary=False, timeout=60):
        r = subprocess.run([ADB_EXE, "-s", self.serial] + args,
                           capture_output=True, timeout=timeout)
        return r.stdout if binary else r.stdout.decode("utf-8", "replace")

    def sh(self, cmd, timeout=60):
        return self._run(["shell", cmd], timeout=timeout)

    def tap(self, x, y, wait=0.0):
        self.sh(f"input tap {int(x)} {int(y)}")
        if wait:
            time.sleep(wait)

    def swipe(self, x1, y1, x2, y2, ms=600, wait=0.0):
        self.sh(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(ms)}")
        if wait:
            time.sleep(wait)

    def grab(self):
        raw = self._run(["exec-out", "screencap", "-p"], binary=True)
        if not raw:
            return None
        return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)

    def check_size(self):
        out = self.sh("wm size")
        m = re.search(r"(\d+)x(\d+)", out)
        if not m:
            raise SystemExit(f"讀不到 {self.serial} 的解析度：{out!r}")
        w, h = sorted(map(int, m.groups()))
        if (w, h) != tuple(sorted(DEVICE_SIZE)):
            raise SystemExit(f"{self.serial} 是 {m.group(0)}，座標是照 900x1600 裁的，先改解析度")

    def zigzag_capture(self, pairs, local_dir, sleep_s=0.35):
        """
        一次進場把三頁都抓完：兩張卡一組，來回走 1→2→3（下一張）3→2→1，
        每組 6 張截圖、6 次點擊。這樣每張卡的三頁一定是同一張卡的，
        不必事後跨趟對齊；截圖裡的頁碼圓點又能驗證有沒有脫拍。
        """
        os.makedirs(local_dir, exist_ok=True)
        for f in os.listdir(local_dir):
            os.remove(os.path.join(local_dir, f))
        nt, pt, nc = PAGE_NEXT_TAB, PAGE_PREV_TAB, PAGE_NEXT_CARD
        self.sh(f"mkdir -p {DEV_DIR}; rm -f {DEV_DIR}/*", timeout=60)
        script = (
            f"i=0; while [ $i -lt {pairs} ]; do b=$((i*6)); "
            f"screencap -p {DEV_DIR}/f$((b+0)).png; input tap {nt[0]} {nt[1]}; sleep {sleep_s}; "
            f"screencap -p {DEV_DIR}/f$((b+1)).png; input tap {nt[0]} {nt[1]}; sleep {sleep_s}; "
            f"screencap -p {DEV_DIR}/f$((b+2)).png; input tap {nc[0]} {nc[1]}; sleep {sleep_s}; "
            f"screencap -p {DEV_DIR}/f$((b+3)).png; input tap {pt[0]} {pt[1]}; sleep {sleep_s}; "
            f"screencap -p {DEV_DIR}/f$((b+4)).png; input tap {pt[0]} {pt[1]}; sleep {sleep_s}; "
            f"screencap -p {DEV_DIR}/f$((b+5)).png; input tap {nc[0]} {nc[1]}; sleep {sleep_s}; "
            f"i=$((i+1)); done")
        self.sh(script, timeout=max(180, int(pairs * 12)))
        self._run(["pull", DEV_DIR + "/.", local_dir], timeout=max(180, pairs * 8))
        self.sh(f"rm -f {DEV_DIR}/*", timeout=60)
        files = [f for f in os.listdir(local_dir) if f.endswith(".png")]
        files.sort(key=lambda t: int(re.sub(r"\D", "", t) or 0))
        return [os.path.join(local_dir, f) for f in files]

    def batch_capture(self, n, local_dir, tap_xy, sleep_s=0.40):
        """在裝置裡連拍 n 張：截圖 -> 點 tap_xy -> 睡。回傳本機檔案清單（依序）。"""
        os.makedirs(local_dir, exist_ok=True)
        for f in os.listdir(local_dir):
            os.remove(os.path.join(local_dir, f))
        x, y = tap_xy
        self.sh(f"mkdir -p {DEV_DIR}; rm -f {DEV_DIR}/*", timeout=60)
        script = (f"i=0; while [ $i -lt {n} ]; do "
                  f"screencap -p {DEV_DIR}/f$i.png; "
                  f"input tap {x} {y}; sleep {sleep_s}; "
                  f"i=$((i+1)); done")
        self.sh(script, timeout=max(120, int(n * 3)))
        self._run(["pull", DEV_DIR + "/.", local_dir], timeout=max(120, n * 2))
        self.sh(f"rm -f {DEV_DIR}/*", timeout=60)
        files = [f for f in os.listdir(local_dir) if f.endswith(".png")]
        files.sort(key=lambda s: int(re.sub(r"\D", "", s) or 0))
        return [os.path.join(local_dir, f) for f in files]


# ---------------------------------------------------------------- OCR

def setup_ocr():
    if pytesseract is None:
        raise SystemExit("需要 pytesseract：pip install pytesseract")
    if os.path.exists(TESSERACT):
        pytesseract.pytesseract.tesseract_cmd = TESSERACT
    try:
        pytesseract.get_tesseract_version()
    except Exception as e:
        raise SystemExit(f"Tesseract 不能用：{e}")


def _masks(img):
    """
    回傳幾種候選遮罩（白＝字）。卡面的字疊在會發光變色的背景上，單一 Otsu
    很容易被背景亮度帶偏，所以另外用「這塊裡最亮的幾成像素」去挑字：
    數字都有深色描邊，在自己的小方框裡幾乎一定是最亮的東西，
    這樣跟背景是咖啡色、紫色還是紅色都無關。
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    out = []
    for p in (85, 92):                       # 最常成功的排前面，配合早退省時間
        t = float(np.percentile(gray, p))
        out.append(((gray >= t) * 255).astype(np.uint8))
    _, o = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    out.append(o)
    if img.ndim == 3:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        out.append(cv2.inRange(hsv, np.array([0, 0, 200], np.uint8),
                                    np.array([179, 60, 255], np.uint8)))
    t = float(np.percentile(gray, 75))
    out.append(((gray >= t) * 255).astype(np.uint8))
    out.append(255 - o)
    if img.ndim == 3:
        out.append(cv2.inRange(cv2.cvtColor(img, cv2.COLOR_BGR2HSV),
                               np.array([0, 0, 165], np.uint8),
                               np.array([179, 90, 255], np.uint8)))
    return out


def _prep(mask, scale):
    """放大、轉成黑字白底、四周留白。沒有留白 tesseract 常常整塊讀不到。"""
    m = cv2.resize(mask, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    m = cv2.bitwise_not(m)
    return cv2.copyMakeBorder(m, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=255)


def _read(mask, whitelist, psm, scale):
    """單次 OCR，回傳 (文字, 平均信心)。"""
    cfg = f"--psm {psm}"
    if whitelist:
        cfg += f" -c tessedit_char_whitelist={whitelist}"
    try:
        d = pytesseract.image_to_data(_prep(mask, scale), lang="eng", config=cfg,
                                      output_type=pytesseract.Output.DICT)
    except Exception:
        return "", -1.0
    words, confs = [], []
    for t, c in zip(d["text"], d["conf"]):
        t = t.strip()
        if not t:
            continue
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if c < 0:
            continue
        words.append(t)
        confs.append(c)
    if not words:
        return "", -1.0
    return " ".join(words), sum(confs) / len(confs)


def ocr_field(img, whitelist=None, psm=7, scale=4, early=88.0, min_len=2):
    """
    對每種遮罩各讀一次，取信心最高的；信心接近時取字比較多的那個。
    讀到夠有信心就收工——名字這一塊佔了整張卡解析時間的一半以上。
    """
    got = []
    for m in _masks(img):
        txt, conf = _read(m, whitelist, psm, scale)
        if txt:
            got.append((conf, len(txt), txt))
            if conf >= early and len(txt) >= min_len:
                break
    if not got:
        return "", -1.0
    top = max(c for c, _, _ in got)
    near = [g for g in got if g[0] >= top - 10]
    near.sort(key=lambda g: (g[1], g[0]))       # 信心差不多就選讀到比較完整的
    conf, _, txt = near[-1]
    return txt, conf


def ocr_int(img, lo=0, hi=999, psm=7, scale=5, want_votes=2, full=False):
    """
    只讀數字，靠投票：單次讀取會漏掉前面那位數（把 78 讀成 8），所以每種遮罩各讀一次。
    有兩種遮罩讀到同一個值就直接收工——不早退的話一個數字要跑七次 tesseract，
    一張卡二十幾個數字就要三十秒，整批掃描會被 OCR 拖死。
    回傳 (值, 得票率 0~100)。full=True 時跑完所有遮罩（給字形收集用）。
    """
    votes = []
    for m in _masks(img):
        txt, _ = _read(m, "0123456789", psm, scale)
        digits = re.sub(r"\D", "", txt)
        if digits:
            v = int(digits)
            if lo <= v <= hi:
                votes.append(v)
                if not full and votes.count(v) >= want_votes:
                    return v, round(100.0 * votes.count(v) / len(votes), 1)
    if not votes:
        return None, 0.0
    best = max(set(votes), key=votes.count)
    return best, round(100.0 * votes.count(best) / len(votes), 1)


def name_box(strip):
    """
    在卡面下緣那一條裡找出名字的實際範圍。名字的位置會隨卡種左右移動
    （HOF 是全名居中、季卡是「A. Kirk'26」偏左），固定裁切一定會切到字，
    所以先用亮度遮罩把字圈出來，取字高合理的那些區塊的外框。
    """
    gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
    t = float(np.percentile(gray, 88))
    mask = ((gray >= max(t, 150)) * 255).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 9), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, 8)
    xs0, ys0, xs1, ys1 = [], [], [], []
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if not (14 <= h <= 46) or area < 40 or w > 300:
            continue
        xs0.append(x); ys0.append(y); xs1.append(x + w); ys1.append(y + h)
    if not xs0:
        return None
    x0, y0 = max(0, min(xs0) - 6), max(0, min(ys0) - 6)
    x1, y1 = min(strip.shape[1], max(xs1) + 6), min(strip.shape[0], max(ys1) + 6)
    if x1 - x0 < 40 or y1 - y0 < 16:
        return None
    return strip[y0:y1, x0:x1]


NAME_CHARS = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.'- 0123456789")


def ocr_name(img):
    """img 是卡面下緣那一條；會先找字框再讀，找不到字框就整條讀。"""
    # 不早退：名字讀到一半也可能有高信心（"Babe Ruth" 只讀到 "Babe" 也給 85 分），
    # 所以每種遮罩都跑完，信心接近時取字比較長的。
    cands = []
    box = name_box(img)
    for cand in ([box] if box is not None else []) + [img]:
        txt, conf = ocr_field(cand, whitelist=NAME_CHARS, psm=7, scale=4, early=200)
        txt = re.sub(r"\s+", " ", txt).strip(" .-|_")
        if len(txt) >= 3:
            cands.append((conf, len(txt), txt))
    if not cands:
        return "", -1.0
    top = max(c for c, _, _ in cands)
    near = [c for c in cands if c[0] >= top - 12]
    near.sort(key=lambda c: (c[1], c[0]))
    conf, _, txt = near[-1]
    return txt, conf


# ------------------------------------------------- 自建字形比對（數字用）
#
# tesseract 的 eng 模型讀這款遊戲的數字會錯得很有規律：OVR 的大斜體
# 84 讀成 24、78 讀成 18，表格裡的 95 讀成 99、74 讀成 14——都是同一個
# 字型它沒見過。但遊戲的字型是固定的，同一個數字每次畫出來的點陣一模一樣，
# 所以自己收一份字形樣本、用點陣距離比對，比通用 OCR 準得多。
#
# 每種字型（ovr／stat）各存一份 20x28 的字形樣本在
# dex_data/glyphs/<font>/<數字>_<序號>.png，用 dex.py glyphs 指令收集與標記。

GLYPH_W, GLYPH_H = 20, 28
GLYPH_DIR = os.path.join(OUT_DIR, "glyphs")
_GLYPHS = {}


# 每種字型的字高／字寬是固定的，拿來擋掉火焰、邊框那些雜訊區塊
GLYPH_SPEC = {
    "ovr":  {"h": (44, 62), "w": (14, 52), "area": 220},
    "stat": {"h": (20, 40), "w": (6, 34),  "area": 45},
    "zone": {"h": (28, 62), "w": (10, 46), "area": 90},
    "grade": {"h": (20, 46), "w": (8, 40), "area": 40},
    "pos":   {"h": (18, 40), "w": (6, 34), "area": 40},
}


def _norm_glyph(g):
    """縮成 20x28，但保留寬高比（置中留白）——不然 1 會被拉成跟 7 一樣寬。"""
    h, w = g.shape[:2]
    scale = GLYPH_H / float(h)
    nw = max(1, min(GLYPH_W, int(round(w * scale))))
    r = cv2.resize(g, (nw, GLYPH_H), interpolation=cv2.INTER_AREA)
    out = np.zeros((GLYPH_H, GLYPH_W), np.uint8)
    x0 = (GLYPH_W - nw) // 2
    out[:, x0:x0 + nw] = r
    return (out > 127).astype(np.uint8)


def _glyph_masks(img, font):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    out = []
    for p in (93, 88, 82, 75):
        t = float(np.percentile(gray, p))
        out.append(((gray >= t) * 255).astype(np.uint8))
    _, o = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    out += [o, 255 - o]
    return out


def segment_digits(img, font):
    """
    從一小塊圖裡切出數字點陣，回傳 [(x, 20x28 bitmap)]，依左到右排序。
    字高／字寬要落在這個字型的固定範圍裡，這樣火焰、格線、簽名那些
    亮區塊就不會被當成數字。斜體的兩位數偶爾黏在一起，太寬就對半切。
    """
    spec = GLYPH_SPEC[font]
    hlo, hhi = spec["h"]
    wlo, whi = spec["w"]
    best = []
    for mask in _glyph_masks(img, font):
        n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        cand = []
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            if not (hlo <= h <= hhi) or area < spec["area"]:
                continue
            piece = (lab[y:y + h, x:x + w] == i).astype(np.uint8) * 255
            if w > whi and w / h <= 2.2:            # 兩個數字黏在一起
                half = w // 2
                if wlo <= half <= whi:
                    cand.append((x, _norm_glyph(piece[:, :half])))
                    cand.append((x + half, _norm_glyph(piece[:, half:])))
                continue
            if not (wlo <= w <= whi):
                continue
            cand.append((x, _norm_glyph(piece)))
        if len(cand) > 3:                            # 圖鑑的數字最多三位，取基線最一致的
            cand = cand[:3]
        cand.sort(key=lambda t: t[0])
        if len(cand) > len(best):
            best = cand
        if len(best) >= 2:
            break
    return best


def load_glyphs(font):
    if font in _GLYPHS:
        return _GLYPHS[font]
    protos = []
    d = os.path.join(GLYPH_DIR, font)
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            # 檔名有兩種：數字字型用「7_0003.png」，名字字型用碼點「x61_0003.png」
            # （Windows 檔名不分大小寫，a 和 A 會撞，所以要編碼）
            m = re.match(r"x([0-9a-f]{2,6})_", f)
            if m:
                lab = chr(int(m.group(1), 16))
            else:
                m = re.match(r"([0-9A-Z]+)_", f)
                if not m:
                    continue
                lab = m.group(1)
            img = cv2.imdecode(np.fromfile(os.path.join(d, f), dtype=np.uint8),
                               cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            protos.append((lab, _norm_glyph(img)))
    _GLYPHS[font] = protos
    return protos


def match_glyph(bitmap, font):
    """回傳 (數字, 相似度 0~1, 與次佳的差距)。沒有樣本就回 (None, 0, 0)。"""
    protos = load_glyphs(font)
    if not protos:
        return None, 0.0, 0.0
    scores = {}
    total = GLYPH_W * GLYPH_H
    for digit, p in protos:
        sim = 1.0 - float(np.count_nonzero(bitmap ^ p)) / total
        if sim > scores.get(digit, -1):
            scores[digit] = sim
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, s1 = ranked[0]
    s2 = ranked[1][1] if len(ranked) > 1 else 0.0
    return best, s1, s1 - s2


def read_digits(img, font, lo, hi, dump=None):
    """
    用字形樣本讀數字。回傳 (值, 信心 0~100)。
    信心＝最差那個字形的相似度；有字形對不上就順手存起來，方便補樣本。
    """
    glyphs = segment_digits(img, font)
    if not glyphs:
        return None, 0.0
    hits, drops, worst = [], 0, 1.0
    for x, g in glyphs:
        d, sim, margin = match_glyph(g, font)
        if d is None or sim < 0.88 or margin < 0.008:
            if dump:
                os.makedirs(dump, exist_ok=True)
                h = hashlib.md5(g.tobytes()).hexdigest()[:10]
                cv2.imencode(".png", g * 255)[1].tofile(
                    os.path.join(dump, f"unknown_{font}_{h}.png"))
            drops += 1
            continue
        hits.append(str(d))
        worst = min(worst, sim)
    if not hits:
        return None, 0.0
    # 對不上的區塊多半是卡圖碎片（OVR 那格疊在會發光的卡面上），所以允許丟掉，
    # 但值一定要落在合理範圍內——丟掉真的數字時值會超出範圍，自然會被擋掉。
    try:
        v = int("".join(hits))
    except ValueError:
        return None, 0.0
    if not (lo <= v <= hi):
        return None, 0.0
    conf = worst * 100 - (12 * drops)
    return v, round(max(0.0, conf), 1)


# 這些字型不要退回 tesseract：它對這款遊戲的數字有系統性錯誤（7 讀成 1、5 讀成 9），
# 寫進資料庫的錯數字沒人看得出來，不如留空白，之後用排序或存下來的截圖補。
NO_TESSERACT = ("ovr", "stat")


def read_number(img, font, lo, hi, psm=7, scale=5, dump=None):
    """先用字形比對；對不上時看這個字型能不能信 tesseract。"""
    v, conf = read_digits(img, font, lo, hi, dump=dump)
    if v is not None or font in NO_TESSERACT:
        return v, conf
    return ocr_int(img, lo, hi, psm=psm, scale=scale)


def phash(img):
    """給中文字圖用的指紋：縮成 16x16 灰階再雜湊，同一個詞每次都一樣。"""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    g = cv2.resize(g, (32, 16), interpolation=cv2.INTER_AREA)
    _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return hashlib.md5(b.tobytes()).hexdigest()[:16]


# ---------------------------------------------------------------- 解析

def looks_like_detail(img):
    """畫面是不是卡片詳情頁：卡面右邊有面板、底部有 ✕。粗略檢查，用來擋彈窗。"""
    if img is None or img.shape[:2] != (DEVICE_SIZE[1], DEVICE_SIZE[0]):
        return False
    card = crop(img, R_CARD)
    if card.size == 0:
        return False
    return float(card.std()) > 25.0        # 卡面是圖，變化大；純色彈窗會很平


VALID_POS = ("SP", "RP", "CP", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH")

# 守位徽章：文字是金色或青色、底是深色方塊，所以用顏色遮罩把整塊字圈出來，
# 正規化成 64x32 再跟樣本比對。用整塊而不是逐字，是因為 SS、RF 這種字會黏在一起。
BADGE_DIR = os.path.join(OUT_DIR, "badges")
BADGE_W, BADGE_H = 64, 32
_BADGES = None


def badge_masks(img):
    """回傳畫面上每個守位徽章的遮罩（由上到下），已正規化成 64x32。"""
    reg = img[272:400, 98:192]
    hsv = cv2.cvtColor(reg, cv2.COLOR_BGR2HSV)
    m = cv2.bitwise_or(
        cv2.inRange(hsv, np.array([78, 90, 110]), np.array([105, 255, 255])),   # 青
        cv2.inRange(hsv, np.array([12, 70, 120]), np.array([38, 255, 255])))    # 金
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, 8)
    parts = []
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if 16 <= h <= 40 and 5 <= w <= 44 and area >= 60:
            parts.append((y, y + h, x, x + w))
    rows = []
    for y0, y1, x0, x1 in sorted(parts):
        if rows and abs(rows[-1][0] - y0) <= 14:          # 同一列的字併起來
            r = rows[-1]
            rows[-1] = (min(r[0], y0), max(r[1], y1), min(r[2], x0), max(r[3], x1))
        else:
            rows.append((y0, y1, x0, x1))
    out = []
    for y0, y1, x0, x1 in rows:
        if x1 - x0 < 12:
            continue
        sub = m[max(0, y0 - 2):y1 + 2, max(0, x0 - 2):x1 + 2]
        if sub.size == 0:
            continue
        g = cv2.resize(sub, (BADGE_W, BADGE_H), interpolation=cv2.INTER_AREA)
        out.append((g > 110).astype(np.uint8))
    return out


def load_badges():
    global _BADGES
    if _BADGES is not None:
        return _BADGES
    out = []
    if os.path.isdir(BADGE_DIR):
        for f in sorted(os.listdir(BADGE_DIR)):
            m = re.match(r"([A-Z0-9]+)_", f)
            if not m or not f.endswith(".png"):
                continue
            img = cv2.imdecode(np.fromfile(os.path.join(BADGE_DIR, f), dtype=np.uint8),
                               cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            g = cv2.resize(img, (BADGE_W, BADGE_H), interpolation=cv2.INTER_AREA)
            out.append((m.group(1), (g > 110).astype(np.uint8)))
    _BADGES = out
    return out


def match_badge(g):
    protos = load_badges()
    if not protos:
        return None, 0.0
    best, bs = None, -1.0
    for code, p in protos:
        sim = 1.0 - float(np.count_nonzero(g ^ p)) / (BADGE_W * BADGE_H)
        if sim > bs:
            best, bs = code, sim
    return best, round(bs, 3)


def read_positions(img, dump=None):
    """
    守位徽章（最多兩個疊著）。整塊比對，回傳 (守位清單, 最差相似度 0~100)。
    對不上的樣式會存起來，方便之後補樣本。
    """
    out, worst = [], 1.0
    for g in badge_masks(img):
        code, sim = match_badge(g)
        if code is None or sim < 0.86 or code not in VALID_POS:
            if dump:
                os.makedirs(dump, exist_ok=True)
                h = hashlib.md5(g.tobytes()).hexdigest()[:10]
                cv2.imencode(".png", g * 255)[1].tofile(
                    os.path.join(dump, f"badge_{h}.png"))
            continue
        if code not in out:
            out.append(code)
        worst = min(worst, sim)
    return out, round(worst * 100, 1) if out else 0.0


def parse_header(img, read_name=True):
    ovr, ovr_conf = read_number(crop(img, R_OVR), "ovr", 10, 130)
    name, name_conf = ocr_name(crop(img, R_NAME)) if read_name else ("", 0.0)
    positions, pos_conf = read_positions(img, dump=os.path.join(BADGE_DIR, "_unknown"))
    return {
        "ovr": ovr, "ovr_conf": round(ovr_conf, 1),
        "name": name, "name_conf": round(name_conf, 1),
        "positions": positions, "pos_conf": round(pos_conf, 1),
        "card_hash": phash(crop(img, R_CARD)),
    }


def card_class(img):
    """
    打者還是投手：看數值表第一個欄位標題是「力量」還是「球速」。
    （打者 力量/準確/選球/耐力/跑壘/守備；投手 球速/變化/球威/控球/持久力/守備）
    """
    cell = img[980:1020, 300:378]
    sb = find_tpl(cell, "圖鑑_打者欄位.png", 0.0)[1]
    sp = find_tpl(cell, "圖鑑_投手欄位.png", 0.0)[1]
    if max(sb, sp) < 0.55:
        return None, round(max(sb, sp), 3)
    return ("batter" if sb >= sp else "pitcher"), round(abs(sb - sp), 3)


STAT_LABELS = {
    "batter":  ["力量", "準確", "選球", "耐力", "跑壘", "守備"],
    "pitcher": ["球速", "變化", "球威", "控球", "持久力", "守備"],
}


def parse_page1(img):
    """六項數值：基本列與合計列，另外判斷這張是打者還是投手（欄位名不同）。"""
    kls, margin = card_class(img)
    out = {"class": kls, "class_margin": margin}
    for key, y in (("base", Y_BASE), ("total", Y_TOTAL)):
        vals, confs = [], []
        for x in STAT_X:
            v, c = read_number(img[y:y + STAT_H, x:x + STAT_W], "stat", 1, 200)
            vals.append(v)
            confs.append(round(c, 1))
        out[key] = vals
        out[key + "_conf"] = confs
    return out


def potential_slots(img, x, y):
    """
    數潛力那一列的圓點與鎖頭。名稱本身每個類別都一樣（打者五項、投手五項），
    真正每張卡不同的是「這一項可以練到幾級」——亮綠點是現在的等級，
    灰點是可解鎖的等級，鎖頭是要界限突破才開的等級。回傳 (點數, 鎖數)。
    """
    strip = img[y + 40:y + 72, x + 5:x + 195]
    if strip.size == 0:
        return 0, 0
    hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
    v, sat = hsv[:, :, 2], hsv[:, :, 1]
    mask = ((v > 55) * 255).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, 8)
    dots = locks = 0
    for i in range(1, n):
        bx, by, bw, bh, area = st[i]
        if area < 25:
            continue
        bright = float(np.percentile(v[by:by + bh, bx:bx + bw], 90))
        s_med = float(np.median(sat[by:by + bh, bx:bx + bw]))
        # 圓點是彩色的小圓（亮綠或暗灰綠，飽和度高）；鎖頭是白灰色、略大一點
        if bright > 180 and s_med < 60 and 11 <= bw <= 22 and 12 <= bh <= 26:
            locks += 1
        elif 8 <= bw <= 14 and 8 <= bh <= 14:
            dots += 1
    return dots, locks


def parse_page2(img):
    """
    潛力：左三右二，取名稱（中文，存點陣指紋，之後查表變文字）。
    等級不抓——沒持有的卡在圖鑑裡一律顯示 D（實測 240 個框都是 D），
    那是本帳號的解鎖進度而不是卡片本身的資料，讀它只是白花時間。
    """
    slots = []
    for i, y in enumerate(POT_Y):
        for x in (POT_L_X, POT_R_X):
            if x == POT_R_X and i == 2:          # 右欄只有兩格
                continue
            name_img = img[y:y + POT_NAME_H, x:x + POT_NAME_W]
            if float(name_img.std()) < 6.0:      # 空格
                continue
            dots, locks = potential_slots(img, x, y)
            slots.append({"name_hash": phash(name_img), "dots": dots, "locks": locks})
    return {"potentials": slots}


def zone_grid_present(img):
    """
    第 3 頁只有打者有紅區九宮格；投手那一頁放的是「體力」與「球種資訊」。
    所以先看九宮格區域是不是紅／藍色塊，再決定要讀什麼。
    """
    h = int(3 * ZONE_STEP_Y)
    w = int(3 * ZONE_STEP_X)
    reg = img[ZONE_Y0:ZONE_Y0 + h, ZONE_X0:ZONE_X0 + w]
    if reg.size == 0:
        return False
    hsv = cv2.cvtColor(reg, cv2.COLOR_BGR2HSV)
    red = cv2.bitwise_or(cv2.inRange(hsv, np.array([0, 90, 60]), np.array([12, 255, 255])),
                         cv2.inRange(hsv, np.array([168, 90, 60]), np.array([179, 255, 255])))
    blue = cv2.inRange(hsv, np.array([100, 90, 60]), np.array([130, 255, 255]))
    return float(cv2.bitwise_or(red, blue).mean()) / 255.0 >= 0.40


# 投手第 3 頁的「球種資訊」：左三右二，每列是 球種代碼 ＋ 等級
PITCH_Y = [1105, 1176, 1247]
PITCH_COLS = ((165, 375), (525, 735))        # (名稱 x, 等級 x)
PITCH_NAME_W, PITCH_GRADE_W, PITCH_H = 205, 66, 60


def parse_page3(img):
    """
    打者：紅區九宮格（數字＋冷熱）與平均擊球仰角。
    投手：這一頁換成「體力」與「球種資訊」，所以改抓球種與等級。
    """
    if not zone_grid_present(img):
        pitches = []
        for y in PITCH_Y:
            for xn, xg in PITCH_COLS:
                name, nconf = ocr_field(img[y:y + PITCH_H, xn:xn + PITCH_NAME_W],
                                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                                        psm=7, scale=4)
                name = re.sub(r"[^A-Z0-9]", "", (name or "").upper())
                grade, gconf = ocr_field(img[y:y + PITCH_H, xg:xg + PITCH_GRADE_W],
                                         "SABCD", psm=10, scale=5)
                grade = (grade or "").strip()[:1]
                if not (2 <= len(name) <= 4) or grade not in ("S", "A", "B", "C", "D"):
                    continue
                pitches.append({"pitch": name, "grade": grade,
                                "conf": round(min(nconf, 100.0), 1)})
        return {"zones": [], "angle": None, "pitches": pitches}

    zones = []
    for r in range(3):
        for c in range(3):
            x = int(ZONE_X0 + c * ZONE_STEP_X)
            y = int(ZONE_Y0 + r * ZONE_STEP_Y)
            cell = img[y:y + ZONE_SIZE, x:x + ZONE_SIZE]
            v, conf = read_number(cell, "zone", 0, 9, psm=10, scale=6)
            b, g, rr = [float(m) for m in cv2.mean(cell)[:3]]
            kind = "hot" if rr > g + 25 and rr > b + 25 else                    "cold" if b > rr + 25 else "neutral"
            zones.append({"v": v, "kind": kind, "conf": round(conf, 1)})
    angle, a_conf = read_number(crop(img, R_ANGLE), "zone", 0, 90)
    return {"zones": zones, "angle": angle, "angle_conf": round(a_conf, 1),
            "pitches": []}


PARSERS = {1: parse_page1, 2: parse_page2, 3: parse_page3}


def parse_frame(img, page):
    rec = parse_header(img)
    rec.update(PARSERS[page](img))
    return rec


# ---------------------------------------------------------------- 篩選

_TPL = {}


def _tpl(name):
    if name not in _TPL:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", name)
        if not os.path.exists(p):
            raise SystemExit(f"缺模板 {name}，先跑 dex.py mktemplate")
        _TPL[name] = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)
    return _TPL[name]


def find_tpl(img, name, thr=0.70):
    """回傳 (左上角座標, 分數)；找不到就 (None, 分數)。"""
    t = _tpl(name)
    if img is None or img.shape[0] < t.shape[0] or img.shape[1] < t.shape[1]:
        return None, 0.0
    r = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(r)
    return (loc if score >= thr else None), score


def _panel_to_top(dev):
    """
    進階搜尋面板會記住上次的捲動位置，所以每次都先用力往下拉回頂端，
    不然「球隊全部」那顆按鈕的固定座標會點到別的東西。
    回傳「OVR」標籤的左上角，之後所有座標都相對它算。
    """
    for _ in range(3):
        for _ in range(4):
            dev.swipe(105, 600, 105, 1320, 450, wait=0.35)
        loc, score = find_tpl(dev.grab(), "圖鑑_OVR.png", 0.70)
        if loc:
            return loc
    return None


# OVR 滑桿：把手中心 x = SLIDER_BX + 值 * SLIDER_PX（相對「OVR」標籤的 x）
# 這組數字是拿「顯示 76~120 時兩顆把手的位置」反解出來的
SLIDER_DY = 95
SLIDER_BX = 2.4          # 相對錨點 x
SLIDER_PX = 5.364


def read_ovr_fields(img, anchor):
    """
    讀 OVR 區間。不讀那兩個數字欄位——它們的字型 tesseract 也會讀錯（76 讀成 16）——
    改成用模板找滑桿上的兩顆把手，由位置換算回數值。lo 等於 hi 時兩顆會疊在一起，
    只會找到一群，那就兩邊都回同一個值。
    """
    ax, ay = anchor
    band = img[ay + SLIDER_DY - 20:ay + SLIDER_DY + 20, ax:ax + 700]
    t = _tpl("圖鑑_滑桿把手.png")
    if band.shape[0] < t.shape[0] or band.shape[1] < t.shape[1]:
        return None, None
    r = cv2.matchTemplate(band, t, cv2.TM_CCOEFF_NORMED)
    xs = sorted(int(x) + t.shape[1] // 2 for x in np.where(r >= 0.72)[1])
    if not xs:
        return None, None
    groups = [[xs[0]]]
    for x in xs[1:]:
        if x - groups[-1][-1] <= 12:
            groups[-1].append(x)
        else:
            groups.append([x])
    cent = [sum(g) / len(g) for g in groups]
    to_v = lambda x: max(0, min(120, int(round((x - SLIDER_BX) / SLIDER_PX))))
    if len(cent) == 1:
        v = to_v(cent[0])
        return v, v
    return to_v(cent[0]), to_v(cent[-1])


def set_ovr_range(dev, anchor, lo, hi, verbose=False):
    """
    OVR 區間只能拉滑桿（數字欄位是唯讀的）。兩個實測到的怪癖：
      1. 太短的拖曳不算拖曳——差 1~2 格時滑桿完全不動，所以先繞遠再拉回來。
      2. 放手的位置會系統性偏右半格，所以目標 x 扣掉 0.5 格。
    每一輪都重新讀把手位置，只補剩下的差距。
    """
    ax, ay = anchor
    bx, by = ax + SLIDER_BX, ay + SLIDER_DY
    px = SLIDER_PX
    lim = (bx - 4, bx + 120 * px + 4)
    clamp = lambda x: min(max(x, lim[0]), lim[1])
    at = lambda v: clamp(bx + (v - 0.5) * px)
    stuck = 0
    for _ in range(20):
        cur_lo, cur_hi = read_ovr_fields(dev.grab(), anchor)
        if verbose:
            print(f"    滑桿現在 {cur_lo}~{cur_hi}（目標 {lo}~{hi}）")
        if cur_lo is None:
            time.sleep(0.4)
            continue
        if (cur_lo, cur_hi) == (lo, hi):
            return True
        which = "hi" if cur_hi != hi else "lo"
        cur = cur_hi if which == "hi" else cur_lo
        want = hi if which == "hi" else lo
        if abs(want - cur) < 4:                     # 差太少，先繞遠一點再拉回來
            detour = cur - 20 if cur > 60 else cur + 20
            dev.swipe(clamp(bx + cur * px), by, at(detour), by, 500, wait=0.5)
            cur = detour
        dev.swipe(clamp(bx + cur * px), by, at(want), by, 700, wait=0.6)
        got = read_ovr_fields(dev.grab(), anchor)
        if got == (cur_lo, cur_hi):
            stuck += 1
            if stuck >= 4:
                return False
        else:
            stuck = 0
    return False


def reset_ovr(dev, anchor):
    """按 OVR 右邊那顆 ↻ 把區間還原成 0~120。"""
    dev.tap(anchor[0] + 617, anchor[1] + 27, wait=0.8)
    return read_ovr_fields(dev.grab(), anchor) == (0, 120)


def set_ovr_exact(dev, anchor, v, verbose=False):
    """
    把 OVR 區間設成剛好 [v, v]。
    先還原成 0~120，再把上限拉到 v（拖不到就繞遠再拉），最後把下限往右拉過頭——
    下限會被上限卡住，剛好停在 v，一次就到位，不必微調。
    """
    ax, ay = anchor
    bx, by = ax + SLIDER_BX, ay + SLIDER_DY
    lim = (bx - 4, bx + 120 * SLIDER_PX + 4)
    clamp = lambda x: min(max(x, lim[0]), lim[1])
    at = lambda t: clamp(bx + (t - 0.5) * SLIDER_PX)
    reset_ovr(dev, anchor)
    for _ in range(8):
        cur_lo, cur_hi = read_ovr_fields(dev.grab(), anchor)
        if verbose:
            print(f"    滑桿 {cur_lo}~{cur_hi} -> [{v},{v}]")
        if cur_lo is None:
            time.sleep(0.4)
            continue
        if cur_hi != v:
            cur = cur_hi
            if abs(v - cur) < 4:                       # 太近拖不動，先繞遠
                detour = cur - 20 if cur > 40 else cur + 20
                dev.swipe(clamp(bx + cur * SLIDER_PX), by, at(detour), by, 500, wait=0.5)
                cur = detour
            dev.swipe(clamp(bx + cur * SLIDER_PX), by, at(v), by, 700, wait=0.6)
            continue
        if cur_lo != v:                                # 往右拉過頭，會卡在上限
            dev.swipe(clamp(bx + cur_lo * SLIDER_PX), by, at(min(120, v + 12)), by, 700, wait=0.6)
            continue
        return True
    return read_ovr_fields(dev.grab(), anchor) == (v, v)


def find_type_grid(dev, settle_tries=6):
    """
    把面板捲到看得見整組卡片類型，回傳 (第一顆類型鈕左上角, 標題錨點)。
    捲動有慣性——剛放手時清單還在滑，這時算出來的座標會偏一格，
    就會點到隔壁的類型（實測點成 Live／Season／Impact），所以要等它停下來。
    """
    for _ in range(settle_tries):
        loc, _ = find_tpl(dev.grab(), "圖鑑_卡片類型.png", 0.70)
        if loc and 250 <= loc[1] <= 900:
            time.sleep(0.45)
            again, _ = find_tpl(dev.grab(), "圖鑑_卡片類型.png", 0.70)
            if again and abs(again[1] - loc[1]) <= 2:          # 兩次位置一樣＝停穩了
                return (again[0] + 4, again[1] + 65), again
            continue
        dev.swipe(105, 1180, 105, 820, 600, wait=1.2)
    return None, None


def type_selection(dev, anchor):
    """回傳目前被選中的卡片類型清單（看按鈕有沒有變藍）。"""
    img = dev.grab()
    mx, my = anchor[0] + 4, anchor[1] + 65
    on = []
    for name, r, c in CARD_TYPES:
        x = mx + TYPE_W / 2 + TYPE_COL_PITCH * c
        y = my + TYPE_H / 2 + TYPE_ROW_PITCH * r
        if _selected(img, x, y):
            on.append(name)
    return on


# ------------------------------------------------- 現在在哪個畫面
#
# 要連續跑好幾個小時，所以不能假設畫面停在原地：彈窗、誤觸、閃退都會跑掉。
# 每個切片開始前先把畫面收回「圖鑑清單」這個已知狀態。

MANAGE_DEX_ROW = (450, 1026)        # 球隊管理裡的「球員圖鑑」
NAV_MANAGE     = (516, 1520)        # 底部導覽的「球隊管理」


def page_of(img):
    """回傳 (目前第幾頁 0-based, 總頁數)；不是詳情頁就 (None, 頁數)。"""
    strip = img[1424:1456, 280:620]
    hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)

    def centers(mask):
        n, lab, st, cent = cv2.connectedComponentsWithStats(mask, 8)
        return sorted(int(cent[i][0]) for i in range(1, n) if st[i][4] >= 30)

    dots = centers(cv2.inRange(hsv, np.array([0, 0, 70]), np.array([179, 255, 255])))
    live = centers(cv2.inRange(hsv, np.array([18, 120, 150]), np.array([38, 255, 255])))
    if len(dots) < 2 or len(live) != 1:
        return None, len(dots)
    idx = min(range(len(dots)), key=lambda i: abs(dots[i] - live[0]))
    return idx, len(dots)


def where(dev, img=None):
    """
    判斷現在在哪個畫面。用「比分數取最高」而不是「第一個過門檻就算」——
    「球員圖鑑」跟「球隊管理」兩個標題都是深底白中文，單看門檻會互相誤判。
    """
    img = dev.grab() if img is None else img
    if img is None:
        return "unknown", None
    if page_of(img)[0] is not None:
        return "detail", img
    scores = {}
    for state, tpl in (("panel", "圖鑑_進階搜尋.png"),
                       ("grid", "圖鑑_分頁列.png"),
                       ("manage", "圖鑑_球隊管理標題.png"),
                       ("main", "圖鑑_導覽球隊管理.png")):
        scores[state] = find_tpl(img, tpl, 0.0)[1]
    state = max(scores, key=scores.get)
    if scores[state] < 0.80:
        return "unknown", img
    return state, img


def goto_dex(dev, tries=8, verbose=True):
    """不管現在在哪，把畫面帶回圖鑑清單。回傳 True/False。"""
    for _ in range(tries):
        state, img = where(dev)
        if state == "grid":
            return True
        if verbose:
            print(f"    現在在 {state}，往圖鑑清單走")
        if state == "panel":
            dev.tap(*PANEL_CLOSE, wait=1.5)
        elif state == "detail":
            dev.tap(*DETAIL_CLOSE, wait=1.8)
        elif state == "manage":
            dev.tap(*MANAGE_DEX_ROW, wait=3.0)
        elif state == "main":
            dev.tap(*NAV_MANAGE, wait=3.0)
        else:
            dev.key(4)                       # 退一步再看
            time.sleep(2.0)
    return False


def read_count(img):
    txt, _ = ocr_field(crop(img, R_COUNT), whitelist="0123456789/", psm=7, scale=3)
    m = re.search(r"(\d+)\s*/\s*(\d+)", txt.replace(" ", ""))
    return int(m.group(2)) if m else None


R_CHIP_TEAM = (50, 243, 240, 54)     # 清單上方第一個籤：球隊
R_CHIP_ROW  = (40, 243, 730, 54)     # 整條籤：只選一個類型時會直接顯示類型名稱


def _closest_team(txt):
    """把球隊籤的 OCR 結果比對到最接近的球隊代碼（容一個字元的誤差）。"""
    t = re.sub(r"[^A-Z]", "", (txt or "").upper())
    if not t:
        return None, 99
    best, bd = None, 99
    for code in TEAM_SLOT:
        a, b = code, t
        # 簡單編輯距離
        dp = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(dp[j] + 1, cur[j - 1] + 1, dp[j - 1] + (ca != cb)))
            dp = cur
        if dp[-1] < bd:
            best, bd = code, dp[-1]
    return best, bd


def _selected(img, x, y):
    """
    面板上的按鈕被選中會變成亮藍底白字。取整塊的中位數顏色來判斷——
    只看正中央那一點會打到白色字，反而判成沒選中。
    """
    x, y = int(x), int(y)
    y0, y1 = max(0, y - 16), min(img.shape[0], y + 16)
    x0, x1 = max(0, x - 30), min(img.shape[1], x + 30)
    patch = img[y0:y1, x0:x1]
    if patch.size == 0:
        return False
    b, g, r = [float(np.median(patch[:, :, i])) for i in range(3)]
    return b > 130 and b > r + 45


def _tap_until_selected(dev, xy, tries=3, wait=0.6):
    """點下去並確認真的選中了（座標算得再準，面板慣性捲動還是會讓點擊落空）。"""
    for _ in range(tries):
        if _selected(dev.grab(), *xy):
            return True
        dev.tap(*xy, wait=wait)
    return _selected(dev.grab(), *xy)


def verify_filter(img, card_type, team):
    """
    確認篩選真的套上了。單選一個卡片類型時，清單上方的籤會直接顯示類型名稱
    （多選才顯示「卡片 (N)」），所以就找那個名字；球隊則比對球隊籤。
    回傳問題清單，空的代表沒問題。
    """
    bad = []
    txt, _ = ocr_field(crop(img, R_CHIP_ROW),
                       "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ()",
                       psm=7, scale=3, early=200)
    flat = re.sub(r"[^A-Za-z0-9]", "", txt).lower()
    want = re.sub(r"[^A-Za-z0-9]", "", card_type).lower()
    # 籤上的字會被讀錯幾個字母（Supreme 讀成 Suprame），所以比「最長共同子序列」的比例，
    # 不要求完全相符。真正精確的那道檢查是面板裡「只有這一顆按鈕變藍」。
    m = [[0] * (len(flat) + 1) for _ in range(len(want) + 1)]
    for i, a in enumerate(want, 1):
        for j, b in enumerate(flat, 1):
            m[i][j] = m[i - 1][j - 1] + 1 if a == b else max(m[i - 1][j], m[i][j - 1])
    if not want or m[-1][-1] / len(want) < 0.6:
        bad.append(f"籤上找不到 {card_type}（讀到 {txt!r}）")
    if team:
        t, _ = ocr_field(crop(img, R_CHIP_TEAM),
                         "ABCDEFGHIJKLMNOPQRSTUVWXYZ", psm=7, scale=4, early=200)
        flat_t = re.sub(r"[^A-Z]", "", (t or "").upper())
        got, dist = _closest_team(t)
        if team not in flat_t and (got != team or dist > 1):
            bad.append(f"球隊籤是 {got}（讀到 {t!r}）")
    return bad


def apply_filter(dev, card_type, ovr=None, team=None, expect_max=20000, verbose=True):
    """把圖鑑清單篩成「這個卡片類型（＋球隊／OVR）」，回傳張數。"""
    if card_type not in TYPE_POS:
        raise SystemExit(f"沒有這個卡片類型：{card_type}")
    if team and team not in TEAM_SLOT:
        raise SystemExit(f"沒有這個球隊：{team}")
    row, col = TYPE_POS[card_type]
    for attempt in (1, 2, 3):
        dev.tap(*GRID_SEARCH, wait=2.0)
        top = _panel_to_top(dev)
        if top is None:
            if verbose:
                print("  面板捲不回頂端，重試")
            dev.tap(*PANEL_CLOSE, wait=1.5)
            continue
        dev.tap(top[0] + TEAM_ALL_D[0], top[1] + TEAM_ALL_D[1], wait=0.6)   # 球隊＝全部
        if team:
            lg, trow, tcol = TEAM_SLOT[team]     # 不能叫 row/col，會蓋掉卡片類型的座標
            dev.tap(top[0] + TEAM_TAB_D[lg][0], top[1] + TEAM_TAB_D[lg][1], wait=0.9)
            if not _tap_until_selected(dev, (top[0] + TEAM_DX[tcol], top[1] + TEAM_DY[trow])):
                if verbose:
                    print(f"  {team} 按鈕沒選上，重試")
                dev.tap(*PANEL_CLOSE, wait=1.5)
                continue
        if ovr is None:
            reset_ovr(dev, top)
        elif not set_ovr_exact(dev, top, ovr):
            if verbose:
                print(f"  OVR 設不到 {ovr}，重試")
            dev.tap(*PANEL_CLOSE, wait=1.5)
            continue
        grid, anchor = find_type_grid(dev)
        if grid is None:
            if verbose:
                print("  找不到卡片類型區塊，重試")
            dev.tap(*PANEL_CLOSE, wait=1.5)
            continue
        mx, my = grid
        dev.tap(anchor[0] + 565, anchor[1] + 29, wait=0.7)   # 「全部」＝清空已選
        tx = mx + TYPE_W / 2 + TYPE_COL_PITCH * col
        ty = my + TYPE_H / 2 + TYPE_ROW_PITCH * row
        dev.tap(tx, ty, wait=0.7)
        on = type_selection(dev, anchor)
        if on != [card_type]:                                # 點到隔壁或多選就重來
            if verbose:
                print(f"  類型選成 {on or '（沒選上）'}，重試"
                      f"（錨 {anchor}、點在 {int(tx)},{int(ty)}）")
                cv2.imencode(".png", dev.grab())[1].tofile(
                    os.path.join(OUT_DIR, "_filter_fail.png"))
            dev.tap(*PANEL_CLOSE, wait=1.5)
            continue
        dev.tap(*PANEL_SEARCH, wait=2.8)
        img = dev.grab()
        n = read_count(img)
        # 只看張數不夠：有一次「全部」把類型全清掉，結果拿到整個球隊 1157 張也小於上限，
        # 就這樣掃了一整批錯的卡。所以一定要核對籤上的條件。
        bad = verify_filter(img, card_type, team)
        if not n or n >= expect_max:
            bad.append(f"張數 {n}")
        if not bad:
            if verbose:
                print(f"  篩選 {card_type}/{team or '全部'} -> {n} 張")
            return n
        if verbose:
            print(f"  篩選沒套好（{'、'.join(bad)}），重試")
    return None


def open_first_detail(dev, page):
    """從清單頂端開第一張卡的詳細資訊，並切到指定頁。"""
    dev.tap(*GRID_FIRST, wait=1.2)
    dev.tap(*GRID_DETAIL, wait=2.4)
    for _ in range(page - 1):
        dev.tap(*PAGE_NEXT_TAB, wait=1.0)
    img = dev.grab()
    return looks_like_detail(img), img


# ---------------------------------------------------------------- 掃描

ZIG_PAGES = [0, 1, 2, 2, 1, 0]      # 一組六張截圖各自應該在第幾頁（0-based）


def _load(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def build_record(frames, idx, card_type, name_dir=None, read_name=False):
    """
    frames = 這張卡的 [第1頁, 第2頁, 第3頁] 影像，組成一筆資料。
    名字那一條會另外存成小圖：名字是這份資料的主鍵，但也是最難讀的一欄，
    留著就能之後改進辨識、離線重跑，不必再進遊戲掃一次。
    """
    rec = parse_header(frames[0], read_name=read_name)
    # OVR 也三頁都試：卡圖會發光變化，某一頁被光影蓋住的數字，換一頁常常就乾淨了
    if rec.get("ovr") is None or rec.get("ovr_conf", 0) < 90:
        for extra in frames[1:]:
            v, c = read_number(crop(extra, R_OVR), "ovr", 10, 130)
            if v is not None and c > rec.get("ovr_conf", 0):
                rec["ovr"], rec["ovr_conf"] = v, c
    if not rec.get("positions"):            # 守位徽章同理
        for extra in frames[1:]:
            pos, pconf = read_positions(extra)
            if pos:
                rec["positions"], rec["pos_conf"] = pos, pconf
                break
    if read_name:
        # 名字在三頁都是同一行字，但背景卡圖會動，所以三張都讀、取最好的那次
        for extra in frames[1:]:
            txt, conf = ocr_name(crop(extra, R_NAME))
            if len(txt) >= 3 and (conf > rec.get("name_conf", -1) + 5 or
                                  (conf > rec.get("name_conf", -1) - 5 and
                                   len(txt) > len(rec.get("name", "")))):
                rec["name"], rec["name_conf"] = txt, round(conf, 1)
    rec.update(parse_page1(frames[0]))
    rec.update(parse_page2(frames[1]))
    rec.update(parse_page3(frames[2]))
    rec["idx"] = idx
    rec["type"] = card_type
    rec["pages"] = page_of(frames[0])[1]
    if name_dir:
        os.makedirs(name_dir, exist_ok=True)
        cv2.imencode(".png", crop(frames[0], R_NAME))[1].tofile(
            os.path.join(name_dir, f"{idx:05d}.png"))
    flags = []
    if rec.get("ovr") is None:
        flags.append("no_ovr")
    if rec.get("name_conf", 0) < 60 or len(rec.get("name", "")) < 3:
        flags.append("weak_name")     # 現場不讀名字時一律會有這個旗標，由離線重讀補
    if any(v is None for v in rec.get("base", [])):
        flags.append("weak_stats")
    if rec.get("zones") and any(z["v"] is None for z in rec["zones"]):
        flags.append("weak_zones")
    if not rec.get("zones") and not rec.get("pitches"):
        flags.append("no_page3")
    if flags:
        rec["flags"] = flags
    return rec


_READY = False


def _ensure_ready():
    global _READY
    if not _READY:
        _worker_init()
        _READY = True


def _worker_init():
    setup_ocr()
    load_glyphs("ovr"); load_glyphs("stat"); load_glyphs("zone")
    load_glyphs("grade"); load_glyphs("pos"); load_badges()


def _parse_job(job):
    """子行程的工作：三張截圖 -> 一筆資料。丟路徑而不是影像，省得搬 13MB。"""
    _ensure_ready()
    paths, idx, meta, name_dir, review_dir = job
    card_type, team = meta
    imgs = [_load(p) for p in paths]
    if any(i is None for i in imgs):
        return None
    rec = build_record(imgs, idx, card_type, name_dir)
    rec["team"] = team              # 球隊來自篩選條件，不是辨識出來的
    # 只有「數字類欄位」讀不到才留現場截圖，而且只留相關的那一頁。
    # 名字不留：名字那一條每張卡都另外存了小圖，留整頁只是把硬碟吃光
    # （實測 288 張卡就吃掉 567MB）。
    flags = set(rec.get("flags", []))
    want = set()
    if flags & {"no_ovr", "weak_stats"}:
        want.add(1)
    if flags & {"weak_zones", "no_page3"}:
        want.add(3)
    if review_dir and want:
        os.makedirs(review_dir, exist_ok=True)
        for k in sorted(want):
            cv2.imencode(".png", imgs[k - 1])[1].tofile(
                os.path.join(review_dir, f"{team}_{idx:05d}_p{k}.png"))
    return rec


def sweep(dev, card_type, team=None, batch_pairs=12, limit=None, sleep_s=0.35,
          resume=False, workers=4):
    """
    掃一個卡片類型：一路 › 翻過去，每張卡抓三頁。
    擷取受模擬器速度限制、解析吃 CPU，所以兩件事重疊做——這一批在解析時，
    下一批已經在模擬器裡拍了（用兩個交替的暫存目錄，避免蓋到還沒解析完的檔）。
    """
    setup_ocr()
    dev.check_size()
    out_dir = os.path.join(OUT_DIR, card_type.replace(" ", "_"))
    os.makedirs(out_dir, exist_ok=True)
    jsonl = os.path.join(out_dir, f"{team or 'ALL'}.jsonl")
    keep = os.path.join(out_dir, "_suspect")
    os.makedirs(keep, exist_ok=True)

    if not goto_dex(dev):
        print("！回不到圖鑑清單，停下")
        return -1, 0
    total = apply_filter(dev, card_type, team=team,
                         expect_max=TYPE_COUNTS.get(card_type, 20000) + 1)
    if total is None:
        print("！篩選失敗，停下")
        return -1, 0
    if total == 0:
        print(f"  {card_type} / {team}：0 張，跳過")
        return 0, 0
    if limit:
        total = min(total, limit)

    ok, img = open_first_detail(dev, 1)
    if not ok or page_of(img)[0] != 0:
        print(f"！開不了第一張卡的第 1 頁（狀態 {where(dev)[0]}），停下")
        return -1, total

    tag = f"{card_type}/{team or 'ALL'}"
    who = dev.serial.replace(":", "_")
    dirs = [os.path.join(out_dir, f"_batch_{who}_a"),
            os.path.join(out_dir, f"_batch_{who}_b")]
    idx, slips, done, t0 = 0, 0, 0, time.time()
    pending = []          # [(futures, 這一批用的目錄)]
    fh = open(jsonl, "a" if resume else "w", encoding="utf-8")

    def drain(upto=0):
        nonlocal done
        while len(pending) > upto:
            futures, _ = pending.pop(0)
            recs = []
            for fu in futures:
                try:
                    r = fu.result()
                except Exception as e:
                    print(f"！解析失敗：{e}")
                    r = None
                if r:
                    recs.append(r)
            recs.sort(key=lambda r: r["idx"])
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + chr(10))
            fh.flush()
            done += len(recs)

    try:
        with _fut.ProcessPoolExecutor(max_workers=workers,
                                      initializer=_worker_init) as pool:
            while idx < total:
                pairs = min(batch_pairs, (total - idx + 1) // 2)
                if pairs <= 0:
                    break
                drain(upto=1)                       # 最多讓一批在背景解析
                tmp = dirs[(idx // max(1, batch_pairs * 2)) % 2]
                files = dev.zigzag_capture(pairs, tmp, sleep_s)
                if len(files) < pairs * 6:
                    print(f"！只拉回 {len(files)}/{pairs*6} 張截圖，停下")
                    break
                jobs = []
                for p in range(pairs):
                    grp = files[p * 6:p * 6 + 6]
                    pages = [page_of(_load(f))[0] for f in grp]
                    if pages != ZIG_PAGES:
                        slips += 1
                        for k, f in enumerate(grp):
                            img = _load(f)
                            cv2.imencode(".png", img)[1].tofile(
                                os.path.join(keep, f"slip_{idx + p*2:05d}_{k}.png"))
                        continue
                    names = os.path.join(out_dir, "names", team or "ALL")
                    review = os.path.join(out_dir, "_review")
                    meta = (card_type, team)
                    jobs.append(((grp[0], grp[1], grp[2]), idx + p * 2, meta, names, review))
                    jobs.append(((grp[5], grp[4], grp[3]), idx + p * 2 + 1, meta, names, review))
                pending.append(([pool.submit(_parse_job, j) for j in jobs], tmp))
                idx += pairs * 2
                rate = idx / max(1e-6, time.time() - t0)
                print(f"  {tag}: 擷取 {idx}/{total}（已寫 {done}）  "
                      f"{rate:.2f} 張/秒  剩 {(total-idx)/max(1e-6,rate)/60:.0f} 分  "
                      f"脫拍 {slips}", flush=True)
            drain()
    finally:
        fh.close()
        dev.tap(*DETAIL_CLOSE, wait=1.5)
    print(f"完成 {tag}：擷取 {idx} 張、寫入 {done} 筆、脫拍 {slips} 組 -> {jsonl}")
    return done, total


# ---------------------------------------------------------------- 工具指令

def cmd_calibrate(args):
    img = cv2.imdecode(np.fromfile(args.frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    boxes = [("ovr", R_OVR), ("team", R_TEAM), ("pos", R_POS), ("name", R_NAME),
             ("type", R_TYPE)]
    if args.page == 1:
        boxes.append(("hdr", R_STAT_HDR))
        for i, x in enumerate(STAT_X):
            boxes.append((f"b{i}", (x, Y_BASE, STAT_W, STAT_H)))
            boxes.append((f"t{i}", (x, Y_TOTAL, STAT_W, STAT_H)))
    elif args.page == 2:
        for i, y in enumerate(POT_Y):
            for x in (POT_L_X, POT_R_X):
                if x == POT_R_X and i == 2:
                    continue
                boxes.append((f"p{i}", (x, y, POT_NAME_W, POT_NAME_H)))
                boxes.append((f"g{i}", (x + POT_GRADE_DX, y + 24,
                                        POT_GRADE_W, POT_GRADE_H)))
    else:
        for r in range(3):
            for c in range(3):
                boxes.append((f"z{r}{c}", (int(ZONE_X0 + c * ZONE_STEP_X),
                                           int(ZONE_Y0 + r * ZONE_STEP_Y),
                                           ZONE_SIZE, ZONE_SIZE)))
        boxes.append(("angle", R_ANGLE))
    for name, (x, y, w, h) in boxes:
        cv2.rectangle(img, (x, y), (x + w, y + h), (0, 255, 255), 2)
        cv2.putText(img, name, (x, max(12, y - 4)), cv2.FONT_HERSHEY_PLAIN,
                    1.1, (0, 255, 255), 1, cv2.LINE_AA)
    cv2.imencode(".png", img)[1].tofile(args.out)
    print(f"-> {args.out}")


def cmd_parse(args):
    setup_ocr()
    img = cv2.imdecode(np.fromfile(args.frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    print(json.dumps(parse_frame(img, args.page), ensure_ascii=False, indent=1))


def cmd_shots(args):
    """從現在的畫面連拍 N 張存下來（校正座標、收字形樣本用），不做解析。"""
    dev = Device(args.serial)
    dev.check_size()
    if args.type:
        setup_ocr()
        goto_dex(dev)
        if not apply_filter(dev, args.type):
            raise SystemExit("篩選失敗")
        ok, _ = open_first_detail(dev, args.page)
        if not ok:
            raise SystemExit("開不了詳情頁")
    files = dev.batch_capture(args.n, args.out, PAGE_NEXT_CARD, args.sleep)
    print(f"{len(files)} 張 -> {args.out}")


FONT_PAGE = {"stat": 0, "grade": 1, "zone": 2}      # 這個字型只出現在第幾頁（0-based）


def _glyph_regions(img, font):
    if font == "ovr":
        return [crop(img, R_OVR)]
    if font == "stat":
        out = []
        for y in (Y_BASE, Y_TOTAL):
            for x in STAT_X:
                out.append(img[y:y + STAT_H, x:x + STAT_W])
        return out
    if font == "pos":
        return [crop(img, R_POS)]
    if font == "zone":
        if not zone_grid_present(img):        # 投手那一頁沒有九宮格，別收進來當樣本
            return []
        cells = [img[int(ZONE_Y0 + r * ZONE_STEP_Y):int(ZONE_Y0 + r * ZONE_STEP_Y) + ZONE_SIZE,
                     int(ZONE_X0 + c * ZONE_STEP_X):int(ZONE_X0 + c * ZONE_STEP_X) + ZONE_SIZE]
                 for r in range(3) for c in range(3)]
        cells.append(crop(img, R_ANGLE))       # 仰角是同一款字型，一起收
        return cells
    raise SystemExit(f"不認識的字型：{font}")


def _save_protos(font, pairs, cap=60):
    """把 (標籤, 點陣) 存成字形樣本；太像的就不重複收，每個標籤最多 cap 個。"""
    dest = os.path.join(GLYPH_DIR, font)
    os.makedirs(dest, exist_ok=True)
    have = {}
    for lab, g in load_glyphs(font):
        have.setdefault(str(lab), []).append(g)
    added = 0
    total = GLYPH_W * GLYPH_H
    for lab, g in pairs:
        lab = str(lab)
        cur = have.setdefault(lab, [])
        if len(cur) >= cap:
            continue
        if any(1.0 - np.count_nonzero(g ^ h) / total >= 0.97 for h in cur):
            continue
        cur.append(g)
        n = len(os.listdir(dest)) + added
        fname = (f"{lab}_{n:04d}.png" if re.fullmatch(r"[0-9A-Z]+", lab)
                 else f"x{ord(lab):02x}_{n:04d}.png")
        cv2.imencode(".png", g * 255)[1].tofile(os.path.join(dest, fname))
        added += 1
    _GLYPHS.pop(font, None)
    return added


def cmd_glyphs_harvest(args):
    """
    自動收字形樣本，不用人眼標記：
      表格數字  「基本」列與「合計」列在沒培育的卡上是同一組數字，兩列讀出來一致
                 而且所有遮罩投票一致，才當成可信答案。
      紅區數字  九宮格的字大又乾淨，所有遮罩一致就收。
      潛力等級  同上，字母只有 S/A/B/C/D 幾種。
    """
    import glob as _glob
    setup_ocr()
    frames = sorted(_glob.glob(args.frames))
    stat_pairs, zone_pairs, grade_pairs, pos_pairs = [], [], [], []
    used = 0
    for path in frames:
        img = _load(path)
        if img is None or img.shape[:2] != (DEVICE_SIZE[1], DEVICE_SIZE[0]):
            continue
        page = page_of(img)[0]
        if page is not None:                     # 守位在三頁都看得到
            region = crop(img, R_POS)
            txt, conf = ocr_field(region, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                                  psm=6, scale=5)
            toks = [t for t in re.findall(r"[A-Z0-9]{1,2}", txt.upper()) if t in VALID_POS]
            if toks and conf >= 85:
                spec = GLYPH_SPEC["pos"]
                letters = "".join(toks)
                got = []
                for mask in _glyph_masks(region, "pos"):
                    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, 8)
                    cand = []
                    for i in range(1, n):
                        x, y, w, h, area = st[i]
                        if not (spec["h"][0] <= h <= spec["h"][1]) or area < spec["area"]:
                            continue
                        if not (spec["w"][0] <= w <= spec["w"][1]):
                            continue
                        piece = (lab[y:y + h, x:x + w] == i).astype(np.uint8) * 255
                        cand.append((round((y + h / 2) / 20), x, _norm_glyph(piece)))
                    if len(cand) > len(got):
                        got = cand
                got.sort(key=lambda t: (t[0], t[1]))
                if len(got) == len(letters):
                    pos_pairs += [(c, g) for c, (_, _, g) in zip(letters, got)]
        if page == 0:
            used += 1
            for x in STAT_X:
                b = img[Y_BASE:Y_BASE + STAT_H, x:x + STAT_W]
                t = img[Y_TOTAL:Y_TOTAL + STAT_H, x:x + STAT_W]
                # 有字形樣本之後就用字形讀「基本」列當答案（比 tesseract 準也快得多）；
                # 沒有樣本才退回 tesseract 的全遮罩投票。
                vb, cb = read_digits(b, "stat", 1, 200)
                if vb is None:
                    vb, cb = ocr_int(b, 1, 200, full=True)
                    if vb is None or cb < 100:
                        continue
                    vt, ct = ocr_int(t, 1, 200, full=True)
                    if vt != vb or ct < 100:
                        continue
                elif cb < 95:
                    continue
                for cell in (b, t):      # 合計列是彩色字（綠／灰），也要收樣本
                    gs = segment_digits(cell, "stat")
                    if len(gs) == len(str(vb)):
                        stat_pairs += [(d, g) for d, (_, g) in zip(str(vb), gs)]
        elif page == 2:
            used += 1
            for r in range(3):
                for c in range(3):
                    x = int(ZONE_X0 + c * ZONE_STEP_X)
                    y = int(ZONE_Y0 + r * ZONE_STEP_Y)
                    cell = img[y:y + ZONE_SIZE, x:x + ZONE_SIZE]
                    v, conf = ocr_int(cell, 0, 9, psm=10, scale=6, full=True)
                    if v is None or conf < 100:
                        continue
                    gs = segment_digits(cell, "zone")
                    if len(gs) == 1:
                        zone_pairs.append((str(v), gs[0][1]))
        elif page == 1:
            used += 1
            for i, y in enumerate(POT_Y):
                for x in (POT_L_X, POT_R_X):
                    if x == POT_R_X and i == 2:
                        continue
                    name_img = img[y:y + POT_NAME_H, x:x + POT_NAME_W]
                    if float(name_img.std()) < 6.0:
                        continue
                    gx = x + POT_GRADE_DX
                    box = img[y + 24:y + 24 + POT_GRADE_H, gx:gx + POT_GRADE_W]
                    txt, conf = ocr_field(box, "SABCD", psm=10, scale=5)
                    lab = (txt or "").strip()[:1]
                    if lab not in ("S", "A", "B", "C", "D") or conf < 80:
                        continue
                    gs = segment_digits(box, "grade")
                    if len(gs) == 1:
                        grade_pairs.append((lab, gs[0][1]))
    print(f"看了 {used}/{len(frames)} 張畫面")
    for font, pairs in (("stat", stat_pairs), ("zone", zone_pairs),
                        ("grade", grade_pairs), ("pos", pos_pairs)):
        if pairs:
            print(f"  {font}: 收到 {len(pairs)} 個樣本，新增 {_save_protos(font, pairs)} 個字形")


def cmd_glyphs(args):
    """收集字形樣本（collect）或把montage上的順序標成數字（label）。"""
    import glob as _glob
    pool = os.path.join(GLYPH_DIR, args.font, "_pool")
    if args.action == "collect":
        os.makedirs(pool, exist_ok=True)
        for f in os.listdir(pool):
            os.remove(os.path.join(pool, f))
        clusters = []                     # [(代表點陣, 次數)]
        frames = sorted(_glob.glob(args.frames))
        want_page = FONT_PAGE.get(args.font)
        for path in frames:
            img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is None or img.shape[:2] != (DEVICE_SIZE[1], DEVICE_SIZE[0]):
                continue
            if want_page is not None and page_of(img)[0] != want_page:
                continue                       # 別把別頁的畫面當成這個字型的來源
            for region in _glyph_regions(img, args.font):
                for _, g in segment_digits(region, args.font):
                    total = GLYPH_W * GLYPH_H
                    if args.only_unknown:
                        d, sim, margin = match_glyph(g, args.font)
                        # 也要收「兩個標籤分不出來」的：0 和 6 在某些卡面上只差
                        # 兩三個像素，補一個該樣式的樣本就分得開了
                        if d is not None and sim >= 0.90 and margin >= 0.02:
                            continue
                    for i, (rep, n) in enumerate(clusters):
                        if 1.0 - np.count_nonzero(g ^ rep) / total >= 0.93:
                            clusters[i] = (rep, n + 1)
                            break
                    else:
                        clusters.append((g, 1))
        clusters.sort(key=lambda t: -t[1])
        cols, cell, pad = 8, 5, 10
        rows = (len(clusters) + cols - 1) // cols
        cw, ch = GLYPH_W * cell + pad * 2, GLYPH_H * cell + pad * 2 + 16
        sheet = np.full((max(1, rows) * ch, cols * cw), 255, np.uint8)
        for i, (rep, n) in enumerate(clusters):
            big = cv2.resize(rep * 255, (GLYPH_W * cell, GLYPH_H * cell),
                             interpolation=cv2.INTER_NEAREST)
            r, c = divmod(i, cols)
            y0, x0 = r * ch + 16, c * cw + pad
            sheet[y0:y0 + big.shape[0], x0:x0 + big.shape[1]] = 255 - big
            cv2.putText(sheet, f"{i+1}({n})", (c * cw + 2, r * ch + 12),
                        cv2.FONT_HERSHEY_PLAIN, 0.8, 0, 1, cv2.LINE_AA)
            cv2.imencode(".png", rep * 255)[1].tofile(
                os.path.join(pool, f"{i+1:03d}.png"))
        cv2.imencode(".png", sheet)[1].tofile(args.out)
        print(f"{len(frames)} 張畫面 -> {len(clusters)} 種字形，看 {args.out} 再 label")
    else:
        digits = [d.strip() for d in args.digits.replace(",", " ").split()]
        files = sorted(f for f in os.listdir(pool) if f.endswith(".png"))
        if len(digits) != len(files):
            raise SystemExit(f"給了 {len(digits)} 個標記，但池子裡有 {len(files)} 種字形")
        pairs = []
        for i, (f, d) in enumerate(zip(files, digits)):
            if d in ("-", "x", "?", ""):          # 看不準的就不收
                continue
            if not re.fullmatch(r"[0-9A-Z]+", d):
                raise SystemExit(f"第 {i+1} 個標記不合法：{d!r}")
            img = cv2.imdecode(np.fromfile(os.path.join(pool, f), dtype=np.uint8),
                               cv2.IMREAD_GRAYSCALE)
            if img is not None:
                pairs.append((d, _norm_glyph(img)))
        added = _save_protos(args.font, pairs)
        have = len(load_glyphs(args.font))
        print(f"新增 {added} 個字形，{args.font} 現在共 {have} 個樣本")


QUEUE_PATH = os.path.join(OUT_DIR, "queue.json")
TYPE_COUNTS = {                      # 之前用篩選器數出來的，用來核對有沒有掃齊
    "Moment": 4222, "Supreme Moment": 300, "Impact": 3039, "Prime": 2961,
    "Signature": 2742, "Signature Black": 1491, "FA Impact": 3039,
    "FA Prime": 2961, "FA Signature": 2742, "FA Signature Black": 1491,
    "WBC Prime": 172, "WBC Signature": 172, "WBC Signature Black": 90, "HOF": 56,
}


class _QueueLock:
    """
    工作清單的檔案鎖。兩台模擬器各跑一個程序，共用同一份 queue.json，
    沒有鎖的話兩邊會同時「認領」到同一個切片（實測發生過，兩台掃同一批卡）。
    """

    def __init__(self, path=None, timeout=30):
        self.path = (path or QUEUE_PATH) + ".lock"
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        end = time.time() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                if time.time() > end:            # 上一個程序被砍掉留下的鎖
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
                    end = time.time() + self.timeout
                time.sleep(0.15)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            os.remove(self.path)
        except OSError:
            pass


def _queue_save(q):
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = QUEUE_PATH + f".tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(q, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, QUEUE_PATH)


def _queue_load():
    """
    工作清單＝(卡種 × 球隊) 的切片。球隊用篩選器指定，所以每張卡的球隊是已知的
    ——卡面上的 logo 疊在會變的卡圖上，比對只有八成準，不能拿來當資料。
    大卡種排前面，兩台模擬器共用這份清單就會自然分工。
    """
    if os.path.exists(QUEUE_PATH):
        with open(QUEUE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    q = {}
    for t in sorted(SWEEP_TYPES, key=lambda t: -TYPE_COUNTS.get(t, 0)):
        for team in TEAMS_AL + TEAMS_NL:
            q[f"{t}|{team}"] = {"state": "pending"}
    _queue_save(q)
    return q


def _lines(path):
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def slice_path(card_type, team):
    return os.path.join(OUT_DIR, card_type.replace(" ", "_"), f"{team}.jsonl")


def claim_slice(serial):
    """挑一個還沒做的切片並標成正在做（整段在鎖裡做，兩台才不會搶到同一片）。"""
    with _QueueLock():
        q = _queue_load()
        for key, info in q.items():
            if info.get("state") != "pending":
                continue
            info.update(state="running", by=serial)
            _queue_save(q)
            t, team = key.split("|")
            return t, team
    return None


def finish_slice(serial, card_type, team, state, got=None):
  with _QueueLock():
    q = _queue_load()
    key = f"{card_type}|{team}"
    q.setdefault(key, {}).update(
        state=state, by=serial,
        got=_lines(slice_path(card_type, team)) if got is None else got)
    _queue_save(q)


def cmd_run(args):
    """一直從工作清單拿切片來掃。兩台各跑一個這個指令就會自己分工。"""
    dev = Device(args.serial)
    fails = 0
    while True:
        job = claim_slice(args.serial)
        if job is None:
            print("工作清單做完了")
            return
        t, team = job
        print(f"=== {args.serial} 開始掃 {t} / {team}", flush=True)
        try:
            got, total = sweep(dev, t, team, args.pairs, args.limit, args.sleep,
                               args.resume, args.workers)
        except Exception as e:
            print(f"！{t}/{team} 掃到一半失敗：{e}", flush=True)
            finish_slice(args.serial, t, team, "failed")
            fails += 1
            if fails >= 3:
                print("連續失敗太多次，停下來讓人看一眼")
                return
            continue
        if got is None or got < 0:
            finish_slice(args.serial, t, team, "failed")
            fails += 1
            if fails >= 3:
                print("連續失敗太多次，停下來讓人看一眼")
                return
        else:
            fails = 0
            # 掃不齊就標成 partial，之後可以只補這些切片
            state = "done" if got >= max(1, total) * 0.98 or total == 0 else "partial"
            finish_slice(args.serial, t, team, state, got)


def cmd_status(args):
    """看工作清單進度，並核對每個卡種掃到的張數對不對。"""
    q = _queue_load()
    by_state, per_type = {}, {}
    for key, info in q.items():
        st = info.get("state", "pending")
        by_state[st] = by_state.get(st, 0) + 1
        t, team = key.split("|")
        per_type[t] = per_type.get(t, 0) + _lines(slice_path(t, team))
    print("切片狀態:", by_state)
    print(f"{'卡種':22}{'已掃':>8}{'應有':>8}")
    tot_got = tot_want = 0
    for t in sorted(per_type, key=lambda t: -TYPE_COUNTS.get(t, 0)):
        got, want = per_type[t], TYPE_COUNTS.get(t, 0)
        tot_got += got
        tot_want += want
        print(f"{t:22}{got:>8}{want:>8}" + ("" if got >= want else "  <-- 未完"))
    print(f"{'合計':22}{tot_got:>8}{tot_want:>8}")
    # 用兩份 log 的最新速率估剩餘時間
    rate = 0.0
    for name in ("dex.A.log", "dex.B.log"):
        try:
            with open(name, encoding="utf-8", errors="replace") as fh:
                for line in fh.read().splitlines()[::-1]:
                    m = re.search(r"([0-9.]+) 張/秒", line)
                    if m:
                        rate += float(m.group(1))
                        break
        except OSError:
            pass
    if rate > 0 and tot_want > tot_got:
        left = (tot_want - tot_got) / rate / 3600.0
        print(f"目前合計 {rate:.2f} 張/秒，剩下 {tot_want - tot_got:,} 張 → 約 {left:.1f} 小時")


def cmd_audit(args):
    """
    掃完之後的資料體檢：每個欄位讀到幾成、OVR 排序有沒有矛盾、哪些切片張數不足。
    OVR 一定是隨清單遞減的，所以「遞增」就是讀錯的證據，可以自己抓出來。
    """
    tot = 0
    miss = {"ovr": 0, "stats": 0, "zones": 0, "angle": 0, "pots": 0, "pos": 0, "name": 0}
    bat = pit = 0
    bad_order, short = [], []
    for t in SWEEP_TYPES:
        for team in TEAMS_AL + TEAMS_NL:
            f = slice_path(t, team)
            if not os.path.exists(f):
                continue
            recs = {}
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        r = json.loads(line)
                        recs[r["idx"]] = r
            recs = [recs[k] for k in sorted(recs)]
            if not recs:
                continue
            if recs[-1]["idx"] + 1 != len(recs):
                short.append(f"{t}/{team}（{len(recs)} 筆但最大序號 {recs[-1]['idx']}）")
            seq = [r["ovr"] for r in recs if r.get("ovr") is not None]
            rises = sum(1 for a, b in zip(seq, seq[1:]) if b > a)
            if rises:
                bad_order.append(f"{t}/{team}（{rises} 處）")
            for r in recs:
                tot += 1
                if r.get("ovr") is None:
                    miss["ovr"] += 1
                if any(v is None for v in r.get("base", [])) or not r.get("base"):
                    miss["stats"] += 1
                if not r.get("positions"):
                    miss["pos"] += 1
                if len(r.get("potentials", [])) < 5:
                    miss["pots"] += 1
                if not (r.get("name") or "").strip():
                    miss["name"] += 1
                if r.get("class") == "pitcher":
                    pit += 1
                else:
                    bat += 1
                    if not r.get("zones") or any(z.get("v") is None for z in r["zones"]):
                        miss["zones"] += 1
                    if r.get("angle") is None:
                        miss["angle"] += 1
    if not tot:
        print("還沒有資料")
        return
    print(f"共 {tot:,} 張（打者 {bat:,}／投手 {pit:,}）")
    def pct(k, base=None):
        base = base or tot
        return f"{base - miss[k]:,}/{base:,}（{(base - miss[k]) * 100.0 / max(1, base):.1f}%）"
    print(f"  OVR      讀到 {pct('ovr')}")
    print(f"  六項數值 讀到 {pct('stats')}")
    print(f"  守位     讀到 {pct('pos')}")
    print(f"  潛力五項 讀到 {pct('pots')}")
    print(f"  名字     有值 {pct('name')}   ← 現場不讀，靠 names read 補")
    print(f"  紅區     讀到 {pct('zones', bat)}（只算打者）")
    print(f"  仰角     讀到 {pct('angle', bat)}（只算打者）")
    if bad_order:
        print(f"！OVR 排序有矛盾的切片（可能讀錯）：{', '.join(bad_order[:8])}")
    if short:
        print(f"！張數不連續的切片：{', '.join(short[:8])}")


def cmd_teams(args):
    """
    自動建立球隊 logo 模板（現在只當備援／查核用，球隊本身是靠篩選器切片得到的）。
    """
    dev = Device(args.serial)
    setup_ocr()
    dev.check_size()
    codes = [c.strip() for c in args.teams.split(",")] if args.teams else list(TEAM_SLOT)
    for code in codes:
        if not goto_dex(dev):
            print("！回不到圖鑑清單")
            return
        n = apply_filter(dev, args.type, team=code, expect_max=args.expect_max)
        if not n:
            print(f"  {code}: 篩選失敗或 0 張，跳過")
            continue
        ok, _ = open_first_detail(dev, 1)
        if not ok:
            print(f"  {code}: 開不了詳情頁，跳過")
            continue
        tmp = os.path.join(TEAM_DIR, "_tmp")
        files = dev.batch_capture(min(args.per, n), tmp, PAGE_NEXT_CARD, 0.4)
        dest = os.path.join(TEAM_DIR, code)
        os.makedirs(dest, exist_ok=True)
        for f in os.listdir(dest):
            os.remove(os.path.join(dest, f))
        kept = 0
        for i, f in enumerate(files):
            img = _load(f)
            if img is None or page_of(img)[0] is None:
                continue
            logo = crop(img, R_TEAM)
            if float(logo.std()) < 8:
                continue
            cv2.imencode(".png", logo)[1].tofile(os.path.join(dest, f"{i:02d}.png"))
            kept += 1
        dev.tap(*DETAIL_CLOSE, wait=1.2)
        print(f"  {code}: {n} 張，收了 {kept} 個 logo 樣本", flush=True)



def fix_ovr(recs):
    """
    用清單排序修 OVR：圖鑑是按稀有度（＝OVR）遞減排的，所以整段 OVR 只能持平或往下。
    先挑出「最長不遞增子序列」當骨幹（違反的視為讀錯），剩下的位置若前後骨幹值相同
    就補那個值，不同就標成不確定。回傳 (確定數, 不確定數)。
    """
    cand = [(i, r["ovr"]) for i, r in enumerate(recs)
            if r.get("ovr") is not None and r.get("ovr_conf", 0) >= 80]
    keep = set()
    if cand:
        n = len(cand)
        best = [1] * n
        prev = [-1] * n
        for i in range(n):
            for j in range(i):
                if cand[j][1] >= cand[i][1] and best[j] + 1 > best[i]:
                    best[i] = best[j] + 1
                    prev[i] = j
        i = max(range(n), key=lambda k: best[k])
        while i >= 0:
            keep.add(cand[i][0])
            i = prev[i]
    sure = unsure = 0
    for i, r in enumerate(recs):
        if i in keep:
            sure += 1
            continue
        before = next((recs[j]["ovr"] for j in range(i - 1, -1, -1) if j in keep), None)
        after = next((recs[j]["ovr"] for j in range(i + 1, len(recs)) if j in keep), None)
        if before is not None and before == after:
            r["ovr"] = before
            r["ovr_src"] = "順序推得"
            sure += 1
        else:
            r["ovr_src"] = "不確定"
            r.setdefault("flags", []).append("ovr_uncertain")
            unsure += 1
    return sure, unsure


# ------------------------------------------------- 名字（離線改進用）
#
# 名字是圖鑑的主鍵，也是最難讀的一欄：字疊在會發光的卡圖上，tesseract 常常
# 少讀一段或把年份 '80 讀成 'SO。所以掃描時每張卡都把名字那一條存成小圖
# （dex_data/<類型>/names/<球隊>/<序號>.png），之後可以離線重讀，不必再進遊戲。
#
# 重讀的方式跟數字一樣是字形比對，但字母有大小寫，所以正規化時要保留
# 「這個字在行內多高、位置多高」——不然 o 和 O 會變成同一個點陣。

NAME_GLYPH_DIR = os.path.join(GLYPH_DIR, "name")
NAME_OK = re.compile(r"^[A-Z][A-Za-z.'\- ]{2,}(?:'\d\d)?$")
# 自動標記只收「純字母加空白」的名字：句點和撇號太小，切割時會被濾掉，
# 一旦字元數對不上就會把字形配到錯的標籤上（數字字型就是這樣被汙染過一次）。
NAME_CLEAN = re.compile(r"^[A-Z][a-z]+(?: [A-Z][a-z]+)+$")


def _name_parts(strip):
    """
    切出名字那一行的每個字元區塊。兩個關鍵：
      1. 用自適應門檻，不用全域亮度——名字後半常常疊在亮的卡圖上，
         全域門檻會把後半整段吃掉（實測 "Enos Slaughter" 只剩 "Enos Slaug"）。
      2. 只留跟多數字元同一條基線的區塊，這樣 HOF 卡左下角那個徽章就不會混進來。
    回傳 (區塊清單, 行的上下界)。
    """
    gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
    mask = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, 25, -8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask, 8)
    cand = []
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if not (12 <= h <= 48) or area < 35 or w > 62 or w < 2:
            continue
        cand.append([x, y, x + w, y + h, (lab[y:y + h, x:x + w] == i)])
    if not cand:
        return [], None
    centres = sorted((p[1] + p[3]) / 2 for p in cand)
    mid = centres[len(centres) // 2]
    cand = [p for p in cand if abs((p[1] + p[3]) / 2 - mid) <= 15]
    if not cand:
        return [], None
    cand.sort(key=lambda p: p[0])
    merged = []
    for p in cand:
        if merged:
            q = merged[-1]
            overlap = min(q[2], p[2]) - max(q[0], p[0])
            if overlap > 0.5 * min(q[2] - q[0], p[2] - p[0]):      # i／j 的點
                nx0, ny0 = min(q[0], p[0]), min(q[1], p[1])
                nx1, ny1 = max(q[2], p[2]), max(q[3], p[3])
                canvas = np.zeros((ny1 - ny0, nx1 - nx0), bool)
                canvas[q[1] - ny0:q[3] - ny0, q[0] - nx0:q[2] - nx0] |= q[4]
                canvas[p[1] - ny0:p[3] - ny0, p[0] - nx0:p[2] - nx0] |= p[4]
                merged[-1] = [nx0, ny0, nx1, ny1, canvas]
                continue
        merged.append(p)
    # 卡圖的亮斑常在名字左右兩端各留一個假字元，用字距把它們剃掉
    if len(merged) >= 4:
        xs = [p[0] for p in merged]
        gaps = sorted(xs[i + 1] - xs[i] for i in range(len(xs) - 1))
        med = gaps[len(gaps) // 2] or 1
        while len(merged) > 3 and (merged[-1][0] - merged[-2][0]) > med * 2.2:
            merged.pop()
        while len(merged) > 3 and (merged[1][0] - merged[0][0]) > med * 2.2:
            merged.pop(0)
    top = min(p[1] for p in merged)
    bot = max(p[3] for p in merged)
    return merged, (top, bot)


def name_box(strip):
    """名字實際的文字範圍（給 tesseract 用）。"""
    parts, line = _name_parts(strip)
    if not parts:
        return None
    x0 = max(0, min(p[0] for p in parts) - 6)
    x1 = min(strip.shape[1], max(p[2] for p in parts) + 6)
    y0 = max(0, line[0] - 6)
    y1 = min(strip.shape[0], line[1] + 6)
    if x1 - x0 < 40 or y1 - y0 < 16:
        return None
    return strip[y0:y1, x0:x1]


def name_glyphs(strip):
    """
    切出名字裡的每個字元，回傳 [(x, 20x28 點陣)]。
    大小寫要分得出來，所以縮放與擺放都相對「整行高度」，不是各自拉滿——
    不然 o 和 O 會變成同一個點陣。
    """
    parts, line = _name_parts(strip)
    if not parts:
        return []
    top, bot = line
    H = max(1, bot - top)
    scale = float(GLYPH_H) / H
    out = []
    for x0, y0, x1, y1, m in parts:
        piece = (m.astype(np.uint8) * 255)
        nw = max(1, min(GLYPH_W, int(round((x1 - x0) * scale))))
        nh = max(1, min(GLYPH_H, int(round((y1 - y0) * scale))))
        r = cv2.resize(piece, (nw, nh), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((GLYPH_H, GLYPH_W), np.uint8)
        oy = max(0, min(GLYPH_H - nh, int(round((y0 - top) * scale))))
        ox = (GLYPH_W - nw) // 2
        canvas[oy:oy + nh, ox:ox + nw] = r
        out.append((x0, (canvas > 127).astype(np.uint8)))
    return out


def read_name_glyphs(strip, dump=None):
    """用字形樣本讀名字；回傳 (文字, 信心 0~100)。空白由字距推得。"""
    gs = name_glyphs(strip)
    if not gs:
        return "", 0.0
    widths = [1] * len(gs)
    chars, worst, unknown = [], 1.0, 0
    prev_x = None
    med_gap = None
    xs = [x for x, _ in gs]
    if len(xs) > 2:
        gaps = sorted(xs[i + 1] - xs[i] for i in range(len(xs) - 1))
        med_gap = gaps[len(gaps) // 2]
    for x, g in gs:
        if prev_x is not None and med_gap and (x - prev_x) > med_gap * 1.7:
            chars.append(" ")
        prev_x = x
        lab, sim, margin = match_glyph(g, "name")
        if lab is None or sim < 0.86 or margin < 0.01:
            unknown += 1
            chars.append("?")
            if dump:
                os.makedirs(dump, exist_ok=True)
                h = hashlib.md5(g.tobytes()).hexdigest()[:10]
                cv2.imencode(".png", g * 255)[1].tofile(
                    os.path.join(dump, f"name_{h}.png"))
            continue
        chars.append(lab)
        worst = min(worst, sim)
    txt = "".join(chars).strip()
    if not txt or unknown > len(gs) * 0.25:
        return txt, 0.0
    return txt, round(worst * 100 - unknown * 10, 1)


def cmd_names(args):
    """
    collect：把名字裁圖裡的字元分群，做成montage讓人一次標完（字型固定，分群很乾淨）
    label  ：照montage順序把字元標上去
    harvest：自動收樣本（只用「純字母＋空白」且字數對得上的名字，寧缺勿錯）
    read   ：把所有名字裁圖重讀一遍，寫成對照表給 export 用
    """
    import glob as _glob
    setup_ocr()
    files = sorted(_glob.glob(args.frames or
                              os.path.join(OUT_DIR, "*", "names", "*", "*.png")))
    if args.limit:
        files = files[:args.limit]
    if args.action == "collect":
        pool = os.path.join(NAME_GLYPH_DIR, "_pool")
        os.makedirs(pool, exist_ok=True)
        for f in os.listdir(pool):
            os.remove(os.path.join(pool, f))
        clusters = []                       # [(代表點陣, 次數)]
        total = GLYPH_W * GLYPH_H
        for f in files:
            img = _load(f)
            if img is None:
                continue
            for _, g in name_glyphs(img):
                if args.only_unknown:
                    lab, sim, margin = match_glyph(g, "name")
                    if lab is not None and sim >= 0.90 and margin >= 0.02:
                        continue
                for i, (rep, n) in enumerate(clusters):
                    if 1.0 - np.count_nonzero(g ^ rep) / total >= 0.93:
                        clusters[i] = (rep, n + 1)
                        break
                else:
                    clusters.append((g, 1))
        clusters.sort(key=lambda t: -t[1])
        clusters = clusters[:args.max_clusters]
        cols, cell, pad = 8, 5, 10
        rows = (len(clusters) + cols - 1) // cols
        cw, ch = GLYPH_W * cell + pad * 2, GLYPH_H * cell + pad * 2 + 18
        sheet = np.full((max(1, rows) * ch, cols * cw), 255, np.uint8)
        for i, (rep, n) in enumerate(clusters):
            big = cv2.resize(rep * 255, (GLYPH_W * cell, GLYPH_H * cell),
                             interpolation=cv2.INTER_NEAREST)
            r, c = divmod(i, cols)
            y0, x0 = r * ch + 18, c * cw + pad
            sheet[y0:y0 + big.shape[0], x0:x0 + big.shape[1]] = 255 - big
            cv2.putText(sheet, f"{i+1}({n})", (c * cw + 3, r * ch + 14),
                        cv2.FONT_HERSHEY_PLAIN, 0.9, 0, 1, cv2.LINE_AA)
            cv2.imencode(".png", rep * 255)[1].tofile(
                os.path.join(pool, f"{i+1:03d}.png"))
        cv2.imencode(".png", sheet)[1].tofile(args.out)
        print(f"{len(files)} 張裁圖 -> {len(clusters)} 種字元，看 {args.out} 再 label")
        return
    if args.action == "label":
        pool = os.path.join(NAME_GLYPH_DIR, "_pool")
        chars = [c for c in args.chars.split(",")]
        pool_files = sorted(f for f in os.listdir(pool) if f.endswith(".png"))
        if len(chars) != len(pool_files):
            raise SystemExit(f"給了 {len(chars)} 個標記，但池子裡有 {len(pool_files)} 種字形")
        pairs = []
        for f, c in zip(pool_files, chars):
            c = c.strip()
            if c in ("", "-", "x", "?"):
                continue
            if len(c) != 1:
                raise SystemExit(f"標記要剛好一個字元：{c!r}")
            img = cv2.imdecode(np.fromfile(os.path.join(pool, f), dtype=np.uint8),
                               cv2.IMREAD_GRAYSCALE)
            if img is not None:
                pairs.append((c, _norm_glyph(img)))
        added = _save_protos("name", pairs, cap=40)
        print(f"新增 {added} 個字形，name 現在共 {len(load_glyphs('name'))} 個樣本")
        return
    if args.action == "harvest":
        pairs, used = [], 0
        why = {"信心不足": 0, "格式不像名字": 0, "字數對不齊": 0}
        for f in files:
            img = _load(f)
            if img is None:
                continue
            txt, conf = ocr_name(img)
            if conf < args.min_conf:
                why["信心不足"] += 1
                continue
            if not NAME_CLEAN.match(txt):
                why["格式不像名字"] += 1
                continue
            letters = txt.replace(" ", "")
            gs = name_glyphs(img)
            if len(gs) != len(letters):        # 對不齊就跳過，寧缺勿錯
                why["字數對不齊"] += 1
                continue
            pairs += [(c, g) for c, (_, g) in zip(letters, gs)]
            used += 1
        print("  略過原因:", why)
        added = _save_protos("name", pairs, cap=40)
        print(f"{len(files)} 張裁圖裡，{used} 張讀得夠乾淨可當答案；"
              f"收到 {len(pairs)} 個字元、新增 {added} 個字形樣本")
        return
    # read
    fixed, better, worse = {}, 0, 0
    for f in files:
        img = _load(f)
        if img is None:
            continue
        parts = f.replace("\\", "/").split("/")
        card_type, team, idx = parts[-4], parts[-2], int(os.path.splitext(parts[-1])[0])
        g_txt, g_conf = read_name_glyphs(img)
        o_txt, o_conf = ocr_name(img)
        if g_conf >= 70 and len(g_txt) >= 3:
            fixed[f"{card_type}|{team}|{idx}"] = g_txt
            if g_txt != o_txt:
                better += 1
        elif o_conf > 0:
            fixed[f"{card_type}|{team}|{idx}"] = o_txt
            worse += 1
    path = os.path.join(OUT_DIR, "name_fix.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fixed, fh, ensure_ascii=False, indent=0)
    print(f"重讀 {len(files)} 張：字形讀出 {len(fixed) - worse} 筆"
          f"（其中 {better} 筆跟原本不同）、退回 tesseract {worse} 筆 -> {path}")


def cmd_export(args):
    """把每個切片的 jsonl 併成一份網頁要吃的 cards.json。"""
    labels = {}
    if os.path.exists(LABELS_PATH):
        labels = json.load(open(LABELS_PATH, encoding="utf-8"))
    fix_path = os.path.join(OUT_DIR, "name_fix.json")
    name_fix = {}
    if os.path.exists(fix_path):
        name_fix = json.load(open(fix_path, encoding="utf-8"))
    out, summary, unknown_pots = [], {}, set()
    for t in SWEEP_TYPES:
        d = os.path.join(OUT_DIR, t.replace(" ", "_"))
        if not os.path.isdir(d):
            continue
        got = 0
        for team in TEAMS_AL + TEAMS_NL:
            f = slice_path(t, team)
            if not os.path.exists(f):
                continue
            recs = {}
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        r = json.loads(line)
                        recs[r["idx"]] = r          # 同 idx 取後寫入的那筆
            recs = [recs[k] for k in sorted(recs)]
            fix_ovr(recs)                            # 每個切片各自也是 OVR 遞減
            got += len(recs)
            for r in recs:
                kls = r.get("class") or (
                    "pitcher" if any(p in ("SP", "RP", "CP")
                                     for p in r.get("positions", [])) else "batter")
                names = STAT_LABELS[kls]
                pots = []
                for slot in r.get("potentials", []):
                    h = slot.get("name_hash")
                    nm = labels.get(h)
                    if nm is None:
                        unknown_pots.add(h)
                    # 等級不收（未持有的卡一律顯示 D）；點數與鎖數才是每張卡不同的東西
                    pots.append({"name": nm or f"?{h}",
                                 "dots": slot.get("dots"), "locks": slot.get("locks")})
                stats = {k: v for k, v in zip(names, r.get("base", []))}
                total = {k: v for k, v in zip(names, r.get("total", []))}
                rec_out = {
                    "type": r.get("type"),
                    "team": r.get("team") or team,
                    "idx": r.get("idx"),
                    "ovr": r.get("ovr"),
                    "name": name_fix.get(f"{t}|{team}|{r.get('idx')}") or r.get("name") or "",
                    "name_conf": r.get("name_conf"),
                    "positions": r.get("positions", []),
                    "class": kls,
                    "stats": stats,
                    "zones": [z.get("v") for z in r.get("zones", [])],
                    "zone_kinds": [z.get("kind") for z in r.get("zones", [])],
                    "angle": r.get("angle"),
                    "potentials": pots,
                }
                # 省檔案大小：合計跟基本一樣就不寫（未培育的卡大多如此）、空欄位不寫
                if total != stats:
                    rec_out["total"] = total
                if r.get("pitches"):
                    rec_out["pitches"] = r["pitches"]
                if r.get("flags"):
                    rec_out["flags"] = r["flags"]
                if r.get("ovr_src") and r["ovr_src"] != "讀取":
                    rec_out["ovr_src"] = r["ovr_src"]
                out.append(rec_out)
        summary[t] = {"已掃": got, "應有": TYPE_COUNTS.get(t, 0)}
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "cards.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    # 網頁直接用 file:// 開的時候 fetch 會被 CORS 擋掉，所以也輸出一份 .js
    webdir = os.path.join(os.path.dirname(OUT_DIR), "web")
    os.makedirs(webdir, exist_ok=True)
    with open(os.path.join(webdir, "cards.js"), "w", encoding="utf-8") as fh:
        fh.write("window.CARDS=")
        json.dump(out, fh, ensure_ascii=False)
        fh.write(";")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"共 {len(out)} 張 -> {path}")
    if unknown_pots:
        print(f"！有 {len(unknown_pots)} 個潛力名稱還沒對照（跑 pots collect/label 補）")


def cmd_pots(args):
    """
    潛力名稱是中文，而 Tesseract 只裝了 eng；但那些名稱來自固定的一組詞，
    畫出來每次都一模一樣，所以用點陣指紋分群，再由人一次性對照montage標上文字，
    之後就都靠查表。collect 產生montage，label 寫進對照表。
    """
    import glob as _glob
    if args.action == "collect":
        seen = {}
        for path in sorted(_glob.glob(args.frames)):
            img = _load(path)
            if img is None or page_of(img)[0] != 1:
                continue
            for i, y in enumerate(POT_Y):
                for x in (POT_L_X, POT_R_X):
                    if x == POT_R_X and i == 2:
                        continue
                    c = img[y:y + POT_NAME_H, x:x + POT_NAME_W]
                    if float(c.std()) < 6.0:
                        continue
                    h = phash(c)
                    if h not in seen:
                        seen[h] = [c, 0]
                    seen[h][1] += 1
        order = sorted(seen.items(), key=lambda kv: -kv[1][1])
        cell_h = POT_NAME_H + 8
        sheet = np.zeros((max(1, len(order)) * cell_h, POT_NAME_W + 210, 3), np.uint8)
        for i, (h, (c, n)) in enumerate(order):
            y0 = i * cell_h
            sheet[y0:y0 + POT_NAME_H, 200:200 + POT_NAME_W] = c
            cv2.putText(sheet, f"{i+1:2d} x{n}", (6, y0 + 30),
                        cv2.FONT_HERSHEY_PLAIN, 1.4, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.imencode(".png", sheet)[1].tofile(args.out)
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "_pot_pool.json"), "w", encoding="utf-8") as fh:
            json.dump([h for h, _ in order], fh)
        print(f"{len(order)} 種潛力名稱 -> {args.out}（依序標記後跑 pots label）")
    else:
        pool = json.load(open(os.path.join(OUT_DIR, "_pot_pool.json"), encoding="utf-8"))
        names = [n.strip() for n in args.names.split(",")]
        if len(names) != len(pool):
            raise SystemExit(f"給了 {len(names)} 個名字，但池子裡有 {len(pool)} 種")
        table = {}
        if os.path.exists(LABELS_PATH):
            table = json.load(open(LABELS_PATH, encoding="utf-8"))
        for h, n in zip(pool, names):
            if n and n not in ("-", "?"):
                table[h] = n
        with open(LABELS_PATH, "w", encoding="utf-8") as fh:
            json.dump(table, fh, ensure_ascii=False, indent=1)
        print(f"對照表現在有 {len(table)} 筆 -> {LABELS_PATH}")


def cmd_check(args):
    """
    檢查字形樣本有沒有自相矛盾：不同標籤的樣本長得幾乎一樣，就是有人標錯。
    這個檢查是必要的——自動標記的答案來自 tesseract，而 tesseract 把 7 讀成 1
    是系統性錯誤（兩列都讀成一樣的錯值，看起來還「一致」），會把 7 收成 1 的樣本。
    """
    bad = 0
    for font in ("ovr", "stat", "zone", "pos", "grade"):
        protos = load_glyphs(font)
        if not protos:
            continue
        total = GLYPH_W * GLYPH_H
        clash = []
        for i in range(len(protos)):
            for j in range(i + 1, len(protos)):
                (la, ga), (lb, gb) = protos[i], protos[j]
                if la == lb:
                    continue
                sim = 1.0 - np.count_nonzero(ga ^ gb) / total
                if sim >= 0.96:
                    clash.append((round(sim, 3), la, lb))
        counts = {}
        for lab, _ in protos:
            counts[str(lab)] = counts.get(str(lab), 0) + 1
        print(f"{font}: {len(protos)} 個樣本 {counts}")
        for sim, la, lb in sorted(clash, reverse=True)[:10]:
            print(f"   ！{la} 和 {lb} 的樣本相似度 {sim} —— 有一邊標錯了")
        bad += len(clash)
    print("有問題的配對:", bad)


def cmd_mktemplate(args):
    """從一張進階搜尋的截圖裁出「卡片類型」標題當錨點。"""
    img = cv2.imdecode(np.fromfile(args.frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    x, y, w, h = args.box
    t = img[y:y + h, x:x + w]
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "templates", "圖鑑_卡片類型.png")
    cv2.imencode(".png", t)[1].tofile(p)
    print(f"-> {p}  {t.shape}")


def main():
    ap = argparse.ArgumentParser(description="球員圖鑑掃描器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("filter", help="套用篩選並印出張數")
    p.add_argument("--serial", default="emulator-5554")
    p.add_argument("--type", required=True)
    p.add_argument("--ovr-lo", type=int)
    p.add_argument("--ovr-hi", type=int)

    p = sub.add_parser("sweep", help="掃一個切片（一次抓完三頁）")
    p.add_argument("--serial", default="emulator-5554")
    p.add_argument("--type", required=True)
    p.add_argument("--team")
    p.add_argument("--pairs", type=int, default=12, help="一批幾組（一組兩張卡）")
    p.add_argument("--limit", type=int)
    p.add_argument("--sleep", type=float, default=0.35)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--workers", type=int, default=4)

    p = sub.add_parser("calibrate", help="畫出取樣框")
    p.add_argument("frame")
    p.add_argument("--page", type=int, choices=(1, 2, 3), default=1)
    p.add_argument("--out", default="calib.png")

    p = sub.add_parser("parse", help="解析單張截圖")
    p.add_argument("frame")
    p.add_argument("--page", type=int, choices=(1, 2, 3), default=1)

    p = sub.add_parser("shots", help="連拍畫面存檔（不解析）")
    p.add_argument("--serial", default="emulator-5554")
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--out", required=True)
    p.add_argument("--type")
    p.add_argument("--page", type=int, choices=(1, 2, 3), default=1)
    p.add_argument("--sleep", type=float, default=0.40)

    p = sub.add_parser("glyphs", help="收集／標記數字字形樣本")
    p.add_argument("action", choices=("collect", "label", "harvest"))
    p.add_argument("--font", required=True, choices=("ovr", "stat", "zone", "pos", "grade"))
    p.add_argument("--frames", default="")
    p.add_argument("--digits", default="")
    p.add_argument("--out", default="glyphs.png")
    p.add_argument("--only-unknown", action="store_true",
                   help="只收現有樣本認不出來的字形，montage 會短很多")

    p = sub.add_parser("audit", help="資料體檢：各欄位讀到幾成、排序有無矛盾")

    p = sub.add_parser("check", help="檢查字形樣本有沒有標錯")

    p = sub.add_parser("status", help="看進度")

    p = sub.add_parser("run", help="照工作清單一路掃（兩台可同時跑）")
    p.add_argument("--serial", default="emulator-5554")
    p.add_argument("--pairs", type=int, default=12)
    p.add_argument("--sleep", type=float, default=0.35)
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--resume", action="store_true")

    p = sub.add_parser("teams", help="自動建立球隊 logo 模板")
    p.add_argument("--serial", default="emulator-5554")
    p.add_argument("--type", default="Moment")
    p.add_argument("--teams", default="")
    p.add_argument("--per", type=int, default=6)
    p.add_argument("--expect-max", type=int, default=1200)

    p = sub.add_parser("names", help="名字：收字形樣本／離線重讀")
    p.add_argument("action", choices=("collect", "label", "harvest", "read"))
    p.add_argument("--frames", default="")
    p.add_argument("--limit", type=int)
    p.add_argument("--min-conf", type=float, default=88.0)
    p.add_argument("--chars", default="")
    p.add_argument("--out", default="names.png")
    p.add_argument("--max-clusters", type=int, default=96)
    p.add_argument("--only-unknown", action="store_true")

    p = sub.add_parser("export", help="併成網頁要吃的 cards.json")

    p = sub.add_parser("pots", help="潛力名稱對照表（中文，靠指紋查表）")
    p.add_argument("action", choices=("collect", "label"))
    p.add_argument("--frames", default="")
    p.add_argument("--names", default="")
    p.add_argument("--out", default="pots.png")

    p = sub.add_parser("mktemplate", help="裁「卡片類型」錨點模板")
    p.add_argument("frame")
    p.add_argument("--box", type=int, nargs=4, default=[118, 415, 152, 60])

    a = ap.parse_args()
    if a.cmd == "filter":
        dev = Device(a.serial)
        setup_ocr()
        goto_dex(dev)
        apply_filter(dev, a.type, ovr=(a.ovr_lo, a.ovr_hi) if a.ovr_lo else None)
    elif a.cmd == "sweep":
        sweep(Device(a.serial), a.type, a.team, a.pairs, a.limit, a.sleep,
              a.resume, a.workers)
    elif a.cmd == "calibrate":
        cmd_calibrate(a)
    elif a.cmd == "parse":
        cmd_parse(a)
    elif a.cmd == "shots":
        cmd_shots(a)
    elif a.cmd == "glyphs":
        (cmd_glyphs_harvest if a.action == "harvest" else cmd_glyphs)(a)
    elif a.cmd == "audit":
        cmd_audit(a)
    elif a.cmd == "check":
        cmd_check(a)
    elif a.cmd == "status":
        cmd_status(a)
    elif a.cmd == "run":
        cmd_run(a)
    elif a.cmd == "teams":
        cmd_teams(a)
    elif a.cmd == "names":
        cmd_names(a)
    elif a.cmd == "export":
        cmd_export(a)
    elif a.cmd == "pots":
        cmd_pots(a)
    elif a.cmd == "mktemplate":
        cmd_mktemplate(a)


if __name__ == "__main__":
    main()
