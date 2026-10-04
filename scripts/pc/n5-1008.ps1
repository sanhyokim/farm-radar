# 10/8 ごろの N5 最終判定（Windows PowerShell 5.1 用）。2026-10-05 指示書「10/8のN5最終判定を一発で出せる状態にする」。
#
# やること（これだけ）:
#   1. 今の版（18000）と新しい版（18001）が動いているかを確かめる（何も変えない）。今の版の様子を控える。
#   2. 今の版のデータベースの写しを取り直す（scripts/pc/copy-18000.ps1。今の版は止めない・読むだけ）。
#   3. N5 最終判定レポートを作る（backend/farm_radar/n5_report.py をパソコンの Python で動かす。新しい版の答えを読むだけ）。
#   4. 今の版の様子をもう一度見て、写す前と同じか確かめる。
#   5. いちばん下に「結果」のまとめを出す。
#
# しないこと: コードの更新・入れ直し・Docker の作り直しや起動し直し。新しい版の記録（Merkl・預け方の歴史など）には触らない。
# 今の版のフォルダーとデータには触らない（写しは copy-18000.ps1 がファイルを読んで作る）。お金を動かすことはしない。秘密の鍵は読まない。
#
# 使い方（PowerShell に1行貼る。<SHA> はこのファイルが入ったコミット）:
#   $s='<SHA>'; Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/sanhyokim/farm-radar/$s/scripts/pc/n5-1008.ps1" -OutFile C:\Users\user\Downloads\n5-1008.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\user\Downloads\n5-1008.ps1 -Sha $s

