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
    [int]$WaitMinutes = 20     # N5a で1日1回の読み取りが増えた（DefiLlama と Lighter の過去）ので、起動のあとの最初の回が長い
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
$MustHave = @('backend\farm_radar\backtest.py', 'backend\farm_radar\feeds\trial.py', 'backend\farm_radar\feeds\shadow.py', 'scripts\pc\copy-18000.ps1', 'backend\farm_radar\feeds\vaults.py', 'backend\farm_radar\execution\loss_lines.py', 'backend\farm_radar\execution\hedge_guard.py', 'backend\farm_radar\riskscore.py', 'docker-compose.yml', 'scripts\pc\update-v2.ps1')

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
    # 今の版は毎時0分・15分ごとの集計中に返事が遅くなることがあるので（2026-10-02 22:58 JST に30秒で時間切れ）、
    # 90秒まで待ち、だめなら20秒あけて3回まで試す
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

try { Start-Transcript -Path $Log -Force | Out-Null } catch { }
try {
    Say '新しい版（18001）を更新します。今の版（18000）には触りません。'
    Say '終わるまで 15〜30 分ほどかかります。途中でこの画面を閉じないでください。'

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
    $since = (Get-Date).ToUniversalTime().AddMinutes(-1)     # 8 で「起動のあとに会場の見分けを読んだか」を見るため
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
        # 会場の見分け（venue_checks）は、起動のあとに1回読み終わるまで待つ（前の回の数がまとめに出ないように）
        $vcs = @($fs.sources | Where-Object { $_.id -eq 'venue_checks' })
        $vcFresh = ($vcs.Count -eq 0) -or ($vcs[0].last_run_at -and ([datetime]$vcs[0].last_run_at).ToUniversalTime() -ge $since)
        if ($busy.Count -eq 0 -and $vcFresh) { break }
        if (-not $vcFresh) { $busy += $vcs[0] }
        Say "   読んでいる途中: $(($busy | ForEach-Object { $_.id }) -join ', ')"
    }
    if ($null -eq $fs) { throw "$NewApi が答えません。" }

    # --- 9. 結果のまとめ --------------------------------------------------------------------------
    Step '9/9 結果のまとめ（ここから下を全部コピーして送ってください）'
    Say "[一覧の保存] enabled: $($fs.enabled) / chain_reads: $($fs.chain_reads) / problem: $($fs.problem) / 一覧の数: $(@($fs.sources).Count)（前は $beforeSources）"
    foreach ($s in $fs.sources) { Say ("   {0,-22} {1,-12} {2}" -f $s.id, $s.status, $s.items) }

    $o = Invoke-RestMethod "$NewApi/api/opportunities?amount=1000&show_excluded=true&limit=300" -TimeoutSec 120
    Say "[入れる先] total: $($o.counts.total) / computed: $($o.counts.computed) / listed: $($o.counts.listed) / above_target: $($o.counts.above_target) / recommended: $($o.counts.recommended) / target: $($o.target_apr_pct)"
    $dg = $o.counts.danger
    # 0件の区分は空欄ではなく 0 と出す（2026-10-03 オーナー）
    function N0($x) { if ($null -eq $x) { 0 } else { $x } }
    if ($dg) { Say "[危なさ] low: $(N0 $dg.low) / mid: $(N0 $dg.mid) / high: $(N0 $dg.high) / very_high: $(N0 $dg.very_high) / venue_verified: $(N0 $o.counts.venue_verified)" }
    foreach ($x in @($o.items | Where-Object { $_.recommended })) {
        $mk = if ($x.flags.code -contains 'SUDDEN_CHANGE') { '  [年利が急に変わった]' } else { '' }
        Say ("   おすすめ: {0}  年利 {1}%  危なさ {2}{3}" -f $x.name, [math]::Round($x.best.apr_pct, 1), $x.safety.level, $mk)
    }
    $sd = @($o.items | Where-Object { $_.flags.code -contains 'SUDDEN_CHANGE' })
    Say "[年利が急に変わった] $($sd.Count) 件"
    foreach ($x in ($sd | Select-Object -First 5)) {
        Say ("   {0}: {1}" -f $x.name, (@($x.flags | Where-Object { $_.code -eq 'SUDDEN_CHANGE' })[0].text))
    }
    # 値動きが急に大きくなった（2026-10-04 オーナー決定 ①A。直近24時間が7日の1.5倍をこえた）
    $vj = @($o.items | Where-Object { $_.flags.code -contains 'VOL_JUMP' })
    Say "[値動きが急に大きくなった] $($vj.Count) 件"
    foreach ($x in ($vj | Select-Object -First 5)) {
        Say ("   {0}: {1}" -f $x.name, (@($x.flags | Where-Object { $_.code -eq 'VOL_JUMP' })[0].text))
    }
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
    $lv = if ($g.loss_lines.level) { $g.loss_lines.level } else { 'なし' }     # 線を越えていなければ「なし」
    Say "[守る] loss_lines: $(@($g.loss_lines.periods).Count) / level: $lv / stages: $(@($g.stages).Count) / lighter: $(@($g.venues | Where-Object { $_.venue_id -eq 'lighter' }).Count)"
    # 保険（Lighter）の預け金の減り方（2026-10-04 オーナーのお願い1。練習の建玉ごと、1時間に1回の記録）
    Say "[守る] 預け金の減り方: 建玉 $(@($g.margin_log).Count)"
    foreach ($m in @($g.margin_log | Select-Object -First 5)) {
        Say ("   {0}: 預けたお金 `${1} → 今 `${2}（{3}%）/ いちばん低いとき `${4} / 置き直し {5} 回" -f $m.pair, [math]::Round([double]$m.margin_usd, 2), `
            $(if ($null -eq $m.equity_usd) { '-' } else { [math]::Round([double]$m.equity_usd, 2) }), $(if ($null -eq $m.change_pct) { '-' } else { [math]::Round([double]$m.change_pct, 1) }), `
            $(if ($null -eq $m.low_equity_usd) { '-' } else { [math]::Round([double]$m.low_equity_usd, 2) }), $m.rebalances)
    }
    # 預け金を「足したとしたら」（2026-10-04 オーナー決定 ③A。記録だけ。本物のお金は動かさない）
    Say "[守る] 足したとしたら: 建玉 $(@($g.topups).Count)（線は余裕がはじめの $([math]::Round([double]$g.topup_line_frac * 100, 0))% を切ったとき）"
    foreach ($t in @($g.topups | Select-Object -First 5)) {
        Say ("   {0}: {1} 回・合計 `${2}・見込みの費用 `${3}・最後の回 プール `${4} から足す / 売り `${5} → `${6}" -f $t.pair, $t.count, `
            [math]::Round([double]$t.total_usd, 2), $(if ($null -eq $t.cost_usd) { '-' } else { [math]::Round([double]$t.cost_usd, 2) }), `
            $(if ($null -eq $t.last.pool_usd) { '-' } else { [math]::Round([double]$t.last.pool_usd, 0) }), `
            $(if ($null -eq $t.last.short_before_usd) { '-' } else { [math]::Round([double]$t.last.short_before_usd, 0) }), `
            $(if ($null -eq $t.last.short_after_usd) { '-' } else { [math]::Round([double]$t.last.short_after_usd, 0) }))
    }
    $vs = @($fs.sources | Where-Object { $_.id -eq 'vault_states' })[0]
    $vc = @($fs.sources | Where-Object { $_.id -eq 'venue_checks' })[0]
    # 「今回」は最後の回に読んだ数。答えが出たものは読み直さないので、たまった数（確かめ済み・金庫）も出す
    Say "[見張り] venue_checks: $($vc.status) 今回 $(N0 $vc.items) / 確かめ済み $(N0 $vc.stored.checked)（公式の工場が作った $(N0 $vc.stored.verified)）"
    Say "[見張り] vault_states: $($vs.status) 今回 $(N0 $vs.items) / 見張っている金庫 $(N0 $vs.stored.vaults)"

    # N5a: 試すための記録（Merkl の配った額・DefiLlama と Lighter の過去・影の記録・Aero の住所）
    try {
        $t = Invoke-RestMethod "$NewApi/api/trial/records" -TimeoutSec 60
        $f = $t.feeds
        Say "[試す] Merkl の配った額: キャンペーン $(N0 $f.merkl_rewards.campaigns) / 行 $(N0 $f.merkl_rewards.rows)"
        Say "[試す] DefiLlama の毎日の記録: プール $(N0 $f.llama_history.pools) / 行 $(N0 $f.llama_history.rows)（$($f.llama_history.from) 〜 $($f.llama_history.to)）"
        Say "[試す] Lighter の資金調達率の過去: 銘柄 $(N0 $f.lighter_history.markets) / 行 $(N0 $f.lighter_history.rows)"
        Say "[試す] Lighter の値段の過去: 銘柄 $(N0 $f.lighter_prices.markets) / 行 $(N0 $f.lighter_prices.rows)（1日1回。最初は90日分）"
        Say "[試す] 影の記録: $(N0 $f.shadow.hours) 回 / 入れる先 $(N0 $f.shadow.opportunities) / 行 $(N0 $f.shadow.rows)"
        Say "[試す] Aero の住所のファイル: $((@($f.aero_addresses) | ForEach-Object { $_.name }) -join ', ')"
        # Lighter の Robinhood Chain 版（2026-10-04 オーナー決定 ②A。読むだけ。本物のお金は動かさない）
        $rh = $f.lighter_rh
        if ($rh) {
            Say "[Lighter RH版] 市場 $(N0 $rh.markets) / 保険に使う市場のうち、この版にあるもの $(N0 $rh.hedge_markets)（読んだ時刻 $($rh.updated_at)）"
            foreach ($x in @($rh.rows)) {
                $fr = if ($null -eq $x.funding_daily_rh) { '-' } else { [math]::Round([double]$x.funding_daily_rh * 365 * 100, 1) }
                $fm = if ($null -eq $x.funding_daily_main) { '-' } else { [math]::Round([double]$x.funding_daily_main * 365 * 100, 1) }
                Say ("   {0}: 値段 RH {1} / 本体 {2}・維持の割合 RH {3}% / 本体 {4}%・売りの資金調達料（年、7日。プラス = 払う）RH {5}% / 本体 {6}%・過去 値段 {7} 点 / 資金調達率 {8} 点" -f `
                    $x.symbol, $x.price_rh, $x.price_main, $x.mmf_rh_pct, $x.mmf_main_pct, $fr, $fm, $x.price_points, $x.funding_points)
            }
            # 探すの見込みで、どちらの Lighter の数字を使ったか（Robinhood Chain のプールで RH版にある市場は RH版）
            if ($o) {
                $hm = @($o.items | ForEach-Object { @($_.hedge_markets) } | Where-Object { $_ })
                $nr = @($hm | Where-Object { $_.book -eq 'rh' }).Count
                Say ("[Lighter RH版] 探すの保険の見込み: RH版の数字 {0} 件 / 本体の数字 {1} 件" -f $nr, ($hm.Count - $nr))
            }
        } else { Say "[Lighter RH版] まだ読めていません" }
        # N5c: Merkl の答え合わせ（実際に配った額 ÷ 探すの見込み。1 に近いほど見込みどおり。読むだけ）
        $mp = $f.merkl_positions
        if ($mp) { Say "[答え合わせ Merkl] 幅と量を読んだ預け方 $(N0 $mp.positions)（読めた回 $(N0 $mp.ok) / 読めなかった回 $(N0 $mp.errors)・最後 $($mp.last)）" }
        $mc = $f.merkl_check
        if ($mc -and @($mc.campaigns).Count -gt 0) {
            $med = if ($null -eq $mc.ratio_median) { '-' } else { [math]::Round([double]$mc.ratio_median, 2) }
            $dsh = if ($null -eq $mc.denominator_share_median) { '-' } else { "$([math]::Round([double]$mc.denominator_share_median * 100))%" }
            Say "[答え合わせ Merkl] 比べた組 $(N0 $mc.pairs_in_range)・実際 ÷ 見込み（まん中）$med（1 より大きい = 見込みは控えめ）"
            Say "[答え合わせ Merkl] コインの分の分母は、預かり額の $dsh に見える（100% に近い = 全員が分母。小さい = 幅の中の預け方だけに近い）"
            foreach ($x in @($mc.campaigns | Select-Object -First 8)) {
                $m = if ($null -eq $x.ratio_median) { '-' } else { [math]::Round([double]$x.ratio_median, 2) }
                $lo = if ($null -eq $x.ratio_p25) { '-' } else { [math]::Round([double]$x.ratio_p25, 2) }
                $hi = if ($null -eq $x.ratio_p75) { '-' } else { [math]::Round([double]$x.ratio_p75, 2) }
                $op = if ($null -eq $x.out_of_range_paid_share) { '-' } else { "$([math]::Round([double]$x.out_of_range_paid_share * 100))%" }
                $ds = if ($null -eq $x.denominator_share_median) { '-' } else { "$([math]::Round([double]$x.denominator_share_median * 100))%" }
                Say ("   {0}: 区切り {1} / 幅の中の組 {2} / 実際 ÷ 見込み {3}（{4} 〜 {5}）/ 分母 {6} / 幅の外でももらえた組 {7}" -f `
                    $x.pair, $x.intervals, $x.pairs_in_range, $m, $lo, $hi, $ds, $op)
            }
        } else { Say "[答え合わせ Merkl] まだ比べられる記録がありません（2時間ごとに増えます）" }
    } catch {
        Write-Host "   注意: 試すための記録の数を読めませんでした（$($_.Exception.Message)）。" -ForegroundColor Yellow
    }

    # N5b: さかのぼりの計算（今の版の写しで、見込みと実際を比べる。最初の1回は数十秒〜数分かかる）
    try {
        Step '9/9 の続き: さかのぼりの計算（数分かかることがあります）'
        $bk = Invoke-RestMethod "$NewApi/api/trial/backtest" -TimeoutSec 900
        if (-not $bk.present) { Say "[さかのぼり] $($bk.text)" } else {
            $r = $bk.result
            $dd = @($r.days)
            Say "[さかのぼり] プール $($r.pools) / プールと日の組 $($r.pool_days) / 日 $($dd.Count)（$($dd[0]) 〜 $($dd[-1])）"
            function BtPct($x, $d = 2) { if ($null -eq $x) { '-' } else { [math]::Round([double]$x * 100, $d) } }
            function BtNum($x, $d = 2) { if ($null -eq $x) { '-' } else { [math]::Round([double]$x, $d) } }
            foreach ($g in @($r.ranges.all)) {
                Say ("   幅 ±{0}%（プールと日 {1}件）: 置き直し {2}/{3} 回/日（合 {4}%） 幅の中 {5}/{6}% 値動きの損 {7}/{8}%（実際の値動きで式 {9}%。合 {10}%） 費用 {11}/{12}%" -f `
                    $g.r_pct, $g.days, (BtNum $g.rebalances.pred), (BtNum $g.rebalances.real), (BtPct $g.rebalances.ok_share 0), `
                    (BtPct $g.in_range.pred 1), (BtPct $g.in_range.real 1), (BtPct $g.gamma.pred 3), (BtPct $g.gamma.real 3), (BtPct $g.gamma.formula_real_sigma 3), `
                    (BtPct $g.gamma.ok_share 0), (BtPct $g.cost.pred 3), (BtPct $g.cost.real 3))
            }
            # 合 = 差が見込みの30%以内（ごく小さい率は 1日 0.002% まで）。前の数え方（資金の0.1%まで）は「前」（2026-10-04 オーナーの質問3）
            foreach ($x in @($r.funding)) { Say ("   資金調達 {0}（{1}日）: 見込み {2}/実際 {3} %/日（合 {4}%。前の数え方 {5}%）" -f $x.perp, $x.days, (BtPct $x.pred 4), (BtPct $x.real 4), (BtPct $x.ok_share 0), (BtPct $x.ok_share_loose 0)) }
            foreach ($x in @($r.margins)) { Say ("   預け金 {0}: いちばんの上げ {1}%（{2}日の記録。耐える {3}%）/ 15分の飛び {4}%" -f $x.symbol, (BtNum $x.rise_pct 1), (BtNum $x.span_days 1), $x.withstand_pct, (BtNum $x.jump_up_pct 1)) }
            # Lighter の値段の過去（最大90日の1時間の足）で見た預け金（2026-10-04 オーナーの質問4）。上げの大きい順
            $ml = @($r.margins_long | Sort-Object { -[double]$_.rise_pct })
            if ($ml.Count -gt 0) {
                Say "[さかのぼり 預け金 90日] 市場 $($ml.Count) / 耐える $($ml[0].withstand_pct)% を超えた市場 $(@($ml | Where-Object { -not $_.enough }).Count)"
                # 耐える幅を超えた市場は全部、ほかは上げの大きい順に、合わせて8つまで
                $over = @($ml | Where-Object { -not $_.enough })
                foreach ($x in @($over + @($ml | Where-Object { $_.enough } | Select-Object -First ([math]::Max(0, 8 - $over.Count))))) {
                    Say ("   {0}: 14日以内のいちばんの上げ {1}%（{2}日の記録）/ 1時間でいちばんの上げ {3}%" -f $x.symbol, (BtNum $x.rise_pct 1), (BtNum $x.span_days 0), (BtNum $x.jump_up_pct 1))
                }
                # その市場を保険に使う入れる先が、今の一覧に何行あるか（2026-10-04 オーナーの質問2。$1,000・控えめの見込み）
                foreach ($x in $over) {
                    $rows = @($o.items | Where-Object { @($_.hedge_markets | Where-Object { [string]$_.market_id -eq [string]$x.market_id }).Count -gt 0 })
                    $ls = @($rows | Where-Object { -not $_.excluded })
                    $hd = @($ls | Where-Object { $_.best -and $_.best.hedge })
                    $rc = @($ls | Where-Object { $_.recommended })
                    $top = @($ls | Where-Object { $_.best } | Sort-Object { -[double]$_.best.apr_pct } | Select-Object -First 1)
                    $tx = if ($top.Count -gt 0) { "$($top[0].name) $([math]::Round([double]$top[0].best.apr_pct, 1))%（$(if ($top[0].best.hedge) { '保険あり' } else { '保険なし' })）" } else { '-' }
                    Say ("   {0} を保険に使う入れる先: 全部 {1} 行 / 一覧に出る {2} / うち保険ありがよい {3} / おすすめ {4} / 年利のいちばん高い行 {5}" -f $x.symbol, $rows.Count, $ls.Count, $hd.Count, $rc.Count, $tx)
                }
            } else { Say "[さかのぼり 預け金 90日] まだ値段の過去がありません（1日1回の読み取りのあとに出ます）" }
            # 値段が動いていないプール（取引がほとんどない）は、見込みと実際を比べるのから外した（2026-10-04 オーナーの質問3）
            $st = @($r.still_pools)
            Say ("[さかのぼり 値段が動いていないプール] {0} 個（となりの記録と同じ値段が {1}% 以上。比べるのから外しました）" -f $st.Count, (BtPct $r.still_share_line 0))
            # 同じ名前のプールが複数あるので、プールの住所と手数料の段・最初と最後の値段も出す（2026-10-04 オーナーの質問1）
            # 空欄なら、とっておいた古い計算の結果を読んでいる（2026-10-04。VERSION の上げ忘れ。今はファイルの中身の印もキーに入れた）
            # 手数料の段は、今の版が15分ごとにチェーンから読んだ値（2026-10-04: up. の工場の記録には手数料がないので、前は「不明」だった）。
            # 手数料が動くプールは、記録の中の最小〜最大も出す
            function FeeText($f) {
                if ($null -eq $f.fee_pct) { return '不明' }
                $t = "$($f.fee_pct)%"
                if ($null -ne $f.fee_min_pct -and $f.fee_min_pct -ne $f.fee_max_pct) { $t += "（記録の中で $($f.fee_min_pct)〜$($f.fee_max_pct)%）" }
                return $t
            }
            foreach ($x in $st) {
                $edge = ''
                if ($x.at_edge -eq 'max' -or $x.at_edge -eq 'min') {
                    $edge = " ※プールが空（流動性 0）で、値段が仕組みの{0}の端にあります（本当のコインの値段ではありません）" -f $(if ($x.at_edge -eq 'max') { '上' } else { '下' })
                } elseif ($x.empty) { $edge = ' ※プールが空（流動性 0）' }
                Say ("   {0}（{1}・手数料の段 {2}）: 同じ値段の割合 {3}%（記録 {4} 点・違う値段 {5} 個）/ 最初 {6} / 最後 {7}{8}" -f $x.pair, $x.pool_id, (FeeText $x), `
                    (BtPct $x.still_share 1), $x.points, $x.distinct_prices, (BtNum $x.first_price 6), (BtNum $x.last_price 6), $edge)
                foreach ($o in @($x.same_pair)) {
                    Say ("      同じ組のほかのプール: {0}（手数料の段 {1}・記録 {2} 点・{3}）" -f $o.pool_id, (FeeText $o), $o.points, $(if ($o.still) { '値段が動いていない' } else { '値段が動いている' }))
                }
            }
            # 中央値と種類ごと（2026-10-03 オーナーの質問3）。幅 ±0.5%・±2%・±15% だけ
            $kn = @{ kind_stock = '株'; kind_stable = 'ステーブル'; kind_coin = 'ふつうのコイン'; kind_bonus = 'ボーナスのコイン' }
            Say "[さかのぼり 種類] 株 $($r.kinds.stock) / ステーブル $($r.kinds.stable) / ふつうのコイン $($r.kinds.coin) / ボーナスのコイン $($r.kinds.bonus)（プールの数）"
            foreach ($k in @('kind_stock', 'kind_stable', 'kind_coin', 'kind_bonus')) {
                foreach ($g in @($r.ranges.$k | Where-Object { @(0.5, 2, 15) -contains [double]$_.r_pct })) {
                    Say ("   {0} ±{1}%（{2}件）: 置き直し 中央値 {3}/{4} 回/日 / 値動きの損 中央値 {5}/{6}% / 値動き σ 中央値 {7}/{8}%" -f `
                        $kn[$k], $g.r_pct, $g.days, (BtNum $g.rebalances.pred_median), (BtNum $g.rebalances.real_median), `
                        (BtPct $g.gamma.pred_median 3), (BtPct $g.gamma.real_median 3), (BtPct $g.sigma.pred_median 1), (BtPct $g.sigma.real_median 1))
                }
            }
            # 1日の損（値動きの損 + 置き直しの費用）の見込みと実際の差（ドル）の大きい順（2026-10-04 オーナーの質問5）
            foreach ($x in @($r.misses | Select-Object -First 5)) {
                Say ("   ずれの大きいプール {0}（{1}・±{2}%）: 1日の損 見込み `${3}/実際 `${4}（差 `${5}）/ 値動き σ {6}/{7}% / 置き直し {8}/{9} 回/日" -f $x.pair, $kn["kind_$($x.kind)"], $x.r_pct, `
                    (BtNum $x.loss_pred_usd_day), (BtNum $x.loss_real_usd_day), (BtNum $x.gap_usd_day), (BtPct $x.sigma_pred 1), (BtPct $x.sigma_real 1), `
                    (BtNum $x.rebalances_pred), (BtNum $x.rebalances_real))
            }
            foreach ($x in @($r.spy)) {
                Say ("   SPY {0}（±{1}%）: 値動き σ {2}/{3}% / 値動きの損 {4}/{5}% / 置き直し {6}/{7} 回/日" -f $x.pair, $x.r_pct, `
                    (BtPct $x.sigma_pred 1), (BtPct $x.sigma_real 1), (BtPct $x.gamma_pred 3), (BtPct $x.gamma_real 3), (BtNum $x.rebalances_pred), (BtNum $x.rebalances_real))
            }
            $c1 = $r.stage1.counts
            $c1b = $r.stage1.counts_big_pools
            Say "[さかのぼり 段階1] プールのお金 1時間で -20%: $($c1.'20') 回 / -30%: $($c1.'30') 回 / -40%: $($c1.'40') 回 / -50%: $($c1.'50') 回"
            Say "   うち 5万ドル以上のプール: -20%: $($c1b.'20') / -30%: $($c1b.'30') / -40%: $($c1b.'40') / -50%: $($c1b.'50')"
            $bd = $r.stage1.breakdown
            Say "   -$($r.stage1.threshold_pct)% の $($bd.total) 回の中身（重なりあり）: 小さいプール $($bd.small) / 6時間以内に戻った $($bd.recovered_6h) / そのあと24時間でコインが -20% 以上 $($bd.big_drop) / どれでもない $($bd.none)"
            # 5万ドル以上のプールだけの中身と1回ずつ（2026-10-04 オーナーの追加2）
            $bb = $r.stage1.breakdown_big_pools
            Say "   5万ドル以上のプールの $($bb.total) 回: 6時間以内に戻った $($bb.recovered_6h) / そのあと24時間でコインが -20% 以上 $($bb.big_drop) / どれでもない $($bb.none)"
            foreach ($x in @($r.stage1.big_pool_events)) {
                $at = [DateTimeOffset]::FromUnixTimeSeconds([long]$x.at).ToOffset([TimeSpan]::FromHours(9)).ToString('MM/dd HH:mm')
                Say ("     {0}（{1}）{2} 日本時間: プールのお金 {3}%（前 `${4}）/ 6時間以内に戻った {5} / そのあと24時間のコインの最低 {6}%" -f $x.pair, $kn["kind_$($x.kind)"], $at, `
                    (BtNum $x.change_pct 1), (BtNum $x.funds_before_usd 0), $(if ($x.recovered_6h) { 'はい' } else { 'いいえ' }), (BtNum $x.coin_min_24h_pct 1))
            }
            foreach ($x in @($r.stage2_reward.tokens)) {
                $e = $x.events
                Say "[さかのぼり 段階2] $($x.symbol) 24時間で -10%: $(@($e.'-10').Count) 回 / -15%: $(@($e.'-15').Count) 回 / -20%: $(@($e.'-20').Count) 回 / -25%: $(@($e.'-25').Count) 回"
            }
            Say "[さかのぼり 投げ売り] 合図が出たコイン: $(@($r.stage2_dump.tokens).Count)（1時間で $($r.stage2_dump.threshold_1h_pct)% 以下、または24時間で $($r.stage2_dump.threshold_24h_pct)% 以下）"
            # コインごとの回数と、最初の合図のあと24時間（2026-10-04 オーナーの質問3）
            foreach ($x in @($r.stage2_dump.tokens)) {
                $ev = @(@($x.events_1h) + @($x.events_24h) | Sort-Object { [long]$_.at })
                $f = $ev[0]
                $at = [DateTimeOffset]::FromUnixTimeSeconds([long]$f.at).ToOffset([TimeSpan]::FromHours(9)).ToString('MM/dd HH:mm')
                Say ("   {0}: 1時間の合図 {1} 回 / 24時間の合図 {2} 回 / 最初 {3} 日本時間 {4}% → そのあと24時間の最低 {5}%・24時間後 {6}%" -f $x.symbol, `
                    @($x.events_1h).Count, @($x.events_24h).Count, $at, (BtNum $f.change_pct 1), (BtNum $f.min_24h_pct 1), (BtNum $f.after_24h_pct 1))
            }
            # N5c: 比べる相手（年あたり、建玉のお金あたり。収入は今の版の見込み × 実際に幅の中にいた割合、損は実際の値段で計算）
            $bl = $r.baselines
            if ($bl -and $bl.all.days -gt 0) {
                Say ("[さかのぼり 比べる相手] 貸し出し = {0}（出典 {1}、確認 {2}）/ 広い幅 = ±{3}% で置きっぱなし" -f $bl.lending.name, $bl.lending.source, $bl.lending.checked, $bl.wide_r_pct)
                foreach ($k in @('all', 'kind_stock', 'kind_coin', 'kind_bonus', 'kind_stable')) {
                    $g = $bl.$k
                    if (-not $g -or $g.days -eq 0) { continue }
                    $nm = @{ all = 'ぜんぶ'; kind_stock = '株'; kind_coin = 'ふつうのコイン'; kind_bonus = 'ボーナスのコイン'; kind_stable = 'ステーブル' }[$k]
                    Say ("   {0}（プールと日 {1}件）: 今のやり方 年 {2}%（収入だけ {3}%）/ 広い幅で置きっぱなし {4}% / 貸し出し {5}%（{6}日）/ 何もしない 0% ・今のやり方が勝った日: 広い幅に {7}% / 貸し出しに {8}% / 何もしないに {9}%" -f `
                        $nm, $g.days, (BtNum $g.now_year_pct 1), (BtNum $g.now_income_year_pct 1), (BtNum $g.wide_year_pct 1), (BtNum $g.lend_year_pct 1), $g.lend_days, `
                        (BtPct $g.beat_wide_share 0), (BtPct $g.beat_lend_share 0), (BtPct $g.beat_nothing_share 0))
                }
            } else { Say "[さかのぼり 比べる相手] まだ比べられません（スコアの幅ごとの収入か、貸し出しの毎日の記録がありません）" }
        }
    } catch {
        Write-Host "   注意: さかのぼりの計算を読めませんでした（$($_.Exception.Message)）。" -ForegroundColor Yellow
    }

    # 新しい版の更新はもう終わっているので、ここでうまくいかなくても止めずに注意だけ出す
    Say "[今の版] 前: eval: $($before.eval) / open: $($before.open) / last_ok_at: $($before.last)"
    try {
        $after = OldState
        Say "[今の版] 後: eval: $($after.eval) / open: $($after.open) / stale: $($after.stale) / last_ok_at: $($after.last)"
        if ($after.eval -ne $before.eval -or $after.open -ne $before.open -or $after.stale -eq 'True') {
            Write-Host '   注意: 今の版の状態が前と違います。このまとめを送ってください。' -ForegroundColor Yellow
        } else {
            Ok '今の版はそのまま動いています。'
        }
    } catch {
        Write-Host "   注意: 今の版の状態を読めませんでした（$($_.Exception.Message)）。新しい版の更新は終わっています。このまとめを送ってください。" -ForegroundColor Yellow
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
exit 0
