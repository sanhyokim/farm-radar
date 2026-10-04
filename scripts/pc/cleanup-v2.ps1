# 新しい版の、もう使わないフォルダーを消す（Windows PowerShell 5.1 用）。
#
# 使い方: オーナーに送る1行の中で、update-v2.ps1 の前に動かす。ここで止まったら、更新は始めない。
#
# 決まり（オーナー）:
# - 消すのは、オーナーが「消してよい」と言った名前のフォルダーだけ（-Names）。名前の一部が合うだけのものは消さない。
# - 今の版（ポート 18000、フォルダー farm-radar-claude-project-thread-np18zb）と新しい版（farm-radar-v2）は消さない。
# - docker が使っているフォルダーは消さない（その場で止まる）。
# - 消したフォルダーはごみ箱に入らない（元に戻せない）。
# - お金を動かすことはしない。秘密の鍵は読まない。

param(
    [string]$Desk = 'C:\Users\user\Desktop',
    # 2026-10-02 23:15 JST オーナー「この3つは消してかまいません」、23:34 JST「farm-radar-v2-old-20261002-2258 を消す手順をください」
    # （-File で渡すと配列が1つの文字になるので、カンマで区切った1つの文字でも受け取る）
    [string[]]$Names = @('farm-radar-v2-failed-20261002-2248', 'farm-radar-v2-old', 'farm-radar-v2-n2b-old', 'farm-radar-v2-old-20261002-2258'),
    [string]$Docker = 'docker',
    # 2026-10-04 オーナーの決まり「控えは1つだけ残す。更新がうまくいったら、次の1行で前の控えを消す」。
    # 名前を前もって知らないときに使う: farm-radar-v2-old-（8けたの日付）-（4けたの時刻）の形のフォルダーのうち、
    # いちばん新しい1つを残して、ほかを消す。消す前に名前を並べ、y を押したときだけ消す（ほかのキーなら止まる）
    [switch]$OldBackups
)

$ErrorActionPreference = 'Stop'
$Keep = @('farm-radar-claude-project-thread-np18zb', 'farm-radar-v2')   # 動いている2つの版（決して消さない）
$Never = @('farm-radar-1001-old')   # 2026-10-03 オーナー「farm-radar-1001-old には触らないでください」

function Step([string]$text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Ok([string]$text) { Write-Host "   OK: $text" -ForegroundColor Green }

function Projects {
    # docker compose ls の一覧（Windows PowerShell 5.1 は JSON の配列を1つの物として返すので、ばらしてから使う）
    $raw = (& $Docker compose ls -a --format json) | Out-String
    if ($LASTEXITCODE -ne 0) { throw 'docker compose ls が動きません。Docker Desktop が起動しているか確かめてください。' }
    $j = $raw | ConvertFrom-Json
    return @($j | ForEach-Object { $_ })
}

$Names = @($Names | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($OldBackups) {
    $bk = @(Get-ChildItem -LiteralPath $Desk -Directory | Where-Object { $_.Name -cmatch '^farm-radar-v2-old-\d{8}-\d{4}$' } |
            ForEach-Object { $_.Name } | Sort-Object)
    $Names = @(if ($bk.Count -gt 1) { $bk[0..($bk.Count - 2)] } else { @() })
    Write-Host "   控え: $($bk -join ', ')（いちばん新しい $(if ($bk.Count) { $bk[-1] } else { 'なし' }) は残します）"
}

try {
    Write-Host 'もう使わないフォルダーを消します。今の版（18000）と新しい版（18001）には触りません。'

    Step '片付け 1/3 消すフォルダーを確かめる（何も変えません）'
    foreach ($k in $Keep) {
        if (-not (Test-Path -LiteralPath (Join-Path $Desk $k))) { throw "動いている版のフォルダー $k が見つかりません。" }
    }
    foreach ($n in $Names) {
        if ($Keep -contains $n -or $Never -contains $n -or $n -notlike 'farm-radar-*' -or $n -match '[\\/*?]') { throw "$n は消せない名前です。" }
    }
    $targets = @($Names | Where-Object { Test-Path -LiteralPath (Join-Path $Desk $_) })
    foreach ($n in $Names) { if ($targets -contains $n) { Write-Host "   消す: $n" } else { Write-Host "   もう無い: $n" } }
    if ($OldBackups -and $targets.Count -gt 0) {
        $ans = Read-Host '   上の「消す」のフォルダーを消してよければ y を押して Enter（ほかのキーなら何も消さずに止めます）'
        if ($ans -ne 'y') { throw '消すのをやめました（y 以外が押されました）。' }
    }

    Step '片付け 2/3 docker が使っていないか確かめる（何も変えません）'
    foreach ($p in Projects) {
        $files = "$($p.ConfigFiles)"
        foreach ($n in $targets) {
            if ($files -like "*\$n\*") { throw "docker の $($p.Name) が $n を使っています（$files）。消しません。" }
        }
        Write-Host "   $($p.Name) ($($p.Status)): $files"
    }
    Ok '消すフォルダーは、どれも使われていません。'

    Step '片付け 3/3 消す（ごみ箱に入らず、元に戻せません）'
    foreach ($n in $targets) {
        Remove-Item -LiteralPath (Join-Path $Desk $n) -Recurse -Force
        Ok "消しました: $n"
    }
    $left = @(Get-ChildItem -LiteralPath $Desk -Directory -Filter 'farm-radar*' | ForEach-Object { $_.Name })
    Write-Host "   残っているフォルダー: $($left -join ', ')"
    Write-Host '片付けが終わりました。' -ForegroundColor Green
} catch {
    Write-Host ''
    Write-Host "止めました（片付け）: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host '更新は始めません。この画面の文字を全部コピーして送ってください。' -ForegroundColor Red
    exit 1
}
exit 0
