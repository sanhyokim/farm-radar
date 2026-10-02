# 9/29 にまちがって入れた古い版のフォルダー（farm-radar-m4-wrong）を、データが今の版に引き継がれているか確かめてから消す（Windows PowerShell 5.1 用）。
#
# 決まり（オーナー 2026-10-02 23:45 JST）:
# - 今の版（ポート 18000、フォルダー farm-radar-claude-project-thread-np18zb）のフォルダーとデータは読むだけ。
# - 消す前に、今の版のデータベースの方が大きく新しいことを確かめる。そうでなければ何も消さずに止まる。
# - さらに、古いフォルダーのデータベースの表ごとに、同じ期間の記録が今の版に同じ数以上あるかを確かめる（読むだけ）。
# - docker が使っているフォルダーは消さない。farm-radar-1001-old と、別のプロジェクト（srpca など）には触らない。
# - 消したフォルダーはごみ箱に入らない（元に戻せない）。お金を動かすことはしない。秘密の鍵は読まない。

param(
    [string]$Desk = 'C:\Users\user\Desktop',
    [string]$Name = 'farm-radar-m4-wrong',
    [string]$Live = 'farm-radar-claude-project-thread-np18zb',
    [string]$Docker = 'docker',
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'

function Step([string]$text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Ok([string]$text) { Write-Host "   OK: $text" -ForegroundColor Green }

# 古いフォルダーの表ごとに、同じ期間の記録の数を今の版と比べる（どちらも読むだけで開く）
$Check = @'
import sqlite3, sys, pathlib
old_p, live_p = sys.argv[1], sys.argv[2]
old = sqlite3.connect(pathlib.Path(old_p).as_uri() + "?mode=ro&immutable=1", uri=True)
live = sqlite3.connect(pathlib.Path(live_p).as_uri() + "?mode=ro", uri=True)
def tables(c):
    return [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
def cols(c, t):
    return [r[1] for r in c.execute(f'PRAGMA table_info("{t}")')]
live_tables = set(tables(live))
bad = 0
for t in sorted(tables(old)):
    n_old = old.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    if n_old == 0:
        continue
    if t not in live_tables:
        print(f"   NG {t}: 今の版にこの表がない（古い方 {n_old} 件）"); bad += 1; continue
    if "ts" in cols(old, t) and "ts" in cols(live, t):
        lo, hi = old.execute(f'SELECT MIN(ts), MAX(ts) FROM "{t}"').fetchone()
        n_live = live.execute(f'SELECT COUNT(*) FROM "{t}" WHERE ts >= ? AND ts <= ?', (lo, hi)).fetchone()[0]
        span = f"{lo} 〜 {hi} の期間"
    else:
        n_live = live.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        span = "全部"
    mark = "OK" if n_live >= n_old else "NG"
    bad += mark == "NG"
    print(f"   {mark} {t}: {span} 古い方 {n_old} 件 / 今の版 {n_live} 件")
print("RESULT", "OK" if bad == 0 else f"NG {bad}")
sys.exit(0 if bad == 0 else 3)
'@

try {
    Write-Host "$Name を、データが今の版に引き継がれているか確かめてから消します。今の版は読むだけです。"

    Step '1/4 フォルダーを確かめる（何も変えません）'
    if ($Name -notlike 'farm-radar-*' -or $Name -in @($Live, 'farm-radar-v2', 'farm-radar-1001-old') -or $Name -match '[\\/*?]') {
        throw "$Name は消せない名前です。"
    }
    $old = Join-Path $Desk $Name
    $liveDir = Join-Path $Desk $Live
    if (-not (Test-Path -LiteralPath $old)) { throw "$old がありません（もう消えています）。" }
    if (-not (Test-Path -LiteralPath $liveDir)) { throw "今の版のフォルダー $liveDir が見つかりません。" }
    $oldDb = Join-Path $old 'data\farm_radar.sqlite3'
    $liveDb = Join-Path $liveDir 'data\farm_radar.sqlite3'
    foreach ($f in @($oldDb, $liveDb)) { if (-not (Test-Path -LiteralPath $f)) { throw "$f がありません。" } }
    Ok 'どちらのフォルダーにもデータベースがあります。'

    Step '2/4 docker が古いフォルダーを使っていないか確かめる（何も変えません）'
    $raw = (& $Docker compose ls -a --format json) | Out-String
    if ($LASTEXITCODE -ne 0) { throw 'docker compose ls が動きません。Docker Desktop が起動しているか確かめてください。' }
    foreach ($p in @(($raw | ConvertFrom-Json) | ForEach-Object { $_ })) {
        Write-Host "   $($p.Name) ($($p.Status)): $($p.ConfigFiles)"
        if ("$($p.ConfigFiles)" -like "*\$Name\*") { throw "docker の $($p.Name) が $Name を使っています。消しません。" }
    }
    Ok "$Name は使われていません。"

    Step '3/4 今の版のデータベースの方が大きく新しいか、記録が引き継がれているかを確かめる（読むだけ）'
    $o = Get-Item -LiteralPath $oldDb
    $l = Get-Item -LiteralPath $liveDb
    $lTime = $l.LastWriteTime
    $wal = "$liveDb-wal"
    if (Test-Path -LiteralPath $wal) { $w = (Get-Item -LiteralPath $wal).LastWriteTime; if ($w -gt $lTime) { $lTime = $w } }
    Write-Host ("   古い方: {0:N0} バイト / 最後に書かれたのは {1:yyyy-MM-dd HH:mm}" -f $o.Length, $o.LastWriteTime)
    Write-Host ("   今の版: {0:N0} バイト / 最後に書かれたのは {1:yyyy-MM-dd HH:mm}" -f $l.Length, $lTime)
    if ($l.Length -le $o.Length) { throw '今の版のデータベースの方が小さいです。何も消しません。' }
    if ($lTime -le $o.LastWriteTime) { throw '今の版のデータベースの方が古いです。何も消しません。' }
    Ok '今の版のデータベースの方が大きく新しいです。'

    if (-not $Python) {
        foreach ($cand in @('py', 'python')) {
            try {
                $v = (& $cand --version) 2>&1 | Out-String
                if ($LASTEXITCODE -eq 0 -and $v -match 'Python 3') { $Python = $cand; break }
            } catch { }
        }
    }
    if (-not $Python) { throw 'Python 3 が見つかりません（表ごとの確かめができないので、何も消しません）。' }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) 'farm-radar-compare.py'
    [IO.File]::WriteAllText($tmp, $Check, (New-Object Text.UTF8Encoding($false)))
    $env:PYTHONIOENCODING = 'utf-8'
    $enc = [Console]::OutputEncoding
    try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false) } catch { }   # Python の日本語を化けさせない
    & $Python $tmp $oldDb $liveDb
    $code = $LASTEXITCODE
    try { [Console]::OutputEncoding = $enc } catch { }
    Remove-Item -LiteralPath $tmp -Force
    if ($code -ne 0) { throw '古いフォルダーにしかない記録があるかもしれません（NG の表）。何も消しません。' }
    Ok '古いフォルダーの記録は、どの表も今の版に同じ数以上あります。'

    Step "4/4 $Name を消す（ごみ箱に入らず、元に戻せません）"
    Remove-Item -LiteralPath $old -Recurse -Force
    Ok "消しました: $Name"
    $left = @(Get-ChildItem -LiteralPath $Desk -Directory -Filter 'farm-radar*' | ForEach-Object { $_.Name })
    Write-Host "   残っているフォルダー: $($left -join ', ')"
    Write-Host '片付けが終わりました。' -ForegroundColor Green
} catch {
    Write-Host ''
    Write-Host "止めました（片付け）: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'この画面の文字を全部コピーして送ってください。' -ForegroundColor Red
    exit 1
}
exit 0
