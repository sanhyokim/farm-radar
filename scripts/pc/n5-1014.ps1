# 10/14 の14日判定（損の線だけ。Windows PowerShell 5.1 用）。2026-10-14 指示書「10/14の14日判定を軽量に実行する」。
#
# やること（これだけ）:
#   1. 今の版（18000）と新しい版（18001）が答えるかを、軽いAPIだけで確かめる（何も変えない）。
#   2. 今の版のデータベースの写しを取り直す（scripts/pc/copy-18000.ps1 -NoNewCheck。今の版は止めない・読むだけ）。
#   3. さかのぼり（/api/trial/backtest）だけを計算し直し、損の線（1日・1週間・始めてから）をまとめる
#      （backend/farm_radar/n5_1014_report.py をパソコンの Python で動かす。新しい版の答えを読むだけ）。
#   4. 今の版の様子をもう一度見て、写す前と同じか確かめる。
#   5. N6 の今の様子（読むだけ）と「10/14 判定」のまとめを、いちばん下に出す。
#
# 呼ばない重いAPI: フルの /api/trial/records・/api/trial/final・/api/trial/summary（Merkl の答え合わせは計算しない）。
# しないこと: 新しい版の更新・Docker の作り直しや起動し直し・控えのフォルダー作り。N6 の設定や判断・線・お金には触らない。
# 今の版のフォルダーとデータには触らない（写しは copy-18000.ps1 がファイルを読んで作る）。秘密の鍵は読まない。途中で y を押す操作はない。
#
# 使い方（PowerShell に1行貼る。<SHA> はこのファイルが入ったコミット）:
#   $s='<SHA>'; Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/sanhyokim/farm-radar/$s/scripts/pc/n5-1014.ps1" -OutFile C:\Users\user\Downloads\n5-1014.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\user\Downloads\n5-1014.ps1 -Sha $s

