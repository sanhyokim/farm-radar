# 新しい版（ポート 18001、フォルダー farm-radar-v2）を、ブランチの最新に更新する（Windows PowerShell 5.1 用）。
#
# 使い方（PowerShell に1行貼る）:
#   Invoke-WebRequest -UseBasicParsing -Uri https://raw.githubusercontent.com/sanhyokim/farm-radar/claude/project-thread-np18zb/scripts/pc/update-v2.ps1 -OutFile C:\Users\user\Downloads\update-v2.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\user\Downloads\update-v2.ps1
#
# 決まり（オーナー）:
# - 今の版（ポート 18000、フォルダー farm-radar-claude-project-thread-np18zb）は止めない。フォルダーにもデータにも触らない。
# - 新しい版の今のフォルダーは、日時つきの名前（farm-radar-v2-old-日付-時刻）で残す。前からある控えのフォルダーにも触らない。
# - 設定ファイル（.env）と保存した一覧（data）は引き継ぐ。
# - おかしなところがあれば、その場で止まって理由を出す。途中で止まったときは、前の状態に戻してから止まる。
# - お金を動かすことはしない。秘密の鍵は読まない。

param(
    [string]$Desk = 'C:\Users\user\Desktop',
    [string]$Downloads = 'C:\Users\user\Downloads',
    [string]$ZipUrl = 'https://github.com/sanhyokim/farm-radar/archive/refs/heads/claude/project-thread-np18zb.zip',
    [string]$OldApi = 'http://localhost:18000',
    [string]$NewApi = 'http://localhost:18001',
    [string]$Docker = 'docker',
    [int]$WaitMinutes = 10
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'      # ダウンロードと展開を速くする（進み具合の表示を出さない）

$Inner = 'farm-radar-claude-project-thread-np18zb'   # ZIP の中のフォルダーの名前（今の版のフォルダーと同じ名前）
$Stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$V2 = Join-Path $Desk 'farm-radar-v2'
$Backup = Join-Path $Desk "farm-radar-v2-old-$Stamp"
$Tmp = Join-Path $Desk 'farm-radar-v2-tmp'
$Live = Join-Path $Desk $Inner
$Zip = Join-Path $Downloads 'farm-radar-v2-update.zip'
$Log = Join-Path $Downloads "farm-radar-v2-update-$Stamp.txt"
# 新しい版に入っているはずのファイル（無ければ古い ZIP なので止める）
$MustHave = @('backend\farm_radar\feeds\pools.py', 'backend\farm_radar\safety.py', 'docker-compose.yml', 'scripts\pc\update-v2.ps1')

$state = @{ stopped = $false; renamed = $false; moved = $false; started = $false }

function Say([string]$text) { Write-Host $text }
function Step([string]$text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Ok([string]$text) { Write-Host "   OK: $text" -ForegroundColor Green }

function Compose([string[]]$Rest) {
    # 新しい版だけを動かす（-p で名前を決めるので、今の版には届かない）。
    # Windows PowerShell 5.1 は docker の進み具合の表示を受け取ると「インデックスが配列の境界外です」で止まることがあるので
    # （2026-10-02 オーナーのパソコンで起きた）、PowerShell を通さずに docker を直接動かして、終わりの番号だけを見る
    $all = @('compose', '-f', (Join-Path $V2 'docker-compose.yml'), '--project-directory', $V2, '-p', 'farm-radar-v2') + $Rest
    $argLine = ($all | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
    $exe = (Get-Command $Docker -ErrorAction Stop).Source
    $p = Start-Process -FilePath $exe -ArgumentList $argLine -NoNewWindow -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "docker compose $($Rest -join ' ') がうまくいきませんでした（終わりの番号 $($p.ExitCode)）。" }
}

function Restore {
    # 途中で止まったとき、前の新しい版に戻す（今の版には触らない）
    if ($state.started) { return }
    Write-Host ''
    Write-Host '前の状態に戻しています……' -ForegroundColor Yellow
    try {
        if ($state.moved -and (Test-Path $V2)) {
            Rename-Item $V2 ("farm-radar-v2-failed-$Stamp")
            Say "   新しい中身のフォルダーは farm-radar-v2-failed-$Stamp という名前で残しました。"
        }
        if ($state.renamed -and (Test-Path $Backup) -and -not (Test-Path $V2)) {
            Rename-Item $Backup 'farm-radar-v2'
            Say '   前の新しい版のフォルダーを farm-radar-v2 に戻しました。'
        }
        if ($state.stopped) {
            Compose @('up', '-d', '--build', 'api', 'feeds')
            Say '   前の新しい版を起動し直しました。'
        }
        if (Test-Path $Tmp) { Remove-Item $Tmp -Recurse -Force }
    } catch {
        Write-Host "   戻す途中でも問題がありました: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host '   この画面の文字を全部送ってください。' -ForegroundColor Red
    }
}

function Projects {
    # docker compose ls の一覧（Windows PowerShell 5.1 は JSON の配列を1つの物として返すので、ばらしてから使う）
    $raw = (& $Docker compose ls -a --format json) | Out-String
    if ($LASTEXITCODE -ne 0) { throw 'docker compose ls が動きません。Docker Desktop が起動しているか確かめてください。' }
    $j = $raw | ConvertFrom-Json
    return @($j | ForEach-Object { $_ })
}

function Running([object[]]$list, [string]$name) {
    foreach ($p in $list) { if ($p.Name -eq $name -and $p.Status -like 'running*') { return $true } }
    return $false
}

function OldState {
    $e = Invoke-RestMethod "$OldApi/api/paper/evaluation" -TimeoutSec 30
    $q = Invoke-RestMethod "$OldApi/api/pulse" -TimeoutSec 30
    return [pscustomobject]@{ eval = "$($e.state)"; open = "$($e.open_positions)"; stale = "$($q.stale)"; last = "$($q.last_ok_at)" }
}

try { Start-Transcript -Path $Log -Force | Out-Null } catch { }
try {
    Say '新しい版（18001）を更新します。今の版（18000）には触りません。'
    Say '終わるまで 10〜20 分ほどかかります。途中でこの画面を閉じないでください。'

    # --- 1. 確かめる（まだ何も変えない） -------------------------------------------------------
    Step '1/9 今あるフォルダーと、動いているアプリを確かめる（何も変えません）'
    if (-not (Test-Path $Live)) { throw "今の版のフォルダー $Live が見つかりません。" }
    if (-not (Test-Path $V2)) { throw "新しい版のフォルダー $V2 が見つかりません。" }
    if (Test-Path $Tmp) { throw "$Tmp がもうあります（前の更新の残りです）。消さずに、この画面の文字を送ってください。" }
    if (Test-Path $Backup) { throw "$Backup がもうあります。1分待ってから、もう一度貼ってください。" }
    $envFile = Join-Path $V2 '.env'
    if (-not (Test-Path $envFile)) { throw "$envFile がありません。" }
    $envText = Get-Content $envFile -Raw
    if ($envText -notmatch 'COMPOSE_PROJECT_NAME=farm-radar-v2' -or $envText -notmatch 'FARM_RADAR_PORT=18001') {
        throw ".env の中身が思っていたものと違います（COMPOSE_PROJECT_NAME=farm-radar-v2 と FARM_RADAR_PORT=18001 が要ります）。"
    }
    $projects = Projects
    if (-not (Running $projects $Inner)) { throw "今の版（$Inner）が動いていません。" }
    if (-not (Running $projects 'farm-radar-v2')) { throw '新しい版（farm-radar-v2）が動いていません。' }
    Ok 'フォルダーとアプリは思っていたとおりです。'

    # --- 2. 前の状態をメモする ------------------------------------------------------------------
    Step '2/9 今の版と新しい版の状態をメモする（読むだけ）'
    $before = OldState
    Say "   今の版: eval: $($before.eval) / open: $($before.open) / stale: $($before.stale) / last_ok_at: $($before.last)"
    if ($before.stale -eq 'True') { throw '今の版のデータが古くなっています（stale: True）。更新はやめておきます。' }
    $fs = Invoke-RestMethod "$NewApi/api/feeds/status" -TimeoutSec 30
    $beforeSources = @($fs.sources).Count
    Say "   新しい版: enabled: $($fs.enabled) / chain_reads: $($fs.chain_reads) / 一覧の数: $beforeSources"

    # --- 3. ダウンロードと展開 ------------------------------------------------------------------
    Step '3/9 新しい版をダウンロードして、仮のフォルダーに展開する'
    Invoke-WebRequest -UseBasicParsing -Uri $ZipUrl -OutFile $Zip
    Expand-Archive -Path $Zip -DestinationPath $Tmp -Force
    $inside = @(Get-ChildItem $Tmp)
    if ($inside.Count -ne 1 -or $inside[0].Name -ne $Inner) { throw "ZIP の中身が思っていたものと違います（$($inside.Name -join ', ')）。" }
    $src = Join-Path $Tmp $Inner
    foreach ($f in $MustHave) {
        if (-not (Test-Path (Join-Path $src $f))) { throw "ZIP に $f がありません（古い版です）。" }
    }
    Ok 'ダウンロードした中身は新しい版です。'

    # --- 4. 新しい版を止める ---------------------------------------------------------------------
    Step '4/9 新しい版を止める（今の版は止めません）'
    Compose @('down')
    $state.stopped = $true
    $projects = Projects
    if (-not (Running $projects $Inner)) { throw '今の版が止まっています。すぐにこの画面の文字を送ってください。' }
    Ok '新しい版だけを止めました。今の版は動いたままです。'

    # --- 5. フォルダーの入れ替え ----------------------------------------------------------------
    Step "5/9 前の新しい版のフォルダーを farm-radar-v2-old-$Stamp という名前で残す"
    Rename-Item $V2 "farm-radar-v2-old-$Stamp"
    $state.renamed = $true
    Move-Item $src $V2
    $state.moved = $true
    Remove-Item $Tmp -Recurse -Force
    Ok "farm-radar-v2 を新しい中身にしました。前のものは farm-radar-v2-old-$Stamp です。"

    # --- 6. 設定とデータを引き継ぐ --------------------------------------------------------------
    Step '6/9 設定ファイル（.env）と保存した一覧（data）を引き継ぐ（数十秒かかることがあります）'
    Copy-Item (Join-Path $Backup '.env') (Join-Path $V2 '.env') -Force
    $newData = Join-Path $V2 'data'
    if (Test-Path $newData) { Remove-Item $newData -Recurse -Force }
    Copy-Item (Join-Path $Backup 'data') $newData -Recurse
    if (-not (Test-Path (Join-Path $newData 'feeds.sqlite3'))) { throw 'data に feeds.sqlite3 がありません。' }
    Ok '.env と data を写しました。'

    # --- 7. 組み立てて起動 ----------------------------------------------------------------------
    Step '7/9 新しい版を組み立てて起動する（数分〜10分）'
    Compose @('up', '-d', '--build', 'api', 'feeds')
    $state.started = $true
    Ok '起動しました。'

    # --- 8. 一覧を読み終わるまで待つ ------------------------------------------------------------
    Step "8/9 一覧を読み終わるまで待つ（最大 $WaitMinutes 分。30秒ごとに確かめます）"
    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    $fs = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 30
        try { $fs = Invoke-RestMethod "$NewApi/api/feeds/status" -TimeoutSec 30 } catch { $fs = $null; Say '   まだ画面が起動していません……'; continue }
        $busy = @($fs.sources | Where-Object { $_.status -eq 'running' -or $_.status -eq 'none' })
        if ($busy.Count -eq 0) { break }
        Say "   読んでいる途中: $(($busy | ForEach-Object { $_.id }) -join ', ')"
    }
    if ($null -eq $fs) { throw "$NewApi が答えません。" }

    # --- 9. 結果のまとめ --------------------------------------------------------------------------
    Step '9/9 結果のまとめ（ここから下を全部コピーして送ってください）'
    Say "[一覧の保存] enabled: $($fs.enabled) / chain_reads: $($fs.chain_reads) / problem: $($fs.problem) / 一覧の数: $(@($fs.sources).Count)（前は $beforeSources）"
    foreach ($s in $fs.sources) { Say ("   {0,-22} {1,-12} {2}" -f $s.id, $s.status, $s.items) }

    $o = Invoke-RestMethod "$NewApi/api/opportunities?amount=1000&show_excluded=true&limit=300" -TimeoutSec 120
    Say "[入れる先] total: $($o.counts.total) / computed: $($o.counts.computed) / listed: $($o.counts.listed) / above_target: $($o.counts.above_target) / target: $($o.target_apr_pct)"
    $c = @($o.items | Where-Object { $_.flags.code -contains 'RANGE_CHAIN' })
    $u = @($o.items | Where-Object { $_.safety.uncertain_match })
    Say "[チェーンの記録で計算] chain: $($c.Count) / 会場の見分けが不確か: $($u.Count)"
    foreach ($x in ($c | Select-Object -First 5)) { Say ("   {0}  幅 ±{1}%  年利 {2}%" -f $x.name, $x.best.range_pct, [math]::Round($x.best.apr_pct, 1)) }
    if ($c.Count -gt 0) {
        $d = Invoke-RestMethod ("$NewApi/api/opportunities/" + $c[0].key + "?amount=1000") -TimeoutSec 120
        $v = $d.item.calc.'1000'.cautious.no_hedge
        Say "[1つの入れる先] $($d.item.name) / range: $($v.range_pct) / share: $($v.liquidity_share)"
    }
    $g = Invoke-RestMethod "$NewApi/api/guard" -TimeoutSec 60
    Say "[守る] placed: $($g.placed_usd) / left: $($g.total_left_usd) / loss_line: $($g.loss_line.state) / venues: $(@($g.venues).Count) / chains: $(@($g.chains).Count)"

    $after = OldState
    Say "[今の版] 前: eval: $($before.eval) / open: $($before.open) / last_ok_at: $($before.last)"
    Say "[今の版] 後: eval: $($after.eval) / open: $($after.open) / stale: $($after.stale) / last_ok_at: $($after.last)"
    if ($after.eval -ne $before.eval -or $after.open -ne $before.open -or $after.stale -eq 'True') {
        Write-Host '   注意: 今の版の状態が前と違います。このまとめを送ってください。' -ForegroundColor Yellow
    } else {
        Ok '今の版はそのまま動いています。'
    }
    Write-Host ''
    Write-Host '更新が終わりました。上の「9/9 結果のまとめ」から下を全部コピーして送ってください。' -ForegroundColor Green
    Say "（同じ内容を $Log にも残しました。画面は http://localhost:18001/explore で開けます）"
} catch {
    Write-Host ''
    Write-Host "止めました: $($_.Exception.Message)" -ForegroundColor Red
    Restore
    Write-Host ''
    Write-Host 'この画面の文字を全部コピーして送ってください。' -ForegroundColor Red
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}
try { Stop-Transcript | Out-Null } catch { }
