# 今の版（ポート 18000）のデータベースを、壊れていない写しにして、新しい版（18001）のフォルダーに置く（Windows PowerShell 5.1 用）。
# N5a（試す）。2026-10-03 オーナー: 判断① A「写しを作って新しい版が読む」と、
# 「動いている最中なので、壊れた写しにならないように。写したあと、整合の確かめと表ごとの件数を元と比べる。今の版は読むだけ」。
#
# やり方:
# - 今の版のデータベースそのものは開かない（コンテナが書いている最中のファイルを、別のところから開くと安全でないため）。
#   ファイルを読んで写すだけ。今の版は止めない。
# - 書き込みの少ない時刻を待ってから、同じものを2回続けて写す。2回の写しがまったく同じなら、写している間に書き込みはなかった。
#   違ったら、時間をおいてやり直す（3回まで）。
# - 写しを開いて、整合の確かめ（integrity_check）をする。表ごとの件数と期間を数える。
# - 今の版の画面の答え（/api/health の会場ごとの「最後に集め終わった時刻」）が、写す前と後で同じで、写しの中の最後の記録とも同じか確かめる。
# - 前の写しがあれば、表ごとの件数が減っていないか見る。
# - 全部よければ、新しい版の data\import\old-18000.sqlite3 に置く（前の写しと入れ替え）。確かめの記録は old-18000.json。
# - お金を動かすことはしない。秘密の鍵は読まない。.env は読まない。