param(
    [Parameter(Mandatory = $true)][string]$Sha,
    [string]$Desk = 'C:\Users\user\Desktop',
    [string]$Downloads = 'C:\Users\user\Downloads',
    [string]$Live = 'farm-radar-claude-project-thread-np18zb',
    [string]$V2Name = 'farm-radar-v2',
    [string]$OldApi = 'http://localhost:18000',
    [string]$NewApi = 'http://localhost:18001',
    [string]$RawBase = 'https://raw.githubusercontent.com/sanhyokim/farm-radar',
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$Work = Join-Path $Downloads "farm-radar-n5-$Stamp"
$Log = Join-Path $Downloads "farm-radar-n5-$Stamp.txt"
$Report = Join-Path $Downloads "n5-report-$Stamp.txt"
$SummaryJson = Join-Path $Work 'summary.json'
$res = [ordered]@{ copy = 'まだ'; v1 = 'まだ'; report = 'まだ' }

function Say([string]$text) { Write-Host $text }
function Step([string]$text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Ok([string]$text) { Write-Host "   OK: $text" -ForegroundColor Green }

function OldState {
    # 今の版は集計中に返事が遅くなることがあるので、90秒まで待ち、だめなら20秒あけて3回まで試す（update-v2.ps1 と同じ）
    for ($i = 1; $i -le 3; $i++) {
        try {
            $e = Invoke-RestMethod "$OldApi/api/paper/evaluation" -TimeoutSec 90
            $q = Invoke-RestMethod "$OldApi/api/pulse" -TimeoutSec 90
            return [pscustomobject]@{ eval = "$($e.state)"; open = "$($e.open_positions)"; stale = "$($q.stale)"; last = "$($q.last_ok_at)" }
        } catch {
            if ($i -eq 3) { throw "今の版（18000）が答えません（3回試しました）: $($_.Exception.Message)" }
            Say "   今の版の返事が遅いので、20秒後にもう一度確かめます（$i/3）……"
            Start-Sleep -Seconds 20
        }
    }
}

function Fetch([string]$path, [string]$out) {
    if ($Sha -notmatch '^[0-9a-f]{7,40}$') { throw "コミットの印（$Sha）の形が違います。" }
    Invoke-WebRequest -UseBasicParsing -Uri "$RawBase/$Sha/$path" -OutFile $out -TimeoutSec 120
    if (-not (Test-Path -LiteralPath $out) -or (Get-Item -LiteralPath $out).Length -lt 1000) { throw "$path を受け取れませんでした。" }
}

try { Start-Transcript -Path $Log -Force | Out-Null } catch { }
try {
    Say 'N5 最終判定: 今の版（18000）の写しを取り直して、判定レポートを作ります。'
    Say '今の版は止めません。新しい版の更新や起動し直しもしません。10〜30 分ほどかかります。途中でこの画面を閉じないでください。'

    # --- 1. 確かめる（何も変えない） -----------------------------------------------------------
    Step '1/5 フォルダーと、動いているアプリを確かめる（何も変えません）'
    $liveDir = Join-Path $Desk $Live
    $v2Dir = Join-Path $Desk $V2Name
    if (-not (Test-Path -LiteralPath $liveDir)) { throw "今の版のフォルダー $liveDir が見つかりません。" }
    if (-not (Test-Path -LiteralPath $v2Dir)) { throw "新しい版のフォルダー $v2Dir が見つかりません。" }
    $before = OldState
    Say "   今の版（写す前）: 評価 $($before.eval) / 建玉 $($before.open) / 古い $($before.stale) / 最後 $($before.last)"
    try { $rec0 = Invoke-RestMethod "$NewApi/api/trial/records" -TimeoutSec 300 } catch { throw "新しい版（18001）が答えません: $($_.Exception.Message)" }
    $ms = $rec0.feeds.merkl_sums
    if (-not $ms -or -not $rec0.feeds.pool_history) {
        throw '新しい版に Merkl の全部のページの記録か預け方の歴史が見つかりません（PR #41 の版ではないかもしれません）。'
    }
    Say "   新しい版: Merkl の全部のページ complete=1 $($ms.complete) 件（$($ms.from) 〜 $($ms.to)）/ 前の写し $($rec0.old_copy.copied_at)"
    if (-not $Python) {
        foreach ($cand in @('py', 'python')) {
            try {
                $v = (& $cand --version) 2>&1 | Out-String
                if ($LASTEXITCODE -eq 0 -and $v -match 'Python 3') { $Python = $cand; break }
            } catch { }
        }
    }
    if (-not $Python) { throw 'Python 3 が見つかりません（写しの確かめとレポートに使います）。' }
    Ok '今の版と新しい版が答えています。Python もあります。'

    # --- 2. 道具を受け取る（コミットの印で固定） ------------------------------------------------
    Step "2/5 写しの道具とレポートの道具を受け取る（コミット $Sha）"
    New-Item -ItemType Directory -Path $Work -Force | Out-Null
    $copyPs = Join-Path $Work 'copy-18000.ps1'
    $reportPy = Join-Path $Work 'n5_report.py'
    Fetch 'scripts/pc/copy-18000.ps1' $copyPs
    Fetch 'backend/farm_radar/n5_report.py' $reportPy
    Ok '受け取りました。'

    # --- 3. 今の版の写しを取り直す ----------------------------------------------------------------
    Step '3/5 今の版の写しを取り直す（今の版は止めません。データベースのファイルを読むだけ）'
    & powershell -NoProfile -ExecutionPolicy Bypass -File $copyPs -Desk $Desk -Live $Live -V2Name $V2Name -OldApi $OldApi -NewApi $NewApi -Python $Python
    if ($LASTEXITCODE -ne 0) { $res.copy = '失敗'; throw '写しがうまくいきませんでした（上の「止めました（写し）」の理由）。レポートは作りません。' }
    $res.copy = '成功'
    Ok '写しを取り直しました。'

    # --- 4. N5 最終判定レポート ------------------------------------------------------------------
    Step '4/5 N5 最終判定レポートを作る（新しい版の答えを読むだけ。さかのぼりの計算し直しで数分かかることがあります）'
    $env:PYTHONIOENCODING = 'utf-8'
    $enc = [Console]::OutputEncoding
    try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false) } catch { }
    & $Python $reportPy --api $NewApi --out $Report --summary $SummaryJson
    $code = $LASTEXITCODE
    try { [Console]::OutputEncoding = $enc } catch { }
    if ($code -ne 0 -or -not (Test-Path -LiteralPath $SummaryJson)) { $res.report = '失敗'; throw 'レポートを作れませんでした（上の STOP の理由）。写しはできています。' }
    $res.report = '成功'
    Ok "レポートを $Report に保存しました。"

    # --- 5. 今の版がそのままか --------------------------------------------------------------------
    Step '5/5 今の版がそのままか確かめる'
    $after = OldState
    Say "   今の版（写した後）: 評価 $($after.eval) / 建玉 $($after.open) / 古い $($after.stale) / 最後 $($after.last)"
    if ($after.eval -ne $before.eval -or $after.open -ne $before.open) { $res.v1 = '変わった'; throw '今の版の評価か建玉の数が、写す前と違います。' }
    if ($after.stale -eq 'True' -and $before.stale -ne 'True') { $res.v1 = '集めていない'; throw '今の版が、写したあとに新しい記録を集めていない（古い）ようです。' }
    if ($after.stale -eq 'True') { Write-Host '   注意: 今の版は写す前から「古い」でした（写しとは関係ありません）。' -ForegroundColor Yellow }
    $res.v1 = 'そのまま'
    Ok '今の版はそのまま動いています。'

    $s = Get-Content -LiteralPath $SummaryJson -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host ''
    Write-Host '==================== 結果 ====================' -ForegroundColor Green
    Write-Host "写し: 成功（写した時刻 UTC $($s.copied_at)・確かめ $(if ($s.copy_checked) { 'OK' } else { 'なし' })）"
    Write-Host "v1（今の版）: そのまま（評価 $($after.eval) / 建玉 $($after.open) / 古い $($after.stale)）"
    Write-Host "記録日数: $($s.backtest_days) 日（$($s.first_day) 〜 $($s.last_day)）"
    Write-Host "7日の判定: $(if ($s.seven_day_ready) { 'できる' } else { 'まだ' }) / 14日の判定: $(if ($s.fourteen_day_ready) { 'できる' } else { "まだ（$($s.fourteen_day_from) まで入った写しから）" })"
    Write-Host "Merkl A/B の条件: $(if ($s.merkl_ab_ready) { 'すべて満たした' } else { 'まだ' })"
    foreach ($c in $s.merkl_ab_conditions) { Write-Host "   $($c[0]): $($c[1])" }
    Write-Host "預け方の歴史: $(if ($s.pool_history -eq 'ready') { '全部読み終わり' } else { '読み途中（読めている区切りだけ使う）' })"
    Write-Host "判断待ち・記録待ち: $(@($s.waiting).Count) 件"
    foreach ($w in $s.waiting) { Write-Host "   - $w" }
    Write-Host "レポート: $Report"
    Write-Host '結果: 成功。この画面の文字を全部コピーして、指示役に送ってください（レポートのファイルも一緒に）。' -ForegroundColor Green
} catch {
    Write-Host ''
    Write-Host "止めました: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "写し: $($res.copy) / レポート: $($res.report) / v1（今の版）: $($res.v1)" -ForegroundColor Red
    Write-Host '今の版には何もしていません（止めていない・データも変えていない）。新しい版の更新もしていません。' -ForegroundColor Red
    Write-Host 'この画面の文字を全部コピーして送ってください。' -ForegroundColor Red
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}
try { Stop-Transcript | Out-Null } catch { }
exit 0