param(
    [Parameter(Mandatory = $true)][string]$Sha,
    [string]$Desk = 'C:\Users\user\Desktop',
    [string]$Downloads = 'C:\Users\user\Downloads',
    [string]$Live = 'farm-radar-claude-project-thread-np18zb',
    [string]$V2Name = 'farm-radar-v2',
    [string]$OldApi = 'http://localhost:18000',
    [string]$NewApi = 'http://localhost:18001',
    [string]$RawBase = 'https://raw.githubusercontent.com/sanhyokim/farm-radar',
    [string]$Python = '',
    [string]$Shell = 'powershell',
    [int]$BacktestTimeout = 3600
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$Work = Join-Path $Downloads "farm-radar-n5-1014-$Stamp"
$Log = Join-Path $Downloads "farm-radar-n5-1014-$Stamp-log.txt"
$Report = Join-Path $Downloads "n5-loss-review-$Stamp.txt"
$SummaryJson = Join-Path $Work 'summary.json'
$res = [ordered]@{ copy = 'まだ'; v1 = 'まだ'; report = 'まだ' }

function Say([string]$text) { Write-Host $text }
function Step([string]$text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Ok([string]$text) { Write-Host "   OK: $text" -ForegroundColor Green }

function OldState {
    # 今の版は集計中に返事が遅くなることがあるので、90秒まで待ち、だめなら20秒あけて3回まで試す（n5-1008.ps1 と同じ）
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

function AddReport([string[]]$lines) {
    if (Test-Path -LiteralPath $Report) { Add-Content -LiteralPath $Report -Value $lines -Encoding UTF8 }
}

try { Start-Transcript -Path $Log -Force | Out-Null } catch { }
try {
    Say '10/14 の14日判定: 今の版（18000）の写しを取り直して、損の線だけをまとめます。'
    Say '今の版は止めません。新しい版の更新や起動し直しもしません。10〜40 分ほどかかります。途中でこの画面を閉じないでください。'

    # --- 1. 確かめる（軽いAPIだけ。何も変えない） ---------------------------------------------------
    Step '1/5 フォルダーと、動いているアプリを確かめる（軽いAPIだけ。何も変えません）'
    $liveDir = Join-Path $Desk $Live
    $v2Dir = Join-Path $Desk $V2Name
    if (-not (Test-Path -LiteralPath $liveDir)) { throw "今の版のフォルダー $liveDir が見つかりません。" }
    if (-not (Test-Path -LiteralPath $v2Dir)) { throw "新しい版のフォルダー $v2Dir が見つかりません。" }
    $before = OldState
    Say "   今の版（写す前）: 評価 $($before.eval) / 建玉 $($before.open) / 古い $($before.stale) / 最後 $($before.last)"
    if ($before.stale -eq 'True') { $res.v1 = '古い'; throw '今の版が「古い」（stale: True）になっています。写しは取りません。' }
    try { $p2 = Invoke-RestMethod "$NewApi/api/pulse" -TimeoutSec 90 } catch { throw "新しい版（18001）が答えません: $($_.Exception.Message)" }
    Say "   新しい版: 答えています（N6 はいちばん最後に、読むだけで確かめます）"
    if (-not $Python) {
        foreach ($cand in @('py', 'python')) {
            try {
                $v = (& $cand --version) 2>&1 | Out-String
                if ($LASTEXITCODE -eq 0 -and $v -match 'Python 3') { $Python = $cand; break }
            } catch { }
        }
    }
    if (-not $Python) { throw 'Python 3 が見つかりません（写しの確かめと損の線のまとめに使います）。' }
    Ok '今の版と新しい版が答えています。Python もあります。'

    # --- 2. 道具を受け取る（コミットの印で固定） ------------------------------------------------
    Step "2/5 写しの道具と損の線の道具を受け取る（コミット $Sha）"
    New-Item -ItemType Directory -Path $Work -Force | Out-Null
    $copyPs = Join-Path $Work 'copy-18000.ps1'
    $reportPy = Join-Path $Work 'n5_1014_report.py'
    Fetch 'scripts/pc/copy-18000.ps1' $copyPs
    Fetch 'backend/farm_radar/n5_1014_report.py' $reportPy
    Ok '受け取りました。'

    # --- 3. 今の版の写しを取り直す ----------------------------------------------------------------
    Step '3/5 今の版の写しを取り直す（今の版は止めません。データベースのファイルを読むだけ）'
    & $Shell -NoProfile -ExecutionPolicy Bypass -File $copyPs -Desk $Desk -Live $Live -V2Name $V2Name -OldApi $OldApi -NewApi $NewApi -Python $Python -NoNewCheck
    if ($LASTEXITCODE -ne 0) { $res.copy = '失敗'; throw '写しがうまくいきませんでした（上の「止めました（写し）」の理由）。損の線は計算しません。' }
    $res.copy = '成功'
    Ok '写しを取り直しました。'

    # --- 4. さかのぼりと損の線 ---------------------------------------------------------------------
    Step '4/5 さかのぼりだけを計算し直して、損の線をまとめる（Merkl の答え合わせは計算しません）'
    $manifest = Join-Path $v2Dir 'data\import\old-18000.json'
    $v1b = "評価 $($before.eval) / 建玉 $($before.open) / 古い $($before.stale) / 最後 $($before.last)"
    $env:PYTHONIOENCODING = 'utf-8'
    $enc = [Console]::OutputEncoding
    try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false) } catch { }
    & $Python $reportPy --api $NewApi --manifest $manifest --out $Report --summary $SummaryJson --v1-before $v1b --timeout $BacktestTimeout
    $code = $LASTEXITCODE
    try { [Console]::OutputEncoding = $enc } catch { }
    if ($code -ne 0 -or -not (Test-Path -LiteralPath $SummaryJson)) { $res.report = '失敗'; throw 'さかのぼりを計算できませんでした（上の STOP の理由）。写しはできています。' }
    $res.report = '成功'
    Ok "レポートを $Report に保存しました。"

    # --- 5. 今の版がそのままか --------------------------------------------------------------------
    Step '5/5 今の版がそのままか確かめる'
    $after = OldState
    Say "   今の版（写した後）: 評価 $($after.eval) / 建玉 $($after.open) / 古い $($after.stale) / 最後 $($after.last)"
    $v1Line = "今の版（写した後）: 評価 $($after.eval) / 建玉 $($after.open) / 古い $($after.stale) / 最後 $($after.last)"
    if ($after.eval -ne $before.eval -or $after.open -ne $before.open) { $res.v1 = '変わった'; AddReport @('', $v1Line); throw '今の版の評価か建玉の数が、写す前と違います。' }
    if ($after.stale -eq 'True') { $res.v1 = '古い'; AddReport @('', $v1Line); throw '今の版が、写したあとに「古い」（stale: True）になりました。' }
    $res.v1 = 'そのまま'
    Ok '今の版はそのまま動いています。'

    $s = Get-Content -LiteralPath $SummaryJson -Raw -Encoding UTF8 | ConvertFrom-Json
    $out = @()
    $out += '==================== 10/14 判定 ===================='
    $out += "写し: 成功（写した時刻 $($s.copied_at_text)）"
    $out += "v1: そのまま（評価 $($after.eval) / 建玉 $($after.open) / 古い $($after.stale)）"
    foreach ($l in $s.display) { $out += "$l" }
    $out += "レポート: $Report"
    if ($s.notes) { foreach ($n in $s.notes) { $out += "注意: $n" } }
    if ($s.ok) {
        $out += '結果: 成功'
    } else {
        foreach ($x in $s.stop) { $out += "止める理由: $x" }
        $out += '結果: 止めました（上の「止める理由」。今の版・N6 には何もしていません）'
    }
    $out += '===================================================='
    AddReport (@('', $v1Line, '') + $out)
    Write-Host ''
    foreach ($l in $out) {
        if ($l -like '結果: 成功*') { Write-Host $l -ForegroundColor Green }
        elseif ($l -like '結果: 止めました*' -or $l -like '止める理由*') { Write-Host $l -ForegroundColor Red }
        elseif ($l -like '注意*') { Write-Host $l -ForegroundColor Yellow }
        else { Write-Host $l }
    }
    Write-Host 'この「10/14 判定」の枠の中だけをコピーして、指示役に送ってください（くわしくはレポートのファイル）。'
    try { Stop-Transcript | Out-Null } catch { }
    if ($s.ok) { exit 0 } else { exit 1 }
} catch {
    Write-Host ''
    Write-Host '==================== 10/14 判定 ====================' -ForegroundColor Red
    Write-Host "止めました: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "写し: $($res.copy) / 損の線のまとめ: $($res.report) / v1: $($res.v1)" -ForegroundColor Red
    if (Test-Path -LiteralPath $Report) { Write-Host "レポート: $Report（損の線の数字はこのファイルにあります）" -ForegroundColor Red }
    Write-Host '今の版には何もしていません（止めていない・データも変えていない）。新しい版の更新や N6 の変更もしていません。' -ForegroundColor Red
    Write-Host '結果: 止めました' -ForegroundColor Red
    Write-Host '====================================================' -ForegroundColor Red
    Write-Host 'この画面の文字を全部コピーして送ってください。'
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}