param(
    [string]$Desk = 'C:\Users\user\Desktop',
    [string]$Live = 'farm-radar-claude-project-thread-np18zb',
    [string]$V2Name = 'farm-radar-v2',
    [string]$OldApi = 'http://localhost:18000',
    [string]$NewApi = 'http://localhost:18001',
    [string]$Python = '',
    [int]$Tries = 3,
    [int]$WaitMinutes = 20
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Step([string]$text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Ok([string]$text) { Write-Host "   OK: $text" -ForegroundColor Green }

# 写しの確かめ（一時フォルダーの写しだけを開く。今の版のデータベースは開かない）。
# 終わりの番号: 0 = よい / 3 = 写している間に書き込みがあった（やり直す） / 4 = 壊れている（止める）
$Check = @'
import hashlib, json, os, sqlite3, sys
from datetime import datetime, timezone
a, b, hb, ha, out, prev = sys.argv[1:7]
NAME = "old.sqlite3"

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

for suf in ("", "-wal"):
    pa, pb = os.path.join(a, NAME + suf), os.path.join(b, NAME + suf)
    if os.path.exists(pa) != os.path.exists(pb) or (os.path.exists(pa) and sha(pa) != sha(pb)):
        print("   やり直し: 2回写したものが同じではありません（写している間に今の版が書き込みました）")
        sys.exit(3)
print("   OK 2回写したものがまったく同じです（写している間に書き込みはありませんでした）")

h1 = json.load(open(hb, encoding="utf-8"))
h2 = json.load(open(ha, encoding="utf-8"))
v1 = {v["venue_id"]: v.get("last_ok_at") for v in h1.get("venues", [])}
v2 = {v["venue_id"]: v.get("last_ok_at") for v in h2.get("venues", [])}
if v1 != v2:
    print("   やり直し: 写している間に、今の版が集め終わった回がありました")
    sys.exit(3)

db = os.path.join(a, NAME)
c = sqlite3.connect(db)
r = [x[0] for x in c.execute("PRAGMA integrity_check").fetchall()]
if r != ["ok"]:
    print("   NG 整合の確かめ（integrity_check）:", r[:5])
    sys.exit(4)
print("   OK 整合の確かめ（integrity_check）: ok")
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
mode = c.execute("PRAGMA journal_mode=DELETE").fetchone()[0]     # 1つのファイルにまとめる（新しい版は読むだけで開く）
r = [x[0] for x in c.execute("PRAGMA integrity_check").fetchall()]
if r != ["ok"] or mode != "delete":
    print("   NG 1つのファイルにまとめたあとの確かめ:", r[:5], mode)
    sys.exit(4)

tables = {}
for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
    cols = [x[1] for x in c.execute(f'PRAGMA table_info("{t}")')]
    tcol = next((x for x in ("ts", "started_at", "opened_at", "checked_at") if x in cols), None)
    n = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    lo, hi = c.execute(f'SELECT MIN("{tcol}"), MAX("{tcol}") FROM "{t}"').fetchone() if tcol else (None, None)
    tables[t] = {"rows": n, "from": lo, "to": hi}

match, bad = [], 0
for vid, api_last in sorted(v1.items()):
    row = c.execute("SELECT finished_at FROM collection_runs WHERE venue_id=? AND status='ok' ORDER BY id DESC LIMIT 1",
                    (vid,)).fetchone()
    mine = row[0] if row else None
    same = mine == api_last
    bad += not same
    match.append({"venue": vid, "api": api_last, "copy": mine, "same": same})
    print(f"   {'OK' if same else 'NG'} {vid}: 今の版の答え {api_last} / 写しの中 {mine}")
c.close()
if bad:
    print("   やり直し: 今の版の答えと、写しの中の最後の記録が違います")
    sys.exit(3)

fewer = []
if prev and os.path.exists(prev):
    old = json.load(open(prev, encoding="utf-8")).get("tables", {})
    fewer = [t for t, v in old.items() if t in tables and tables[t]["rows"] < v.get("rows", 0)]
for t in ("pool_snapshots", "scores", "collection_runs", "positions", "position_pnl", "token_prices", "funding_rates"):
    if t in tables:
        v = tables[t]
        print(f"   {t}: {v['rows']} 件（{v['from']} 〜 {v['to']}）")
for t in fewer:
    print(f"   注意: {t} の件数が前の写しより少なくなっています（{tables[t]['rows']} 件）")

manifest = {"copied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": "18000 data/farm_radar.sqlite3",
            "bytes": os.path.getsize(db), "sha256": sha(db), "integrity": "ok", "journal_mode": mode,
            "health_match": match, "tables": tables, "fewer_than_previous": fewer}
json.dump(manifest, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"RESULT OK {len(tables)} 表 / {manifest['bytes']:,} バイト")
sys.exit(0)
'@

$work = $null
try {
    Write-Host '今の版（18000）のデータベースの写しを作ります。今の版は止めません。データベースは読むだけです。'

    Step '写し 1/5 フォルダーと道具を確かめる（何も変えません）'
    $liveDir = Join-Path $Desk $Live
    $v2Dir = Join-Path $Desk $V2Name
    $src = Join-Path $liveDir 'data\farm_radar.sqlite3'
    if (-not (Test-Path -LiteralPath $src)) { throw "今の版のデータベース $src が見つかりません。" }
    if (-not (Test-Path -LiteralPath $v2Dir)) { throw "新しい版のフォルダー $v2Dir が見つかりません。" }
    if (-not $Python) {
        foreach ($cand in @('py', 'python')) {
            try {
                $v = (& $cand --version) 2>&1 | Out-String
                if ($LASTEXITCODE -eq 0 -and $v -match 'Python 3') { $Python = $cand; break }
            } catch { }
        }
    }
    if (-not $Python) { throw 'Python 3 が見つかりません（写しの確かめができないので、写しを置きません）。' }
    $size = (Get-Item -LiteralPath $src).Length
    if (Test-Path -LiteralPath "$src-wal") { $size += (Get-Item -LiteralPath "$src-wal").Length }
    $free = (Get-PSDrive -Name ($env:TEMP.Substring(0, 1))).Free
    Write-Host ("   今の版のデータベース: {0:N0} バイト / 空き: {1:N0} バイト" -f $size, $free)
    if ($free -lt 4 * $size + 200MB) { throw '空きが足りません（データベースの4倍ほど要ります）。' }
    Ok 'フォルダーと Python があります。'

    $work = Join-Path ([IO.Path]::GetTempPath()) ('farm-radar-copy18000-' + (Get-Date -Format 'yyyyMMddHHmmss'))
    New-Item -ItemType Directory -Path $work | Out-Null
    $checkPy = Join-Path $work 'check.py'
    [IO.File]::WriteAllText($checkPy, $Check, (New-Object Text.UTF8Encoding($false)))
    $importDir = Join-Path $v2Dir 'data\import'
    $prev = Join-Path $importDir 'old-18000.json'
    $done = $false

    for ($try = 1; $try -le $Tries -and -not $done; $try++) {
        Step "写し 2/5 書き込みの少ない時刻を待つ（$try 回目。最大 $WaitMinutes 分）"
        # 今の版は 5 分ごとの区切り（0・5・10…分）に仕事を始める。区切りの直後を避け、ファイルが1分以上変わっていないときに写す
        $deadline = (Get-Date).AddMinutes($WaitMinutes)
        while ($true) {
            $now = Get-Date
            if ($now -gt $deadline) { throw '書き込みの少ない時刻が来ませんでした。' }
            $last = (Get-Item -LiteralPath $src).LastWriteTime
            if (Test-Path -LiteralPath "$src-wal") {
                $w = (Get-Item -LiteralPath "$src-wal").LastWriteTime
                if ($w -gt $last) { $last = $w }
            }
            if (($now.Minute % 5) -ge 2 -and ($now - $last).TotalSeconds -ge 60) { break }
            Start-Sleep -Seconds 10
        }
        Ok ("{0:HH:mm:ss} に写し始めます。" -f (Get-Date))

        Step '写し 3/5 同じものを2回写す（今の版のファイルは読むだけ）'
        $h1 = Join-Path $work "health-before-$try.json"
        $h2 = Join-Path $work "health-after-$try.json"
        Invoke-WebRequest -UseBasicParsing -Uri "$OldApi/api/health" -OutFile $h1 -TimeoutSec 90
        $dirs = @()
        foreach ($k in @('a', 'b')) {
            $d = Join-Path $work "$k$try"
            New-Item -ItemType Directory -Path $d | Out-Null
            Copy-Item -LiteralPath $src -Destination (Join-Path $d 'old.sqlite3')
            if (Test-Path -LiteralPath "$src-wal") { Copy-Item -LiteralPath "$src-wal" -Destination (Join-Path $d 'old.sqlite3-wal') }
            $dirs += $d
        }
        Invoke-WebRequest -UseBasicParsing -Uri "$OldApi/api/health" -OutFile $h2 -TimeoutSec 90
        Ok '2回写しました。'

        Step '写し 4/5 写しを確かめる（2回の写しが同じか・整合・件数・今の版の答えとの照合）'
        $manifest = Join-Path $work "manifest-$try.json"
        $env:PYTHONIOENCODING = 'utf-8'
        $enc = [Console]::OutputEncoding
        try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false) } catch { }
        & $Python $checkPy $dirs[0] $dirs[1] $h1 $h2 $manifest $prev
        $code = $LASTEXITCODE
        try { [Console]::OutputEncoding = $enc } catch { }
        if ($code -eq 0) { $done = $true; $good = $dirs[0]; break }
        if ($code -ne 3) { throw '写しが壊れています（整合の確かめで問題）。写しは置きません。今の版はそのままです。' }
        Remove-Item -LiteralPath $dirs[0], $dirs[1] -Recurse -Force
        if ($try -lt $Tries) {
            Write-Host '   1分待ってから、もう一度写します。' -ForegroundColor Yellow
            Start-Sleep -Seconds 60
        }
    }
    if (-not $done) { throw "$Tries 回写しても、写している間に書き込みがありました。時間をおいて、もう一度貼ってください。" }

    Step '写し 5/5 新しい版のフォルダーに置く（data\import\old-18000.sqlite3）'
    if (-not (Test-Path -LiteralPath $importDir)) { New-Item -ItemType Directory -Path $importDir | Out-Null }
    Copy-Item -LiteralPath (Join-Path $good 'old.sqlite3') -Destination (Join-Path $importDir 'old-18000.sqlite3.new') -Force
    Move-Item -LiteralPath (Join-Path $importDir 'old-18000.sqlite3.new') -Destination (Join-Path $importDir 'old-18000.sqlite3') -Force
    Copy-Item -LiteralPath $manifest -Destination $prev -Force
    $m = Get-Content -LiteralPath $prev -Raw -Encoding UTF8 | ConvertFrom-Json
    Ok ("置きました: {0:N0} バイト / 写した時刻（UTC） {1}" -f $m.bytes, $m.copied_at)
    Write-Host '[写し] integrity: ok / 今の版の答えと照合: OK / 表の数: ' -NoNewline
    Write-Host @($m.tables.PSObject.Properties).Count
    # 新しい版が写しを読めるか（読むだけ）。読めなくても写しはできているので、注意だけ出す
    try {
        $r = Invoke-RestMethod "$NewApi/api/trial/records" -TimeoutSec 60
        $ps = $r.old_copy.tables.pool_snapshots
        Write-Host "[写し] 新しい版で読めた: $($r.old_copy.checked) / pool_snapshots $($ps.rows) 件（$($ps.from) 〜 $($ps.to)）"
    } catch {
        Write-Host "   注意: 新しい版で写しを読めるか確かめられませんでした（$($_.Exception.Message)）。" -ForegroundColor Yellow
    }
    Write-Host '写しが終わりました。' -ForegroundColor Green
} catch {
    Write-Host ''
    Write-Host "止めました（写し）: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host '今の版には何もしていません。この画面の文字を全部コピーして送ってください。' -ForegroundColor Red
    exit 1
} finally {
    if ($work -and (Test-Path -LiteralPath $work)) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
}
exit 0
